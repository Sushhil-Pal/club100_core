"""Rollback-only Frappe smoke for Stage 5C. NEVER run on production.

Creates a synthetic referral association against an existing TEST Razorpay-paid
membership, then rolls back. No payment or gateway operation is made. A GET
is mocked only to exercise grant posting, not to assert gateway verification.
"""
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import frappe
from frappe.utils import getdate
from club100_core.referrals import settlement as s
from club100_core.referrals.settlement_logic import money


def _expect(value, message):
    if not value:
        raise AssertionError(message)


def run():
    if (frappe.local.site != 'club100.local'
            or os.environ.get('CLUB100_ALLOW_REFERRAL_SETTLEMENT_TEST') != '1'
            or not s._enabled()):
        raise RuntimeError('Requires club100.local, explicit local feature flag and smoke-test environment flag')
    if frappe.session.user not in ('Administrator',) and 'System Manager' not in frappe.get_roles():
        raise RuntimeError('System Manager required')

    # Use a captured Razorpay Test Mode payment from an EXISTING disposable
    # member, but never modify its paid Enrollment. Attribution is synthetic
    # and all smoke data is rolled back. No reward is issued to existing users.
    candidates = frappe.db.sql('''
        SELECT e.name, e.member FROM `tabClub100 Enrollment` e
        WHERE e.payment_status='Paid' AND e.payment_gateway='Razorpay'
          AND COALESCE(e.razorpay_capture_verified,0)=1
          AND e.payment_id LIKE 'pay_%%'
          AND COALESCE(e.membership_duration_months,0)>=1
          AND e.status!='Cancelled'
          AND NOT EXISTS (SELECT 1 FROM `tabClub100 Referral Attribution` a
                          WHERE a.referred_member=e.member)
          AND NOT EXISTS (SELECT 1 FROM `tabClub100 Referral Reward` r
                          WHERE r.enrollment=e.name)
          AND NOT EXISTS (SELECT 1 FROM `tabClub100 Wallet Refund Case` c
                          WHERE c.enrollment=e.name)
        ORDER BY e.creation DESC LIMIT 30''', as_dict=True)
    partner = frappe.db.get_value('Club100 Referral Partner',
            {'partner_type':'Member Advocate','status':'Active'}, ['name','member','partner_code'],as_dict=True)
    target = next((e for e in candidates if partner and partner.member != e.member), None)
    if not target:
        return {'outcome':'SKIP','reason':'Need a disposable captured Razorpay-paid member with no attribution/reward and a different active member advocate'}
    enrollment = frappe.get_doc('Club100 Enrollment',target.name)
    proof_basis = min(s._gateway_due(enrollment),Decimal('100.00'))
    if proof_basis <= Decimal('0.10'):
        return {'outcome':'SKIP','reason':'No eligible gateway tender sufficient for test reward'}
    expected_reward = s.verified_amount(proof_basis,10,s._gateway_due(enrollment))

    watched = ('Club100 Referral Policy','Club100 Referral Attribution','Club100 Referral Reward',
               'Club100 Wallet','Club100 Wallet Grant','Club100 Wallet Ledger')
    before = {t:frappe.db.count(t) for t in watched}
    original_user = frappe.session.user
    orig_gateway = s._gateway_is_captured_without_refund
    orig_today = s.nowdate
    steps = []
    frappe.db.savepoint('club100_referral_settlement_smoke')
    try:
        frappe.set_user('Administrator')
        tag = uuid.uuid4().hex[:10].upper()
        policy = frappe.get_doc({'doctype':'Club100 Referral Policy',
            'policy_code':'LOCAL5C'+tag,'reward_track':'Member Credit','enabled':0,
            'first_purchase_rate':10,'renewal_rate':2,'minimum_duration_months':1,
            'currency':'INR'}).insert(ignore_permissions=True)
        attr = frappe.get_doc({'doctype':'Club100 Referral Attribution',
            'referred_member':target.member,'partner':partner.name,
            'referral_code':partner.partner_code,'reward_track':'Member Credit',
            'policy':policy.name,'first_rate':10,'renewal_rate':2,
            'currency':'INR','minimum_duration_months':1,'status':'Captured',
            'source':'Admin','captured_on':frappe.utils.now_datetime()}).insert(ignore_permissions=True)
        reward = frappe.get_doc({'doctype':'Club100 Referral Reward',
            'event_key':'RPAY:'+enrollment.payment_id,
            'attribution':attr.name,'partner':partner.name,'referred_member':target.member,
            'enrollment':enrollment.name,'payment_reference':enrollment.payment_id,
            'reward_track':'Member Credit','reward_event':'First Purchase',
            'basis_amount':money(enrollment.amount_payable), 'rate':10,
            'reward_amount':money(enrollment.amount_payable)*Decimal('0.10'),
            'currency':'INR','status':'On Hold',
            'basis_verification_status':'Unverified'}).insert(ignore_permissions=True)
        _expect(not frappe.db.exists('Club100 Wallet Grant',{'referral_reward':reward.name}),
                'Provisional reward unexpectedly created spendable credits')
        steps.append('Provisional payment-linked reward does not post wallet credits')
        approved = s.finance_verify_reward(reward.name,str(proof_basis),
                              'LOCAL:FINANCE:'+tag,commit=False)
        _expect(approved['status']=='Approved' and money(approved['verified_reward'])==expected_reward,
                'Finance verification or reward amount mismatch')
        steps.append('Finance verified eligible basis independently of gross provisional estimate')
        try:
            s.mature_one(reward.name,commit=False)
            raise AssertionError('Hold period did not block posting')
        except ValueError as exc:
            _expect('hold period' in str(exc),'Unexpected maturity failure: '+str(exc))
        steps.append('Unmatured approved reward cannot post early')

        # Move clock strictly inside this test to simulate maturity; actual
        # gateway GET is mocked. Real gateway check remains in production code.
        future = str(getdate(approved['matures_on']) + timedelta(days=1))
        s.nowdate = lambda: future
        s._gateway_is_captured_without_refund = lambda payment, amount: True
        posted = s.mature_one(reward.name,commit=False)
        _expect(posted['status']=='Available','Reward was not posted')
        grant = frappe.get_doc('Club100 Wallet Grant',posted['wallet_grant'])
        _expect(grant.member == partner.member and grant.source_type == 'Referral'
                and grant.referral_reward == reward.name and money(grant.amount)==expected_reward,
                'Wrong member or value credited')
        credits = frappe.db.get_all('Club100 Wallet Ledger',filters={'grant':grant.name,'entry_type':'Credit'},fields=['amount'])
        _expect(len(credits)==1 and money(credits[0].amount)==expected_reward,'Incorrect wallet ledger credit')
        steps.append('Matured approved reward creates exactly one referrer wallet grant + ledger credit')
        _expect(s.mature_one(reward.name,commit=False)['wallet_grant']==grant.name,
                'Second maturity created a new grant')
        _expect(frappe.db.count('Club100 Wallet Ledger',{'grant':grant.name,'entry_type':'Credit'})==1,
                'Maturity replay posted duplicate credit')
        steps.append('Repeated maturity is idempotent')
        s.refund_preflight(enrollment.name)
        reverse=s.reverse_for_enrollment(enrollment.name)
        _expect(reverse[0]['status']=='Reversed','Reward not reversed')
        ledger=frappe.get_all('Club100 Wallet Ledger',filters={'grant':grant.name},fields=['entry_type','amount'])
        _expect(len(ledger)==2 and sum((money(x.amount) for x in ledger),Decimal('0.00'))==0,
                'Reversal did not zero the grant')
        _expect(s.reverse_for_enrollment(enrollment.name)[0]['status']=='already_reversed',
                'Refund replay was not idempotent')
        steps.append('Refund preflight + full reversal restores zero net liability exactly once')
    finally:
        s._gateway_is_captured_without_refund = orig_gateway
        s.nowdate = orig_today
        frappe.db.rollback(save_point='club100_referral_settlement_smoke')
        frappe.set_user(original_user)
    after={t:frappe.db.count(t) for t in watched}
    _expect(before==after,'Smoke test rollback changed record counts')
    return {'site':frappe.local.site,'outcome':'PASS','passed':len(steps),
            'cases':steps,'rollback_verified_by_record_counts':True,
            'razorpay_requests':0,'note':'Synthetic attribution on existing disposable paid enrollment; all records rolled back. Gateway GET mocked only in smoke.'}
