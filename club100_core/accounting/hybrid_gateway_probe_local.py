"""INTERNAL local-only Razorpay GET proof inspection; never posts payments.

Only tests with a pre-existing Hybrid Checkout and stored Razorpay order ID.
NO whitelisting; does not create, capture or refund orders or wallet balances.
The bridge is not invoked by existing browser/webhook endpoints in this stage.
"""
import os
import json
import requests

import frappe
from frappe.utils import cint, get_datetime, now_datetime
from club100_core.api.enrollment import _get_razorpay_config, _get_razorpay_webhook_secret
from club100_core.accounting.hybrid_reservations import _pair
from club100_core.accounting.hybrid_integration_rules import (
    validate_frozen_checkout, verify_razorpay_evidence,
    invoice_preflight, example_projection,
)
from club100_core.accounting.hybrid_gateway_auth import (
    browser_signature_matches, parse_signed_webhook, assert_webhook_matches_fetched,
)
from club100_core.accounting.promotion_rules import rupees

API='https://api.razorpay.com/v1'


def _guard():
    if (frappe.local.site!='club100.local'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower()!='test'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2A_GATEWAY_GET')!='1'):
        raise ValueError('Probe disabled; local test site, test mode and explicit gateway GET flag required')
    user=frappe.session.user
    if user!='Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)


def _snapshot(enrollment_id):
    """Read-only preflight, NOT locking; never sufficient for posting."""
    enrollment_id=str(enrollment_id or '').strip()
    if not enrollment_id:
        raise ValueError('Enrollment ID required')
    e=frappe.get_doc('Club100 Enrollment',enrollment_id)
    if (e.payment_status!='Pending' or e.status!='Registered' or not e.member
            or e.payment_id or e.website_source!='PWA Commerce'
            or e.pricing_model!='Online Plan' or cint(e.is_trial)):
        raise ValueError('Expected unpaid, member-linked, nontrial PWA enrollment')
    if not e.payment_order_id:
        raise ValueError('This read-only probe requires an existing stored Razorpay order')
    name=frappe.db.get_value('Club100 Hybrid Checkout',{'enrollment':e.name},'name')
    if not name:
        raise ValueError('Enrollment has no hybrid checkout hold')
    hybrid=frappe.get_doc('Club100 Hybrid Checkout',name)
    if hybrid.member!=e.member or hybrid.status!='Reserved':
        raise ValueError('Hybrid hold is not reserved or belongs to a different member')
    if get_datetime(hybrid.expires_at)<=now_datetime():
        raise ValueError('Hybrid reservation expired')
    if rupees(hybrid.original_price)!=rupees(e.amount_payable):
        raise ValueError('Enrollment original price differs from frozen hybrid snapshot')
    pair=_pair(hybrid,('Reserved',))
    fields=('enrollment','member','status','policy_version','original_price',
            'promotion_discount','funded_wallet_tender','gateway_due','gst_percent',
            'promotion_reservation_key','funded_reservation_key','plan_snapshot_json')
    frozen={key:hybrid.get(key) for key in fields}
    pre=validate_frozen_checkout(frozen,pair['promotion'],pair['funded'])
    if pre['expected_gateway_paise']<=0:
        raise ValueError('Wallet-only payments have no Razorpay order or payment')
    invoice_preflight(pre,example_projection(pre))
    return e,hybrid,pre


def _server_fetch(config, order_id, payment_id):
    """Read-only GET against Razorpay using server credentials, never caller data."""
    if config['mode']!='test' or not config['key_id'].startswith('rzp_test_'):
        raise ValueError('Only Razorpay Test Mode allowed')
    auth=(config['key_id'],config['key_secret'])
    out=[]
    for kind,identifier in (('orders',order_id),('payments',payment_id)):
        if (kind=='orders' and not identifier.startswith('order_')) or (kind=='payments' and not identifier.startswith('pay_')):
            raise ValueError('Invalid provider ID')
        if len(identifier)>128 or not all(c.isascii() and (c.isalnum() or c=='_') for c in identifier):
            raise ValueError('Invalid provider ID characters')
        try:
            response=requests.get(f'{API}/{kind}/{identifier}',auth=auth,timeout=12)
            response.raise_for_status()
            value=response.json()
        except (requests.RequestException,ValueError) as exc:
            raise ValueError('Razorpay evidence cannot be fetched or decoded') from exc
        if not isinstance(value,dict) or value.get('id')!=identifier:
            raise ValueError('Razorpay returned mismatched provider identity')
        out.append(value)
    return out


def _probe(enrollment_id, payment_id, browser_signature=None, signed_webhook=None):
    _guard()
    e,h,pre=_snapshot(enrollment_id)
    config=_get_razorpay_config()
    payment_id=str(payment_id or '').strip()
    stored_order=str(e.payment_order_id or '').strip()
    if browser_signature is not None:
        browser_signature_matches(stored_order,payment_id,browser_signature,config['key_secret'])
    elif signed_webhook is not None:
        if signed_webhook['order_id']!=stored_order or signed_webhook['payment_id']!=payment_id:
            raise ValueError('Signed webhook IDs do not match ERP-stored order/payment')
    else:
        raise ValueError('Authenticated checkout or webhook proof required before gateway GET')
    order,payment=_server_fetch(config,stored_order,payment_id)
    if signed_webhook is not None:
        assert_webhook_matches_fetched(signed_webhook,payment)
    proof=verify_razorpay_evidence(pre,stored_order,order,payment)
    return {'outcome':'VERIFIED_EVIDENCE_ONLY','site':frappe.local.site,
        'enrollment':e.name,'hybrid':h.name,'snapshot_sha256':pre['snapshot_sha256'],
        'provider_order_id':proof['order_id'],'provider_payment_id':proof['payment_id'],
        'amount_paise':proof['amount_paise'],'read_only':True,
        'wallet_captured':False,'enrollment_finalized':False,'invoicing_posted':False,
        'posting_authorized':False,'production_ready':False}


def inspect_browser_payment(enrollment_id, payment_id, checkout_signature):
    """Administrator CLI only, non-posting. No whitelisted public API."""
    return _probe(enrollment_id,payment_id,browser_signature=checkout_signature)


def inspect_signed_webhook(enrollment_id, raw_body_utf8, webhook_signature):
    """Administrator CLI probe; exact provided body UTF-8 bytes needed.

    Production webhook must pass raw HTTP bytes directly, not JSON re-encoding.
    This function is for LOCAL diagnostics, not an HTTP webhook implementation.
    """
    _guard()
    if not isinstance(raw_body_utf8,str):
        raise ValueError('CLI webhook body must be a string')
    signed=parse_signed_webhook(raw_body_utf8.encode('utf-8'),webhook_signature,
                                _get_razorpay_webhook_secret())
    return _probe(enrollment_id,signed['payment_id'],signed_webhook=signed)


def healthcheck():
    # This does not fetch provider credentials or make external calls.
    user=frappe.session.user
    if user!='Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)
    return {'site':frappe.local.site,'stage':'5F-D2C2A',
        'browser_hmac_contract':True,'raw_webhook_hmac_contract':True,
        'authenticated_provider_get_adapter':True,
        'provider_get_opt_in':os.environ.get('CLUB100_ALLOW_STAGE5FD2C2A_GATEWAY_GET')=='1',
        'browser_and_webhook_hooks_changed':False,'orders_created':False,
        'wallet_captured':False,'invoice_posting_enabled':False,
        'production_ready':False}
