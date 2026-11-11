"""Stage 5F-A: evidence-linked corporate and referral GL recognition (local pilot).

Intentional boundaries:
* Corporate: requires an *already submitted* ERPNext customer advance Payment Entry
  received into a real Bank-type account, and reclassifies receivables credit to
  corporate wallet liability exactly once. It does NOT create or verify bank cash.
* Referral: accrues expense/liability for a *matured, issued* wallet credit only
  after the underlying membership's paid Sales Invoice is submitted.
* Promotional credits, refunds, expiry and historical grants are REPORT ONLY;
  Finance must approve their treatment before they can be posted.
* This is non-whitelisted, local test-mode only, and separately gated. No hooks.
"""
from contextlib import contextmanager
from decimal import Decimal
import frappe
from frappe.utils import cint, getdate, nowdate, now_datetime

from club100_core.accounting.rules import money
from club100_core.accounting.settlement import _admin, _config, _company_check, _local_pilot
from club100_core.accounting.funding_rules import (
    require_pool_receipt, require_grant_credit, journal_rows_for_corporate,
    journal_rows_for_referral,
)

EVENT_DT = 'Club100 Wallet Accounting Event'
CORPORATE_EVENT = 'CLUB100:CORP:'
REFERRAL_EVENT = 'CLUB100:REFERRAL:'


def _post_allowed():
    """Explicit Administrator-run accounting booking, local Test Mode only.

    No stage-level operational feature flags. Receipt, document, source-GL,
    pool-identity and duplicate-event checks remain mandatory in the posting
    functions and cannot be bypassed by this guard.
    """
    return bool(_local_pilot())


def _require_post():
    _admin()
    if not _post_allowed():
        raise ValueError('Funding booking is restricted to club100.local in Razorpay Test Mode')


@contextmanager
def _event_write():
    old = getattr(frappe.flags, 'club100_wallet_accounting_internal', False)
    frappe.flags.club100_wallet_accounting_internal = True
    try:
        yield
    finally:
        frappe.flags.club100_wallet_accounting_internal = old


def _gl_amount(voucher_type, voucher_no, account, side, *, company, party=None):
    if side not in ('credit', 'debit'):
        raise ValueError('Invalid GL column')
    conditions = (' AND party=%s' if party is not None else '')
    values = (voucher_type, voucher_no, account, company) + ((party,) if party is not None else ())
    row = frappe.db.sql('''SELECT COALESCE(SUM(`''' + side + '''`),0) AS amount
        FROM `tabGL Entry`
        WHERE voucher_type=%s AND voucher_no=%s AND account=%s
        AND company=%s AND is_cancelled=0''' + conditions, values, as_dict=True)[0]
    return money(row.amount)


def _account(name, company, expected_root=None, expected_type=None):
    _company_check('Account', name, company)
    row = frappe.db.get_value('Account', name, ['root_type', 'account_type', 'company'], as_dict=True)
    if expected_root and row.root_type != expected_root:
        raise ValueError('Wrong account root for ' + name + ': expected ' + expected_root)
    if expected_type and row.account_type != expected_type:
        raise ValueError('Wrong account type for ' + name + ': expected ' + expected_type)
    return row


def _corporate_proof(pool_name, payment_entry_name):
    cfg = _config()
    _company_check('Company', cfg['company'], cfg['company'])
    _account(cfg['Corporate'], cfg['company'], expected_root='Liability')
    pool = frappe.get_doc('Club100 Corporate Credit Pool', str(pool_name))
    # New pools start Awaiting Funding. The Desk confirmation action validates
    # the submitted receipt and posts GL *before* marking them Funded.
    pending = (pool.status == 'Awaiting Funding' and not cint(pool.funding_verified))
    funded = (pool.status == 'Funded' and cint(pool.funding_verified))
    if not (pending or funded) or pool.currency != 'INR' or money(pool.amount) <= 0:
        raise ValueError('Pool funding status is invalid or has been partially confirmed')
    if not frappe.db.exists('Customer', pool.corporate_customer):
        raise ValueError('Corporate Customer does not exist')
    pe = frappe.get_doc('Payment Entry', str(payment_entry_name))
    if (pe.docstatus != 1 or pe.company != cfg['company']
            or pe.payment_type != 'Receive' or pe.party_type != 'Customer'
            or pe.party != pool.corporate_customer or pe.paid_from == pe.paid_to):
        raise ValueError('Payment Entry is not a submitted corporate customer receipt')
    _account(pe.paid_to, cfg['company'], expected_root='Asset', expected_type='Bank')
    _account(pe.paid_from, cfg['company'], expected_root='Asset', expected_type='Receivable')
    if pe.paid_to == cfg['razorpay_clearing']:
        raise ValueError('Corporate bank funding cannot be represented by Razorpay Clearing')
    if money(pe.paid_amount) != money(pe.received_amount):
        raise ValueError('Corporate receipt has unhandled exchange difference')
    prior = frappe.get_all(EVENT_DT, filters={
        'event_type':'Corporate Advance Reclassified','payment_entry':pe.name,
        'status':'Posted'}, fields=['amount'])
    already_linked = sum((money(row.amount) for row in prior), Decimal('0.00'))
    require_pool_receipt(pool.amount, pe.received_amount, pe.unallocated_amount, already_linked,
                         pool_currency=pool.currency, receipt_currency=pe.paid_to_account_currency,
                         same_customer=pe.party == pool.corporate_customer,
                         same_reference=(str(pe.reference_no or '').strip() == str(pool.funding_reference or '').strip()
                                         and len(str(pool.funding_reference or '').strip()) >= 6),
                         submitted=pe.docstatus == 1,
                         is_real_bank=pe.paid_to != cfg['razorpay_clearing'])
    if (_gl_amount('Payment Entry', pe.name, pe.paid_to, 'debit', company=cfg['company']) < money(pool.amount)
        or _gl_amount('Payment Entry', pe.name, pe.paid_from, 'credit',
                      company=cfg['company'], party=pool.corporate_customer) < money(pool.amount)):
        raise ValueError('Submitted Payment Entry GL does not contain matching bank/receivable legs')
    return cfg, pool, pe


def corporate_pool_diagnostic(pool_name):
    """Read-only diagnostics to locate a matching submitted employer receipt.

    A matching ERPNext voucher is not evidence that the payment cleared the
    external bank. Finance must independently reconcile that before production.
    """
    _admin()
    pool = frappe.db.get_value('Club100 Corporate Credit Pool', str(pool_name),
        ['name', 'corporate_customer', 'funding_reference', 'amount', 'currency',
         'funding_verified', 'status'], as_dict=True)
    if not pool:
        raise ValueError('Corporate credit pool not found: ' + str(pool_name))
    event_key = CORPORATE_EVENT + str(pool_name)
    event = frappe.db.get_value(EVENT_DT, {'event_key':event_key},
        ['name', 'status', 'journal_entry', 'payment_entry', 'amount'], as_dict=True)
    candidate_receipts = []
    if pool.corporate_customer and pool.funding_reference:
        rows = frappe.get_all('Payment Entry', filters={
            'docstatus': 1, 'company': _config()['company'],
            'payment_type': 'Receive', 'party_type': 'Customer',
            'party': pool.corporate_customer,
            'reference_no': pool.funding_reference,
        }, fields=['name', 'paid_from', 'paid_to', 'paid_amount', 'received_amount',
                   'unallocated_amount', 'reference_no', 'docstatus'],
           limit_page_length=30)
        for row in rows:
            candidate_receipts.append({
                'payment_entry': row.name,
                'received_amount': str(money(row.received_amount)),
                'unallocated_amount': str(money(row.unallocated_amount)),
                'bank_account': row.paid_to,
                'receivable_account': row.paid_from,
            })
    return {
        'pool': str(pool_name), 'corporate_customer': pool.corporate_customer,
        'amount': str(money(pool.amount)), 'funding_reference': pool.funding_reference,
        'local_test_reference': str(pool.funding_reference or '').upper().startswith('LOCAL-'),
        'currency': pool.currency, 'pool_status': pool.status,
        'funding_verified_flag': bool(cint(pool.funding_verified)),
        'posted_source_event': dict(event) if event else None,
        'matching_submitted_receipts': candidate_receipts,
        'posting_available_on_this_site': _post_allowed(),
        'read_only': True,
        'caution': 'A LOCAL reference or ERPNext Payment Entry alone does not prove external bank settlement. Never fabricate real-company receipts.',
    }


def corporate_receipt_preview(pool_name, payment_entry_name):
    """Read-only. A Payment Entry in GL is NOT proof it cleared the bank statement."""
    _admin()
    event_key = CORPORATE_EVENT + str(pool_name)
    existing = frappe.db.get_value(EVENT_DT, {'event_key':event_key}, ['journal_entry','payment_entry'], as_dict=True)
    issues=[]; pool=None; pe=None; cfg=_config()
    try:
        cfg, pool, pe = _corporate_proof(pool_name, payment_entry_name)
    except Exception as exc:
        issues.append(str(exc))
    if existing:
        issues.append('Pool funding has already been reclassified: ' + existing.journal_entry)
    return {'pool':str(pool_name), 'receipt':str(payment_entry_name),
            'amount':str(money(pool.amount)) if pool else None,
            'company':cfg['company'], 'status':'Ready for controlled UAT' if not issues else 'Blocked',
            'issues':issues, 'existing_accounting_event':bool(existing),
            'bank_statement_reconciled':False,
            'caution':'Submitted Payment Entry + GL are required; bank-statement reconciliation and Finance sign-off are separate production controls.',
            'preview_only':True}


def _post_journal(cfg, rows, remark, when=None):
    entry = frappe.get_doc({
        'doctype':'Journal Entry', 'company':cfg['company'], 'voucher_type':'Journal Entry',
        'posting_date':getdate(when or nowdate()), 'user_remark':str(remark)[:500],
        'accounts':rows,
    })
    entry.insert(ignore_permissions=True)
    entry.submit()
    if entry.docstatus != 1:
        raise ValueError('Journal Entry did not submit')
    return entry


def _add_event(values):
    with _event_write():
        event = frappe.get_doc({'doctype':EVENT_DT, **values, 'status':'Posted', 'currency':'INR'})
        event.insert(ignore_permissions=True)
    return event


def post_corporate_receipt(pool_name, payment_entry_name, commit=True, verified_by=None):
    """Local-only, idempotent reclass of actual submitted customer advance into wallet liability.

    This never fabricates a cash receipt. The Payment Entry must already be in
    ERPNext and backed by its own GL. Production requires bank reconciliation.
    """
    _require_post()
    event_key = CORPORATE_EVENT + str(pool_name)
    frappe.db.sql('SELECT name FROM `tabClub100 Corporate Credit Pool` WHERE name=%s FOR UPDATE', str(pool_name))
    frappe.db.sql('SELECT name FROM `tabPayment Entry` WHERE name=%s FOR UPDATE', str(payment_entry_name))
    existing = frappe.db.get_value(EVENT_DT, {'event_key':event_key},
                                   ['name','payment_entry','journal_entry'], as_dict=True)
    if existing:
        if existing.payment_entry != str(payment_entry_name):
            raise ValueError('Corporate pool already linked to another receipt')
        if cint(frappe.db.get_value('Journal Entry', existing.journal_entry, 'docstatus')) != 1:
            raise ValueError('Existing corporate event GL voucher was cancelled or removed')
        return {'status':'Already Posted','event':existing.name,'journal_entry':existing.journal_entry}
    cfg, pool, pe = _corporate_proof(pool_name, payment_entry_name)
    point='club100_stage5f_corporate'
    frappe.db.savepoint(point)
    try:
        rows=journal_rows_for_corporate(pe.paid_from, pool.corporate_customer,
                                        cfg['Corporate'], pool.amount)
        journal=_post_journal(cfg, rows, 'Club100 corporate funding advance ' + pool.name + ' / ' + pe.name,
                              when=pe.posting_date)
        if _gl_amount('Journal Entry', journal.name, cfg['Corporate'], 'credit',
                      company=cfg['company']) != money(pool.amount):
            raise ValueError('Corporate liability GL voucher amount mismatch')
        event=_add_event(dict(event_key=event_key, event_type='Corporate Advance Reclassified',
            source_type='Corporate', company=cfg['company'], corporate_pool=pool.name,
            payment_entry=pe.name, journal_entry=journal.name, amount=pool.amount,
            notes='Corporate receipt advance reclassified; external bank reconciliation still required'))
        if pool.status == 'Awaiting Funding':
            # Same transaction as the JE and source event. Never publish Funded
            # before the liability entry is submitted and its GL verified.
            frappe.db.set_value('Club100 Corporate Credit Pool', pool.name, {
                'status': 'Funded', 'funding_verified': 1,
                'verified_by': verified_by or frappe.session.user,
                'verified_at': now_datetime(),
            }, update_modified=False)
        if commit:
            frappe.db.commit()
        return {'status':'Posted','event':event.name,'journal_entry':journal.name,
                'pool':pool.name,'liability_credit':str(money(pool.amount)),
                'bank_statement_reconciled':False}
    except Exception:
        frappe.db.rollback(save_point=point)
        raise


def _referral_proof(grant_name):
    cfg=_config()
    _company_check('Company', cfg['company'], cfg['company'])
    _account(cfg['Referral'], cfg['company'], expected_root='Liability')
    expense = str(frappe.conf.get('club100_accounting_referral_expense_account') or '').strip()
    _account(expense, cfg['company'], expected_root='Expense')
    grant=frappe.get_doc('Club100 Wallet Grant', str(grant_name))
    if grant.source_type!='Referral' or not grant.referral_reward or not grant.member:
        raise ValueError('Not a referral-funded member wallet grant')
    reward=frappe.get_doc('Club100 Referral Reward', grant.referral_reward)
    if (reward.status!='Available' or reward.reward_track!='Member Credit'
            or reward.basis_verification_status!='Verified'
            or reward.settlement_reference!=grant.name
            or money(reward.verified_reward_amount)!=money(grant.amount)):
        raise ValueError('Reward is not verified, matured and linked to issued grant')
    enrollment=frappe.get_doc('Club100 Enrollment', reward.enrollment)
    if enrollment.payment_status!='Paid' or not enrollment.sales_invoice:
        raise ValueError('Referred enrollment does not have a paid accounting invoice')
    invoice=frappe.get_doc('Sales Invoice', enrollment.sales_invoice)
    if (invoice.docstatus!=1 or money(invoice.outstanding_amount)!=0
            or invoice.company!=cfg['company']):
        raise ValueError('Referred membership Sales Invoice is unpaid, unsubmitted or wrong company')
    ledger=frappe.db.sql('''SELECT COALESCE(SUM(CASE WHEN entry_type='Credit' THEN amount ELSE 0 END),0) AS credit,
      COALESCE(SUM(amount),0) AS net,
      SUM(CASE WHEN entry_type IN ('Spend','Reversal') THEN 1 ELSE 0 END) AS taken
      FROM `tabClub100 Wallet Ledger` WHERE `grant`=%s''', grant.name, as_dict=True)[0]
    require_grant_credit(grant.amount, ledger.credit, ledger.net, available=True,
                         no_prior_spend=int(ledger.taken or 0)==0,
                         confirmed_invoice=True)
    return cfg, grant, reward, invoice, expense


def referral_issuance_preview(grant_name):
    _admin()
    key=REFERRAL_EVENT+str(grant_name)
    existing=frappe.db.get_value(EVENT_DT, {'event_key':key},'journal_entry')
    issues=[]; amount=None
    try:
        _cfg, grant, _reward, _invoice, _expense=_referral_proof(grant_name)
        amount=str(money(grant.amount))
    except Exception as exc:
        issues.append(str(exc))
    if existing:
        issues.append('Referral issuance already booked: '+existing)
    return {'grant':str(grant_name),'amount':amount,
            'status':'Ready for controlled UAT' if not issues else 'Blocked',
            'issues':issues, 'existing_journal':existing,
            'preview_only':True}


def post_referral_issuance(grant_name, commit=True):
    """Local-only expense + liability GL booking for existing matured/issued credit."""
    _require_post()
    key=REFERRAL_EVENT+str(grant_name)
    frappe.db.sql('SELECT name FROM `tabClub100 Wallet Grant` WHERE name=%s FOR UPDATE', str(grant_name))
    existing=frappe.db.get_value(EVENT_DT, {'event_key':key}, ['name','journal_entry'], as_dict=True)
    if existing:
        if cint(frappe.db.get_value('Journal Entry', existing.journal_entry, 'docstatus'))!=1:
            raise ValueError('Existing referral GL voucher was cancelled or removed')
        return {'status':'Already Posted','event':existing.name,'journal_entry':existing.journal_entry}
    cfg, grant, reward, _invoice, expense=_referral_proof(grant_name)
    point='club100_stage5f_referral'
    frappe.db.savepoint(point)
    try:
        rows=journal_rows_for_referral(expense,cfg['Referral'],grant.amount)
        journal=_post_journal(cfg,rows,'Club100 matured referral credit '+grant.name+' / '+reward.name,
                              when=grant.valid_from)
        if _gl_amount('Journal Entry', journal.name,cfg['Referral'],'credit',
                      company=cfg['company'])!=money(grant.amount):
            raise ValueError('Referral credit GL voucher amount mismatch')
        event=_add_event(dict(event_key=key,event_type='Referral Credit Issued',
            source_type='Referral',company=cfg['company'],grant=grant.name,
            referral_reward=reward.name,member=grant.member,
            journal_entry=journal.name,amount=grant.amount,
            notes='Referral credit issued after verified payment, invoice and reward maturity'))
        if commit:
            frappe.db.commit()
        return {'status':'Posted','event':event.name,'journal_entry':journal.name,
                'grant':grant.name,'liability_credit':str(money(grant.amount))}
    except Exception:
        frappe.db.rollback(save_point=point)
        raise


def readiness_report(limit=100):
    """Read-only per-source provenance; do not treat UAT verified flags as cash proof."""
    _admin()
    if type(limit) is not int or limit<1 or limit>500:
        raise ValueError('limit must be int 1..500')
    cfg=_config()
    pools=frappe.get_all('Club100 Corporate Credit Pool',
                        fields=['name','amount','funding_reference','funding_verified'],
                        limit_page_length=limit,order_by='creation asc')
    grants=frappe.get_all('Club100 Wallet Grant',
                         fields=['name','member','source_type','amount','corporate_pool','referral_reward'],
                         limit_page_length=limit,order_by='creation asc')
    events=frappe.get_all(EVENT_DT,fields=['event_key','event_type','amount','corporate_pool','grant',
                                          'journal_entry','payment_entry'],limit_page_length=limit)
    pool_linked={r.corporate_pool for r in events if r.corporate_pool}
    grant_linked={r.grant for r in events if r.grant}
    unbooked_pools=[{'pool':p.name,'amount':str(money(p.amount)),
                     'funding_verified_flag':bool(p.funding_verified),
                     'local_test_reference':str(p.funding_reference or '').upper().startswith('LOCAL-')}
                    for p in pools if p.name not in pool_linked]
    unbooked_grants=[{'grant':g.name,'source_type':g.source_type,
                      'amount':str(money(g.amount)), 'reason':
                      'Corporate pool not GL-backed' if g.source_type=='Corporate' else
                      'Referral credit has no GL issuance voucher' if g.source_type=='Referral' else
                      'Promotion policy and GL treatment unapproved'}
                     for g in grants if (g.source_type=='Corporate' and g.corporate_pool not in pool_linked)
                     or (g.source_type=='Referral' and g.name not in grant_linked)
                     or g.source_type=='Promotion']
    gl={}
    for source in ('Corporate','Referral','Promotion'):
        account=cfg[source]
        if account and frappe.db.exists('Account',account):
            row=frappe.db.sql('''SELECT COALESCE(SUM(credit-debit),0) AS balance
              FROM `tabGL Entry` WHERE account=%s AND company=%s AND is_cancelled=0''',
              (account,cfg['company']),as_dict=True)[0]
            gl[source]={'account':account,'credit_balance':str(money(row.balance))}
        else:
            gl[source]={'account':account or None,'credit_balance':None}
    missing_refunds=frappe.get_all('Club100 Membership Accounting',filters={'status':'Refunded',
        'credit_note':['is','not set']},pluck='enrollment',limit_page_length=limit)
    return {'site':frappe.local.site,'posting_enabled':_post_allowed(),
        'pools_reviewed':len(pools),'grants_reviewed':len(grants), 'events_reviewed':len(events),
        'unbooked_corporate_pools':unbooked_pools,'unbooked_wallet_grants':unbooked_grants,
        'liability_gl':gl,'accounted_refund_missing_credit_notes':missing_refunds,
        'promotion_auto_post_enabled':False,'expiry_auto_post_enabled':False,
        'refund_auto_gl_post_enabled':False,'bank_statement_reconciled':False,
        'legacy_uat_reconciliation_required':bool(unbooked_pools or unbooked_grants),
        'production_ready':False,
        'caution':'Source records and GL-backed events are separate; no UAT funding labels are accepted as proof of actual bank settlement. Existing historical wallet spends require additional reconciliation.'}


def healthcheck():
    _admin()
    return {'site':frappe.local.site, 'pilot_site':_local_pilot(),
            'posting_enabled':_post_allowed(),
            'corporate_receipt_requires_submitted_payment_entry':True,
            'referral_issuance_requires_paid_sales_invoice':True,
            'promotion_accounting_awaits_finance_policy':True,
            'automatic_refund_gl_posting':False,
            'automatic_expiry_gl_posting':False,
            'production_ready':False}
