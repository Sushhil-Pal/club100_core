"""Stage 5F-B: read-only, source-specific GL backing for wallet redemptions.

This module never posts cash, JEs, invoices, grants, refunds, or opening
balances. It deliberately rejects promotional credit accounting until an
approved policy exists. It also rejects pre-existing fictitious UAT pools even
if an unrelated GL account happens to have enough credit balance.

Source events must have been created by the guarded Stage 5F-A accounting
process. These checks do NOT prove that an incoming corporate bank receipt
has been independently matched to a bank statement; production stays blocked.
"""
from collections import defaultdict
from decimal import Decimal

import frappe
from frappe.utils import cint
from club100_core.accounting.rules import money
from club100_core.accounting.funding import CORPORATE_EVENT, REFERRAL_EVENT, EVENT_DT


def allocation_totals(allocations):
    """Pure allocation validation: positive paise, unique grants, known sources."""
    if not allocations:
        raise ValueError('Wallet backing requires nonempty captured allocations')
    seen = set()
    totals = defaultdict(lambda: Decimal('0.00'))
    for allocation in allocations:
        grant = str(allocation.get('grant') or '').strip()
        source = str(allocation.get('source') or '').strip()
        amount = money(allocation.get('amount'))
        if not grant or grant in seen:
            raise ValueError('Missing or duplicated grant in wallet redemption')
        if source not in ('Corporate', 'Referral', 'Promotion') or amount <= 0:
            raise ValueError('Invalid wallet grant source or amount')
        if source == 'Promotion':
            raise ValueError('Promotion wallet GL settlement requires an approved, implemented accounting policy')
        seen.add(grant)
        totals[source] += amount
    return dict(sorted(totals.items()))


def _event(key, *, event_type, source_type, company, pool=None, grant=None, member=None):
    row = frappe.db.get_value(EVENT_DT, {'event_key':key},
        ['name','event_type','source_type','company','corporate_pool','grant',
         'member','payment_entry','journal_entry','amount','status'], as_dict=True)
    if not row:
        raise ValueError('No source-backed posted wallet GL event for ' + source_type)
    if (row.status!='Posted' or row.event_type!=event_type or row.source_type!=source_type
            or row.company!=company or (pool is not None and row.corporate_pool!=pool)
            or (grant is not None and row.grant!=grant)
            or (member is not None and row.member!=member)):
        raise ValueError('Wallet source GL event identity or status mismatch')
    if not row.journal_entry or cint(frappe.db.get_value('Journal Entry',row.journal_entry,'docstatus'))!=1:
        raise ValueError('Source liability Journal Entry was cancelled or is missing')
    return row


def _voucher_credit(journal, liability_account, company):
    r=frappe.db.sql("""SELECT COALESCE(SUM(credit-debit),0) AS net
      FROM `tabGL Entry` WHERE voucher_type='Journal Entry' AND voucher_no=%s
       AND account=%s AND company=%s AND is_cancelled=0""",
       (journal, liability_account, company),as_dict=True)[0]
    return money(r.net)


def _grant_ledger_credit(grant_name, value):
    r=frappe.db.sql("""SELECT COALESCE(SUM(amount),0) AS credit
       FROM `tabClub100 Wallet Ledger` WHERE `grant`=%s AND entry_type='Credit' """,
       (grant_name,),as_dict=True)[0]
    if money(r.credit)!=money(value):
        raise ValueError('Wallet grant issuance ledger does not match grant amount')


def _corporate_source(cfg, grant, event):
    pool=frappe.db.get_value('Club100 Corporate Credit Pool',grant.corporate_pool,
        ['name','corporate_customer','currency','amount','status','funding_verified',
         'funding_reference'],as_dict=True)
    if not pool or pool.status!='Funded' or not cint(pool.funding_verified) or pool.currency!='INR':
        raise ValueError('Corporate pool is not a verified funded INR pool')
    if money(event.amount)!=money(pool.amount):
        raise ValueError('Corporate source GL amount does not match pool funding')
    pe=frappe.db.get_value('Payment Entry',event.payment_entry,
        ['name','docstatus','company','payment_type','party_type','party','paid_to',
         'reference_no','paid_amount','received_amount'],as_dict=True) if event.payment_entry else None
    if (not pe or pe.docstatus!=1 or pe.company!=cfg['company'] or pe.payment_type!='Receive'
        or pe.party_type!='Customer' or pe.party!=pool.corporate_customer
        or pe.paid_to==cfg['razorpay_clearing'] or pe.reference_no!=pool.funding_reference
        or money(pe.received_amount)<money(pool.amount)
        or money(pe.paid_amount)!=money(pe.received_amount)):
        raise ValueError('Corporate funding receipt is missing or inconsistent with pool')
    bank = frappe.db.get_value('Account',pe.paid_to,['root_type','account_type'],as_dict=True)
    if not bank or bank.root_type!='Asset' or bank.account_type!='Bank':
        raise ValueError('Corporate receipt account is not a bank asset account')
    allocated=frappe.db.sql("""SELECT COALESCE(SUM(amount),0) AS n
        FROM `tabClub100 Wallet Grant` WHERE corporate_pool=%s AND source_type='Corporate' """,
        (grant.corporate_pool,),as_dict=True)[0]
    if money(allocated.n)>money(pool.amount):
        raise ValueError('Corporate pool wallet grants exceed credited GL funding')


def _referral_source(cfg, grant, event):
    if not grant.referral_reward or money(event.amount)!=money(grant.amount):
        raise ValueError('Referral grant is not linked to exact booked referral liability')
    reward=frappe.db.get_value('Club100 Referral Reward',grant.referral_reward,
        ['name','status','reward_track','basis_verification_status',
         'settlement_reference','verified_reward_amount'],as_dict=True)
    if (not reward or reward.status!='Available' or reward.reward_track!='Member Credit'
        or reward.basis_verification_status!='Verified' or reward.settlement_reference!=grant.name
        or money(reward.verified_reward_amount)!=money(grant.amount)):
        raise ValueError('Referral reward is not a matured, issued and verified credit')


def verify_wallet_grant_backing(cfg, allocations):
    """Return verified per-source sums, or fail before any Sales Invoice is posted.

    This requires source events *and* real GL entry credits. It is intentionally
    safe to call during read-only preview or after row locks in post_one.
    """
    totals=allocation_totals(allocations)
    company=cfg['company']
    for allocation in allocations:
        grant=frappe.db.get_value('Club100 Wallet Grant', allocation['grant'],
            ['name','member','source_type','amount','currency','funding_verified','corporate_pool','referral_reward'],as_dict=True)
        if not grant or grant.source_type!=allocation['source']:
            raise ValueError('Wallet grant missing or source mismatch')
        if grant.currency != 'INR':
            raise ValueError('Wallet grant must be denominated in INR')
        if money(allocation['amount'])>money(grant.amount):
            raise ValueError('Redemption amount exceeds original wallet grant')
        _grant_ledger_credit(grant.name,grant.amount)
        if grant.source_type=='Corporate':
            if not grant.corporate_pool or not cint(grant.funding_verified):
                raise ValueError('Corporate grant has no verified funding pool')
            event=_event(CORPORATE_EVENT+grant.corporate_pool,
                event_type='Corporate Advance Reclassified',source_type='Corporate',
                company=company,pool=grant.corporate_pool)
            _corporate_source(cfg,grant,event)
        elif grant.source_type=='Referral':
            event=_event(REFERRAL_EVENT+grant.name,
                event_type='Referral Credit Issued',source_type='Referral',
                company=company,grant=grant.name,member=grant.member)
            _referral_source(cfg,grant,event)
        else:
            raise ValueError('Promotion wallet accounting policy not implemented')
        if _voucher_credit(event.journal_entry,cfg[grant.source_type],company)!=money(event.amount):
            raise ValueError('Source liability GL credit voucher does not match source event')
    return totals
