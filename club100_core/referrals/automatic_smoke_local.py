"""Rollback-only automatic approval smoke, explicitly local disposable test site.

Never calls Razorpay and never inserts permanent grants or rewards. Uses an
existing paid disposable Self enrollment with no referral attribution/reward.
"""
import os
import uuid
from decimal import Decimal

import frappe
from frappe.utils import cint
from club100_core.referrals import settlement as s
from club100_core.referrals.settlement_logic import money, net_of_inclusive_tax


def run():
    if (frappe.local.site != 'club100.local'
            or os.environ.get('CLUB100_ALLOW_REFERRAL_AUTOMATIC_SMOKE') != '1'
            or not s._auto_enabled()):
        raise RuntimeError('Local test site + enabled pilot + explicit smoke environment flag required')
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        raise RuntimeError('System Manager required')
    from club100_core.referrals.auto_basis import _self_checkout
    candidates = frappe.db.sql('''
        SELECT e.name,e.member FROM `tabClub100 Enrollment` e
        WHERE e.payment_status='Paid' AND e.payment_gateway='Razorpay'
          AND COALESCE(e.razorpay_capture_verified,0)=1 AND e.status!='Cancelled'
          AND e.website_source='PWA Commerce' AND COALESCE(e.is_trial,0)=0
          AND (e.sales_invoice IS NULL OR e.sales_invoice='')
          AND NOT EXISTS (SELECT 1 FROM `tabClub100 Referral Attribution` a
                          WHERE a.referred_member=e.member)
          AND NOT EXISTS (SELECT 1 FROM `tabClub100 Referral Reward` r
                          WHERE r.enrollment=e.name)
          AND NOT EXISTS (SELECT 1 FROM `tabClub100 Wallet Refund Case` c
                          WHERE c.enrollment=e.name)
        ORDER BY e.creation DESC LIMIT 20''', as_dict=True)
    partner = frappe.db.get_value('Club100 Referral Partner',
        {'partner_type':'Member Advocate','status':'Active'},
        ['name','member','partner_code'], as_dict=True)
    target = next((e for e in candidates if partner and partner.member != e.member
                   and _self_checkout(frappe.get_doc('Club100 Enrollment',e.name))), None)
    if not target:
        return {'outcome':'SKIP','reason':'No un-attributed paid disposable PWA Self enrollment; create one for smoke or run rule tests'}
    enrollment = frappe.get_doc('Club100 Enrollment',target.name)
    basis = net_of_inclusive_tax(s._gateway_due(enrollment),5)
    observed = ('Club100 Referral Policy','Club100 Referral Attribution','Club100 Referral Reward',
                'Club100 Wallet','Club100 Wallet Grant','Club100 Wallet Ledger')
    before = {d:frappe.db.count(d) for d in observed}
    original_user = frappe.session.user
    frappe.db.savepoint('club100_stage5d_auto_smoke')
    try:
        frappe.set_user('Administrator')
        tag = uuid.uuid4().hex[:10].upper()
        policy = frappe.get_doc({'doctype':'Club100 Referral Policy',
            'policy_code':'LOCAL5D'+tag, 'reward_track':'Member Credit','enabled':0,
            'first_purchase_rate':10,'renewal_rate':2,'minimum_duration_months':1,
            'currency':'INR'}).insert(ignore_permissions=True)
        attr = frappe.get_doc({'doctype':'Club100 Referral Attribution',
            'referred_member':target.member,'partner':partner.name,
            'referral_code':partner.partner_code,'reward_track':'Member Credit',
            'policy':policy.name,'first_rate':10,'renewal_rate':2,'currency':'INR',
            'minimum_duration_months':1,'status':'Captured','source':'Admin',
            'captured_on':frappe.utils.now_datetime()}).insert(ignore_permissions=True)
        reward = frappe.get_doc({'doctype':'Club100 Referral Reward',
            'event_key':'RPAY:'+enrollment.payment_id,
            'attribution':attr.name,'partner':partner.name,'referred_member':target.member,
            'enrollment':enrollment.name,'payment_reference':enrollment.payment_id,
            'reward_track':'Member Credit','reward_event':'First Purchase',
            'basis_amount':money(enrollment.amount_payable),'rate':10,
            'reward_amount':(money(enrollment.amount_payable)*Decimal('.10')),
            'auto_tax_rate_percent':5,'currency':'INR','status':'On Hold',
            'basis_verification_status':'Unverified'}).insert(ignore_permissions=True)
        approved = s.auto_approve_one(reward.name,commit=False)
        if (approved['status']!='Approved'
                or approved['verified_basis']!=str(basis)
                or money(approved['verified_reward'])!=s.verified_amount(basis,10,s._gateway_due(enrollment))):
            raise AssertionError('Automatic GST-exclusive approval failed')
        if frappe.db.exists('Club100 Wallet Grant',{'referral_reward':reward.name}):
            raise AssertionError('Unmatured approved credit was posted prematurely')
        if s.auto_approve_one(reward.name,commit=False)['status']!='Approved':
            raise AssertionError('Automatic approval replay was not idempotent')
        return {'outcome':'PASS','cases':3,'verified_basis':str(basis),
                'verified_reward':approved['verified_reward'],
                'no_wallet_credit_before_maturity':True,
                'rollback_verified_after_return':True,
                'gateway_requests':0,
                'note':'Records are rolled back; future maturity covered by Stage 5C smoke.'}
    finally:
        frappe.db.rollback(save_point='club100_stage5d_auto_smoke')
        frappe.set_user(original_user)
        after={d:frappe.db.count(d) for d in observed}
        if before!=after:
            raise AssertionError('Automatic smoke rollback did not restore record counts')
