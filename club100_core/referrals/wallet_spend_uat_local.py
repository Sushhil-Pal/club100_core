"""Club100 rollback-only referral wallet spend test, pinned to LOCAL test reward.

Install to: club100_core/referrals/wallet_spend_uat_local.py
Run: bench --site club100.local execute club100_core.referrals.wallet_spend_uat_local.run

Exercises real reward maturity -> grant/ledger -> referral-only wallet reserve,
capture and replay. All effects are rolled back to a Frappe DB savepoint.

NOT a real membership checkout, Sales Invoice, JE/GL posting, or Razorpay
verification. The gateway GET is mocked solely for the simulated date.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate, nowdate

from club100_core.referrals import settlement as settlement
from club100_core.referrals.settlement_logic import money
from club100_core.wallet import spending, core

SITE = 'club100.local'
REWARD = 'RPAY:pay_TxJNlIfJcAtJOm'
REFERRED = 'C100-MEM-00242'
REFERRER = 'C100-MEM-00240'
BASIS = Decimal('760.95')
CREDIT = Decimal('76.10')
PRICE = Decimal('799.00')
GATEWAY_DUE = PRICE - CREDIT  # Informational projection; NOT an actual order/invoice.


def require(ok, msg):
    if not ok:
        raise AssertionError(msg)


def snapshot(reward_name, source_key, referrer, reservation_key=None):
    reward = frappe.db.get_value('Club100 Referral Reward', reward_name,
        ['status', 'eligible_on', 'settlement_reference', 'settled_on',
         'verified_reward_amount', 'verified_basis_amount'], as_dict=True)
    names = frappe.get_all('Club100 Wallet Grant',
        filters={'source_event_key': source_key}, pluck='name', limit_page_length=10)
    grants = [dict(frappe.db.get_value('Club100 Wallet Grant', name,
        ['name','amount','status','valid_from','source_event_key'], as_dict=True))
        for name in names]
    res = frappe.db.get_value('Club100 Wallet Reservation',
        {'reservation_key':reservation_key},'name') if reservation_key else None
    return {
        'reward': dict(reward) if reward else None,
        'grants': grants,
        'grant_ledgers': [frappe.db.count('Club100 Wallet Ledger', {'grant': n}) for n in names],
        'reservation': res,
        'referrer_wallet': frappe.db.get_value('Club100 Wallet', {'member': referrer}, 'name'),
        'referrer_ledger_count': frappe.db.count('Club100 Wallet Ledger', {'member':referrer}),
        'referrer_reservation_count': frappe.db.count('Club100 Wallet Reservation', {'member':referrer}),
    }


def run(reward_name=REWARD):
    require(frappe.local.site == SITE, 'Only club100.local is allowed')
    require(str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test',
            'Razorpay Test Mode is required')
    require(frappe.session.user == 'Administrator'
            or 'System Manager' in frappe.get_roles(frappe.session.user),
            'System Manager / Administrator required')
    require(reward_name == REWARD, 'Unexpected reward ID; this script is pinned to one UAT case')
    require(settlement._enabled(), 'Local referral settlement not enabled')

    reward = frappe.get_doc('Club100 Referral Reward', reward_name)
    require(reward.status == 'Approved' and reward.basis_verification_status == 'Verified',
            'Reward must remain Approved + Verified')
    require(reward.reward_track == 'Member Credit' and reward.reward_event == 'First Purchase',
            'Wrong reward track/event')
    require(reward.referred_member == REFERRED, 'Wrong referred member')
    require(frappe.db.get_value('Club100 Referral Partner', reward.partner, 'member') == REFERRER,
            'Wrong referrer')
    require(money(reward.verified_basis_amount) == BASIS, 'Verified basis mismatch')
    require(money(reward.verified_reward_amount) == CREDIT, 'Verified reward mismatch')
    require(str(reward.eligible_on)[:10] == '2026-12-07', 'Maturity date changed')
    require(getdate(nowdate()) < getdate(reward.eligible_on),
            'Maturity already reached: never spoof date for an eligible reward')
    require(not reward.settlement_reference and not reward.settled_on,
            'Reward has already settled')
    require(reward.payment_reference == 'pay_TxJNlIfJcAtJOm', 'Unexpected payment ID')

    source_key = settlement.REFERRAL_KEY_PREFIX + reward_name
    attempt = uuid4().hex[:20]
    res_key = 'UATREFSPEND-' + attempt
    ref = 'LOCAL-UAT-REFERRAL-' + attempt
    before = snapshot(reward_name, source_key, REFERRER, res_key)
    require(not before['grants'] and not before['reservation'],
            'The UAT reward has already generated a grant or reservation')

    synthetic_day = getdate(reward.eligible_on) + timedelta(days=1)
    fake_now = datetime.combine(synthetic_day, time(12, 0, 0))
    simulated_day = synthetic_day.isoformat()
    checks = []
    created_grant = None
    created_reservation = None
    result = None
    gateway_checks = []
    frappe.db.savepoint('club100_ref_wallet_spend_uat')
    try:
        # No time spoof for the early-maturity test.
        try:
            settlement.mature_one(reward_name, commit=False)
        except ValueError as exc:
            require('hold period' in str(exc).lower(),
                    'Unexpected early settlement error: ' + str(exc))
        else:
            raise AssertionError('Premature reward maturity incorrectly succeeded')
        require(snapshot(reward_name, source_key, REFERRER, res_key) == before,
                'Premature maturity mutated the data')
        checks.append('Early maturity blocked without grant/ledger writes')

        def mocked_gateway(payment_id, tender):
            require(payment_id == reward.payment_reference, 'Wrong gateway payment identity')
            require(money(tender) == PRICE, 'Unexpected gateway amount for referral-origin purchase')
            gateway_checks.append(payment_id)
            return True

        def forbid_commit(*_args, **_kwargs):
            raise AssertionError('Unexpected COMMIT inside rollback-only test')

        # Simulate December 8 for both referral maturity and wallet validity/hold calculations.
        # No global Frappe dates, real reward fields or clock are changed.
        with patch.object(settlement, 'nowdate', return_value=simulated_day), \
             patch.object(settlement, '_gateway_is_captured_without_refund', side_effect=mocked_gateway), \
             patch.object(spending, 'now_datetime', return_value=fake_now), \
             patch.object(core, 'now_datetime', return_value=fake_now), \
             patch.object(frappe.db, 'commit', side_effect=forbid_commit):
            prior_balance = core.wallet_balance(REFERRER, as_of=simulated_day)
            prior_referral = money(prior_balance['by_source']['Referral'])

            matured = settlement.mature_one(reward_name, commit=False)
            require(matured['status'] == 'Available', 'Settlement did not mark reward Available')
            created_grant = matured.get('wallet_grant')
            require(created_grant, 'No Wallet Grant generated')
            grant = frappe.get_doc('Club100 Wallet Grant', created_grant)
            require(grant.member == REFERRER and grant.source_type == 'Referral',
                    'Wallet Grant owner/source mismatch')
            require(grant.source_event_key == source_key and grant.referral_reward == reward_name,
                    'Wallet Grant reward provenance mismatch')
            require(money(grant.amount) == CREDIT, 'Wallet Grant must use verified 76.10')
            require(money(core.wallet_balance(REFERRER, as_of=simulated_day)['by_source']['Referral'])
                    == prior_referral + CREDIT, 'Credit not reflected in referrer wallet')
            checks.append('Temporary verified 76.10 referral grant and ledger Credit created')

            # Only the specific newly issued Referral grant can fund this test.
            reserved = spending.reserve(REFERRER, res_key, ref, str(CREDIT),
                ttl_minutes=15, allowed_sources=('Referral',), allowed_grants=(created_grant,))
            created_reservation = reserved['reservation']
            require(reserved['status'] == 'Reserved', 'Reservation did not become Reserved')
            require(len(reserved['allocations']) == 1, 'Expected one grant allocation')
            a = reserved['allocations'][0]
            require(a['grant'] == created_grant and a['source'] == 'Referral'
                    and money(a['amount']) == CREDIT, 'Wrong source/amount allocated')
            require(money(core.wallet_balance(REFERRER, as_of=simulated_day)['by_source']['Referral'])
                    == prior_referral, 'Held amount not removed from spendable referral balance')
            checks.append('Referral-only 76.10 hold reduces spendable balance')

            replay = spending.reserve(REFERRER, res_key, ref, str(CREDIT),
                allowed_sources=('Referral',), allowed_grants=(created_grant,))
            require(replay['reservation'] == created_reservation, 'Reservation replay duplicated hold')
            try:
                spending.reserve(REFERRER, res_key, ref, '76.09',
                    allowed_sources=('Referral',), allowed_grants=(created_grant,))
            except ValueError:
                pass
            else:
                raise AssertionError('Different-amount replay was incorrectly accepted')
            checks.append('Reservation replay is idempotent; different amount rejected')

            # TEST ONLY: direct trusted wallet capture is not payment verification.
            captured = spending.capture(res_key, ref)
            require(captured['status'] == 'Captured', 'Wallet capture did not complete')
            require(spending.capture(res_key, ref)['reservation'] == created_reservation,
                    'Capture replay created duplicate reservation')
            entries = frappe.get_all('Club100 Wallet Ledger',
                filters={'grant': created_grant}, fields=['entry_type','amount'], limit_page_length=10)
            credits = [money(e.amount) for e in entries if e.entry_type == 'Credit']
            spends = [money(e.amount) for e in entries if e.entry_type == 'Spend']
            require(credits == [CREDIT] and spends == [-CREDIT],
                    'Expected one +76.10 Credit and one -76.10 Spend ledger entry')
            require(money(core.wallet_balance(REFERRER, as_of=simulated_day)['by_source']['Referral'])
                    == prior_referral, 'Referral wallet remains spendable after capture')
            checks.append('Capture posted one 76.10 Spend; replay did not double-spend')

            again = settlement.mature_one(reward_name, commit=False)
            require(again['wallet_grant'] == created_grant, 'Maturity replay changed grant identity')
            require(frappe.db.count('Club100 Wallet Grant', {'source_event_key': source_key}) == 1,
                    'Duplicate reward grant after replay')
            require(len(gateway_checks) == 1, 'Unexpected gateway lookup count')
            checks.append('Reward replay does not reissue redeemed wallet credit')

        result = {
            'result': 'PASS', 'site': frappe.local.site,
            'reward': reward_name, 'referrer': REFERRER, 'referred_member': REFERRED,
            'simulated_date': simulated_day, 'verified_reward': str(CREDIT),
            'test_only_reservation': created_reservation,
            'reference_price': str(PRICE), 'illustrative_wallet_tender': str(CREDIT),
            'illustrative_gateway_due': str(GATEWAY_DUE),
            'checks': checks,
            'gateway_get': 'MOCKED - external refund status NOT verified',
            'checkout_vouchers_or_gl_posted': False,
            'hybrid_gl_backing_verified': False,
            'committed': False,
        }
    finally:
        frappe.db.rollback(save_point='club100_ref_wallet_spend_uat')

    after = snapshot(reward_name, source_key, REFERRER, res_key)
    require(before == after, 'ROLLBACK FAILED: reward/grant/ledger/reservation state changed')
    if created_grant:
        require(not frappe.db.exists('Club100 Wallet Grant', created_grant),
                'ROLLBACK FAILED: test grant still exists')
        require(not frappe.db.exists('Club100 Wallet Ledger', {'grant': created_grant}),
                'ROLLBACK FAILED: ledger entries still exist')
    if created_reservation:
        require(not frappe.db.exists('Club100 Wallet Reservation', created_reservation),
                'ROLLBACK FAILED: test reservation still exists')
    result['rollback_verified'] = True
    return result
