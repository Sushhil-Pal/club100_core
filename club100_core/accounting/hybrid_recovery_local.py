"""Stage D2C2C2: administrator evidence queue + read-only re-verification.

No public API, no payments/refunds/capture, no mutation of evidence or wallet.
Manual reviewers must not equate a verified payment to a settled invoice.
"""
import os

import frappe
from frappe.utils import cint, get_datetime, now_datetime
from club100_core.accounting import hybrid_payment_inbox as inbox
from club100_core.accounting.hybrid_evidence_rules import (
    identity_digest, replay_equal, decide_review,
)
from club100_core.accounting.hybrid_integration_rules import verify_razorpay_evidence
from club100_core.accounting.hybrid_evidence_rules import require_provider_identity
from club100_core.accounting.hybrid_gateway_auth import assert_webhook_matches_fetched

DT = 'Club100 Hybrid Payment Evidence'


def pending_queue(limit=50):
    """Read-only admin report, never creates or changes settlement state."""
    inbox._admin()
    n = int(limit)
    if n < 1 or n > 100:
        raise ValueError('limit must be in 1..100')
    entries = frappe.get_all(DT,
        filters={'state': ['in', ['Verified Pending Invoice', 'Manual Review']]},
        fields=['name', 'enrollment', 'hybrid_checkout', 'state', 'review_reason',
                'razorpay_order_id', 'razorpay_payment_id', 'amount_paise',
                'verified_on'], order_by='verified_on asc', limit_page_length=n)
    return {'site': frappe.local.site, 'read_only': True,
            'posting_authorized': False, 'count': len(entries), 'items': entries,
            'action': 'Manually reconcile payment, wallet holds and invoice before activation'}


def reverify(evidence_name):
    """Recheck a previously verified evidence row using current provider GET.

    Does not bootstrap unsigned new payments and never promotes Manual Review.
    """
    inbox._admin()
    if (frappe.local.site != 'club100.local'
        or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test'
        or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2C2_RECOVERY_GET') != '1'
        or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2C_PROVIDER_GET') != '1'):
        raise ValueError('Recovery GET disabled; explicit local Test Mode opt-in required')
    name = str(evidence_name or '').strip()
    if not name or not frappe.db.exists(DT, name):
        raise ValueError('Existing verified evidence record required')
    record = frappe.get_doc(DT, name)
    enrollment, hybrid = inbox._get_locked(record.enrollment)
    if hybrid.name != record.hybrid_checkout or enrollment.member != record.member:
        raise ValueError('Evidence ownership changed')
    if enrollment.payment_order_id != record.razorpay_order_id:
        raise ValueError('Enrollment Razorpay order changed')
    cfg = inbox._get_razorpay_config()
    if cfg.get('mode') != 'test' or not str(cfg.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Razorpay TEST credentials required')
    order, payment = inbox._server_fetch(cfg, record.razorpay_order_id,
                                         record.razorpay_payment_id)
    pre, pair = inbox._frozen_preflight(enrollment, hybrid)
    proof = verify_razorpay_evidence(pre, record.razorpay_order_id, order, payment)
    ident = require_provider_identity(pre, hybrid.name, record.razorpay_order_id,
                                      order, payment, proof)
    replay_equal({k: record.get(k) for k in ('enrollment', 'hybrid_checkout', 'member',
                  'razorpay_order_id', 'razorpay_payment_id', 'amount_paise',
                  'snapshot_sha256', 'identity_sha256', 'state')}, ident)
    # Report changes since original verification without altering the immutable row.
    reserved = hybrid.status == 'Reserved'
    holds = True
    expiry_ok = get_datetime(hybrid.expires_at) > now_datetime()
    for key in (hybrid.promotion_reservation_key, hybrid.funded_reservation_key):
        if not key:
            continue
        hold = frappe.db.get_value('Club100 Wallet Reservation',
            {'reservation_key': key}, ['status', 'expires_at'], as_dict=True)
        if not hold or hold.status != 'Reserved':
            holds = False
        if not hold or get_datetime(hold.expires_at) <= now_datetime():
            expiry_ok = False
    # GST approval must be revalidated by Finance separately: no auto-promotion.
    review = decide_review(enrollment.payment_status == 'Pending'
                           and enrollment.status == 'Registered'
                           and not enrollment.payment_id and not enrollment.sales_invoice,
                           reserved, holds, expiry_ok, False)
    return {'site': frappe.local.site, 'record': record.name,
            'stored_state': record.state, 'provider_capture_still_valid': True,
            'current_manual_review_required': True,
            'current_review_reason': review['review_reason'] or 'Finance/GST reapproval required',
            'read_only': True, 'posting_authorized': False,
            'wallet_captured': False, 'invoice_or_gl_posted': False,
            'production_ready': False}
