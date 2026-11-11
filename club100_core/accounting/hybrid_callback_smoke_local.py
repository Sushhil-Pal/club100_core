"""Rollback-free, no-write callback admission smoke with fake Razorpay evidence.

Run only on club100.local with explicit CLI opt-in; no site flags enabled.
Actual D2B/D2C2C DB reservation smoke was validated separately.
"""
import hashlib
import hmac
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from club100_core.accounting import hybrid_callback_bridge as b
from club100_core.accounting import hybrid_payment_inbox as inbox


def run():
    inbox._admin()
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2C2_SMOKE') != '1':
        raise ValueError('Rollback-free callback smoke allowed only on local site with explicit flag')
    tracked = ['Club100 Enrollment', 'Club100 Hybrid Checkout',
               'Club100 Hybrid Payment Evidence', 'Club100 Wallet Ledger',
               'Club100 Wallet Reservation', 'Sales Invoice', 'Payment Entry', 'Journal Entry']
    before = {key: frappe.db.count(key) for key in tracked}
    checks = []
    secret = 'D2C2C2-LOCAL-MOCK-SECRET'
    hook_secret = 'D2C2C2-LOCAL-WEBHOOK-SECRET'
    oid, pid, enrollment = 'order_C2C2LOCAL', 'pay_C2C2LOCAL', 'ENR-C2C2-FAKE'
    e = SimpleNamespace(name=enrollment, payment_order_id=oid)
    config = {'mode': 'test', 'key_id': 'rzp_test_fake', 'key_secret': secret}
    sig = hmac.new(secret.encode(), f'{oid}|{pid}'.encode(), hashlib.sha256).hexdigest()
    body = json.dumps({'event': 'payment.captured',
        'payload': {'payment': {'entity': {'id': pid, 'order_id': oid, 'status': 'captured'}}}},
        separators=(',', ':')).encode()
    hook_sig = hmac.new(hook_secret.encode(), body, hashlib.sha256).hexdigest()
    order, payment = {'id': oid}, {'id': pid, 'order_id': oid, 'status': 'captured'}
    calls = []
    def fetch(cfg, order_id, payment_id):
        calls.append('GET')
        assert cfg == config and (order_id, payment_id) == (oid, pid)
        return order, payment
    def persist(enrollment_id, fetched_order, fetched_payment, origin, signed=None):
        calls.append('PERSIST')
        assert (enrollment_id, fetched_order, fetched_payment) == (enrollment, order, payment)
        assert origin in ('Browser', 'Webhook')
        if origin == 'Webhook':
            assert signed['signed_payload_only'] is True
        return {'record': 'FAKE-EVIDENCE', 'enrollment': enrollment,
                'state': 'Verified Pending Invoice'}
    # Pilot MUST be disabled by default on the site; don't set site config here.
    if b.route_enabled():
        raise ValueError('D2C2C2 route already active; smoke must run with routing DISABLED')
    checks.append('New public callback route is disabled by default')
    with patch.object(b, '_has_hybrid', return_value=True),\
         patch.object(b, 'route_enabled', return_value=True),\
         patch.object(inbox, '_identity', return_value=e),\
         patch.object(inbox, '_get_razorpay_config', return_value=config),\
         patch.object(inbox, '_get_razorpay_webhook_secret', return_value=hook_secret),\
         patch.object(inbox, '_gateway_guard', return_value=None),\
         patch.object(inbox, '_server_fetch', side_effect=fetch),\
         patch.object(inbox, '_persist', side_effect=persist):
        out = b.maybe_browser(enrollment, pid, oid, sig)
        assert out['success'] is False and out['payment_verified'] is True
        assert not out['wallet_captured'] and not out['membership_activated']
        checks.append('Valid browser signature routes to evidence-only service')
        prev = len(calls)
        try:
            b.maybe_browser(enrollment, pid, oid, '0' * 64)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid browser HMAC accepted')
        assert len(calls) == prev
        checks.append('Bad browser HMAC blocked before provider GET')
        out = b.maybe_webhook(enrollment, body, hook_sig)
        assert out['success'] is True and out['processing'] == 'finance_review_pending'
        assert out['invoice_or_gl_posted'] is False
        checks.append('Signed webhook routes to non-posting evidence and 2xx acknowledgement')
        prev = len(calls)
        try:
            b.maybe_webhook(enrollment, body + b' ', hook_sig)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid raw webhook signature accepted')
        assert len(calls) == prev
        checks.append('Changed raw webhook body rejected before provider GET')
    with patch.object(b, '_has_hybrid', return_value=False):
        assert b.maybe_browser('NON-HYBRID', pid, oid, sig) is None
        assert b.maybe_webhook('NON-HYBRID', body, hook_sig) is None
    checks.append('Non-hybrid legacy callbacks remain untouched')
    after = {key: frappe.db.count(key) for key in tracked}
    if before != after:
        raise RuntimeError('Read-only callback smoke changed database counts')
    return {'outcome': 'PASS', 'site': frappe.local.site, 'passed': len(checks),
            'cases': checks, 'mock_provider_get': True,
            'real_gateway_calls': 0, 'record_counts_unchanged': True,
            'wallet_captured': False, 'membership_activated': False,
            'invoice_or_gl_postings': 0, 'production_ready': False,
            'note': 'All provider and persistence calls mocked; routing flags remain disabled.'}
