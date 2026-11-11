"""Rollback-only referral maturity UAT for local Club100, NOT production.

bench --site club100.local execute club100_core.referrals.maturity_uat_local.run \
  --kwargs '{"reward_name":"RPAY:pay_TxJNlIfJcAtJOm"}'

Tests REAL pre-existing Approved reward against actual mature_one() logic, but NEVER
persists changes. The Razorpay GET is deliberately MOCKED for the simulated
future date; real external gateway/refund status is NOT verified by this test.

No whitelisted endpoint, no scheduled job. No Frappe/ERPNext source changes.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import getdate, nowdate

from club100_core.referrals import settlement as s
from club100_core.referrals.settlement_logic import money

EXPECTED_SITE = 'club100.local'
EXPECTED_REWARD = 'RPAY:pay_TxJNlIfJcAtJOm'
EXPECTED_REFERRED = 'C100-MEM-00242'
EXPECTED_REFERRER = 'C100-MEM-00240'
EXPECTED_CREDIT = Decimal('76.10')
EXPECTED_BASIS = Decimal('760.95')
SAVEPOINT = 'club100_referral_maturity_uat_local'


def _require(condition, description):
    if not condition:
        raise AssertionError(description)


def _snapshot(reward_name, source_key):
    """Read exact reward/grant state (not just DB-wide record counts)."""
    reward = frappe.db.get_value(
        'Club100 Referral Reward', reward_name,
        ['status', 'settlement_reference', 'settled_on',
         'verified_reward_amount', 'verified_basis_amount'], as_dict=True,
    )
    grants = frappe.get_all('Club100 Wallet Grant',
        filters={'source_event_key': source_key}, fields=['name', 'member', 'amount'],
        limit_page_length=10)
    return {
        'reward': dict(reward) if reward else None,
        'grants': [dict(g) for g in grants],
    }


def run(reward_name=EXPECTED_REWARD):
    # Explicit safeguards. Do not relax for production or real settlement.
    _require(frappe.local.site == EXPECTED_SITE, 'LOCAL UAT ONLY: wrong site')
    _require(str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test',
             'LOCAL UAT ONLY: Razorpay Test Mode required')
    _require(s._enabled(), 'Referral settlement local pilot is disabled')
    _require(frappe.session.user == 'Administrator'
             or 'System Manager' in frappe.get_roles(frappe.session.user),
             'System Manager or Administrator required')
    _require(reward_name == EXPECTED_REWARD, 'Unexpected reward; script is pinned to this UAT case')

    r = frappe.get_doc('Club100 Referral Reward', reward_name)
    _require(r.status == 'Approved' and r.basis_verification_status == 'Verified',
             'Reward must be verified and Approved')
    _require(r.reward_track == 'Member Credit' and r.reward_event == 'First Purchase',
             'Unexpected reward track or event')
    _require(r.referred_member == EXPECTED_REFERRED, 'Unexpected referred member')
    _require(money(r.verified_basis_amount) == EXPECTED_BASIS,
             'Verified net basis differs from UAT evidence')
    _require(money(r.verified_reward_amount) == EXPECTED_CREDIT,
             'Verified credit differs from expected INR 76.10')
    _require(money(r.reward_amount) == Decimal('79.90'),
             'Provisional amount changed; review before exercising the scenario')
    _require(str(r.eligible_on)[:10] == '2026-12-07', 'Unexpected maturity date')
    _require(getdate(nowdate()) < getdate(r.eligible_on),
             'Reward has already matured; do not simulate future for an eligible reward')
    _require(not r.settlement_reference and not r.settled_on,
             'Reward already has a settlement reference')
    _require(frappe.db.get_value('Club100 Referral Partner', r.partner, 'member') == EXPECTED_REFERRER,
             'Unexpected referrer')
    _require(r.payment_reference == 'pay_TxJNlIfJcAtJOm', 'Unexpected payment reference')
    source_key = s.REFERRAL_KEY_PREFIX + r.name
    before = _snapshot(reward_name, source_key)
    _require(not before['grants'], 'This reward already has a grant; must not recreate it')

    # Savepoint comes BEFORE any call which might lock or write. The test
    # intentionally exercises actual wallet grant + ledger hooks, not a mock.
    frappe.db.savepoint(SAVEPOINT)
    grant_name = None
    events = []
    gateway_checks = []
    outcome = None
    try:
        # Block an EARLY attempt with real current date and no network request.
        try:
            s.mature_one(reward_name, commit=False)
        except ValueError as exc:
            _require('hold period' in str(exc).lower(),
                     'Unexpected pre-maturity error: ' + str(exc))
        else:
            raise AssertionError('Early maturity was improperly accepted')
        _require(_snapshot(reward_name, source_key) == before,
                 'Early maturity changed the reward or grant')
        events.append('Early settlement correctly refused without wallet writes')

        # The only time spoof is the settlement module nowdate() function;
        # global site time and original record eligible_on are unchanged.
        synthetic_today = str(getdate(r.eligible_on) + timedelta(days=1))

        def gateway_mock(payment_id, expected_gateway_amount):
            _require(payment_id == r.payment_reference, 'Wrong payment passed to gateway check')
            _require(money(expected_gateway_amount) == Decimal('799.00'),
                     'Unexpected gateway tender: ' + str(expected_gateway_amount))
            gateway_checks.append({'payment': payment_id,
                                   'expected_tender': str(money(expected_gateway_amount))})
            return True

        def prohibit_commit(*args, **kwargs):
            raise AssertionError('Unexpected DB COMMIT in rollback-only UAT')

        # Keep all effects inside an uncommitted savepoint. Network GET stub
        # is intentional; therefore this does NOT verify gateway/refund state.
        with patch.object(s, 'nowdate', return_value=synthetic_today), \
             patch.object(s, '_gateway_is_captured_without_refund', side_effect=gateway_mock), \
             patch.object(frappe.db, 'commit', side_effect=prohibit_commit):
            posted = s.mature_one(reward_name, commit=False)
            _require(posted['status'] == 'Available', 'Matured reward not marked Available')
            grant_name = posted.get('wallet_grant')
            _require(grant_name, 'Matured reward has no linked grant')
            grant = frappe.get_doc('Club100 Wallet Grant', grant_name)
            _require(grant.member == EXPECTED_REFERRER, 'Grant issued to wrong member')
            _require(grant.source_type == 'Referral', 'Grant not a Referral grant')
            _require(grant.referral_reward == reward_name, 'Grant reward linkage mismatch')
            _require(grant.source_event_key == source_key, 'Grant source identity mismatch')
            _require(grant.status == 'Active', 'Grant not Active')
            _require(money(grant.amount) == EXPECTED_CREDIT,
                     'Wrong credited amount; provisional 79.90 must NEVER be posted')
            credits = frappe.get_all('Club100 Wallet Ledger',
                filters={'grant': grant_name, 'entry_type': 'Credit'},
                fields=['name', 'amount'], limit_page_length=10)
            _require(len(credits) == 1 and money(credits[0].amount) == EXPECTED_CREDIT,
                     'Expected exactly one ledger Credit for 76.10')
            events.append('Synthetic mature date issues one 76.10 referrer grant and ledger Credit')

            replay = s.mature_one(reward_name, commit=False)
            _require(replay['wallet_grant'] == grant_name, 'Replay created different grant')
            _require(frappe.db.count('Club100 Wallet Grant',
                                    {'source_event_key': source_key}) == 1,
                     'Duplicate grant source event')
            _require(frappe.db.count('Club100 Wallet Ledger',
                                    {'grant': grant_name, 'entry_type': 'Credit'}) == 1,
                     'Duplicate ledger Credit')
            events.append('Idempotent replay adds no duplicate grant or Credit')
            _require(len(gateway_checks) == 1,
                     'Gateway check called unexpected number of times')
        outcome = {
            'result': 'PASS', 'site': frappe.local.site, 'reward': reward_name,
            'referrer': EXPECTED_REFERRER, 'referred_member': EXPECTED_REFERRED,
            'verified_basis': str(EXPECTED_BASIS),
            'verified_reward': str(EXPECTED_CREDIT),
            'simulated_date': synthetic_today,
            'simulated_grant': grant_name,
            'checks': events,
            'gateway_get': 'MOCKED: NOT independently verified',
            'new_credits_persisted': False,
            'read_only_in_effect': 'savepoint rollback; temporary writes only',
        }
    finally:
        # MUST run after every success/failure. No reward/grant data retained.
        frappe.db.rollback(save_point=SAVEPOINT)

    after = _snapshot(reward_name, source_key)
    _require(before == after, 'ROLLBACK INTEGRITY FAILURE: reward/grant state changed')
    _require(not frappe.db.exists('Club100 Wallet Grant', {'source_event_key': source_key}),
             'ROLLBACK INTEGRITY FAILURE: grant remains')
    if grant_name:
        _require(not frappe.db.exists('Club100 Wallet Ledger', {'grant': grant_name}),
                 'ROLLBACK INTEGRITY FAILURE: grant ledger remains')
    outcome['rollback_verified'] = True
    return outcome
