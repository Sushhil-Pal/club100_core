"""Stage 5F-D2B: INTERNAL local-pilot hybrid reservation coordinator.

Two wallet reservations have distinct legal meanings:
  Promotion -> supplier discount allowance, NOT tender or a GL payment.
  Corporate/Referral -> funded tender on the reduced-value invoice.

No whitelisted APIs. No hooks into existing checkout, callbacks, invoices, refunds
or ERPNext GL. Captures/refunds in this stage are smoke-only and ALWAYS rolled
back by hybrid_smoke_local; D2C must provide independently verified evidence.
"""
import json
import os
import re
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe.utils import cint, getdate, get_datetime, now_datetime
from club100_core.accounting.promotion_rules import build_hybrid_plan, rupees, verify_source_split
from club100_core.wallet.spending import reserve, capture, release, refund_full
from club100_core.wallet.logic import is_active

PROMOTION = ('Promotion',)
FUNDED = ('Corporate', 'Referral')
POLICY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{2,79}$')


def _pilot():
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test'
            and cint(frappe.conf.get('club100_stage5fd2b_local_enabled', 0)) == 1
            and os.environ.get('CLUB100_ALLOW_STAGE5FD2B_LOCAL') == '1')


def _require_pilot():
    if not _pilot():
        raise ValueError('Hybrid pilot is disabled: local test site, feature and environment guards required')
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)


def _require_smoke():
    _require_pilot()
    if not getattr(frappe.flags, 'club100_stage5fd2b_smoke', False):
        raise ValueError('Real payment and refund capture disabled until Stage 5F-D2C verification is integrated')


@contextmanager
def _pilot_write():
    old = getattr(frappe.flags, 'club100_hybrid_service_write', False)
    frappe.flags.club100_hybrid_service_write = True
    try:
        yield
    finally:
        frappe.flags.club100_hybrid_service_write = old


def _transaction(do):
    savepoint = 'd2b_' + uuid4().hex[:18]
    frappe.db.savepoint(savepoint)
    try:
        return do()
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _locked_enrollment(enrollment_id):
    name = str(enrollment_id or '').strip()
    basic = frappe.db.get_value('Club100 Enrollment', name, 'member')
    if not basic:
        raise ValueError('Enrollment with Member required')
    # Preserve the existing wallet lock order: Member -> wallet -> enrollment.
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', basic)
    frappe.db.sql('SELECT name FROM `tabClub100 Wallet` WHERE member=%s FOR UPDATE', basic)
    frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE', name)
    doc = frappe.get_doc('Club100 Enrollment', name)
    if doc.member != basic:
        raise ValueError('Member changed during enrollment lock')
    return doc


def _assert_fresh_enrollment(doc):
    if (doc.website_source != 'PWA Commerce' or doc.pricing_model != 'Online Plan'
            or cint(doc.is_trial) or doc.payment_status != 'Pending'
            or doc.status != 'Registered' or not doc.member
            or doc.payment_order_id or doc.payment_id or doc.sales_invoice
            or doc.wallet_reservation_key):
        raise ValueError('Only an unpaid, unreserved PWA Self enrollment can enter hybrid pilot')
    if frappe.db.get_value('Club100 Plan', doc.plan, 'plan_code') != 'self':
        raise ValueError('Hybrid pilot is currently Self-only')
    months = cint(doc.membership_duration_months)
    if months not in (1, 3, 6, 12) or rupees(doc.monthly_equivalent) * months != rupees(doc.amount_payable):
        raise ValueError('Server-priced Self membership snapshot is inconsistent')


def _eligible_promotion_grants(member, policy_version):
    """Return active, directly issued Promotion grants with system-generated provenance.

    No separate approval document is required. Historical revocations must be
    represented on the grant before retiring the obsolete Approval DocType.
    The wallet reservation engine still checks remaining balance and expiry.
    """
    if not POLICY_RE.fullmatch(str(policy_version or '')):
        raise ValueError('Valid promotion policy version required')
    rows = frappe.get_all('Club100 Wallet Grant',
        filters={'member': member, 'source_type': 'Promotion', 'status': 'Active'},
        fields=['name', 'source_event_key', 'funding_reference', 'owner'],
        limit_page_length=10000)
    if not rows:
        return []
    eligible = []
    for row in rows:
        key = str(row.source_event_key or '')
        expected = 'PROMO:ADMIN:' + key
        # Grants created by the authenticated administrative issuance path.
        if (row.owner
                and key.startswith('PROMOTION:ADMIN:')
                and str(row.funding_reference or '') == expected):
            eligible.append(row.name)
    return eligible


def _status(doc):
    return {'enrollment': doc.enrollment, 'status': doc.status, 'record': doc.name,
            'original_price': str(rupees(doc.original_price)),
            'promotion_discount': str(rupees(doc.promotion_discount or 0)),
            'funded_wallet': str(rupees(doc.funded_wallet_tender or 0)),
            'gateway_due': str(rupees(doc.gateway_due or 0)),
            'expires_at': str(doc.expires_at),
            'promotion_reservation_key': doc.promotion_reservation_key,
            'funded_reservation_key': doc.funded_reservation_key,
            'accounting_posted': False, 'gateway_called': False}


def _pair(doc, allowed=('Reserved', 'Captured', 'Refunded', 'Released', 'Expired')):
    """Verify immutable source and amount identity against both wallet reservations."""
    items = {}
    for kind, key in (('promotion', doc.promotion_reservation_key),('funded', doc.funded_reservation_key)):
        target = rupees(doc.promotion_discount if kind == 'promotion' else doc.funded_wallet_tender)
        if not key:
            if target:
                raise ValueError('Positive amount missing its reservation')
            items[kind] = []
            continue
        row = frappe.db.get_value('Club100 Wallet Reservation', {'reservation_key':key},
                    ['name','member','reference','total_amount','status'], as_dict=True)
        if not row or row.member != doc.member or row.reference != doc.enrollment or rupees(row.total_amount) != target:
            raise ValueError('Hybrid wallet reservation identity mismatch')
        if row.status not in allowed:
            raise ValueError('Reservation is in an invalid lifecycle state: ' + str(row.status))
        lines = frappe.get_all('Club100 Wallet Reservation Item', filters={'parent':row.name},
                               fields=['grant', 'source_type', 'amount'], order_by='idx asc')
        items[kind] = [{'grant':x.grant,'source':x.source_type,'amount':str(rupees(x.amount))} for x in lines]
    verify_source_split(items, doc.promotion_discount or 0, doc.funded_wallet_tender or 0)
    if json.loads(doc.promotion_allocations_json or '[]') != items['promotion']:
        raise ValueError('Promotion allocations changed after checkout snapshot')
    if json.loads(doc.funded_allocations_json or '[]') != items['funded']:
        raise ValueError('Funded allocations changed after checkout snapshot')
    return items


def _snapshot_equal(doc, promo, funded, policy):
    return (rupees(doc.promotion_discount or 0)==promo and rupees(doc.funded_wallet_tender or 0)==funded
            and doc.policy_version == policy)


def reserve_hybrid(enrollment_id, promotion_discount, funded_wallet, policy_version, ttl_minutes=60):
    """Create both holds atomically; if the second leg fails, neither survives.

    All amounts are calculated against the DB-owned original price, not browser
    amounts. Requires specific authorized Promotion grants (no separate approval record).
    Zero-value legs create no reservation. No payment or invoice occurs.
    """
    # New guarded member flow must still pass explicit E2E/finance flags.
    if getattr(frappe.flags, 'club100_hybrid_e2e_checkout', False):
        from club100_core.accounting.hybrid_runtime import require_financial_enabled
        require_financial_enabled()
    else:
        _require_smoke()
    discount = rupees(promotion_discount)
    funded = rupees(funded_wallet)
    policy = str(policy_version or '').strip()
    if not POLICY_RE.fullmatch(policy):
        raise ValueError('Approved policy version required')
    if type(ttl_minutes) is not int or ttl_minutes < 1 or ttl_minutes > 60:
        raise ValueError('TTL must be an integer from 1 to 60')
    if discount == 0 and funded == 0:
        raise ValueError('Hybrid reservation requires at least one wallet or promotion leg')

    def work():
        doc = _locked_enrollment(enrollment_id)
        prior_name = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment':doc.name}, 'name')
        if prior_name:
            old=frappe.get_doc('Club100 Hybrid Checkout', prior_name)
            if not _snapshot_equal(old,discount,funded,policy) or rupees(old.original_price)!=rupees(doc.amount_payable):
                raise ValueError('Different amount or policy replay for existing enrollment')
            _pair(old)
            return _status(old)
        _assert_fresh_enrollment(doc)
        full = rupees(doc.amount_payable)
        due = full - discount - funded
        from club100_core.accounting.hybrid_gst import current_rate
        tax_percent = str(current_rate())
        plan = build_hybrid_plan(full, discount, funded, due, tax_percent)
        eligible = _eligible_promotion_grants(doc.member,policy) if discount else []
        if discount and not eligible:
            raise ValueError('No eligible authorized Promotion grants available for this member')
        pkey='HYB:P:'+doc.name if discount else None
        fkey='HYB:F:'+doc.name if funded else None
        p = reserve(doc.member,pkey,doc.name,str(discount),ttl_minutes=ttl_minutes,
                    allowed_sources=PROMOTION,allowed_grants=eligible) if discount else None
        f = reserve(doc.member,fkey,doc.name,str(funded),ttl_minutes=ttl_minutes,
                    allowed_sources=FUNDED) if funded else None
        rows={'promotion':p['allocations'] if p else [],'funded':f['allocations'] if f else []}
        verify_source_split(rows,discount,funded)
        # A server-frozen quote and both original grant lists are retained.
        expiry = min(get_datetime(x['expires_at']) for x in (p,f) if x)
        with _pilot_write():
            record = frappe.get_doc({'doctype':'Club100 Hybrid Checkout',
                 'enrollment':doc.name,'member':doc.member,'original_price':str(full),
                 'promotion_discount':str(discount),'funded_wallet_tender':str(funded),
                 'gateway_due':str(due),'gst_percent':tax_percent,'policy_version':policy,
                 'promotion_reservation_key':pkey,'funded_reservation_key':fkey,
                 'promotion_allocations_json':json.dumps(rows['promotion'],sort_keys=True),
                 'funded_allocations_json':json.dumps(rows['funded'],sort_keys=True),
                 'plan_snapshot_json':json.dumps(plan,sort_keys=True),
                 'status':'Reserved','expires_at':expiry}).insert(ignore_permissions=True)
        _pair(record, ('Reserved',))
        return _status(record)
    return _transaction(work)


def _get_locked(enrollment_id):
    enrollment = _locked_enrollment(enrollment_id)
    name = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment':enrollment.name}, 'name')
    if not name:
        raise ValueError('No hybrid reservation for enrollment')
    frappe.db.sql('SELECT name FROM `tabClub100 Hybrid Checkout` WHERE name=%s FOR UPDATE', name)
    record = frappe.get_doc('Club100 Hybrid Checkout',name)
    if record.member != enrollment.member:
        raise ValueError('Hybrid checkout member mismatch')
    return enrollment,record


def simulate_capture_local(enrollment_id, simulated_proof):
    """D2B state-machine UAT ONLY. Not payment verification or settlement.

    This is intentionally UNCALLABLE without the temporary smoke-test flag.
    Stage D2C will add Razorpay verification and invoice event orchestration.
    """
    _require_smoke()
    if not str(simulated_proof or '').startswith('SMOKE:'):
        raise ValueError('Synthetic proof must be visibly marked SMOKE')
    def work():
        enrollment, record = _get_locked(enrollment_id)
        if record.status=='Captured':
            _pair(record,('Captured',))
            if record.payment_reference != simulated_proof:
                raise ValueError('Capture proof changed across replay')
            return _status(record)
        if record.status != 'Reserved':
            raise ValueError('Capture requires Reserved state')
        if get_datetime(record.expires_at) <= now_datetime():
            raise ValueError('Hybrid reservation expired')
        _pair(record,('Reserved',))
        for key in (record.promotion_reservation_key,record.funded_reservation_key):
            if key:
                capture(key,record.enrollment)
        record.status='Captured'
        record.payment_reference=simulated_proof
        record.captured_at=now_datetime()
        with _pilot_write():record.save(ignore_permissions=True)
        _pair(record,('Captured',))
        return _status(record)
    return _transaction(work)


def release_unpaid(enrollment_id):
    """Release holds before payment; cannot release if external payment exists."""
    _require_smoke()
    def work():
        enrollment, record = _get_locked(enrollment_id)
        if record.status=='Released':
            _pair(record,('Released','Expired'))
            return _status(record)
        if record.status!='Reserved':
            raise ValueError('Cannot release a captured/refunded/review checkout')
        if enrollment.payment_id or cint(enrollment.get('razorpay_capture_verified')):
            raise ValueError('Payment evidence exists: request manual review, not release')
        _pair(record,('Reserved',))
        for key in (record.promotion_reservation_key,record.funded_reservation_key):
            if key:release(key)
        record.status='Released'
        record.released_at=now_datetime()
        with _pilot_write():record.save(ignore_permissions=True)
        _pair(record,('Released','Expired'))
        return _status(record)
    return _transaction(work)


def simulate_refund_local(enrollment_id, simulated_refund_proof):
    """Full return to original grant IDs, UAT rollback ONLY; no Razorpay refund.

    If a grant expired or was revoked, fail closed for manual-review treatment.
    """
    _require_smoke()
    if not str(simulated_refund_proof or '').startswith('SMOKE:REFUND:'):
        raise ValueError('Synthetic refund proof must be visibly marked SMOKE:REFUND')
    def work():
        enrollment,record = _get_locked(enrollment_id)
        if record.status=='Refunded':
            _pair(record,('Refunded',))
            if record.refund_reference!=simulated_refund_proof:
                raise ValueError('Refund proof differs from original')
            return _status(record)
        if record.status!='Captured':
            raise ValueError('Only captured hybrid reservation can be refunded')
        rows = _pair(record,('Captured',))
        now = getdate()
        for kind in ('promotion','funded'):
            for item in rows[kind]:
                grant=frappe.db.get_value('Club100 Wallet Grant',item['grant'],
                        ['status','valid_from','validity_type','expires_on'],as_dict=True)
                if not grant or grant.status!='Active' or not is_active(grant.valid_from,grant.validity_type,grant.expires_on,now):
                    raise ValueError('Original grant expired or inactive: manual refund review required')
                if kind=='promotion' and item['grant'] not in _eligible_promotion_grants(record.member,record.policy_version):
                    raise ValueError('Promotion grant is revoked or ineligible: manual review required')
        for key in (record.promotion_reservation_key,record.funded_reservation_key):
            if key: refund_full(key)
        record.status='Refunded'
        record.refund_reference=simulated_refund_proof
        record.refunded_at=now_datetime()
        with _pilot_write():record.save(ignore_permissions=True)
        _pair(record,('Refunded',))
        return _status(record)
    return _transaction(work)


def healthcheck():
    """Administrator-only, read-only; shows no real checkout/posting integration."""
    user=frappe.session.user
    if user!='Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required',frappe.PermissionError)
    return {'site':frappe.local.site,'local_pilot_enabled':_pilot(),
            'dual_reservation_code_installed':True,
            'real_gateway_capture_enabled':False,'real_refund_enabled':False,
            'invoice_discount_enabled':False,'pwa_integration_enabled':False,
            'existing_checkout_unchanged':True,'posting_enabled':False,
            'production_ready':False}
