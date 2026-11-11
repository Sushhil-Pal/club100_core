"""Mocked provider GET and HMAC local smoke. No ledger/checkout/GL mutations."""
import hashlib
import hmac
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from club100_core.accounting import hybrid_gateway_probe_local as bridge
from club100_core.accounting.hybrid_integration_rules import validate_frozen_checkout
from club100_core.accounting.promotion_rules import build_hybrid_plan


class MockReply:
    def __init__(self, value):self.value=value
    def raise_for_status(self):return None
    def json(self):return dict(self.value)


def run():
    if (frappe.local.site!='club100.local'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower()!='test'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2A_SMOKE')!='1'):
        raise ValueError('Explicit local Test Mode smoke flag required')
    if frappe.session.user!='Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required',frappe.PermissionError)
    doctypes=('Club100 Enrollment','Club100 Hybrid Checkout','Club100 Wallet Reservation',
              'Club100 Wallet Ledger','Sales Invoice','Payment Entry','Journal Entry')
    before={dt:frappe.db.count(dt) for dt in doctypes}
    enrollment='SMOKE:D2C2A:ENR'
    order_id='order_mockedD2C2A'
    payment_id='pay_mockedD2C2A'
    secret='LOCAL_D2C2A_SECRET_NOT_REAL'
    webhook_secret='LOCAL_D2C2A_WEBHOOK_SECRET_NOT_REAL'
    plan=build_hybrid_plan('799','100','300','399','5')
    snapshot={'enrollment':enrollment,'member':'SMOKE:D2C2A:MEM','status':'Reserved',
        'policy_version':'LOCAL-SMOKE-1','original_price':'799',
        'promotion_discount':'100','funded_wallet_tender':'300','gateway_due':'399',
        'gst_percent':'5','promotion_reservation_key':'HYB:P:'+enrollment,
        'funded_reservation_key':'HYB:F:'+enrollment,'plan_snapshot_json':json.dumps(plan)}
    pre=validate_frozen_checkout(snapshot,
        [{'grant':'promo_mock','source':'Promotion','amount':'100.00'}],
        [{'grant':'corporate_mock','source':'Corporate','amount':'300.00'}])
    e=SimpleNamespace(name=enrollment,payment_order_id=order_id)
    h=SimpleNamespace(name='SMOKE:HYBRID:ROW')
    order={'id':order_id,'amount':39900,'currency':'INR','receipt':enrollment,
           'notes':{'club100_enrollment':enrollment},'status':'paid','amount_paid':39900,'amount_due':0}
    payment={'id':payment_id,'order_id':order_id,'amount':39900,'currency':'INR',
             'status':'captured','amount_refunded':0}
    config={'mode':'test','key_id':'rzp_test_mock_local','key_secret':secret}
    calls=[]
    def fake_get(url,auth,timeout):
        calls.append((url,auth[0],timeout))
        if url==bridge.API+'/orders/'+order_id:return MockReply(order)
        if url==bridge.API+'/payments/'+payment_id:return MockReply(payment)
        raise AssertionError('Unexpected URL '+url)
    browser_sig=hmac.new(secret.encode(),f'{order_id}|{payment_id}'.encode(),hashlib.sha256).hexdigest()
    body=json.dumps({'event':'payment.captured','payload':{'payment':{'entity':{
       'id':payment_id,'order_id':order_id,'status':'captured'}}}})
    hook_sig=hmac.new(webhook_secret.encode(),body.encode(),hashlib.sha256).hexdigest()
    checks=[]
    with (patch.object(bridge,'_guard',lambda:None),
          patch.object(bridge,'_snapshot',lambda _: (e,h,pre)),
          patch.object(bridge,'_get_razorpay_config',lambda:dict(config)),
          patch.object(bridge,'_get_razorpay_webhook_secret',lambda:webhook_secret),
          patch.object(bridge.requests,'get',side_effect=fake_get)):
        result=bridge.inspect_browser_payment(enrollment,payment_id,browser_sig)
        assert result['outcome']=='VERIFIED_EVIDENCE_ONLY' and result['posting_authorized'] is False
        checks.append('Browser HMAC verified against ERP-stored order ID')
        assert len(calls)==2 and calls[0][0].endswith('/orders/'+order_id) and calls[1][0].endswith('/payments/'+payment_id)
        checks.append('Both Razorpay GET calls mocked with server-side credentials')
        webhook=bridge.inspect_signed_webhook(enrollment,body,hook_sig)
        assert webhook['amount_paise']==39900 and not webhook['wallet_captured']
        checks.append('Raw webhook HMAC + provider identity double-check')
        before_bad=len(calls)
        try:bridge.inspect_browser_payment(enrollment,payment_id,'0'*64)
        except ValueError:pass
        else:raise AssertionError('Invalid signature accepted')
        assert len(calls)==before_bad
        checks.append('Invalid HMAC blocked before provider request')
        payment['amount']=79900
        try:bridge.inspect_browser_payment(enrollment,payment_id,browser_sig)
        except ValueError:pass
        else:raise AssertionError('Undiscounted overcharge accepted')
        checks.append('Overcharged payment rejected after independent fetch')
        payment['amount']=39900
        payment['status']='authorized'
        try:bridge.inspect_browser_payment(enrollment,payment_id,browser_sig)
        except ValueError:pass
        else:raise AssertionError('Uncaptured payment accepted')
        checks.append('Uncaptured payment rejected')
    after={dt:frappe.db.count(dt) for dt in doctypes}
    if before!=after:raise ValueError('Read-only D2C2A smoke changed database record counts')
    return {'site':frappe.local.site,'outcome':'PASS','passed':len(checks),'cases':checks,
       'provider_requests_mocked':True,'live_gateway_calls':0,'record_counts_unchanged':True,
       'wallet_captured':False,'invoice_or_gl_postings':0,
       'read_only':True,'production_ready':False,
       'note':'Browser/webhook HMAC and mock-provider GET only. Existing checkout/callbacks unchanged.'}
