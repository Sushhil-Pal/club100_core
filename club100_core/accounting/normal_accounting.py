"""Mandatory, no-toggle accounting for verified PWA Self membership purchases.

Only callable by trusted internal checkout finalizers (not @frappe.whitelist).
Never accepts browser-supplied price, cash amount, invoice, or payment status.
Uses the existing Stage 5E invoice + PE + wallet JE and its source GL checks.

When a provider-captured Razorpay payment cannot be posted, the local
transaction is rolled back to an explicit savepoint, the enrollment remains
Pending / Registered, and a durable Review state blocks duplicate payment.
No new Razorpay request or refund is made here.
"""
from contextlib import contextmanager
from uuid import uuid4

import frappe
from frappe.utils import now_datetime


@contextmanager
def _trusted_post():
    previous = getattr(frappe.flags, 'club100_verified_checkout_post', False)
    frappe.flags.club100_verified_checkout_post = True
    try:
        yield
    finally:
        frappe.flags.club100_verified_checkout_post = previous


@contextmanager
def _accounting_system_user():
    """Narrowly scope ERPNext voucher/link validation to the accounting service.

    The member remains authenticated/authorized in the checkout API, and
    payment authenticity is verified before this context is ever entered.
    NEVER use this for price, membership, wallet grant or payment verification.
    """
    from club100_core.accounting.settlement import _post_allowed
    if not getattr(frappe.flags, 'club100_verified_checkout_post', False):
        raise PermissionError('Unverified checkout cannot request accounting privileges')
    if not _post_allowed():
        raise PermissionError('Automatic accounting is restricted to local Razorpay Test Mode')
    # frappe.set_user() also replaces session.sid, session.data and
    # frappe.local.form_dict. Restoring just the username can invalidate the
    # member's browser session immediately after a successful checkout.
    previous_user = frappe.session.user
    previous_sid = frappe.session.sid
    previous_data = frappe.session.data
    previous_form_dict = frappe.local.form_dict
    try:
        if previous_user != 'Administrator':
            frappe.set_user('Administrator')
        yield
    finally:
        try:
            if frappe.session.user != previous_user:
                frappe.set_user(previous_user)
        finally:
            frappe.session.sid = previous_sid
            frappe.session.data = previous_data
            frappe.local.form_dict = previous_form_dict


def post_verified(enrollment_id):
    """Invoice + matching settlement in the SAME transaction as entitlement.

    Called after the enrollment is marked Paid and status Active/Scheduled by
    enrollment._finalize_paid_enrollment_doc, BEFORE referral reward posting.
    A failed invoice aborts the entitlement transaction.
    """
    from club100_core.accounting.settlement import post_one
    with _trusted_post():
        with _accounting_system_user():
            return post_one(enrollment_id, commit=False)


def _save_captured_manual_review(enrollment_id, payment_id, reason):
    """Store verified external payment evidence, but withhold Paid entitlement.

    Caller must already have authenticated and independently fetched the
    captured gateway payment. A Review state prevents all payment retries.
    """
    doc = frappe.get_doc('Club100 Enrollment', enrollment_id)
    if doc.payment_status == 'Paid' or doc.status != 'Registered':
        raise ValueError('Cannot overwrite a completed enrollment with Manual Review')
    if doc.payment_id and doc.payment_id != payment_id:
        raise ValueError('Another gateway payment is already bound to this enrollment')
    doc.payment_gateway = 'Razorpay'
    doc.payment_id = payment_id
    doc.payment_date = now_datetime()
    doc.razorpay_capture_verified = 1
    doc.wallet_settlement_status = 'Review'
    doc.wallet_review_reason = str(reason)[:400]
    doc.save(ignore_permissions=True)
    return doc


def finalize_verified_gateway(enrollment, payment_id, *, register=False):
    """One verified Razorpay capture -> settled accounting -> membership.

    The caller (browser HMAC+GET or signed webhook) must independently verify
    the captured payment, expected order ID, amount and currency first.
    """
    if not (enrollment.website_source == 'PWA Commerce'
            and enrollment.pricing_model == 'Online Plan'):
        raise ValueError('Internal accounting finalizer supports PWA online plans only')
    if enrollment.payment_status != 'Pending' or enrollment.status != 'Registered':
        raise ValueError('Only a pending registered enrollment may be finalized')
    if enrollment.get('wallet_settlement_status') == 'Review' or enrollment.payment_id:
        raise ValueError('Payment already recorded for reconciliation; do not retry')
    if not isinstance(payment_id, str) or not payment_id.startswith('pay_'):
        raise ValueError('Authenticated Razorpay payment ID required')

    from club100_core.wallet.checkout import settle_verified_gateway_or_review, review_response
    from club100_core.api.enrollment import _finalize_paid_enrollment_doc

    point = 'club100_normal_settle_' + uuid4().hex[:12]
    frappe.db.savepoint(point)
    try:
        if not settle_verified_gateway_or_review(enrollment, payment_id):
            # Existing wallet-review mechanism already persisted and committed
            # its state; do not roll back across that commit.
            return {**review_response(enrollment), 'do_not_retry_payment': True}
        enrollment.payment_gateway = 'Razorpay'
        enrollment.payment_id = payment_id
        enrollment.payment_date = now_datetime()
        enrollment.payment_status = 'Paid'
        enrollment.razorpay_capture_verified = 1
        activation = _finalize_paid_enrollment_doc(
            enrollment, issue_registration_token=bool(register))
        if not enrollment.sales_invoice:
            raise ValueError('Paid membership has no linked Sales Invoice')
        result = {'success': True, 'reused': False, 'enrollment': enrollment.name,
                  'payment_id': payment_id, 'payment_status': 'Paid',
                  'status': enrollment.status, 'invoice': enrollment.sales_invoice,
                  'currency': 'INR', **activation}
        frappe.db.commit()
        return result
    except Exception as exc:
        # An external Razorpay capture cannot be undone by a DB rollback.
        # Roll back all local invoice, PE, JE, spend ledger and activation
        # changes, then commit the identity into a no-retry review state.
        frappe.db.rollback(save_point=point)
        # Preserve the traceback for finance debugging. The browser receives
        # only the generic no-retry review response, never internal accounts.
        try:
            frappe.log_error(message=frappe.get_traceback(),
                             title='Club100 Captured Payment Accounting Review')
        except Exception:
            pass  # Error logging must not prevent durable payment identity.
        reviewed = _save_captured_manual_review(enrollment.name, payment_id, exc)
        frappe.db.commit()
        return {'success': False, 'manual_review': True,
                'do_not_retry_payment': True, 'payment_verified': True,
                'enrollment': reviewed.name, 'payment_status': 'Pending',
                'status': 'Registered',
                'message': 'Payment captured; accounting requires review. Do not pay again.'}


def healthcheck():
    """Read-only summary. No financial feature flag is required in development."""
    from club100_core.accounting import settlement
    eligible = settlement._post_allowed()
    cfg = settlement._config()
    return {'site': frappe.local.site, 'automatic_invoice_on_verified_pwa_self': eligible,
            'razorpay_mode': str(frappe.conf.get('razorpay_mode') or 'test'),
            'configured': {k: bool(v) for k, v in cfg.items()},
            'manual_review_count': frappe.db.count('Club100 Enrollment',
                                         {'wallet_settlement_status': 'Review'}),
            'paid_without_invoice_count': frappe.db.count('Club100 Enrollment', {
                                         'website_source':'PWA Commerce',
                                         'payment_status':'Paid',
                                         'sales_invoice':['is','not set']}),
            'financial_provenance_required': True,
            'note': 'Local Test Mode only; each captured payment must be audited against invoice and GL'}
