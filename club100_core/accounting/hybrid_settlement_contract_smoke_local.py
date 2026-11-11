"""D2C3B2B read-only synthetic posting contract smoke; no database writes."""
import hashlib
import json
import os
from copy import deepcopy
import frappe
from club100_core.accounting.hybrid_draft_rules import FIELDS
from club100_core.accounting.hybrid_posting_rules import (build_posting_plan,
    validate_submitted_invoice,validate_gateway_entry,validate_wallet_entry,
    validate_final_balance)


def run():
    if (frappe.local.site != 'club100.local' or frappe.session.user != 'Administrator'
        or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test'
        or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2B_SMOKE') != '1'):
        raise ValueError('Local Administrator/Test Mode/explicit smoke opt-in required')
    r={'enrollment':'TEST-ENR','member':'TEST-MEMBER','hybrid_checkout':'TEST-HYBRID',
       'snapshot_sha256':'a'*64,'evidence_name':'TEST-EVIDENCE','evidence_identity_sha256':'b'*64,
       'original_price_incl_gst':'799.00','supplier_discount_incl_gst':'100.00',
       'invoice_total_incl_gst':'699.00','taxable_value_projected':'665.71',
       'gst_projected':'33.29','funded_wallet_payment':'300.00',
       'razorpay_payment':'399.00','corporate_source':'200.00','referral_source':'100.00',
       'posting_authorized':False,'no_automatic_tax_or_invoice_authorization':True,
       'submitted_invoice_verified':False}
    r['invoice_review_sha256']=hashlib.sha256(json.dumps({k:r[k] for k in FIELDS},sort_keys=True,
            separators=(',',':')).encode()).hexdigest()
    cfg={'company':'TEST COMPANY','customer':'TEST CUSTOMER','item':'TEST ITEM',
         'razorpay_clearing':'TEST CLEARING','Corporate':'CORP LIABILITY',
         'Referral':'REF LIABILITY'}
    e={'name':'TEST-EVIDENCE','identity_sha256':'b'*64,'snapshot_sha256':'a'*64,'enrollment':'TEST-ENR',
       'member':'TEST-MEMBER','hybrid_checkout':'TEST-HYBRID','state':'Verified Pending Invoice',
       'provider_status':'captured','razorpay_order_id':'order_test',
       'razorpay_payment_id':'pay_test','amount_paise':39900}
    identity_keys=('enrollment','hybrid_checkout','member','razorpay_order_id',
                   'razorpay_payment_id','amount_paise','snapshot_sha256')
    e['identity_sha256']=hashlib.sha256(json.dumps({k:e[k] for k in identity_keys},sort_keys=True,
              separators=(',',':')).encode()).hexdigest()
    r['evidence_identity_sha256']=e['identity_sha256']
    r['invoice_review_sha256']=hashlib.sha256(json.dumps({k:r[k] for k in FIELDS},sort_keys=True,
            separators=(',',':')).encode()).hexdigest()
    plan=build_posting_plan(r,cfg,e)
    inv={'name':'TEST-INV','docstatus':1,'company':'TEST COMPANY','customer':'TEST CUSTOMER',
        'currency':'INR','apply_discount_on':'Grand Total','discount_amount':100.0,
        'items':[{'item_code':'TEST ITEM','rate':799.0}],
        'grand_total':699.0,'net_total':665.71,'total_taxes_and_charges':33.29,
        'outstanding_amount':699.0}
    pe={'docstatus':1,'payment_type':'Receive','party_type':'Customer','party':'TEST CUSTOMER',
        'paid_to':'TEST CLEARING','reference_no':'pay_test',
        'paid_amount':399.0,'received_amount':399.0,
        'references':[{'reference_doctype':'Sales Invoice','reference_name':'TEST-INV','allocated_amount':399.0}]}
    je={'docstatus':1,'company':'TEST COMPANY','accounts':[
        {'account':'CORP LIABILITY','debit_in_account_currency':200.0,'credit_in_account_currency':0},
        {'account':'REF LIABILITY','debit_in_account_currency':100.0,'credit_in_account_currency':0},
        {'account':'TEST RECEIVABLE','debit_in_account_currency':0,'credit_in_account_currency':300.0,
         'party_type':'Customer','party':'TEST CUSTOMER','reference_type':'Sales Invoice','reference_name':'TEST-INV'}]}
    validate_submitted_invoice(r,cfg,inv)
    validate_gateway_entry(plan,'TEST-INV',pe)
    validate_wallet_entry(plan,'TEST-INV','TEST RECEIVABLE',je)
    validate_final_balance(plan,'0.00','100.00','300.00')
    n=4
    def blocked(fn):
        try:fn()
        except ValueError:return True
        raise AssertionError('Posting safety guard accepted invalid data')
    bad=deepcopy(e);bad['state']='Manual Review';blocked(lambda:build_posting_plan(r,cfg,bad));n+=1
    bad=deepcopy(pe);bad['paid_amount']=499.0;blocked(lambda:validate_gateway_entry(plan,'TEST-INV',bad));n+=1
    bad=deepcopy(je);bad['accounts'][0]['debit_in_account_currency']=300.0
    blocked(lambda:validate_wallet_entry(plan,'TEST-INV','TEST RECEIVABLE',bad));n+=1
    blocked(lambda:validate_final_balance(plan,'50.00','100.00','300.00'));n+=1
    return {'site':frappe.local.site,'stage':'5F-D2C3B2B','outcome':'PASS','passed':n,
      'synthetic_only':True,'database_writes':0,'gateway_calls':0,
      'invoice_submissions':0,'payment_or_gl_postings':0,'wallet_captures':0,
      'membership_activations':0,'posting_authorized':False,'production_ready':False}
