"""LOCAL ONLY, rollback-only: wallet-funded future enrollment and full refund.

No Razorpay requests. A gateway split-refund is covered only by mocked
orchestration tests; it requires separate sandbox proof before live use.
"""
import os
import uuid
from decimal import Decimal

import frappe
from frappe.utils import add_days, nowdate
from club100_core.wallet.logic import money
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.checkout import enabled, attach_reservation, wallet_only_paid
from club100_core.wallet.recovery import prepare_full_refund, confirm_full_refund, refund_preview
from club100_core.api.commerce_checkout import _quote


def _expect(cond, msg):
    if not cond: raise AssertionError(msg)


def run():
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_ROUND4B_TEST') != '1':
        raise RuntimeError('Requires club100.local and CLUB100_ALLOW_ROUND4B_TEST=1')
    meta = frappe.get_meta('Club100 Wallet Refund Case')
    for field in ('enrollment','case_kind','status','gateway_refund_id','wallet_to_restore'):
        _expect(meta.has_field(field),'Missing audit field: '+field)
    if not enabled():
        return {'outcome':'SKIP','reason':'Local split checkout feature flag disabled'}
    plan = frappe.db.get_value('Club100 Plan', {'plan_code':'self','status':'Active'},'name')
    if not plan:
        return {'outcome':'SKIP','reason':'No active Self plan'}
    selected = None
    members = frappe.get_all('Club100 Member',filters={'status':'Active','onboarding_status':'Completed'},
                             fields=['name'],limit_page_length=250)
    programs = frappe.get_all('Club100 Program',pluck='name',limit_page_length=150)
    for m in members:
        if frappe.db.exists('Club100 Wallet Grant', {'member':m.name}): continue
        member = frappe.get_doc('Club100 Member',m.name)
        for program in programs:
            try:
                quote=_quote(member,program,1)
                selected=(member,quote)
                break
            except Exception:
                continue
        if selected:break
    if not selected:
        return {'outcome':'SKIP','reason':'No eligible onboarded Self member without existing grants and active curriculum; create a disposable test member'}
    member, quote=selected
    gross=money(quote['price']['amount_payable'])
    _expect(gross>Decimal('0.00'),'Invalid Self price')
    watch=('Club100 Enrollment','Club100 Wallet','Club100 Wallet Grant',
           'Club100 Wallet Ledger','Club100 Wallet Reservation',
           'Club100 Wallet Refund Case')
    before={t:frappe.db.count(t) for t in watch}
    old_user=frappe.session.user
    steps=[]
    frappe.db.savepoint('club100_round4b_refund_smoke')
    try:
        frappe.set_user('Administrator')
        from club100_core.wallet.recovery import _require_local_admin
        _require_local_admin()
        marker=uuid.uuid4().hex[:14]
        # Future effective membership ensures no real exercise/sessions are revoked.
        future_start=add_days(nowdate(),7)
        grant=frappe.get_doc({'doctype':'Club100 Wallet Grant','member':member.name,
                'source_type':'Promotion','source_event_key':'ROUND4B:SMOKE:'+marker,
                'funding_reference':'ROUND4B:SMOKE:'+marker,
                'amount':str(gross),'currency':'INR','valid_from':nowdate(),
                'validity_type':'Lifetime','status':'Active'}).insert(ignore_permissions=True)
        steps.append('Temporary lifetime grant and ledger credit')
        old_balance=money(wallet_balance(member.name)['available'])
        _expect(old_balance==gross,'Expected exactly the temporary grant')
        order=frappe.get_doc({'doctype':'Club100 Enrollment','member':member.name,
              'full_name':member.full_name or member.name,
              'mobile':member.mobile,'email':member.email,
              'program':quote['program'],'plan':quote['plan'],'pricing_model':'Online Plan',
              'enrollment_type':'Individual','enrollment_date':nowdate(),
              'status':'Registered','payment_status':'Pending','start_date':future_start,
              'membership_duration_months':1,
              'monthly_equivalent':quote['price']['monthly_equivalent'],
              'amount_payable':quote['price']['amount_payable'],
              'website_source':'PWA Commerce'}).insert(ignore_permissions=True)
        attach_reservation(order,str(gross))
        paid=wallet_only_paid(order,commit=False)
        _expect(paid['success'] and order.status=='Scheduled','Future wallet-paid enrollment not Scheduled')
        _expect(money(wallet_balance(member.name)['available'])==Decimal('0.00'),'Wallet spend not recorded')
        steps.append('Full wallet checkout captured and created future scheduled Paid enrollment')
        preview=refund_preview(order.name)
        _expect(preview['case_kind']=='Paid' and money(preview['wallet_to_restore'])==gross,'Refund preview incorrect')
        case=prepare_full_refund(order.name,commit=False)
        _expect(case['status']=='Prepared' and money(case['wallet_to_restore'])==gross,'Refund case not prepared')
        steps.append('Full refund case prepared without gateway requests or wallet mutations')
        done=confirm_full_refund(order.name,commit=False)
        _expect(done['status']=='Completed','Wallet refund did not complete')
        # confirm_full_refund() reloads and saves a separate Enrollment object.
        # Do not assert on the original in-memory order: inspect persisted state.
        stored_order = frappe.get_doc('Club100 Enrollment', order.name)
        _expect(stored_order.payment_status == 'Refunded' and stored_order.status == 'Cancelled',
                'Paid entitlement not cancelled in DB: payment_status={!r}, status={!r}'.format(
                    stored_order.payment_status, stored_order.status))
        _expect(stored_order.wallet_settlement_status == 'Refunded',
                'Wallet refund status not updated in DB: {!r}'.format(
                    stored_order.wallet_settlement_status))
        _expect(money(wallet_balance(member.name)['available'])==gross,'Full grant credits not restored')
        _expect(frappe.db.count('Club100 Wallet Ledger',{'reference':order.name,'entry_type':'Refund'})==1,
                'Expected one full refund ledger entry')
        steps.append('Refund restored original grant and marked enrollment Refunded + Cancelled')
        repeat=confirm_full_refund(order.name,commit=False)
        _expect(repeat['status']=='Completed','Repeated confirmation failed')
        _expect(frappe.db.count('Club100 Wallet Ledger',{'reference':order.name,'entry_type':'Refund'})==1,
                'Refund replay created second ledger entry')
        steps.append('Second confirmation idempotent; no duplicate refund')
    finally:
        frappe.db.rollback(save_point='club100_round4b_refund_smoke')
        frappe.set_user(old_user)
    after={t:frappe.db.count(t) for t in watch}
    _expect(before==after,'Smoke rollback mismatch '+repr({'before':before,'after':after}))
    return {'site':frappe.local.site,'outcome':'PASS','passed':len(steps),'cases':steps,
            'rollback_verified_by_record_counts':True,
            'razorpay_requests':0,'real_wallets_or_enrollments_changed':0,
            'note':'Razorpay refund verification uses separate mocked tests; this smoke tests wallet-only full refunds.'}
