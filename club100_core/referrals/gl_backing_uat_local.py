"""Club100 local rollback-only Referral GL-backing UAT.

Install into: club100_core/referrals/gl_backing_uat_local.py
Execute (from the bench directory):
  bench --site club100.local execute club100_core.referrals.gl_backing_uat_local.run

Simulates maturity for the one pinned Approved reward, then exercises the real
referral expense/liability booking path, source-specific backing verifier and
idempotency. Creates temporary Journal Entry / GL / Accounting Event within a
single DB savepoint and ALWAYS rolls them back. No external gateway request;
Razorpay GET is mocked. NEVER run on production.

A successful result proves accounting integration in a local synthetic future-
date test, NOT production readiness, real-world refund status or approval of
GST/referral expense policy.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import getdate, nowdate

from club100_core.referrals import settlement as ref_settlement
from club100_core.referrals.settlement_logic import money
from club100_core.accounting import settlement as invoice_accounting
from club100_core.accounting import funding
from club100_core.accounting.wallet_backing import verify_wallet_grant_backing

SITE = 'club100.local'
REWARD = 'RPAY:pay_TxJNlIfJcAtJOm'
REFERRER = 'C100-MEM-00240'
REFERRED = 'C100-MEM-00242'
CREDIT = Decimal('76.10')
BASIS = Decimal('760.95')
GATEWAY = Decimal('799.00')
SAVEPOINT = 'club100_referral_gl_backing_uat'
EVENT_DT = 'Club100 Wallet Accounting Event'


def _require(ok, message):
    if not ok:
        raise AssertionError(message)


def _snapshot(source_key):
    """Exact reward state plus absence of its referral grant and funding event."""
    row = frappe.db.get_value('Club100 Referral Reward', REWARD,
        ['status', 'settlement_reference', 'settled_on', 'eligible_on',
         'verified_basis_amount', 'verified_reward_amount'], as_dict=True)
    grants = frappe.get_all('Club100 Wallet Grant',
        filters={'source_event_key': source_key}, pluck='name', limit_page_length=10)
    return {
        'reward': dict(row) if row else None,
        'grants': list(grants),
        'referrer_credit_count': frappe.db.count('Club100 Wallet Ledger',
            {'member': REFERRER, 'entry_type': 'Credit'}),
        'accounting_event_count': frappe.db.count(EVENT_DT),
        'journal_entry_count': frappe.db.count('Journal Entry'),
        'gl_entry_count': frappe.db.count('GL Entry'),
    }


def _gl_amount(journal_entry, account, side, company):
    _require(side in ('debit','credit'), 'invalid GL side')
    rows = frappe.db.sql('''SELECT COALESCE(SUM(`''' + side + '''`), 0) AS amount
      FROM `tabGL Entry` WHERE voucher_type='Journal Entry'
      AND voucher_no=%s AND company=%s AND account=%s AND is_cancelled=0''',
      (journal_entry, company, account), as_dict=True)
    return money(rows[0].amount)


def run(reward_name=REWARD):
    _require(frappe.local.site == SITE, 'LOCAL ONLY: club100.local required')
    _require(str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test',
             'LOCAL ONLY: Razorpay Test Mode required')
    _require(frappe.session.user == 'Administrator'
             or 'System Manager' in frappe.get_roles(frappe.session.user),
             'Administrator or System Manager required')
    _require(reward_name == REWARD, 'Only the pinned UAT reward is allowed')
    _require(ref_settlement._enabled(), 'Referral pilot settlement must be enabled locally')
    _require(funding._post_allowed(), 'Referral GL booking is not enabled for local Test Mode')
    for method in ('referral_issuance_preview', 'post_referral_issuance'):
        _require(callable(getattr(funding, method, None)),
                 'Missing current funding implementation: ' + method)

    reward = frappe.get_doc('Club100 Referral Reward', reward_name)
    _require(reward.status == 'Approved' and reward.basis_verification_status == 'Verified',
             'Expected Approved + Verified, not yet credited')
    _require(reward.reward_track == 'Member Credit' and reward.reward_event == 'First Purchase',
             'Wrong reward event / track')
    _require(reward.referred_member == REFERRED,
             'Unexpected referred member')
    _require(frappe.db.get_value('Club100 Referral Partner', reward.partner, 'member') == REFERRER,
             'Unexpected referrer')
    _require(money(reward.verified_reward_amount) == CREDIT
             and money(reward.verified_basis_amount) == BASIS,
             'Verified reward amount/basis mismatch')
    _require(reward.payment_reference == 'pay_TxJNlIfJcAtJOm',
             'Original payment changed')
    _require(str(reward.eligible_on)[:10] == '2026-12-07',
             'Unexpected 30-day hold date')
    _require(getdate(nowdate()) < getdate(reward.eligible_on),
             'Reward already due: do not simulate maturity')
    _require(not reward.settlement_reference and not reward.settled_on,
             'Live reward has already been settled')
    _require(frappe.db.exists('DocType', EVENT_DT),
             'Missing wallet accounting event DocType')

    cfg = invoice_accounting._config()
    _require(cfg['Referral'] and cfg['company'],
             'Referral liability account or Company not configured')
    expense = str(frappe.conf.get('club100_accounting_referral_expense_account') or '').strip()
    _require(expense, 'Referral expense account not configured')

    source_key = ref_settlement.REFERRAL_KEY_PREFIX + REWARD
    before = _snapshot(source_key)
    _require(not before['grants'], 'Reward already has a live grant')
    _require(frappe.db.count(EVENT_DT, {'referral_reward': REWARD}) == 0,
             'This reward already has a posted accounting event')

    simulated_day = str(getdate(reward.eligible_on) + timedelta(days=1))
    result = None
    temp_grant = None
    temp_event = None
    temp_journal = None
    gateway_calls = []
    frappe.db.savepoint(SAVEPOINT)
    try:
        def mocked_gateway(payment_id, amount):
            _require(payment_id == reward.payment_reference,
                     'Gateway received wrong payment ID')
            _require(money(amount) == GATEWAY, 'Gateway expected amount differs')
            gateway_calls.append(payment_id)
            return True

        def no_commit(*_a, **_kw):
            raise AssertionError('Unexpected COMMIT in rollback-only GL test')

        with patch.object(ref_settlement, 'nowdate', return_value=simulated_day), \
             patch.object(ref_settlement, '_gateway_is_captured_without_refund',
                          side_effect=mocked_gateway), \
             patch.object(frappe.db, 'commit', side_effect=no_commit):
            matured = ref_settlement.mature_one(REWARD, commit=False)
            _require(matured.get('status') == 'Available',
                     'Simulated maturity did not make the reward available')
            temp_grant = matured.get('wallet_grant')
            _require(temp_grant, 'No simulated Wallet Grant')
            grant = frappe.get_doc('Club100 Wallet Grant', temp_grant)
            _require(grant.source_type == 'Referral' and grant.member == REFERRER
                     and grant.referral_reward == REWARD and money(grant.amount) == CREDIT,
                     'Wrong temporary grant identity or value')

            allocation = {'grant': temp_grant, 'source': 'Referral',
                          'amount': str(CREDIT)}
            before_book = 'UNEXPECTED_ALREADY_BACKED'
            before_block = None
            try:
                verify_wallet_grant_backing(cfg, [allocation])
            except Exception as exc:
                before_book = 'BLOCKED_BEFORE_GL_ISSUANCE'
                before_block = str(exc)[:300]
            _require(before_book == 'BLOCKED_BEFORE_GL_ISSUANCE',
                     'Unfunded referral grant passed source-specific GL backing prematurely')

            preview = funding.referral_issuance_preview(temp_grant)
            _require(preview.get('status') == 'Ready for controlled UAT',
                     'Accounting preview is blocked: ' + str(preview.get('issues')))

            booked = funding.post_referral_issuance(temp_grant, commit=False)
            _require(booked.get('status') == 'Posted',
                     'Referral GL booking did not post')
            temp_event = booked.get('event')
            temp_journal = booked.get('journal_entry')
            _require(temp_event and temp_journal,
                     'Funding booking did not return event and JE identifiers')
            event = frappe.get_doc(EVENT_DT, temp_event)
            _require(event.status == 'Posted' and event.source_type == 'Referral'
                     and event.grant == temp_grant and event.referral_reward == REWARD
                     and event.journal_entry == temp_journal and money(event.amount) == CREDIT,
                     'Source accounting event not linked to reward/grant/JE')
            _require(frappe.db.get_value('Journal Entry', temp_journal, 'docstatus') == 1,
                     'Referral JE is not submitted')
            _require(_gl_amount(temp_journal, cfg['Referral'], 'credit', cfg['company']) == CREDIT,
                     'Referral liability GL credit not equal to 76.10')
            _require(_gl_amount(temp_journal, expense, 'debit', cfg['company']) == CREDIT,
                     'Referral expense GL debit not equal to 76.10')
            _require(_gl_amount(temp_journal, cfg['Referral'], 'debit', cfg['company']) == 0,
                     'Unexpected Referral liability debit on issuance')

            verified = verify_wallet_grant_backing(cfg, [allocation])
            _require(money(verified['Referral']) == CREDIT,
                     'Source-specific backing verifier did not validate 76.10')
            invoice_accounting._require_wallet_liability_backing(
                cfg, {'Referral': CREDIT}, [allocation])
            replay = funding.post_referral_issuance(temp_grant, commit=False)
            _require(replay.get('status') == 'Already Posted'
                     and replay.get('journal_entry') == temp_journal,
                     'Repeated posting created a duplicate referral JE')
            _require(frappe.db.count(EVENT_DT, {'grant':temp_grant}) == 1,
                     'More than one source accounting event created')
            _require(frappe.db.count('GL Entry', {
                'voucher_type':'Journal Entry', 'voucher_no':temp_journal,
                'is_cancelled':0}) >= 2, 'GL rows missing from temporary JE')
            _require(gateway_calls == [reward.payment_reference],
                     'Unexpected mocked gateway invocation count')

            result = {
                'result': 'PASS',
                'site': SITE, 'reward': REWARD, 'referrer': REFERRER,
                'verified_reward': str(CREDIT), 'simulated_mature_date': simulated_day,
                'before_posting': before_book, 'before_posting_blocker': before_block,
                'accounting_preview': preview.get('status'),
                'temporary_grant': temp_grant, 'temporary_journal_entry': temp_journal,
                'temporary_source_event': temp_event,
                'liability_account': cfg['Referral'],
                'expense_account': expense,
                'liability_credit': str(CREDIT), 'expense_debit': str(CREDIT),
                'source_backing_after_posting': 'VERIFIED',
                'hybrid_liability_headroom_after_posting': 'VERIFIED',
                'replay': 'IDEMPOTENT',
                'gateway_get': 'MOCKED, not independently verified',
                'persisted': False,
            }
    finally:
        frappe.db.rollback(save_point=SAVEPOINT)

    after = _snapshot(source_key)
    _require(after == before, 'Rollback failed: reward, grant, JE, GL or event changed')
    if temp_grant:
        _require(not frappe.db.exists('Club100 Wallet Grant', temp_grant),
                 'Rollback failed: temporary Referral grant still exists')
        _require(not frappe.db.exists(EVENT_DT, {'grant':temp_grant}),
                 'Rollback failed: temporary accounting event still exists')
    if temp_journal:
        _require(not frappe.db.exists('Journal Entry', temp_journal),
                 'Rollback failed: temporary JE still exists')
        _require(not frappe.db.exists('GL Entry',
                                     {'voucher_type':'Journal Entry', 'voucher_no':temp_journal}),
                 'Rollback failed: temporary JE GL rows still exist')
    _require(result is not None, 'No GL test result generated')
    result['rollback_verified'] = True
    return result
