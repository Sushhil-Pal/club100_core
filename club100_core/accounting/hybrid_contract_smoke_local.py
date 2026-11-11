"""Stage 5F-D2C1 non-mutating local contract smoke, no gateway/network/GL."""
import json
import os

import frappe
from club100_core.accounting.hybrid_integration_rules import (
    validate_frozen_checkout, verify_razorpay_evidence,
    invoice_preflight, example_projection,
)
from club100_core.accounting.promotion_rules import build_hybrid_plan


def run():
    if frappe.local.site!='club100.local' or os.environ.get('CLUB100_ALLOW_STAGE5FD2C1_SMOKE')!='1':
        raise ValueError('D2C1 smoke restricted to club100.local with explicit environment flag')
    if frappe.session.user!='Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required',frappe.PermissionError)
    before={n:frappe.db.count(n) for n in ('Club100 Hybrid Checkout','Club100 Wallet Reservation',
                                           'Club100 Wallet Ledger','Sales Invoice','Payment Entry','Journal Entry')}
    plan=build_hybrid_plan('799','100','300','399','5')
    snapshot={'enrollment':'SMOKE:ENR:1','member':'SMOKE:MEM:1','status':'Reserved','policy_version':'SMOKE:POLICY:1',
       'original_price':'799','promotion_discount':'100','funded_wallet_tender':'300',
       'gateway_due':'399','gst_percent':'5',
       'promotion_reservation_key':'SMOKE:RES:P','funded_reservation_key':'SMOKE:RES:F',
       'plan_snapshot_json':json.dumps(plan)}
    p=[{'grant':'SMOKE:P','source':'Promotion','amount':'100.00'}]
    f=[{'grant':'SMOKE:C','source':'Corporate','amount':'300.00'}]
    pre=validate_frozen_checkout(snapshot,p,f)
    o={'id':'order_smokepreview','amount':39900,'currency':'INR','receipt':'SMOKE:ENR:1',
       'notes':{'club100_enrollment':'SMOKE:ENR:1'},
       'status':'paid','amount_paid':39900,'amount_due':0}
    pay={'id':'pay_smokepreview','order_id':'order_smokepreview','amount':39900,
         'currency':'INR','status':'captured','amount_refunded':0}
    verify_razorpay_evidence(pre,o['id'],o,pay)
    invoice_preflight(pre,example_projection(pre))
    cases=['Immutable dual-wallet and discount snapshot validated',
           'Captured Razorpay order/payment field contract validated',
           'Pre-tax discount and payment-tender invoice projection validated']
    bad=dict(pay,amount=79900)
    try:
        verify_razorpay_evidence(pre,o['id'],o,bad)
    except ValueError:
        cases.append('Undiscounted Razorpay charge rejected')
    else:raise ValueError('Undiscounted order unexpectedly accepted')
    try:
        verify_razorpay_evidence(pre,o['id'],o,dict(pay,status='authorized'))
    except ValueError:
        cases.append('Uncaptured Razorpay payment rejected')
    else:raise ValueError('Uncaptured payment unexpectedly accepted')
    try:
        invoice_preflight(pre,dict(example_projection(pre),funded_accounted_as='invoice_discount'))
    except ValueError:
        cases.append('Corporate wallet incorrectly used as discount rejected')
    else:raise ValueError('Incorrect accounting source unexpectedly accepted')
    after={n:frappe.db.count(n) for n in before}
    if before!=after:
        raise ValueError('D2C1 read-only smoke unexpectedly changed record counts')
    return {'outcome':'PASS','site':frappe.local.site,'passed':len(cases),'cases':cases,
            'snapshot_sha256':pre['snapshot_sha256'],
            'record_counts_unchanged':True, 'synthetic_evidence_only':True,
            'gateway_calls':0,'invoice_or_gl_postings':0,'read_only':True,
            'production_ready':False,
            'note':'This is a field-contract test, NOT HMAC verification, Razorpay GET, legal GST approval or live checkout integration.'}
