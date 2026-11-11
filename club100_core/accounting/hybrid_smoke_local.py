"""Rollback-only REAL Frappe reservation lifecycle test on club100.local.

All synthetic grants, enrollments, holds and ledger entries are
rolled back. NO Razorpay, Invoice, Payment Entry, Journal Entry or GL posting.
"""
import os
from uuid import uuid4

import frappe
from frappe.utils import nowdate, now_datetime
from club100_core.accounting import hybrid_reservations as h
from club100_core.wallet.core import wallet_balance
from club100_core.accounting.promotion_rules import rupees


def _require():
    if (frappe.local.site != 'club100.local'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2B_SMOKE') != '1'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2B_LOCAL') != '1'):
        raise RuntimeError('Explicit local Test Mode and environment smoke guards required')
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles():
        raise RuntimeError('System Manager required')


def run():
    _require()
    members = frappe.get_all('Club100 Member', pluck='name', limit_page_length=80)
    customers = frappe.get_all('Customer', pluck='name', limit_page_length=30)
    programs = frappe.get_all('Club100 Program', pluck='name', limit_page_length=50)
    plan = frappe.db.get_value('Club100 Plan', {'plan_code':'self','status':'Active'}, 'name')
    if not members or not customers or not programs or not plan:
        return {'outcome':'SKIP', 'reason':'Need an existing Member, Customer, Program and active Self Plan'}
    chosen=None
    for member in members:
        for program in programs:
            exists=frappe.db.exists('Club100 Enrollment', {'member':member,'program':program,'plan':plan,
                 'status':['in',['Registered','Scheduled','Active','On Hold']]})
            if not exists:
                chosen=(member,program);break
        if chosen:break
    if not chosen:
        return {'outcome':'SKIP','reason':'No available test member/program combination'}
    member,program=chosen
    customer=next((x for x in customers if frappe.db.get_value('Club100 Corporate Employee',
             {'member':member,'corporate_customer':x},'status') in (None,'Active')),None)
    if not customer:
        return {'outcome':'SKIP','reason':'No employer/customer eligible for test membership'}
    watched = ('Club100 Enrollment', 'Club100 Hybrid Checkout',
        'Club100 Wallet', 'Club100 Wallet Grant','Club100 Wallet Ledger',
        'Club100 Wallet Reservation','Club100 Wallet Reservation Item',
        'Club100 Corporate Credit Pool','Club100 Corporate Employee')
    before={dt:frappe.db.count(dt) for dt in watched}
    token=uuid4().hex[:16].upper()
    old_flag=getattr(frappe.flags,'club100_stage5fd2b_smoke',False)
    old_enabled=frappe.conf.get('club100_stage5fd2b_local_enabled')
    old_gst=frappe.conf.get('club100_hybrid_gst_scheme_approved')
    frappe.db.savepoint('stage5fd2b_smoke')
    checks=[]
    try:
        # Transaction-local test switches only; site_config.json is untouched.
        frappe.flags.club100_stage5fd2b_smoke=True
        frappe.conf['club100_stage5fd2b_local_enabled']=1
        frappe.conf['club100_hybrid_gst_scheme_approved']=1
        # Exercise the current System Manager direct-issuance path. before_insert
        # generates a PROMOTION:ADMIN: key and linked PROMO:ADMIN: reference.
        promo=frappe.get_doc({'doctype':'Club100 Wallet Grant','member':member,
          'source_type':'Promotion','amount':'120.00',
          'currency':'INR','valid_from':nowdate(), 'validity_type':'Lifetime',
          'status':'Active','notes':'LOCAL D2B rollback smoke '+token}).insert(ignore_permissions=True)
        assert str(promo.source_event_key).startswith('PROMOTION:ADMIN:')
        pool=frappe.get_doc({'doctype':'Club100 Corporate Credit Pool',
            'corporate_customer':customer,'funding_event_key':'HYBTEST:CORP:'+token,
            'funding_reference':'LOCAL-SMOKE-DO-NOT-POST:'+token,
            'funding_verified':1,'amount':'600.00','currency':'INR',
            'status':'Funded'}).insert(ignore_permissions=True)
        if not frappe.db.exists('Club100 Corporate Employee',{'member':member,'corporate_customer':customer}):
            frappe.get_doc({'doctype':'Club100 Corporate Employee',
                'member':member,'corporate_customer':customer,'status':'Active'}).insert(ignore_permissions=True)
        grant=frappe.get_doc({'doctype':'Club100 Wallet Grant','member':member,
            'source_type':'Corporate','corporate_pool':pool.name,
            'source_event_key':'HYBTEST:F:'+token,'amount':'600.00',
            'currency':'INR','validity_type':'Lifetime',
            'valid_from':nowdate(),'status':'Active'}).insert(ignore_permissions=True)
        checks.append('Synthetic free Promotion and funded Corporate credits created with explicit, isolated eligibility')
        start=rupees(wallet_balance(member)['available'])

        def enrollment(suffix):
            doc=frappe.get_doc({'doctype':'Club100 Enrollment',
               'member':member,'program':program,'plan':plan,
               'pricing_model':'Online Plan','enrollment_type':'Individual',
               'enrollment_date':nowdate(),'start_date':nowdate(),
               'website_source':'PWA Commerce','status':'Registered',
               'payment_status':'Pending','membership_duration_months':1,
               'monthly_equivalent':'799.00','amount_payable':'799.00',
               'notes':'D2B LOCAL ROLLBACK SMOKE '+token+suffix})
            return doc.insert(ignore_permissions=True)

        a=enrollment('A')
        first=h.reserve_hybrid(a.name,'100.00','300.00','LOCAL-SMOKE-V1')
        assert first['status']=='Reserved' and first['gateway_due']=='399.00',first
        assert rupees(wallet_balance(member)['available'])==start-rupees('400.00')
        replay=h.reserve_hybrid(a.name,'100.00','300.00','LOCAL-SMOKE-V1')
        assert replay['record']==first['record']
        try:
            h.reserve_hybrid(a.name,'101.00','300.00','LOCAL-SMOKE-V1')
            raise AssertionError('Changed amount replay accepted')
        except ValueError:
            pass
        checks.append('Atomic dual hold and idempotent identical replay; mismatch denied')
        c=h.simulate_capture_local(a.name,'SMOKE:CAPTURE:'+token)
        assert c['status']=='Captured'
        assert h.simulate_capture_local(a.name,'SMOKE:CAPTURE:'+token)['status']=='Captured'
        assert rupees(wallet_balance(member)['available'])==start-rupees('400.00')
        checks.append('Two ledger spends captured atomically; repeated simulated capture does not duplicate')
        back=h.simulate_refund_local(a.name,'SMOKE:REFUND:'+token)
        assert back['status']=='Refunded'
        assert h.simulate_refund_local(a.name,'SMOKE:REFUND:'+token)['status']=='Refunded'
        assert rupees(wallet_balance(member)['available'])==start
        checks.append('Refund restores each ORIGINAL grant once, preserving expiry and value')
        a.status='Cancelled';a.save(ignore_permissions=True)
        b=enrollment('B')
        h.reserve_hybrid(b.name,'10.00','20.00','LOCAL-SMOKE-V1')
        assert h.release_unpaid(b.name)['status']=='Released'
        assert h.release_unpaid(b.name)['status']=='Released'
        assert rupees(wallet_balance(member)['available'])==start
        checks.append('Cancellation releases both held legs without spending')
        b.status='Cancelled';b.save(ignore_permissions=True)
        d=enrollment('C')
        # Inject a failure AFTER the promotion hold exists but BEFORE funded
        # hold creation. The whole outer savepoint MUST roll the promo hold back.
        original_reserve=h.reserve
        def fail_funded(*args,**kwargs):
            if kwargs.get('allowed_sources')==h.FUNDED:
                assert frappe.db.exists('Club100 Wallet Reservation',
                       {'reservation_key':'HYB:P:'+d.name}), 'Promo hold missing before injected funded failure'
                raise ValueError('SMOKE injected second-leg failure')
            return original_reserve(*args,**kwargs)
        h.reserve=fail_funded
        try:
            try:
                h.reserve_hybrid(d.name,'100.00','300.00','LOCAL-SMOKE-V1')
                raise AssertionError('Failed funded hold did not fail')
            except ValueError as exc:
                assert 'SMOKE injected' in str(exc),str(exc)
        finally:
            h.reserve=original_reserve
        assert not frappe.db.exists('Club100 Hybrid Checkout',{'enrollment':d.name})
        assert not frappe.db.exists('Club100 Wallet Reservation',{'reservation_key':'HYB:P:'+d.name})
        assert rupees(wallet_balance(member)['available'])==start
        checks.append('Failure of second reservation rolls back first reservation and all state')
        d.status='Cancelled';d.save(ignore_permissions=True)
        e=enrollment('D')
        try:
            h.reserve_hybrid(e.name,'50.00','0.00','UNAPPROVED-V1')
            raise AssertionError('Unapproved legacy promotions were used')
        except ValueError:
            pass
        assert not frappe.db.exists('Club100 Hybrid Checkout',{'enrollment':e.name})
        checks.append('No automatic GST-eligible status for pre-existing Promotion balances')
        # Smoke-only catch of the public/real capture guard is a source contract test.
    finally:
        frappe.db.rollback(save_point='stage5fd2b_smoke')
        frappe.flags.club100_stage5fd2b_smoke=old_flag
        for key,value in (('club100_stage5fd2b_local_enabled',old_enabled),
                          ('club100_hybrid_gst_scheme_approved',old_gst)):
            if value is None:frappe.conf.pop(key,None)
            else:frappe.conf[key]=value
    after={dt:frappe.db.count(dt) for dt in watched}
    if before != after:
        raise RuntimeError('Rollback verification FAILED: '+str({'before':before,'after':after}))
    return {'outcome':'PASS','site':frappe.local.site,'passed':len(checks),
      'cases':checks,'rollback_verified_by_record_counts':True,
      'gateway_calls':0,'invoice_or_gl_postings':0,'production_ready':False,
      'note':'All data rolled back. Simulated payment and refund proofs are NOT real gateway verification.'}
