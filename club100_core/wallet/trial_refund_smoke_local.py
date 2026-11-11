"""Local-only database smoke: refund wallet-paid trial conversion with exact rollback.

Creates fake trial and paid enrollment for an eligible unused member. No Razorpay
calls; does not commit. Never run against production.
"""
import os
import uuid
from decimal import Decimal

import frappe
from frappe.utils import add_days, nowdate
from club100_core.wallet.logic import money
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.checkout import enabled, attach_reservation, wallet_only_paid
from club100_core.wallet.recovery import refund_preview, prepare_full_refund, confirm_full_refund
from club100_core.api.commerce_checkout import _quote


def _expect(ok, explanation):
    if not ok:
        raise AssertionError(explanation)


def run():
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_TRIAL_REFUND_TEST') != '1':
        raise RuntimeError('Trial refund smoke is permitted only on club100.local with CLUB100_ALLOW_TRIAL_REFUND_TEST=1')
    if not enabled():
        return {'outcome':'SKIP','reason':'Local split checkout is disabled'}
    plan = frappe.db.get_value('Club100 Plan', {'plan_code':'self','status':'Active'}, 'name')
    if not plan:
        return {'outcome':'SKIP','reason':'No active Self plan'}
    selected = None
    for candidate in frappe.get_all('Club100 Member',filters={'status':'Active','onboarding_status':'Completed'},fields=['name'],limit_page_length=300):
        if frappe.db.exists('Club100 Wallet Grant', {'member':candidate.name}):
            continue
        member = frappe.get_doc('Club100 Member',candidate.name)
        for program in frappe.get_all('Club100 Program',pluck='name',limit_page_length=150):
            if frappe.db.exists('Club100 Enrollment', {'member':member.name,'program':program,'is_trial':1}):
                continue
            try:
                quote = _quote(member,program,1)
            except Exception:
                continue
            if quote['trial'] is None:
                selected=(member, quote)
                break
        if selected:
            break
    if not selected:
        return {'outcome':'SKIP','reason':'Create a disposable onboarded member without wallet credits/trials and with an eligible Self program'}

    member, quote = selected
    gross = money(quote['price']['amount_payable'])
    _expect(gross > Decimal('0.00'), 'Invalid program price')
    watch=('Club100 Enrollment','Club100 Wallet','Club100 Wallet Grant','Club100 Wallet Ledger',
           'Club100 Wallet Reservation','Club100 Wallet Refund Case')
    before={t:frappe.db.count(t) for t in watch}
    prior_user=frappe.session.user
    steps=[]
    frappe.db.savepoint('club100_trial_refund_local')
    try:
        frappe.set_user('Administrator')
        from club100_core.wallet.recovery import _require_local_admin
        _require_local_admin()
        token=uuid.uuid4().hex[:16]
        trial_start=add_days(nowdate(),-1)
        trial_end=add_days(nowdate(),5)
        paid_start=add_days(trial_end,1)
        trial=frappe.get_doc({
            'doctype':'Club100 Enrollment','member':member.name,
            'program':quote['program'],'plan':quote['plan'],
            'pricing_model':'Online Plan','enrollment_type':'Individual',
            'enrollment_date':trial_start,'start_date':trial_start,'end_date':trial_end,
            'status':'Registered','payment_status':'Not Required',
            'membership_duration_months':1,'is_trial':1,'trial_status':'Active',
            'website_source':'Round4B Local Trial Smoke',
        }).insert(ignore_permissions=True)
        # Simulate an active trial without invoking workout-journey initialization:
        # this test is limited to financial conversion bookkeeping.
        frappe.db.set_value('Club100 Enrollment', trial.name, 'status','Active',update_modified=False)
        steps.append('Temporary trial established with fixed access dates')
        grant=frappe.get_doc({
            'doctype':'Club100 Wallet Grant','member':member.name,'source_type':'Promotion',
            'source_event_key':'TRIALREF:SMOKE:'+token,
            'funding_reference':'TRIALREF:SMOKE:'+token,
            'amount':str(gross),'currency':'INR','valid_from':nowdate(),
            'validity_type':'Lifetime','status':'Active',
        }).insert(ignore_permissions=True)
        _expect(money(wallet_balance(member.name)['available'])==gross,'Wallet credit missing')
        paid=frappe.get_doc({
            'doctype':'Club100 Enrollment','member':member.name,
            'full_name':member.full_name or member.name,
            'mobile':member.mobile,'email':member.email,
            'program':quote['program'],'plan':quote['plan'],
            'pricing_model':'Online Plan','enrollment_type':'Individual',
            'enrollment_date':nowdate(),'status':'Registered',
            'payment_status':'Pending','start_date':paid_start,
            'membership_duration_months':1,
            'monthly_equivalent':quote['price']['monthly_equivalent'],
            'amount_payable':str(gross),'website_source':'PWA Commerce',
            'origin_trial_enrollment':trial.name,
        }).insert(ignore_permissions=True)
        attach_reservation(paid,str(gross))
        result=wallet_only_paid(paid,commit=False)
        _expect(result['success'],'Paid checkout did not succeed')
        paid_db=frappe.get_doc('Club100 Enrollment',paid.name)
        trial_db=frappe.get_doc('Club100 Enrollment',trial.name)
        _expect(paid_db.payment_status=='Paid' and paid_db.status=='Scheduled','Paid membership not Scheduled')
        _expect(trial_db.trial_status=='Converted' and trial_db.converted_enrollment==paid.name,
                'Paid conversion not recorded on original trial')
        _expect(money(wallet_balance(member.name)['available'])==Decimal('0.00'),'Wallet spend not captured')
        steps.append('Paid wallet checkout converted the linked trial without changing trial dates')
        preview=refund_preview(paid.name)
        _expect(preview['eligible'] and preview['case_kind']=='Paid' and money(preview['wallet_to_restore'])==gross,
                'Refund eligibility failed for trial-converted enrollment')
        case=prepare_full_refund(paid.name,commit=False)
        _expect(case['status']=='Prepared','Refund case not prepared')
        steps.append('Trial-converted refund case prepared with no gateway activity')
        done=confirm_full_refund(paid.name,commit=False)
        _expect(done['status']=='Completed','Refund completion failed')
        paid_db=frappe.get_doc('Club100 Enrollment',paid.name)
        trial_db=frappe.get_doc('Club100 Enrollment',trial.name)
        _expect(paid_db.status=='Cancelled' and paid_db.payment_status=='Refunded' and
                paid_db.wallet_settlement_status=='Refunded','Paid enrollment not refunded/cancelled')
        _expect(paid_db.origin_trial_enrollment==trial.name,'Paid-to-trial audit reference was removed')
        _expect(not trial_db.converted_enrollment and trial_db.trial_status=='Active',
                'Original trial conversion link not safely reversed')
        _expect(str(trial_db.start_date)==str(trial_start) and str(trial_db.end_date)==str(trial_end),
                'Original trial dates changed')
        _expect(trial_db.status=='Active','Original trial access status changed')
        _expect(money(wallet_balance(member.name)['available'])==gross,'Wallet funds not fully restored')
        _expect(frappe.db.count('Club100 Wallet Ledger',{'reference':paid.name,'entry_type':'Refund'})==1,
                'Expected a single wallet refund credit')
        steps.append('Refund cancelled paid enrollment, restored wallet once, unlinked trial without extending access')
        duplicate=confirm_full_refund(paid.name,commit=False)
        _expect(duplicate['status']=='Completed','Repeated confirmation failed')
        _expect(frappe.db.count('Club100 Wallet Ledger',{'reference':paid.name,'entry_type':'Refund'})==1,
                'Duplicate refund credit detected')
        steps.append('Repeated refund did not issue duplicate credits or change trial status')
    finally:
        frappe.db.rollback(save_point='club100_trial_refund_local')
        frappe.set_user(prior_user)
    after={t:frappe.db.count(t) for t in watch}
    _expect(before==after,'Smoke test failed to roll back all documents: '+repr((before,after)))
    return {'site':frappe.local.site,'outcome':'PASS','count':len(steps),'cases':steps,
            'rollback_verified_by_record_counts':True,'razorpay_requests':0,
            'note':'No real wallets, enrollments or payments changed. Gateway split-refund remains to be tested separately.'}
