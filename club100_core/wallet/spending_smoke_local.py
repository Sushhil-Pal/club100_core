"""LOCAL-ONLY wallet lifecycle smoke test. Uses DB savepoint & rollback.

Execute ONLY on club100.local with CLUB100_ALLOW_WALLET_TEST=1.
No real payments, actual wallet credits, or cash top-ups are performed.
"""
import os
import uuid
from decimal import Decimal

import frappe
from frappe.utils import today
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.spending import reserve, capture, release, refund_full
from club100_core.wallet.logic import money


def run():
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_WALLET_TEST') != '1':
        raise RuntimeError('Local wallet smoke test may run only on club100.local with explicit opt-in')
    member = frappe.db.get_value('Club100 Member', {}, 'name')
    if not member:
        return {'outcome':'SKIP', 'reason':'No Club100 Member available'}
    watched = ('Club100 Wallet','Club100 Wallet Grant','Club100 Wallet Ledger','Club100 Wallet Reservation')
    before = {n:frappe.db.count(n) for n in watched}
    checks=[]
    token = uuid.uuid4().hex[:18]
    frappe.db.savepoint('wallet_stage2d1_test')
    original_user = frappe.session.user
    try:
        frappe.set_user('Administrator')
        # Promo allocations only, permission controlled. These test credits are rolled back.
        for ix, amount in enumerate(('100.00','50.00')):
            frappe.get_doc({'doctype':'Club100 Wallet Grant', 'member':member,
                'source_type':'Promotion', 'source_event_key':f'WALLETSMOKE:{token}:{ix}',
                'funding_reference':f'LOCAL-TEST-{token}', 'amount':amount,
                'currency':'INR', 'valid_from':today(), 'validity_type':'Lifetime',
                'status':'Active'}).insert(ignore_permissions=True)
        checks.append('Two promotion grants and ledger credits created')
        initial = money(wallet_balance(member)['available'])
        # Existing balances may exist, hence compare deltas only.
        key = 'WALLETSMOKE-' + token + '-A'
        ref = 'LOCALTEST:' + token
        first = reserve(member,key,ref,'120.00')
        assert first['status']=='Reserved' and len(first['allocations'])>=1
        assert money(wallet_balance(member)['available']) == initial - Decimal('120.00')
        checks.append('Reservation holds funds immediately')
        again = reserve(member,key,ref,'120.00')
        assert again['reservation']==first['reservation']
        try:
            reserve(member,key,ref,'121.00')
            raise AssertionError('Wrong-amount replay accepted')
        except ValueError:
            pass
        checks.append('Reservation idempotency and mismatched replay guarded')
        settled = capture(key,ref)
        assert settled['status']=='Captured'
        assert capture(key,ref)['status']=='Captured'
        assert money(wallet_balance(member)['available']) == initial - Decimal('120.00')
        checks.append('Capture writes spend ledger once and keeps funds deducted')
        try:
            release(key)
            raise AssertionError('Captured reservation cancelled')
        except ValueError:
            pass
        returned = refund_full(key)
        assert returned['status']=='Refunded' and refund_full(key)['status']=='Refunded'
        assert money(wallet_balance(member)['available']) == initial
        checks.append('Full refund restores original grants once')
        second = 'WALLETSMOKE-' + token + '-B'
        reserve(member,second,ref,'25.00')
        assert money(wallet_balance(member)['available']) == initial-Decimal('25.00')
        assert release(second)['status']=='Released'
        assert release(second)['status']=='Released'
        assert money(wallet_balance(member)['available']) == initial
        checks.append('Release frees a hold without ledger debit')
        # An elapsed reservation stops blocking spendable balance even when
        # the periodic expiry cleanup has not run.
        third = 'WALLETSMOKE-' + token + '-C'
        expired = reserve(member,third,ref,'10.00')
        assert money(wallet_balance(member)['available']) == initial-Decimal('10.00')
        frappe.db.set_value('Club100 Wallet Reservation', expired['reservation'],
                            'expires_at', '2020-01-01 00:00:00', update_modified=False)
        assert money(wallet_balance(member)['available']) == initial
        try:
            capture(third,ref)
            raise AssertionError('Expired reservation was captured')
        except ValueError:
            pass
        assert release(third)['status']=='Expired'
        checks.append('Expired hold automatically becomes unspendable and cannot capture')
        # Direct Desk writes must be blocked.
        try:
            frappe.get_doc({'doctype':'Club100 Wallet Reservation','reservation_key':'NOPE-'+token,
                'member':member,'wallet':frappe.db.get_value('Club100 Wallet', {'member':member}, 'name'),'currency':'INR',
                'total_amount':1,'reference':'x','status':'Reserved',
                'expires_at':frappe.utils.now_datetime(),
                'items':[{'grant':first['allocations'][0]['grant'],
                           'source_type':'Promotion','amount':1}]}).insert(ignore_permissions=True)
            raise AssertionError('Direct reservation insert was accepted')
        except frappe.ValidationError:
            pass
        checks.append('Direct reservation creation blocked')
    finally:
        frappe.db.rollback(save_point='wallet_stage2d1_test')
        frappe.set_user(original_user)
    after = {n:frappe.db.count(n) for n in watched}
    if before != after:
        raise RuntimeError('Rollback record-count verification failed: '+str({'before':before,'after':after}))
    return {'site':frappe.local.site,'outcome':'PASS','passed':len(checks),
            'cases':checks,'rollback_verified_by_record_counts':True,
            'note':'No Razorpay, production wallets, or employee payments were exercised.'}
