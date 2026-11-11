"""Pure non-posting B2C2A smoke: neither DB writes nor voucher submissions."""
import os
import frappe
from club100_core.accounting.hybrid_wallet_je_uat_local import _fixture
from club100_core.accounting.hybrid_wallet_je_rules import funded_plan,journal_spec


def run():
    if (frappe.local.site!='club100.local' or frappe.session.user!='Administrator'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C2A_SMOKE')!='1'):
        raise ValueError('Explicit local Administrator smoke opt-in required')
    review,evidence=_fixture()
    cfg={'company':'TEST CO','customer':'TEST CUST','razorpay_clearing':'TEST CLEARING',
         'Corporate':'TEST CORP LIABILITY','Referral':'TEST REF LIABILITY'}
    plan=funded_plan(review,cfg,evidence)
    spec=journal_spec(plan,'TEST-INVOICE-NOT-REAL','TEST RECEIVABLE','2026-11-04')
    rows=spec['accounts']
    assert plan['supplier_discount']=='100.00'
    assert plan['invoice_amount']=='699.00'
    assert plan['gateway_amount']=='399.00'
    assert plan['funded_amount']=='300.00'
    assert len(rows)==3
    assert rows[0]['debit_in_account_currency']==200.0
    assert rows[1]['debit_in_account_currency']==100.0
    assert rows[2]['credit_in_account_currency']==300.0
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C2A',
        'outcome':'PASS','passed':8,'synthetic_only':True,'database_writes':0,
        'gateway_calls':0,'invoice_submissions':0,'payment_or_gl_postings':0,
        'wallet_captures':0,'membership_activations':0,
        'posting_authorized':False,'production_ready':False}
