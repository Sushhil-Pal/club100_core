"""Member wallet checkout bridge for local Razorpay TEST-mode development.

Feature toggles do not gate checkout. Financial provenance, the dedicated
hybrid-route boundary, verified payment evidence and anti-retry safeguards do.
Never use browser-provided figures to validate Razorpay captures.
"""
from decimal import Decimal
import frappe
from frappe.utils import get_datetime, now_datetime
from club100_core.wallet.checkout_math import split_total, ensure_tender_integrity
from club100_core.wallet.logic import money
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.spending import reserve, capture


def enabled():
    # Local/test-only environment validation, not an operational feature toggle.
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def require_enabled():
    if not enabled():
        frappe.throw('Checkout integration requires local Razorpay Test Mode.')


def preview(member, gross):
    if not enabled():
        return {'enabled': False, 'available': '0.00'}
    balance = wallet_balance(member)
    # Legacy preview is not a discount quote. The dedicated hybrid quote owns discounts;
    # Promotion grants must never be treated as cash tender here.
    funded = money(balance['by_source']['Corporate']) + money(balance['by_source']['Referral'])
    return {'enabled': True, 'available': str(funded),
            'promotional_discount_available': str(money(balance['by_source']['Promotion'])),
            'promotion_discount_checkout_active': False}

def member_wallet_lock_before_enrollment(enrollment_id):
    """Use member -> enrollment lock order for split-payment callbacks."""
    row = frappe.db.get_value('Club100 Enrollment', str(enrollment_id or ''),
                              ['member', 'wallet_reservation_key'], as_dict=True)
    if row and row.member:
        frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', row.member)


def _hybrid_must_use_dedicated_bridge(doc):
    """Never process a hybrid reservation using legacy order/callback/finalization.

    Check existence even when the new pilot is disabled: if an unpaid hybrid
    order exists, it must fail closed instead of charging the original gross.
    """
    name=getattr(doc, 'name', None)
    if name and frappe.db.exists('Club100 Hybrid Checkout', {'enrollment':name}):
        frappe.throw('Hybrid checkout requires the dedicated guarded payment and invoice flow. Legacy checkout is blocked.')


def expected_gateway_due(doc):
    _hybrid_must_use_dedicated_bridge(doc)
    if not doc.get('wallet_reservation_key'):
        return money(doc.amount_payable)
    if doc.website_source != 'PWA Commerce' or not doc.member:
        raise ValueError('Wallet tender allowed only for authenticated PWA commerce')
    _, wallet, gateway = ensure_tender_integrity(doc.amount_payable,
                                                 doc.wallet_applied_amount,
                                                 doc.gateway_due_amount)
    if wallet <= 0:
        raise ValueError('Wallet checkout must have positive wallet allocation')
    return gateway


def require_settled_before_finalization(doc):
    """Require captured legacy hold OR a fully reconciled dedicated hybrid checkout.

    The hybrid runtime submits vouchers and validates wallet-spend ledger BEFORE
    it calls the shared enrollment finalizer. Do not route hybrid payment through
    legacy expected_gateway_due, which would use the original pre-discount price.
    """
    hybrid = frappe.db.get_value('Club100 Hybrid Checkout',
                {'enrollment': doc.name}, ['status', 'payment_reference'], as_dict=True)
    if hybrid:
        mapping = frappe.db.get_value('Club100 Membership Accounting',
                  {'enrollment':doc.name}, ['status', 'sales_invoice'], as_dict=True)
        if (hybrid.status != 'Captured' or not mapping or mapping.status != 'Posted'
                or not doc.sales_invoice or mapping.sales_invoice != doc.sales_invoice
                or doc.payment_status != 'Paid' or not doc.payment_id
                or hybrid.payment_reference != doc.payment_id
                or frappe.db.get_value('Sales Invoice',doc.sales_invoice,'docstatus') != 1
                or money(frappe.db.get_value('Sales Invoice',doc.sales_invoice,
                                             'outstanding_amount')) != Decimal('0.00')):
            frappe.throw('Hybrid checkout is not fully reconciled; membership cannot activate.')
        return
    if not doc.get('wallet_reservation_key'):
        return
    expected_gateway_due(doc)  # Validates the immutable tender identity.
    if doc.get('wallet_settlement_status') != 'Settled':
        frappe.throw('Wallet credits are not settled. Membership cannot activate.')
    row = frappe.db.get_value('Club100 Wallet Reservation',
         {'reservation_key': doc.wallet_reservation_key},
         ['member','reference','total_amount','status'], as_dict=True)
    if not row or row.status != 'Captured' or row.member != doc.member        or row.reference != doc.name or money(row.total_amount) != money(doc.wallet_applied_amount):
        frappe.throw('Captured wallet reservation does not match enrollment.')


def before_gateway_order(doc):
    due = expected_gateway_due(doc)
    if due <= 0:
        frappe.throw('Wallet-only purchases have no Razorpay order.')
    if not doc.get('wallet_reservation_key'):
        return due
    if doc.get('wallet_settlement_status') != 'Reserved' or doc.get('payment_id'):
        frappe.throw('This checkout requires manual review; do not pay again.')
    row = frappe.db.get_value('Club100 Wallet Reservation',
          {'reservation_key': doc.wallet_reservation_key},
          ['member', 'reference', 'total_amount', 'status', 'expires_at'], as_dict=True)
    if not row or row.member != doc.member or row.reference != doc.name or money(row.total_amount) != money(doc.wallet_applied_amount):
        frappe.throw('Wallet reservation does not match checkout.')
    if row.status != 'Reserved' or get_datetime(row.expires_at) <= now_datetime():
        frappe.throw('Wallet reservation expired. Contact support before retrying checkout.')
    return due


def _require_accounting_backed_new_reservation(doc, reservation):
    """Required source-level and aggregate GL backing for every new funded hold.

    No Payment Entry or Journal Entry is created by this guard. Approval flags
    or a claimed Corporate pool balance never substitute for posted GL proof.
    """
    if not enabled():
        raise ValueError('Wallet GL admission preflight is local Test Mode only')
    from club100_core.accounting.wallet_backing import allocation_totals
    from club100_core.accounting.settlement import (
        _config, _ready, _require_wallet_liability_backing,
    )
    allocations = reservation.get('allocations') or []
    source_totals = allocation_totals(allocations)
    cfg = _config()
    _ready(cfg, source_totals, Decimal('0.00'))
    # Member lock is already held by the caller. Serialize against any other
    # source liability posting that takes the same Account row locks.
    for account in sorted({cfg[source] for source in source_totals}):
        frappe.db.sql('SELECT name FROM `tabAccount` WHERE name=%s FOR UPDATE', account)
    _require_wallet_liability_backing(cfg, source_totals, allocations)


def attach_reservation(doc, wallet_requested):
    """Trusted only while creating a new member-owned enrollment.

    Caller holds Member row lock. This reserves, but does not spend, credits.
    The outer request transaction owns all changes.
    """
    require_enabled()
    gross, wallet, gateway = split_total(doc.amount_payable, wallet_requested)
    if wallet <= 0:
        frappe.throw('Choose a positive wallet amount')
    balance = wallet_balance(doc.member)
    funded = money(balance['by_source']['Corporate']) + money(balance['by_source']['Referral'])
    if wallet > funded:
        frappe.throw('Insufficient funded wallet credits (Corporate/Referral only)')
    key = 'WALLETCHK:' + doc.name
    # Always run admission in a savepoint so a rejected backing check cannot
    # leave a wallet reservation even if its caller catches the exception.
    frappe.db.savepoint('club100_wallet_accounting_admission')
    try:
        result = reserve(doc.member, key, doc.name, str(wallet), ttl_minutes=60,
                         allowed_sources=('Corporate', 'Referral'))
        if result['status'] != 'Reserved':
            frappe.throw('Unable to reserve wallet funds')
        _require_accounting_backed_new_reservation(doc, result)
    except Exception:
        frappe.db.rollback(save_point='club100_wallet_accounting_admission')
        raise
    doc.wallet_reservation_key = key
    doc.wallet_applied_amount = wallet
    doc.gateway_due_amount = gateway
    doc.wallet_settlement_status = 'Reserved'
    doc.save(ignore_permissions=True)
    return gross, wallet, gateway


def wallet_only_paid(doc, commit=True):
    """No gateway charged. Capture and membership finalization in ONE transaction."""
    if expected_gateway_due(doc) != Decimal('0.00'):
        frappe.throw('Wallet-only finalization cannot have Razorpay remainder')
    capture(doc.wallet_reservation_key, doc.name)
    doc.wallet_settlement_status = 'Settled'
    doc.payment_gateway = 'Club100 Wallet'
    doc.payment_id = 'WALLET:' + doc.name
    doc.payment_date = now_datetime()
    doc.payment_status = 'Paid'
    # No Razorpay identity; do NOT fake razorpay_capture_verified.
    from club100_core.api.enrollment import _finalize_paid_enrollment_doc
    activated = _finalize_paid_enrollment_doc(doc)
    if commit:
        frappe.db.commit()
    return {'success': True, 'wallet_only': True, 'enrollment': doc.name,
            'status': doc.status, 'payment_status': 'Paid', 'wallet_applied': str(doc.wallet_applied_amount),
            'gateway_due': '0.00', **activated}


def settle_verified_gateway_or_review(doc, payment_id):
    """On captured RZ payment, settle reservation or durably block activation.

    Caller has authenticated gateway capture, Enrollment row lock and Member
    lock. Never marks Paid unless wallet capture succeeds. Errors on wallet
    capture become a persisted Review status; never invite a second payment.
    """
    if not doc.get('wallet_reservation_key'):
        return True
    if expected_gateway_due(doc) <= 0:
        frappe.throw('Wallet-only enrollment cannot receive Razorpay capture')
    if doc.payment_id and doc.payment_id != payment_id:
        frappe.throw('Different gateway payment already recorded for this enrollment')
    if doc.wallet_settlement_status == 'Settled':
        return True
    if doc.wallet_settlement_status == 'Review':
        return False
    frappe.db.savepoint('club100_wallet_gateway_settle')
    try:
        if doc.wallet_settlement_status != 'Reserved':
            raise ValueError('Invalid wallet settlement state')
        capture(doc.wallet_reservation_key, doc.name)
        doc.wallet_settlement_status = 'Settled'
        return True
    except Exception as exc:
        frappe.db.rollback(save_point='club100_wallet_gateway_settle')
        doc.wallet_settlement_status = 'Review'
        doc.wallet_review_reason = str(exc)[:400]
        doc.payment_gateway = 'Razorpay'
        doc.payment_id = payment_id
        doc.razorpay_capture_verified = 1
        # Keep Pending/Registered: no membership entitlement while review needed.
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return False


def review_response(doc):
    return {'success': False, 'manual_review': True, 'enrollment': doc.name,
            'payment_status': doc.payment_status, 'status': doc.status,
            'message': 'Payment was captured, but wallet settlement needs manual review. Do not retry payment.'}


def healthcheck():
    return {'site': frappe.local.site, 'enabled': enabled(),
            'test_mode_required': True, 'corporate_portal': False,
            'wallet_checkout_enrollments': frappe.db.count('Club100 Enrollment',
                {'wallet_settlement_status': ['in', ['Reserved', 'Settled', 'Review']]}),
            'review_pending': frappe.db.count('Club100 Enrollment', {'wallet_settlement_status': 'Review'})}
