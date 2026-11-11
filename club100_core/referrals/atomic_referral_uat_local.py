"""Rollback-only verification of ATOMIC referral maturity + GL liability.

Install: club100_core/referrals/atomic_referral_uat_local.py
bench --site club100.local execute club100_core.referrals.atomic_referral_uat_local.run

STRICT local/Test-Mode only; pinned existing reward. External gateway GET is mocked;
this is NOT a production payment/refund or tax-policy verification.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import getdate, nowdate
from club100_core.referrals import settlement as s
from club100_core.referrals.settlement_logic import money
from club100_core.accounting import funding
from club100_core.accounting import settlement as acc
from club100_core.accounting.wallet_backing import verify_wallet_grant_backing

SITE = 'club100.local'
REWARD = 'RPAY:pay_TxJNlIfJcAtJOm'
REFERRER = 'C100-MEM-00240'
REFERRED = 'C100-MEM-00242'
AMOUNT = Decimal('76.10')
BASIS = Decimal('760.95')
PAYMENT = Decimal('799.00')
POINT = 'club100_atomic_referral_test'


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def snapshot():
    source_key = s.REFERRAL_KEY_PREFIX + REWARD
    return {
        'reward': dict(frappe.db.get_value('Club100 Referral Reward', REWARD,
            ['status','settlement_reference','settled_on','eligible_on','verified_reward_amount'],
            as_dict=True)),
        'grants': frappe.get_all('Club100 Wallet Grant',
            filters={'source_event_key':source_key}, pluck='name'),
        'credits': frappe.db.count('Club100 Wallet Ledger',
                                  {'member':REFERRER,'entry_type':'Credit'}),
        'events': frappe.db.count('Club100 Wallet Accounting Event'),
        'journals': frappe.db.count('Journal Entry'),
        'gl': frappe.db.count('GL Entry'),
    }


def gl_amount(journal, account, side, company):
    require(side in ('debit','credit'), 'invalid side')
    return money(frappe.db.sql(
        'SELECT COALESCE(SUM(`' + side + '`),0) AS value FROM `tabGL Entry` '
        'WHERE voucher_type=%s AND voucher_no=%s AND company=%s '
        'AND account=%s AND is_cancelled=0',
        ( 'Journal Entry',journal,company,account), as_dict=True)[0].value)


def run(reward_name=REWARD):
    require(frappe.local.site == SITE, 'club100.local only')
    require(str(frappe.conf.get('razorpay_mode') or 'test').lower()=='test', 'Test Mode only')
    require(frappe.session.user=='Administrator'
            or 'System Manager' in frappe.get_roles(frappe.session.user),
            'System Manager required')
    require(reward_name==REWARD,'Pinned reward only')
    require(funding._post_allowed() and s._enabled(), 'Local accounting/referral pilots disabled')
    require('CLUB100_REFERRAL_ATOMIC_GL_V1' in s.mature_one.__code__.co_consts or
            'CLUB100_REFERRAL_ATOMIC_GL_V1' in open(s.__file__, encoding='utf-8').read(),
            'Atomic integration patch not installed')
    reward=frappe.get_doc('Club100 Referral Reward',REWARD)
    require(reward.status=='Approved' and reward.basis_verification_status=='Verified',
            'Expected verified Approved reward')
    require(reward.referred_member==REFERRED and reward.reward_event=='First Purchase'
            and reward.reward_track=='Member Credit', 'Wrong reward identity')
    require(frappe.db.get_value('Club100 Referral Partner',reward.partner,'member')==REFERRER,
            'Referrer changed')
    require(money(reward.verified_reward_amount)==AMOUNT and
            money(reward.verified_basis_amount)==BASIS, 'Verified amount/basis mismatch')
    require(str(reward.eligible_on)[:10]=='2026-12-07','Maturity date differs')
    require(getdate(nowdate())<getdate(reward.eligible_on),'Already matured in real time')
    require(not reward.settlement_reference,'Reward already settled')
    before=snapshot()
    require(not before['grants'],'Test requires zero persisted grants for this reward')
    synthetic_day=str(getdate(reward.eligible_on)+timedelta(days=1))
    temporary_grant=None
    temporary_journal=None
    result=None
    frappe.db.savepoint(POINT)
    try:
        def mock_gateway(payment_id, expected_amount):
            require(payment_id==reward.payment_reference,'Unexpected payment')
            require(money(expected_amount)==PAYMENT,'Unexpected Razorpay tender')
            return True
        def forbidden_commit(*args, **kwargs):
            raise AssertionError('UAT attempted to commit a permanent record')
        with patch.object(frappe.db,'commit',side_effect=forbidden_commit):
            try:
                s.mature_one(REWARD,commit=False)
            except ValueError as exc:
                require('hold period' in str(exc).lower(),'Wrong early-maturity failure')
            else:
                raise AssertionError('Early maturity was not blocked')
            require(snapshot()==before,'Early-maturity test changed persistent state')
            with patch.object(s,'nowdate',return_value=synthetic_day),\
                 patch.object(s,'_gateway_is_captured_without_refund',side_effect=mock_gateway):
                # Inject a late finance posting failure: the entire wallet grant,
                # Credit ledger and Available reward state must roll back.
                with patch.object(funding,'post_referral_issuance',
                                  side_effect=RuntimeError('SIMULATED_GL_FAILURE')):
                    try:
                        s.mature_one(REWARD,commit=False)
                    except RuntimeError as exc:
                        require('SIMULATED_GL_FAILURE' in str(exc),'Wrong injected error')
                    else:
                        raise AssertionError('GL failure was silently ignored')
                require(snapshot()==before,
                        'Accounting failure left grant, credit or reward changes')

                done=s.mature_one(REWARD,commit=False)
                require(done.get('status')=='Available' and done.get('accounting_status')=='Posted',
                        'Atomic settlement did not post liability')
                temporary_grant=done.get('wallet_grant')
                temporary_journal=done.get('accounting_journal')
                require(temporary_grant and temporary_journal,'Missing grant or JE link')
                grant=frappe.get_doc('Club100 Wallet Grant',temporary_grant)
                require(grant.member==REFERRER and grant.source_type=='Referral'
                        and grant.referral_reward==REWARD and money(grant.amount)==AMOUNT,
                        'Wrong member, referral or amount')
                credits=frappe.get_all('Club100 Wallet Ledger',
                    filters={'grant':temporary_grant,'entry_type':'Credit'},
                    fields=['name','amount'])
                require(len(credits)==1 and money(credits[0].amount)==AMOUNT,
                        'Wallet Credit not exactly once at 76.10')
                event=frappe.db.get_value('Club100 Wallet Accounting Event',
                    {'grant':temporary_grant},
                    ['name','status','journal_entry','referral_reward','amount'],as_dict=True)
                require(event and event.status=='Posted' and event.journal_entry==temporary_journal
                        and event.referral_reward==REWARD and money(event.amount)==AMOUNT,
                        'Source-linked Posted event missing')
                cfg=acc._config()
                expense=str(frappe.conf.get('club100_accounting_referral_expense_account') or '')
                require(gl_amount(temporary_journal,cfg['Referral'],'credit',cfg['company'])==AMOUNT,
                        'Wrong referral GL liability credit')
                require(gl_amount(temporary_journal,expense,'debit',cfg['company'])==AMOUNT,
                        'Wrong referral GL expense debit')
                allocations=[{'grant':temporary_grant,'source':'Referral','amount':str(AMOUNT)}]
                require(money(verify_wallet_grant_backing(cfg,allocations)['Referral'])==AMOUNT,
                        'Hybrid grant verifier rejected backed referral')
                acc._require_wallet_liability_backing(cfg,{'Referral':AMOUNT},allocations)
                replay=s.mature_one(REWARD,commit=False)
                require(replay.get('wallet_grant')==temporary_grant and
                        replay.get('accounting_status')=='Verified Existing',
                        'Repeated maturity failed idempotent backed replay')
                require(frappe.db.count('Club100 Wallet Accounting Event',
                                        {'grant':temporary_grant})==1,'Duplicate accounting event')
                require(frappe.db.count('Club100 Wallet Ledger',
                            {'grant':temporary_grant,'entry_type':'Credit'})==1,
                        'Duplicate Wallet Credit')
                result={
                    'result':'PASS','site':SITE,'reward':REWARD,'referrer':REFERRER,
                    'verified_reward':'76.10','simulated_date':synthetic_day,
                    'early_maturity':'BLOCKED',
                    'accounting_failure':'ALL_WRITES_ROLLED_BACK',
                    'temporary_grant':temporary_grant,
                    'temporary_journal':temporary_journal,
                    'expense_debit':'76.10','liability_credit':'76.10',
                    'hybrid_gl_backing':'VERIFIED',
                    'replay':'IDEMPOTENT',
                    'gateway_status':'MOCKED, not externally verified',
                    'persisted':False,
                }
    finally:
        frappe.db.rollback(save_point=POINT)
    require(snapshot()==before,'Rollback verification FAILED')
    if temporary_grant:
        require(not frappe.db.exists('Club100 Wallet Grant',temporary_grant),
                'Temporary grant remained after rollback')
    if temporary_journal:
        require(not frappe.db.exists('Journal Entry',temporary_journal),
                'Temporary submitted JE remained after rollback')
    require(result is not None,'No positive UAT result')
    result['rollback_verified']=True
    return result
