"""D2C2C rollback-only Frappe UAT with synthetic wallet holds and mocked GET.

Never calls Razorpay, never commits, never captures wallet or posts an invoice.
"""
import hashlib
import hmac
import json
import os
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

import frappe
from frappe.utils import nowdate, now_datetime
from club100_core.accounting import hybrid_reservations as h
from club100_core.accounting import hybrid_payment_inbox as inbox
from club100_core.accounting.hybrid_integration_rules import validate_frozen_checkout
from club100_core.accounting.promotion_rules import rupees


def _require():
    if (frappe.local.site!='club100.local'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower()!='test'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2C_SMOKE')!='1'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2C_LOCAL')!='1'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2B_LOCAL')!='1'):
        raise ValueError('D2C2C rollback smoke requires three explicit local Test Mode env flags')
    inbox._admin()


def run():
    _require()
    members=frappe.get_all('Club100 Member',pluck='name',limit_page_length=80)
    customers=frappe.get_all('Customer',pluck='name',limit_page_length=30)
    programs=frappe.get_all('Club100 Program',pluck='name',limit_page_length=50)
    plan=frappe.db.get_value('Club100 Plan',{'plan_code':'self','status':'Active'},'name')
    if not members or not customers or not programs or not plan:
        return {'outcome':'SKIP','reason':'Need Member, Customer, Program and active Self Plan'}
    selected=None
    for member in members:
        for program in programs:
            if not frappe.db.exists('Club100 Enrollment',{'member':member,'program':program,'plan':plan,
                    'status':['in',['Registered','Scheduled','Active','On Hold']]}):
                selected=(member,program);break
        if selected:break
    if not selected:
        return {'outcome':'SKIP','reason':'No member/program pair without active enrollment'}
    member,program=selected
    customer=next((x for x in customers if frappe.db.get_value('Club100 Corporate Employee',
        {'member':member,'corporate_customer':x},'status') in (None,'Active')),None)
    if not customer:return {'outcome':'SKIP','reason':'No eligible corporate test customer'}

    tracked=('Club100 Enrollment','Club100 Hybrid Checkout','Club100 Hybrid Payment Evidence',
       'Club100 Wallet Grant','Club100 Wallet Ledger','Club100 Wallet Reservation',
       'Club100 Wallet Reservation Item',
       'Club100 Corporate Credit Pool','Club100 Corporate Employee',
       'Sales Invoice','Payment Entry','Journal Entry')
    before={dt:frappe.db.count(dt) for dt in tracked}
    point='d2c2c_smoke'
    frappe.db.savepoint(point)
    flags={key:getattr(frappe.flags,key,False) for key in (
        'club100_stage5fd2b_smoke',
        'club100_stage5fd2c2c_smoke')}
    configs={key:frappe.conf.get(key) for key in (
        'club100_stage5fd2b_local_enabled','club100_hybrid_gst_scheme_approved',
        'club100_stage5fd2c2c_local_enabled')}
    checks=[]
    token=uuid4().hex[:12]
    try:
        frappe.flags.club100_stage5fd2b_smoke=True
        frappe.flags.club100_stage5fd2c2c_smoke=True
        frappe.conf['club100_stage5fd2b_local_enabled']=1
        frappe.conf['club100_hybrid_gst_scheme_approved']=1
        frappe.conf['club100_stage5fd2c2c_local_enabled']=1
        # Exercise direct administrative Promotion issuance; no Approval record.
        promo=frappe.get_doc({'doctype':'Club100 Wallet Grant','member':member,
          'source_type':'Promotion','amount':'120.00',
          'currency':'INR','valid_from':nowdate(),'validity_type':'Lifetime',
          'status':'Active','notes':'LOCAL D2C2C rollback smoke '+token}).insert(ignore_permissions=True)
        assert str(promo.source_event_key).startswith('PROMOTION:ADMIN:')
        pool=frappe.get_doc({'doctype':'Club100 Corporate Credit Pool',
            'corporate_customer':customer,'funding_event_key':'D2C2C:CORP:'+token,
            'funding_reference':'LOCAL-ROLLBACK-ONLY:'+token,
            'funding_verified':1,'amount':'600.00','currency':'INR',
            'status':'Funded'}).insert(ignore_permissions=True)
        if not frappe.db.exists('Club100 Corporate Employee',{'member':member,'corporate_customer':customer}):
            frappe.get_doc({'doctype':'Club100 Corporate Employee','member':member,
              'corporate_customer':customer,'status':'Active'}).insert(ignore_permissions=True)
        frappe.get_doc({'doctype':'Club100 Wallet Grant','member':member,
            'source_type':'Corporate','corporate_pool':pool.name,
            'source_event_key':'D2C2C:F:'+token,'amount':'600.00',
            'currency':'INR','validity_type':'Lifetime',
            'valid_from':nowdate(),'status':'Active'}).insert(ignore_permissions=True)

        secret='LOCAL_D2C2C_FAKE_SECRET'
        webhook_secret='LOCAL_D2C2C_FAKE_WEBHOOK_SECRET'
        config={'mode':'test','key_id':'rzp_test_mock_local','key_secret':secret}
        fetch_count=[]
        provider={}
        def fake_fetch(cfg,order_id,payment_id):
            fetch_count.append((order_id,payment_id))
            assert cfg==config
            return provider[order_id],provider[payment_id]
        def setup(suffix,discount,funded):
            enrollment=frappe.get_doc({'doctype':'Club100 Enrollment',
               'member':member,'program':program,'plan':plan,
               'pricing_model':'Online Plan','enrollment_type':'Individual',
               'enrollment_date':nowdate(),'start_date':nowdate(),
               'website_source':'PWA Commerce','status':'Registered',
               'payment_status':'Pending','membership_duration_months':1,
               'monthly_equivalent':'799.00','amount_payable':'799.00',
               'notes':'D2C2C LOCAL ROLLBACK SMOKE '+token+suffix}).insert(ignore_permissions=True)
            details=h.reserve_hybrid(enrollment.name,str(discount),str(funded),'LOCAL-D2C2C-V1')
            hybrid=frappe.get_doc('Club100 Hybrid Checkout',details['record'])
            oid='order_D2C2C'+token+suffix
            pid='pay_D2C2C'+token+suffix
            frappe.db.set_value('Club100 Enrollment',enrollment.name,'payment_order_id',oid,update_modified=False)
            # Derive the expected fingerprint exactly as the D2C1 bridge.
            pair=h._pair(hybrid,('Reserved',))
            fields=('enrollment','member','status','policy_version','original_price',
                 'promotion_discount','funded_wallet_tender','gateway_due','gst_percent',
                 'promotion_reservation_key','funded_reservation_key','plan_snapshot_json')
            pre=validate_frozen_checkout({key:hybrid.get(key) for key in fields},
                       pair['promotion'],pair['funded'])
            amount=pre['expected_gateway_paise']
            provider[oid]={'id':oid,'amount':amount,'currency':'INR','receipt':enrollment.name,
              'notes':{'club100_enrollment':enrollment.name,'club100_hybrid':hybrid.name,
                       'club100_snapshot':pre['snapshot_sha256']},
              'status':'paid','amount_paid':amount,'amount_due':0}
            provider[pid]={'id':pid,'order_id':oid,'amount':amount,'currency':'INR',
               'status':'captured','amount_refunded':0}
            sig=hmac.new(secret.encode(),f'{oid}|{pid}'.encode(),hashlib.sha256).hexdigest()
            body=json.dumps({'event':'payment.captured','payload':{'payment':{'entity':{
               'id':pid,'order_id':oid,'status':'captured'}}}})
            hook=hmac.new(webhook_secret.encode(),body.encode(),hashlib.sha256).hexdigest()
            return enrollment,hybrid,oid,pid,sig,body,hook

        with patch.object(inbox,'_get_razorpay_config',lambda:dict(config)),\
             patch.object(inbox,'_get_razorpay_webhook_secret',lambda:webhook_secret),\
             patch.object(inbox,'_server_fetch',side_effect=fake_fetch):
            e,hyb,oid,pid,sig,body,hook=setup('A','100','300')
            one=inbox.ingest_browser(e.name,pid,sig)
            assert one['outcome']=='EVIDENCE_STORED' and one['state']=='Verified Pending Invoice'
            assert not one['wallet_captured'] and not one['invoice_or_gl_posted']
            checks.append('Captured mock provider + browser HMAC creates one NON-POSTING evidence record')
            two=inbox.ingest_browser(e.name,pid,sig)
            assert two['outcome']=='EVIDENCE_REPLAY' and two['record']==one['record']
            three=inbox.ingest_signed_webhook(e.name,body,hook)
            assert three['outcome']=='EVIDENCE_REPLAY' and three['record']==one['record']
            assert frappe.db.count('Club100 Hybrid Payment Evidence',{'enrollment':e.name})==1
            checks.append('Browser replay and signed-webhook replay reuse one evidence record')
            calls=len(fetch_count)
            try:inbox.ingest_browser(e.name,pid,'0'*64)
            except ValueError:pass
            else:raise AssertionError('Invalid signature accepted')
            assert len(fetch_count)==calls
            checks.append('Invalid browser HMAC rejected before any provider GET')
            provider[pid]['amount']=79900
            try:inbox.ingest_browser(e.name,pid,sig)
            except ValueError:pass
            else:raise AssertionError('Overcharge accepted')
            provider[pid]['amount']=39900
            checks.append('Overcharge rejected even for an already-known payment ID')
            # A *new* second enrollment with expired D2B hold must be reviewed.
            # Clear synthetic active-enrollment uniqueness before creating B.
            frappe.db.set_value('Club100 Enrollment',e.name,'status','Cancelled',update_modified=False)
            e2,hyb2,oid2,pid2,sig2,body2,hook2=setup('B','10','20')
            frappe.db.set_value('Club100 Hybrid Checkout',hyb2.name,
              'expires_at',now_datetime()-timedelta(minutes=1),update_modified=False)
            review=inbox.ingest_signed_webhook(e2.name,body2,hook2)
            assert review['state']=='Manual Review' and 'expired' in review['review_reason'].lower()
            checks.append('Captured payment with expired hold is stored for MANUAL REVIEW')
            # Neither the gateway event nor storage may mutate D2B wallet holds.
            rows=frappe.get_all('Club100 Wallet Reservation',
                    filters={'reference':['in',[e.name,e2.name]]},fields=['status'])
            assert all(row.status=='Reserved' for row in rows)
            assert frappe.db.get_value('Club100 Enrollment',e.name,'payment_status')=='Pending'
            assert frappe.db.get_value('Club100 Enrollment',e2.name,'payment_status')=='Pending'
            checks.append('No wallet reservation captured and no membership finalized')
    finally:
        frappe.db.rollback(save_point=point)
        for key,value in flags.items():setattr(frappe.flags,key,value)
        for key,value in configs.items():
            if value is None:frappe.conf.pop(key,None)
            else:frappe.conf[key]=value
    after={dt:frappe.db.count(dt) for dt in tracked}
    if before!=after:
        raise RuntimeError('D2C2C rollback failed: '+str({'before':before,'after':after}))
    return {'site':frappe.local.site,'outcome':'PASS','passed':len(checks),
      'cases':checks,'mock_provider_get':True,'real_gateway_calls':0,
      'rollback_verified_by_record_counts':True,'wallet_capture_count':0,
      'invoice_or_gl_postings':0,'production_ready':False,
      'note':'All synthetic financial records rolled back; public callbacks not wired.'}
