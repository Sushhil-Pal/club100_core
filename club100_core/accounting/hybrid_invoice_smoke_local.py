"""D2C3A synthetic and immutable math smoke; no DB writes, no Razorpay calls."""
import os
import frappe
from club100_core.accounting.promotion_rules import build_hybrid_plan
from club100_core.accounting.hybrid_integration_rules import validate_frozen_checkout
from club100_core.accounting.hybrid_invoice_contract import (
    build_invoice_review, validate_tax_template_rows)
from club100_core.accounting.hybrid_evidence_rules import identity_digest


def run():
    if (frappe.local.site != 'club100.local'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3A_SMOKE') != '1'):
        raise ValueError('Explicit local smoke opt-in required')
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('Administrator or System Manager required', frappe.PermissionError)
    p = [{'grant':'SMOKE-PROMO','source':'Promotion','amount':'100.00'}]
    f = [{'grant':'SMOKE-CORP','source':'Corporate','amount':'200.00'},
         {'grant':'SMOKE-REF','source':'Referral','amount':'100.00'}]
    plan = build_hybrid_plan('799.00','100.00','300.00','399.00','5')
    from json import dumps
    snapshot = {'enrollment':'SMOKE-ENROLL', 'member':'SMOKE-MEMBER',
        'status':'Reserved', 'policy_version':'SMOKE-POL-1',
        'original_price':'799.00', 'promotion_discount':'100.00',
        'funded_wallet_tender':'300.00','gateway_due':'399.00',
        'gst_percent':'5','promotion_reservation_key':'HYB:P:SMOKE-ENROLL',
        'funded_reservation_key':'HYB:F:SMOKE-ENROLL','plan_snapshot_json':dumps(plan)}
    pre = validate_frozen_checkout(snapshot,p,f)
    ident = {'enrollment':'SMOKE-ENROLL','hybrid_checkout':'SMOKE-HYB',
        'member':'SMOKE-MEMBER','razorpay_order_id':'order_smoke',
        'razorpay_payment_id':'pay_smoke','amount_paise':39900,
        'snapshot_sha256':pre['snapshot_sha256']}
    evidence = {**ident,'identity_sha256':identity_digest(ident),
               'name':'SMOKE-EVIDENCE','state':'Verified Pending Invoice',
               'provider_status':'captured'}
    passed=[]
    result=build_invoice_review(pre,evidence,'SMOKE-HYB',p,f)
    assert result['invoice_total_incl_gst']=='699.00'
    assert result['razorpay_payment']=='399.00'
    assert result['funded_wallet_payment']=='300.00'
    passed.append('Original price minus supplier discount equals discounted invoice')
    assert result['gst_projected']=='33.29' and result['taxable_value_projected']=='665.71'
    passed.append('Inclusive 5% GST derived only from discounted invoice value')
    assert result['corporate_source']=='200.00' and result['referral_source']=='100.00'
    passed.append('Corporate and Referral remain distinct invoice tenders')
    assert result['posting_authorized'] is False
    passed.append('Read-only contract never authorizes financial posting')
    tax = validate_tax_template_rows([{'charge_type':'On Net Total',
        'included_in_print_rate':1,'account_head':'SMOKE-GST','rate':'5'}])
    assert tax['rate_percent']=='5'
    passed.append('Inclusive GST tax-template contract passes')
    for label, changed in (
        ('Evidence state', {**evidence,'state':'Manual Review'}),
        ('Captured amount', {**evidence,'amount_paise':79900}),
        ('Immutable identity digest',{**evidence,'identity_sha256':'0'*64}),
    ):
        try: build_invoice_review(pre,changed,'SMOKE-HYB',p,f)
        except ValueError: passed.append(label+' tampering blocked')
        else: raise AssertionError(label+' unexpectedly accepted')
    try:
        build_invoice_review(pre,evidence,'SMOKE-HYB',p,
            [{'grant':'SMOKE-PROMO','source':'Corporate','amount':'300.00'}])
    except ValueError: passed.append('Cross-leg duplicate grant rejected')
    else: raise AssertionError('Duplicate grant accepted')
    return {'site':frappe.local.site,'outcome':'PASS','passed':len(passed),
        'cases':passed,'real_gateway_calls':0,'database_writes':0,
        'wallet_captures':0,'invoice_or_gl_postings':0,
        'read_only':True,'production_ready':False,
        'note':'Synthetic math-only check; not ERPNext invoice posting or statutory GST approval'}
