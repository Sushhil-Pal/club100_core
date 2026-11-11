"""Opt-in LOCAL Razorpay Test Mode hybrid order creator, internal CLI only.

Never whitelisted. Creates a test order with the frozen gateway_due and saves
its identity on Enrollment; does NOT verify payment, capture grants, activate a
membership, generate an invoice, or post a GL entry. No live-mode access.
"""
import os
import requests
import frappe
from frappe.utils import cint, get_datetime, now_datetime
from club100_core.accounting.hybrid_reservations import _get_locked, _pair, _eligible_promotion_grants
from club100_core.accounting.hybrid_integration_rules import (
    validate_frozen_checkout, invoice_preflight, example_projection)
from club100_core.accounting.hybrid_order_contract import build_order_payload,validate_order_response
from club100_core.accounting.promotion_rules import rupees
from club100_core.api.enrollment import _get_razorpay_config

URL='https://api.razorpay.com/v1/orders'


def _guard(real_order=False):
    if (frappe.local.site != 'club100.local' or
        str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test' or
        cint(frappe.conf.get('club100_stage5fd2b_local_enabled',0)) != 1 or
        os.environ.get('CLUB100_ALLOW_STAGE5FD2B_LOCAL') != '1' or
        os.environ.get('CLUB100_ALLOW_STAGE5FD2C2B_LOCAL') != '1'):
        raise ValueError('D2C2B local pilot disabled; requires site, mode, config and environment safeguards')
    if real_order and (cint(frappe.conf.get('club100_stage5fd2c2b_test_order_enabled',0)) != 1 or
                       os.environ.get('CLUB100_ALLOW_STAGE5FD2C2B_TEST_ORDER') != '1'):
        raise ValueError('Razorpay Test Order creation is disabled')
    user=frappe.session.user
    if user!='Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)


def _locked_plan(enrollment_id):
    enrollment,hybrid=_get_locked(enrollment_id)  # member, wallet, enrollment, hybrid locks
    if hybrid.status!='Reserved' or hybrid.member!=enrollment.member:
        raise ValueError('Reserved hybrid checkout and owner identity required')
    if get_datetime(hybrid.expires_at)<=now_datetime():
        raise ValueError('Hybrid reservations expired; no new gateway order allowed')
    if (enrollment.website_source!='PWA Commerce' or enrollment.pricing_model!='Online Plan'
        or cint(enrollment.is_trial) or enrollment.status!='Registered'
        or enrollment.payment_status!='Pending' or enrollment.payment_id or
        enrollment.wallet_reservation_key or enrollment.sales_invoice):
        raise ValueError('Only eligible, unpaid, unreserved PWA Self enrollments supported')
    if frappe.db.get_value('Club100 Plan',enrollment.plan,'plan_code')!='self':
        raise ValueError('Local pilot is Self-only')
    if rupees(enrollment.amount_payable)!=rupees(hybrid.original_price):
        raise ValueError('Original enrollment price differs from frozen quote')
    if rupees(hybrid.promotion_discount or 0):
        approved=set(_eligible_promotion_grants(hybrid.member,hybrid.policy_version))
    else: approved=set()
    pairs=_pair(hybrid,('Reserved',))
    if any(x['grant'] not in approved for x in pairs['promotion']):
        raise ValueError('Promotion approval revoked: deny gateway order')
    fields=('enrollment','member','status','policy_version','original_price',
        'promotion_discount','funded_wallet_tender','gateway_due','gst_percent',
        'promotion_reservation_key','funded_reservation_key','plan_snapshot_json')
    frozen={field:hybrid.get(field) for field in fields}
    pre=validate_frozen_checkout(frozen,pairs['promotion'],pairs['funded'])
    invoice_preflight(pre,example_projection(pre))
    payload=build_order_payload(pre,hybrid.name,enrollment.program,enrollment.plan,
                                enrollment.membership_duration_months)
    return enrollment,hybrid,pre,payload


def preview_test_order(enrollment_id):
    """Admin CLI only, no gateway request and no mutation."""
    _guard()
    enrollment,hybrid,pre,payload=_locked_plan(enrollment_id)
    # A previous, potentially paid order requires server-side review first.
    if enrollment.payment_order_id:
        raise ValueError('Stored order exists; use guarded order reconciliation, not creation preview')
    return {'site':frappe.local.site,'enrollment':enrollment.name,
            'hybrid':hybrid.name,'snapshot_sha256':pre['snapshot_sha256'],
            'expected_gateway_paise':payload['amount'],'currency':'INR',
            'read_only':True,'order_created':False,'posting_authorized':False,
            'production_ready':False}


def _http_request(method,url,config, payload=None):
    if method not in ('POST','GET') or not url.startswith(URL):
        raise ValueError('Unsupported Razorpay order request')
    try:
        response=requests.request(method,url,json=payload if method=='POST' else None,
                                  auth=(config['key_id'],config['key_secret']),timeout=12)
        response.raise_for_status()
        body=response.json()
    except (requests.RequestException,ValueError) as exc:
        raise ValueError('Razorpay Test Order response failed verification') from exc
    if not isinstance(body,dict):
        raise ValueError('Razorpay response must be a dictionary')
    return body


def create_test_order(enrollment_id):
    """Administrator CLI only: REAL Razorpay TEST ORDER, never a payment or capture.

    The site flag and process environment must BOTH be opted in. Never expose
    through a whitelisted endpoint or invoke in an automatic hook.
    """
    _guard(real_order=True)
    enrollment,hybrid,pre,payload=_locked_plan(enrollment_id)
    config=_get_razorpay_config()
    if config.get('mode')!='test' or not str(config.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Only Razorpay Test Mode credentials supported')
    prior=str(enrollment.payment_order_id or '').strip()
    if prior:
        if not prior.startswith('order_'):
            raise ValueError('Stored Razorpay Order ID is invalid')
        order=_http_request('GET',URL+'/'+prior,config)
        checked=validate_order_response(payload,order,reuse=True)
        if checked['order_id']!=prior:
            raise ValueError('Stored order ID differs from Razorpay response')
        reused=True
    else:
        # No payment, invoice, reservation capture or wallet ledger changes.
        order=_http_request('POST',URL,config,payload)
        checked=validate_order_response(payload,order)
        if get_datetime(hybrid.expires_at)<=now_datetime():
            raise ValueError('Reservation expired during provider request; do not expose new test order')
        enrollment.payment_gateway='Razorpay'
        enrollment.payment_order_id=checked['order_id']
        enrollment.save(ignore_permissions=True)
        frappe.db.commit()
        reused=False
    return {'outcome':'TEST_ORDER_CREATED' if not reused else 'TEST_ORDER_REUSED',
            'enrollment':enrollment.name,'hybrid':hybrid.name,
            'order_id':checked['order_id'],'amount_paise':checked['amount'],
            'snapshot_sha256':pre['snapshot_sha256'],'currency':'INR',
            'test_mode_only':True,'wallet_captured':False,'member_activated':False,
            'invoice_or_gl_posted':False,'posting_authorized':False,
            'production_ready':False}


def healthcheck():
    user=frappe.session.user
    if user!='Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required',frappe.PermissionError)
    return {'site':frappe.local.site,'stage':'5F-D2C2B',
            'guarded_test_order_code_installed':True,
            'new_test_order_enabled':cint(frappe.conf.get('club100_stage5fd2c2b_test_order_enabled',0))==1,
            'legacy_hybrid_order_and_callback_blocked':True,
            'new_public_callback_or_webhook_active':False,
            'wallet_capture_enabled':False,'invoicing_enabled':False,
            'production_ready':False}
