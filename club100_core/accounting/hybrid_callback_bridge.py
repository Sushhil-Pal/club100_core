"""Stage D2C2C2: local Razorpay Test Mode hybrid callback admission bridge.

Only an explicit local pilot can route authenticated payments to D2C2C's
NON-POSTING evidence inbox. It never marks an Enrollment Paid, captures a
wallet hold, activates membership, or creates/submits accounting documents.

This is imported from the existing public callbacks, not itself whitelisted.
"""
import os

import frappe
from frappe.utils import cint
from club100_core.accounting import hybrid_payment_inbox as inbox
from club100_core.accounting.hybrid_gateway_auth import (
    browser_signature_matches, parse_signed_webhook,
)


def route_enabled():
    # Signature and independently fetched captured payment are still mandatory.
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def _pilot_guard():
    if not route_enabled():
        raise ValueError('Hybrid callback disabled; manual payment review required. Do not retry payment.')


def _test_config():
    cfg = inbox._get_razorpay_config()
    if cfg.get('mode') != 'test' or not str(cfg.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Hybrid payment evidence requires Razorpay TEST credentials')
    return cfg


def _has_hybrid(enrollment_id):
    name = str(enrollment_id or '').strip()
    if not name or len(name) > 140:
        return False
    return bool(frappe.db.exists('Club100 Hybrid Checkout', {'enrollment': name}))


def _public_result(proof, source):
    """Never advertise Paid, invoice issued, or membership activated."""
    core = {'enrollment': proof['enrollment'], 'hybrid_evidence_record': proof['record'],
            'evidence_state': proof['state'], 'payment_verified': True,
            'payment_received': True, 'do_not_retry_payment': True,
            'wallet_captured': False, 'membership_activated': False,
            'invoice_or_gl_posted': False, 'production_ready': False}
    if source == 'Browser':
        return {'success': False, **core, 'manual_review': True,
                'message': 'Razorpay payment was verified. Membership activation and invoice '
                           'are pending review; do not pay again.'}
    return {'success': True, **core, 'source': 'webhook', 'manual_review': True,
            'acknowledged': True, 'processing': 'finance_review_pending'}


def maybe_browser(enrollment_id, payment_id, supplied_order_id, checkout_signature):
    """Called BEFORE legacy verify_payment row locks/paid-enrollment shortcut.

    Return None for non-hybrid enrollments; otherwise authenticated non-posting
    response. Invalid/disabled hybrid requests always fail closed.
    """
    if not _has_hybrid(enrollment_id):
        return None
    _pilot_guard()
    e = inbox._identity(enrollment_id)
    if supplied_order_id != e.payment_order_id:
        raise ValueError('Browser order does not match ERP-stored hybrid order')
    cfg = _test_config()
    browser_signature_matches(e.payment_order_id, payment_id,
                              checkout_signature, cfg['key_secret'])
    inbox._gateway_guard()
    order, payment = inbox._server_fetch(cfg, e.payment_order_id, payment_id)
    evidence = inbox._persist(e.name, order, payment, 'Browser')
    from club100_core.accounting.hybrid_runtime import financial_enabled, settle_verified_from_bridge, _flag
    if financial_enabled():
        with _flag('club100_hybrid_authenticated_bridge'):
            return settle_verified_from_bridge(e.name, order, payment)
    return _public_result(evidence, 'Browser')


def maybe_webhook(enrollment_id, raw_body, webhook_signature):
    """Called after legacy webhook verified raw HMAC and resolved order owner.

    Re-verify HMAC using actual raw HTTP bytes before any provider GET.
    """
    if not _has_hybrid(enrollment_id):
        return None
    _pilot_guard()
    signed = parse_signed_webhook(raw_body, webhook_signature,
                                  inbox._get_razorpay_webhook_secret())
    e = inbox._identity(enrollment_id)
    if signed['order_id'] != e.payment_order_id:
        raise ValueError('Signed webhook order differs from stored hybrid order')
    cfg = _test_config()
    inbox._gateway_guard()
    order, payment = inbox._server_fetch(cfg, e.payment_order_id, signed['payment_id'])
    evidence = inbox._persist(e.name, order, payment, 'Webhook', signed=signed)
    from club100_core.accounting.hybrid_runtime import financial_enabled, settle_verified_from_bridge, _flag
    if financial_enabled():
        with _flag('club100_hybrid_authenticated_bridge'):
            result = settle_verified_from_bridge(e.name, order, payment)
        # Acknowledge a captured payment even if accounting requires manual
        # review; retries must never invite another charge.
        return {'success': True, 'acknowledged': True, 'source': 'webhook',
                'settlement': result, 'manual_review': bool(result.get('manual_review')),
                'do_not_retry_payment': True}
    return _public_result(evidence, 'Webhook')


def healthcheck():
    inbox._admin()
    from club100_core.accounting.hybrid_runtime import financial_enabled
    return {'site': frappe.local.site, 'stage': 'HYBRID-ACTIVE-DEV',
            'guarded_callback_code_installed': True, 'route_enabled': route_enabled(),
            'browser_and_webhook_hooks_wired': True, 'evidence_only': not financial_enabled(),
            'wallet_capture_available_with_verified_backing': financial_enabled(),
            'invoice_or_gl_posting_available_with_verified_backing': financial_enabled(),
            'production_ready': False}
