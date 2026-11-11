"""Local-only pure regression smoke; no accounts, wallet, or invoices touched."""
import hashlib
import json
import os
import frappe
from club100_core.accounting.hybrid_draft_rules import draft_spec, verify_calculated_draft, FIELDS


def _fixture():
    review={'enrollment':'SMOKE-ENR-1','member':'SMOKE-MEM-1',
       'hybrid_checkout':'SMOKE-HYB-1','snapshot_sha256':'a'*64,
       'evidence_name':'SMOKE-EVD-1','evidence_identity_sha256':'b'*64,
       'original_price_incl_gst':'799.00','supplier_discount_incl_gst':'100.00',
       'invoice_total_incl_gst':'699.00','taxable_value_projected':'665.71',
       'gst_projected':'33.29','funded_wallet_payment':'300.00',
       'razorpay_payment':'399.00','corporate_source':'200.00','referral_source':'100.00',
       'posting_authorized':False,'no_automatic_tax_or_invoice_authorization':True,
       'submitted_invoice_verified':False}
    review['invoice_review_sha256']=hashlib.sha256(json.dumps(
        {k:review[k] for k in FIELDS},sort_keys=True,
        separators=(',',':')).encode()).hexdigest()
    cfg={'company':'Test Company','customer':'Test Customer',
         'item':'Test Service','tax_template':'Test GST 5'}
    taxes=[{'charge_type':'On Net Total','account_head':'Test GST Account',
            'description':'GST','rate':5,'included_in_print_rate':1}]
    return review,cfg,taxes


def run():
    if frappe.local.site!='club100.local' or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B1_SMOKE')!='1':
        raise ValueError('Local smoke opt-in and club100.local required')
    if frappe.session.user!='Administrator':
        raise ValueError('Administrator only')
    review,cfg,taxes=_fixture()
    spec=draft_spec(review,cfg,taxes,'2026-11-04')
    cases=[]
    assert spec['discount_amount']==100.0 and spec['apply_discount_on']=='Grand Total'
    cases.append('Supplier discount encoded separately from funded wallet payments')
    fake={**spec,'docstatus':0,'grand_total':'699.00','net_total':'665.71',
          'total_taxes_and_charges':'33.29'}
    assert verify_calculated_draft(review,fake,cfg)['draft_verified']
    cases.append('Expected inclusive GST, net, and discounted grand total reconcile')
    for label,change in [('Undiscounted invoice',{'grand_total':'799.00'}),
                         ('Wrong GST',{'total_taxes_and_charges':'38.05'}),
                         ('Submitted invoice',{'docstatus':1}),
                         ('Wrong wallet/customer',{'customer':'Other Customer'})]:
        try:
            verify_calculated_draft(review,{**fake,**change},cfg)
            raise AssertionError('Not rejected: '+label)
        except ValueError:
            cases.append(label+' rejected')
    assert len(cases)==6
    return {'site':frappe.local.site,'stage':'5F-D2C3B1','outcome':'PASS',
         'passed':len(cases),'cases':cases,'synthetic_only':True,
         'database_writes':0,'actual_erpnext_draft_created':False,
         'wallet_captures':0,'gateway_calls':0,'invoice_or_gl_postings':0,
         'production_ready':False}
