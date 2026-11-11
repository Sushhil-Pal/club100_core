"""Stage 5F-D2C3B2C2A — pure synthetic wallet JE voucher contracts.

A wallet liability JE is a PAYMENT TENDER against a previously discounted
invoice. No ledger capture, receipt verification, fund creation or authorization
occurs in this module. Posting is NEVER implied by a passing contract.
"""
from decimal import Decimal
from datetime import date
from collections.abc import Mapping
from club100_core.accounting.hybrid_invoice_contract import money
from club100_core.accounting.hybrid_posting_rules import (
    build_posting_plan, validate_submitted_invoice, validate_gateway_entry,
    validate_wallet_entry,
)


def funded_plan(review, cfg, evidence):
    """Require a BOTH-source synthetic funded-wallet test; do not invent cash."""
    plan=build_posting_plan(review,cfg,evidence)
    if (money(plan['supplier_discount'])!=Decimal('100.00')
            or money(plan['invoice_amount'])!=Decimal('699.00')
            or money(plan['gateway_amount'])!=Decimal('399.00')
            or money(plan['funded_amount'])!=Decimal('300.00')
            or plan['source_amounts']!={'Corporate':'200.00','Referral':'100.00'}
            or plan['posting_authorized'] is not False):
        raise ValueError('B2C2A fixture must be an exact synthetic split payment')
    return plan


def journal_spec(plan, invoice_id, receivable, posting_date):
    """Construct a voucher shape; requires explicit source-account mapping.

    This function is pure; no ERPNext doc is instantiated or submitted here.
    """
    if not isinstance(plan,Mapping) or plan.get('posting_authorized') is not False:
        raise ValueError('Non-authorizing validated posting plan required')
    if not isinstance(invoice_id,str) or not invoice_id.strip() or len(invoice_id)>140:
        raise ValueError('Submitted invoice identity required')
    if not isinstance(receivable,str) or not receivable.strip():
        raise ValueError('Receivable account required')
    if isinstance(posting_date,date):date_text=posting_date.isoformat()
    elif isinstance(posting_date,str) and len(posting_date)==10:
        date_text=date.fromisoformat(posting_date).isoformat()
    else:raise ValueError('Valid posting date required')
    sources=plan.get('source_amounts')
    accounts=plan.get('liability_accounts')
    if not isinstance(sources,Mapping) or not isinstance(accounts,Mapping):
        raise ValueError('Source and liability accounts required')
    if set(sources)!={'Corporate','Referral'} or set(accounts)!=set(sources):
        raise ValueError('Both distinct source liabilities must be present')
    if not isinstance(plan.get('company'),str) or not plan['company'].strip():
        raise ValueError('Company required')
    used=[];rows=[];sums=Decimal('0.00')
    for source in ('Corporate','Referral'):
        account=accounts[source];amount=money(sources[source])
        if not isinstance(account,str) or not account.strip() or amount<=0:
            raise ValueError('Positive source-specific liability debit required')
        used.append(account);sums+=amount
        rows.append({'account':account, 'debit_in_account_currency':float(amount),
                     'credit_in_account_currency':0.0})
    if len(set(used+[receivable,plan.get('clearing_account')])) != 4:
        raise ValueError('Liability, receivable and gateway accounts must all be distinct')
    if sums!=money(plan.get('funded_amount')):
        raise ValueError('Debits do not equal frozen funded wallet amount')
    rows.append({'account':receivable,'party_type':'Customer',
                 'party':plan.get('customer'), 'reference_type':'Sales Invoice',
                 'reference_name':invoice_id, 'debit_in_account_currency':0.0,
                 'credit_in_account_currency':float(sums)})
    return {'doctype':'Journal Entry','company':plan['company'],
            'voucher_type':'Journal Entry','posting_date':date_text,
            'user_remark':'SYNTHETIC UAT ROLLBACK ONLY D2C3B2C2A '+invoice_id,
            'accounts':rows}


def journal_snapshot(doc):
    """Accept ERPNext computed floats ONLY for inspecting submitted vouchers."""
    return {'name':doc.name,'docstatus':doc.docstatus,'company':doc.company,
        'accounts':[{'account':r.account,'debit_in_account_currency':r.debit_in_account_currency,
                     'credit_in_account_currency':r.credit_in_account_currency,
                     'party_type':r.party_type,'party':r.party,
                     'reference_type':r.reference_type,'reference_name':r.reference_name}
                    for r in doc.accounts]}


def check_full_vouchers(review,cfg,plan,invoice,payment,journal,receivable,outstanding):
    if plan['enrollment']!=review['enrollment'] or plan['invoice_review_sha256']!=review['invoice_review_sha256']:
        raise ValueError('Mismatched review identity')
    validate_submitted_invoice(review,cfg,invoice)
    validate_gateway_entry(plan,invoice['name'],payment)
    validate_wallet_entry(plan,invoice['name'],receivable,journal)
    if money(str(outstanding))!=Decimal('0.00'):
        raise ValueError('Invoice does not clear to zero after wallet Journal Entry')
    if money(plan['gateway_amount'])+money(plan['funded_amount'])!=money(plan['invoice_amount']):
        raise ValueError('Two payment tenders must equal discounted invoice')
    return {'invoice_reconciled':True,'payment_entry_validated':True,
         'wallet_journal_validated':True,'zero_outstanding':True,
         'wallet_ledger_captured':False,'posting_authorized':False,
         'membership_activation_enabled':False}
