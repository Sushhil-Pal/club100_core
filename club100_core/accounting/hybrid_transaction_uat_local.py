"""Stage D2C3B2C1: isolated, synthetic ERPNext voucher POST+ROLLBACK trial.

Only Administrator on club100.local Razorpay TEST; disabled by default.
NO real member, enrollment, hybrid reservation or payment is read/written.
NO gateway request, no real wallet credit, no membership activation.
Submitted Sales Invoice and Payment Entry are always rolled back. This uses
ERPNext voucher hooks, which may have NON-DB side effects: run on disposable
local ERPNext with external integrations/notifications OFF, never production.
"""
import hashlib
import json
import os
from uuid import uuid4
from decimal import Decimal

import frappe
from frappe.utils import cint, getdate, nowdate
from club100_core.accounting import settlement
from club100_core.accounting.hybrid_tax_engine_probe_local import _fixture_review
from club100_core.accounting.hybrid_draft_rules import FIELDS, draft_spec, verify_calculated_draft
from club100_core.accounting.hybrid_transaction_uat_rules import check_gateway_only_plan, check_vouchers
from club100_core.accounting.hybrid_invoice_contract import money

DTYPES=('Sales Invoice','Payment Entry','Journal Entry','GL Entry')


def _local_admin():
    if frappe.local.site != 'club100.local' or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test':
        raise ValueError('D2C3B2C1 is local club100.local Razorpay TEST mode only')
    if frappe.session.user != 'Administrator':
        frappe.throw('D2C3B2C1 local Administrator bench session required',frappe.PermissionError)


def _trial_allowed():
    _local_admin()
    # Require both site and process opt-in; never open a public route.
    if os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C1_TRANSACTION_UAT') != '1':
        raise ValueError('Transactional UAT requires explicit environment opt-in')
    if cint(frappe.conf.get('club100_stage5fd2c3b2c1_uat_enabled',0)) != 1:
        raise ValueError('Transactional UAT site flag remains off')
    # Avoid mixing with actual accounting, hybrid checkout or automatic posts.
    if (cint(frappe.conf.get('club100_accounting_post_enabled',0))
            or cint(frappe.conf.get('club100_hybrid_checkout_enabled',0))
            or cint(frappe.conf.get('club100_hybrid_invoice_posting_enabled',0))):
        raise ValueError('Existing financial posting must remain disabled')


def _fixture():
    """Synthetic test review only; the named entities are intentionally nonexistent."""
    review=_fixture_review()
    review.update({'funded_wallet_payment':'0.00','corporate_source':'0.00',
        'referral_source':'0.00','razorpay_payment':'699.00'})
    evidence={'name':review['evidence_name'],'enrollment':review['enrollment'],
       'hybrid_checkout':review['hybrid_checkout'],'member':review['member'],
       'razorpay_order_id':'order_SYNTHETIC_D2C3B2C1',
       'razorpay_payment_id':'pay_SYNTHETIC_D2C3B2C1',
       'amount_paise':69900,'snapshot_sha256':review['snapshot_sha256'],
       'provider_status':'captured','state':'Verified Pending Invoice'}
    keys=('enrollment','hybrid_checkout','member','razorpay_order_id',
          'razorpay_payment_id','amount_paise','snapshot_sha256')
    evidence['identity_sha256']=hashlib.sha256(json.dumps({k:evidence[k] for k in keys},
          sort_keys=True,separators=(',',':')).encode()).hexdigest()
    review['evidence_identity_sha256']=evidence['identity_sha256']
    review['invoice_review_sha256']=hashlib.sha256(json.dumps({k:review[k] for k in FIELDS},
          sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return review,evidence


def _accounting_setup():
    cfg=settlement._config()
    # Company, Customer, Item, tax rows, clearing account checked against ERPNext.
    taxes=settlement._ready(cfg,{},Decimal('699.00'))
    if not cfg.get('razorpay_clearing'):
        raise ValueError('Razorpay clearing account is not configured')
    if money('699.00') <= 0:raise AssertionError('Invalid fixture')
    return cfg,taxes


def _snapshot(doc):
    return {key:getattr(doc,key,None) for key in ('name','docstatus','company',
        'customer','currency','apply_discount_on','discount_amount','grand_total',
        'net_total','total_taxes_and_charges','outstanding_amount') } | {
        'items':[{'item_code':row.item_code,'rate':row.rate} for row in doc.items]}


def _payment_snapshot(doc):
    return {'docstatus':doc.docstatus,'payment_type':doc.payment_type,
        'party_type':doc.party_type,'party':doc.party,'paid_to':doc.paid_to,
        'reference_no':doc.reference_no,'paid_amount':doc.paid_amount,
        'received_amount':doc.received_amount,
        'references':[{'reference_doctype':r.reference_doctype,
           'reference_name':r.reference_name,'allocated_amount':r.allocated_amount}
           for r in doc.references]}


def healthcheck():
    _local_admin()
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C1',
        'synthetic_voucher_uat_installed':True,
        'transaction_uat_site_enabled':bool(cint(frappe.conf.get('club100_stage5fd2c3b2c1_uat_enabled',0))),
        'transaction_uat_process_enabled':os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C1_TRANSACTION_UAT')=='1',
        'no_public_endpoint':True,'live_checkout_wired':False,
        'real_wallet_capture_enabled':False,'membership_activation_enabled':False,
        'production_ready':False,'posting_authorized':False}


def trial():
    """Optionally submits synthetic Sales Invoice and PE; ALWAYS rolls back.

    No commit path, public API or real user/payment IDs. Run on an ISOLATED
    disposable development site only; this is not a tax/legal approval.
    """
    _trial_allowed()
    cfg,taxes=_accounting_setup()
    review,evidence=_fixture()
    plan=check_gateway_only_plan(review,cfg,evidence)
    datestr=str(getdate(nowdate()))
    spec=draft_spec(review,cfg,taxes,datestr)
    spec['remarks']='SYNTHETIC / ROLLBACK ONLY / D2C3B2C1 / NEVER BILL: '+uuid4().hex
    spec['set_posting_time']=1
    baseline={kind:frappe.db.count(kind) for kind in DTYPES}
    savepoint='club100_d2c3b2c1_'+uuid4().hex[:10]
    frappe.db.savepoint(savepoint)
    snapshot=None; result=None; invoice_id=None;payment_id=None
    try:
        invoice=frappe.get_doc(spec)
        invoice.insert(ignore_permissions=True)
        verify_calculated_draft(review,_snapshot(invoice),cfg)
        if invoice.docstatus != 0:raise ValueError('Expected draft status before submission')
        invoice.submit()
        if invoice.docstatus != 1:raise ValueError('Submitted invoice required')
        invoice_id=invoice.name
        before=_snapshot(invoice)
        check_invoice = dict(before)
        if money(str(before.get('outstanding_amount'))) != money(plan['invoice_amount']):
            raise ValueError('Unexpected outstanding after invoice submission')
        from club100_core.accounting.hybrid_posting_rules import validate_submitted_invoice
        validate_submitted_invoice(review,cfg,check_invoice)
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
        payment=get_payment_entry('Sales Invoice',invoice_id,party_amount=float(money(plan['gateway_amount'])))
        payment.paid_to=plan['clearing_account']
        payment.paid_amount=float(money(plan['gateway_amount']))
        payment.received_amount=float(money(plan['gateway_amount']))
        payment.reference_no=plan['payment_id']
        payment.reference_date=getdate(nowdate())
        matches=[r for r in payment.references if r.reference_doctype=='Sales Invoice' and r.reference_name==invoice_id]
        if len(matches)!=1 or len(payment.references)!=1:
            raise ValueError('Payment Entry must only reference this synthetic invoice')
        payment.references[0].allocated_amount=float(money(plan['gateway_amount']))
        payment.insert(ignore_permissions=True)
        payment.submit()
        if payment.docstatus != 1:raise ValueError('Submitted Payment Entry required')
        payment_id=payment.name
        after=frappe.db.get_value('Sales Invoice',invoice_id,'outstanding_amount')
        result=check_vouchers(review,cfg,plan,before,_payment_snapshot(payment),after)
        result.update({'outcome':'PASS','invoice_submitted_within_savepoint':True,
            'payment_submitted_within_savepoint':True,
            'test_invoice':invoice_id,'test_payment_entry':payment_id})
    finally:
        frappe.db.rollback(save_point=savepoint)
    residual={kind:frappe.db.count(kind) for kind in DTYPES}
    if residual!=baseline:
        raise ValueError('ROLLBACK INTEGRITY FAILURE: accounting record counts changed')
    if invoice_id and (frappe.db.exists('Sales Invoice',invoice_id) or frappe.db.exists('Payment Entry',payment_id)):
        raise ValueError('ROLLBACK INTEGRITY FAILURE: synthetic voucher still exists')
    return {**result,'site':frappe.local.site,'stage':'5F-D2C3B2C1',
        'no_gateway_request':True,'synthetic_payment_only':True,
        'record_counts_unchanged':True,'all_vouchers_rolled_back':True,
        'wallet_journal_tested':False,'no_real_wallet_capture':True,
        'membership_activation_enabled':False,'posting_authorized':False,
        'production_ready':False}
