"""LOCAL-ONLY Round 4 financial integrity smoke; rollback-only; no gateway calls.

This verifies internal reservation, capture, expiry and full wallet-ledger refund.
It DOES NOT refund any Razorpay payment or cancel a paid Enrollment.
"""
import os
import uuid
from decimal import Decimal

import frappe
from frappe.utils import add_days, today
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.logic import money
from club100_core.wallet.spending import reserve, capture, release, refund_full

WATCH = (
    'Club100 Wallet', 'Club100 Wallet Grant', 'Club100 Wallet Ledger',
    'Club100 Wallet Reservation', 'Club100 Wallet Reservation Item',
    'Club100 Enrollment',
)


def _must(condition, message):
    if not condition:
        raise AssertionError(message)


def _reject(fn, label):
    try:
        fn()
    except (ValueError, frappe.ValidationError):
        return
    raise AssertionError(label + ': unexpectedly accepted')


def run():
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_ROUND4_TEST') != '1':
        raise RuntimeError('Round 4 smoke is restricted to club100.local with explicit opt-in')
    # A member with no preexisting grants ensures expiry ordering is deterministic.
    selected = None
    for row in frappe.get_all('Club100 Member', fields=['name'], limit_page_length=500):
        if not frappe.db.exists('Club100 Wallet Grant', {'member': row.name}):
            selected = row.name
            break
    if not selected:
        return {'outcome':'SKIP', 'reason':'No member without existing wallet grants; use a disposable fresh member',
                'no_records_changed':True}

    before = {dt: frappe.db.count(dt) for dt in WATCH}
    key = uuid.uuid4().hex[:18]
    original_user = frappe.session.user
    checks = []
    frappe.db.savepoint('club100_round4_integrity')
    try:
        frappe.set_user('Administrator')
        d = today()
        specs = (
            ('EARLY', '300.00', d, 'Expires On', d),
            ('LATER', '400.00', d, 'Expires On', add_days(d, 30)),
            ('LIFE', '200.00', d, 'Lifetime', None),
            ('EXPIRED', '75.00', add_days(d, -10), 'Expires On', add_days(d, -1)),
            ('FUTURE', '50.00', add_days(d, 1), 'Lifetime', None),
        )
        grant_ids = {}
        for label, amount, start, validity, expiry in specs:
            grant = frappe.get_doc({
                'doctype': 'Club100 Wallet Grant', 'member': selected,
                'source_type': 'Promotion', 'source_event_key': f'ROUND4:{key}:{label}',
                'funding_reference': f'LOCAL-ROUND4:{key}', 'amount': amount,
                'currency': 'INR', 'valid_from': start, 'validity_type': validity,
                'expires_on': expiry, 'status': 'Active',
            }).insert(ignore_permissions=True)
            grant_ids[label] = grant.name
        b = wallet_balance(selected)
        _must(money(b['available']) == Decimal('900.00'), 'Expired/future grants included in spendable funds')
        _must(money(b['expired_unspent']) == Decimal('75.00'), 'Expired grant not reported correctly')
        _must(money(b['future_unvested']) == Decimal('50.00'), 'Future grant not reported correctly')
        checks.append('Expired and future-dated grants excluded; lifetime grant available')

        ref = 'ROUND4-ORDER:' + key
        hold = 'ROUND4-HOLD:' + key
        first = reserve(selected, hold, ref, '799.00')
        _must(first['status'] == 'Reserved', 'Hold not Reserved')
        expected = [(grant_ids['EARLY'], Decimal('300.00')),
                    (grant_ids['LATER'], Decimal('400.00')),
                    (grant_ids['LIFE'], Decimal('99.00'))]
        got = [(r['grant'], money(r['amount'])) for r in first['allocations']]
        _must(got == expected, 'Wrong expiry-first allocation: '+repr(got))
        _must(money(wallet_balance(selected)['available']) == Decimal('101.00'), 'Active hold balance incorrect')
        checks.append('Reservation consumes earliest expiry first, then lifetime; availability reduced immediately')

        dup = reserve(selected, hold, ref, '799.00')
        _must(dup['reservation'] == first['reservation'], 'Reservation replay created a second hold')
        _reject(lambda: reserve(selected, hold, ref, '798.00'), 'Mismatched reservation amount')
        _reject(lambda: reserve(selected, hold, ref+'DIFF', '799.00'), 'Mismatched business reference')
        checks.append('Duplicate reservation idempotent; conflicting replay rejected')

        _reject(lambda: capture(hold, ref+'DIFF'), 'Capture with wrong reference')
        captured = capture(hold, ref)
        _must(captured['status'] == 'Captured', 'Wallet capture failed')
        _must(money(wallet_balance(selected)['available']) == Decimal('101.00'), 'Captured balance incorrect')
        _must(capture(hold, ref)['status'] == 'Captured', 'Capture replay not idempotent')
        spend_rows = frappe.get_all('Club100 Wallet Ledger', filters={'reference': ref, 'entry_type':'Spend'},
                                    fields=['grant','amount'], limit_page_length=20)
        _must(len(spend_rows) == 3, 'Duplicate/missing spend ledger events')
        spend_map = {r.grant: money(r.amount) for r in spend_rows}
        _must(spend_map == {g: -amt for g, amt in expected}, 'Incorrect signed debit per grant')
        checks.append('Capture posts one signed debit per grant; duplicate capture never double-spends')
        _reject(lambda: release(hold), 'Release of captured reservation')
        checks.append('Captured reservation cannot be released as a failed payment')

        returned = refund_full(hold)
        _must(returned['status'] == 'Refunded', 'Full wallet reversal failed')
        _must(refund_full(hold)['status'] == 'Refunded', 'Duplicate reversal not idempotent')
        credits = frappe.get_all('Club100 Wallet Ledger', filters={'reference': ref, 'entry_type':'Refund'},
                                 fields=['grant','amount'], limit_page_length=20)
        _must(len(credits) == 3, 'Duplicate/missing wallet refund ledger events')
        refund_map = {r.grant: money(r.amount) for r in credits}
        _must(refund_map == {g: amt for g, amt in expected}, 'Refund credits did not return to original grants')
        _must(money(wallet_balance(selected)['available']) == Decimal('900.00'), 'Full wallet reversal not reflected')
        checks.append('Full wallet-ledger reversal restores original grants, preserving expiry; replay idempotent')

        # Existing grant expiry must continue applying after a reversal.
        future_balance = wallet_balance(selected, as_of=add_days(d, 31))
        _must(money(future_balance['available']) == Decimal('250.00'),
              'Reversal improperly extended grant expiry (expected lifetime 200 + future 50)')
        checks.append('Reversed credits keep original expiry; expiry still enforced on future dates')

        abandoned = 'ROUND4-ABANDON:' + key
        reserve(selected, abandoned, ref, '150.00')
        _must(money(wallet_balance(selected)['available']) == Decimal('750.00'), 'Abandoned hold not reflected')
        _must(release(abandoned)['status'] == 'Released', 'Abandoned hold not released')
        _must(release(abandoned)['status'] == 'Released', 'Repeated release not idempotent')
        _must(money(wallet_balance(selected)['available']) == Decimal('900.00'), 'Released hold did not restore spendable balance')
        _must(frappe.db.count('Club100 Wallet Ledger', {'reference':ref, 'entry_type':'Spend'}) == 3,
              'Abandoned hold unexpectedly posted a spend')
        checks.append('Abandoned/failed checkout release is idempotent and creates no spend')

        elapsed = 'ROUND4-EXPIRED:' + key
        e = reserve(selected, elapsed, ref, '100.00')
        frappe.db.set_value('Club100 Wallet Reservation', e['reservation'], 'expires_at',
                            '2020-01-01 00:00:00', update_modified=False)
        _must(money(wallet_balance(selected)['available']) == Decimal('900.00'), 'Expired hold still consumes balance')
        _reject(lambda: capture(elapsed, ref), 'Capture after reservation expiry')
        _must(release(elapsed)['status'] == 'Expired', 'Expired hold not marked Expired')
        checks.append('Expired reservation stops holding funds and cannot capture')

        _reject(lambda: reserve(selected, 'ROUND4-OVER:'+key, ref, '901.00'), 'Over-reservation')
        _reject(lambda: reserve(selected, 'ROUND4-SOURCE:'+key, ref, '1.00',
                               allowed_sources=('Corporate',)), 'Unavailable source reservation')
        checks.append('Over-spend and prohibited funding-source reservations rejected')
    finally:
        frappe.db.rollback(save_point='club100_round4_integrity')
        frappe.set_user(original_user)
    after = {dt: frappe.db.count(dt) for dt in WATCH}
    _must(before == after, 'Test rollback count mismatch: '+repr({'before':before,'after':after}))
    return {'site':frappe.local.site, 'outcome':'PASS', 'passed':len(checks),
            'cases':checks, 'rollback_verified_by_record_counts':True,
            'note':'No Razorpay requests, refund API calls, paid enrollment changes or commits performed. '
                   'Split-payment refund coordination is not implemented.'}
