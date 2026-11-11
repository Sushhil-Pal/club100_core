"""D2C3B2C2A: OPTIONAL synthetic Sales Invoice + PE + wallet JE rollback trial.

This only validates ERPNext voucher mechanics with TEST values. The source
liability accounts MAY NOT be GL-backed in this local database. The trial NEVER
uses member grants, existing holds, actual Razorpay, ledger capture or enrollment.
Submitting synthetic docs can trigger non-DB side effects: only run this on an
isolated DISPOSABLE local ERPNext with outbound integrations switched off.
"""
import hashlib
import json
import os
from decimal import Decimal
from uuid import uuid4
import frappe
from frappe.utils import cint,getdate,nowdate
from club100_core.accounting import settlement
from club100_core.accounting.hybrid_tax_engine_probe_local import _fixture_review
from club100_core.accounting.hybrid_draft_rules import FIELDS, draft_spec, verify_calculated_draft
from club100_core.accounting.hybrid_wallet_je_rules import (
    funded_plan, journal_spec, journal_snapshot, check_full_vouchers)
from club100_core.accounting.hybrid_transaction_uat_local import _snapshot, _payment_snapshot
from club100_core.accounting.hybrid_invoice_contract import money
from club100_core.accounting.hybrid_posting_rules import validate_submitted_invoice

DTYPES=('Sales Invoice','Payment Entry','Journal Entry','GL Entry','Club100 Wallet Ledger',
        'Club100 Wallet Reservation','Club100 Wallet Grant','Club100 Enrollment')


def _guard():
    if (frappe.local.site!='club100.local' or frappe.session.user!='Administrator'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower()!='test'):
        raise ValueError('Local Administrator with Razorpay Test Mode required')


def _allowed():
    _guard()
    if (os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C2A_TRANSACTION_UAT')!='1'
            or cint(frappe.conf.get('club100_stage5fd2c3b2c2a_uat_enabled',0))!=1):
        raise ValueError('Optional isolated UAT is disabled by default')
    for key in ('club100_accounting_post_enabled','club100_hybrid_checkout_enabled',
                'club100_hybrid_invoice_posting_enabled','club100_accounting_funding_post_enabled'):
        if cint(frappe.conf.get(key,0)):
            raise ValueError('Financial production-posting flags must stay disabled')


def _fixture():
    review=_fixture_review() # fixed synthetic ₹799-₹100=₹699; wallet ₹300; gateway ₹399
    evidence={'name':review['evidence_name'],'enrollment':review['enrollment'],
      'member':review['member'],'hybrid_checkout':review['hybrid_checkout'],
      'razorpay_order_id':'order_SYNTHETIC_B2C2A',
      'razorpay_payment_id':'pay_SYNTHETIC_B2C2A','amount_paise':39900,
      'snapshot_sha256':review['snapshot_sha256'],
      'provider_status':'captured','state':'Verified Pending Invoice'}
    keys=('enrollment','hybrid_checkout','member','razorpay_order_id',
          'razorpay_payment_id','amount_paise','snapshot_sha256')
    evidence['identity_sha256']=hashlib.sha256(json.dumps(
       {k:evidence[k] for k in keys},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    review['evidence_identity_sha256']=evidence['identity_sha256']
    review['invoice_review_sha256']=hashlib.sha256(json.dumps(
       {k:review[k] for k in FIELDS},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return review,evidence


def _setup():
    cfg=settlement._config()
    taxes=settlement._ready(cfg,{'Corporate':Decimal('200.00'), 'Referral':Decimal('100.00')},Decimal('399.00'))
    for source in ('Corporate','Referral'):
        row=frappe.db.get_value('Account',cfg[source],('root_type','is_group','account_currency'),as_dict=True)
        if not row or row.root_type!='Liability' or cint(row.is_group):
            raise ValueError(source+' account must be a non-group Liability account')
        if row.account_currency!='INR':
            raise ValueError(source+' liability account must be INR-denominated')
    if len({cfg['Corporate'],cfg['Referral'],cfg['razorpay_clearing']})!=3:
        raise ValueError('Liability and Razorpay clearing accounts must differ')
    return cfg,taxes


def healthcheck():
    _guard()
    cfg=settlement._config()
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C2A',
      'voucher_trial_installed':True,
      'configured':{k:bool(cfg.get(k)) for k in ('company','customer','item','tax_template',
        'razorpay_clearing','Corporate','Referral')},
      'transaction_uat_site_enabled':bool(cint(frappe.conf.get('club100_stage5fd2c3b2c2a_uat_enabled',0))),
      'trial_process_opt_in':os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C2A_TRANSACTION_UAT')=='1',
      'real_grant_backing_checked':False,'wallet_capture_enabled':False,
      'invoice_posting_enabled':False,'membership_activation_enabled':False,
      'production_ready':False,'posting_authorized':False}


def trial():
    """Run ONLY on isolated throwaway DB. Submit then ALWAYS rollback 3 vouchers.

    This is a voucher-structure test, NOT a true funded-credit settlement test.
    """
    _allowed()
    cfg,taxes=_setup()
    review,evidence=_fixture()
    plan=funded_plan(review,cfg,evidence)
    spec=draft_spec(review,cfg,taxes,str(getdate(nowdate())))
    spec['remarks']='SYNTHETIC / ROLLBACK ONLY / B2C2A / DO NOT BILL: '+uuid4().hex
    spec['set_posting_time']=1
    baseline={d:frappe.db.count(d) for d in DTYPES}
    savepoint='club100_b2c2a_'+uuid4().hex[:12]
    frappe.db.savepoint(savepoint)
    invoice_id=payment_id=journal_id=None
    result=None
    try:
        invoice=frappe.get_doc(spec)
        invoice.insert(ignore_permissions=True)
        verify_calculated_draft(review,_snapshot(invoice),cfg)
        invoice.submit()
        invoice_id=invoice.name
        inv=_snapshot(invoice)
        validate_submitted_invoice(review,cfg,inv)
        if money(str(inv['outstanding_amount']))!=Decimal('699.00'):
            raise ValueError('Invoice must be initially fully outstanding')
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
        payment=get_payment_entry('Sales Invoice', invoice_id,party_amount=399.0)
        payment.paid_to=plan['clearing_account'];payment.paid_amount=399.0
        payment.received_amount=399.0
        payment.reference_no=plan['payment_id']
        payment.reference_date=getdate(nowdate())
        rows=[r for r in payment.references if r.reference_doctype=='Sales Invoice' and r.reference_name==invoice_id]
        if len(rows)!=1 or len(payment.references)!=1:
            raise ValueError('Payment Entry requires exactly one invoice row')
        payment.references[0].allocated_amount=399.0
        payment.insert(ignore_permissions=True)
        payment.submit();payment_id=payment.name
        after_payment=money(str(frappe.db.get_value('Sales Invoice',invoice_id,'outstanding_amount')))
        if after_payment!=Decimal('300.00'):
            raise ValueError('Razorpay clearing must leave exactly ₹300 outstanding')
        j_spec=journal_spec(plan,invoice_id,invoice.debit_to,str(getdate(nowdate())))
        j_spec['user_remark']+=' '+uuid4().hex
        journal=frappe.get_doc(j_spec)
        journal.insert(ignore_permissions=True)
        journal.submit();journal_id=journal.name
        if journal.docstatus!=1:raise ValueError('Journal Entry did not submit')
        remaining=frappe.db.get_value('Sales Invoice',invoice_id,'outstanding_amount')
        result=check_full_vouchers(review,cfg,plan,inv,_payment_snapshot(payment),
             journal_snapshot(journal),invoice.debit_to,remaining)
        result.update({'outcome':'PASS','synthetic_invoice_submitted':True,
              'synthetic_payment_submitted':True,'synthetic_wallet_journal_submitted':True,
              'after_gateway_outstanding':'300.00','test_invoice':invoice_id,
              'test_payment_entry':payment_id,'test_wallet_journal_entry':journal_id})
    finally:
        frappe.db.rollback(save_point=savepoint)
    residual={d:frappe.db.count(d) for d in DTYPES}
    if residual!=baseline:raise ValueError('ROLLBACK INTEGRITY FAILURE: record counts changed')
    for dt,name in (('Sales Invoice',invoice_id),('Payment Entry',payment_id),('Journal Entry',journal_id)):
        if name and frappe.db.exists(dt,name):
            raise ValueError('ROLLBACK INTEGRITY FAILURE: a synthetic voucher still exists')
    return {**result, 'site':frappe.local.site,'stage':'5F-D2C3B2C2A',
        'record_counts_unchanged':True,'all_vouchers_rolled_back':True,
        'synthetic_credit_accounts_only':True,'backed_wallet_grants_verified':False,
        'wallet_captures':0,'gateway_calls':0,'membership_activations':0,
        'posting_authorized':False,'production_ready':False}
