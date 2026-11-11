"""D2C2B read-only Frappe smoke. Pure synthetic examples; NO external HTTP."""
import os
from copy import deepcopy
import frappe
from club100_core.accounting.hybrid_order_contract import build_order_payload,validate_order_response


def run():
    if frappe.local.site!='club100.local' or os.environ.get('CLUB100_ALLOW_STAGE5FD2C2B_SMOKE')!='1':
        raise ValueError('Local-only test requires CLUB100_ALLOW_STAGE5FD2C2B_SMOKE=1')
    if frappe.session.user!='Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required',frappe.PermissionError)
    dt=('Club100 Enrollment','Club100 Hybrid Checkout','Club100 Wallet Reservation',
        'Club100 Wallet Ledger','Sales Invoice','Payment Entry','Journal Entry')
    before={name:frappe.db.count(name) for name in dt}
    pre={'posting_authorized':False,'enrollment':'SMOKE:ENR:1',
         'snapshot_sha256':'a'*64,'expected_gateway_paise':39900,
         'plan':{'invoice_total_incl_gst':'699.00','razorpay_tender':'399.00'}}
    payload=build_order_payload(pre,'SMOKE:HYB:1','SMOKE:PROGRAM','Self','1')
    cases=[]
    assert payload['amount']==39900 and payload['amount']!=79900
    cases.append('Frozen discounted gateway due used instead of original or invoice gross')
    assert payload['notes']['club100_snapshot']=='a'*64
    cases.append('Immutable quote fingerprint and enrollment/hybrid identity embedded in notes')
    order={'id':'order_smoke123','amount':39900,'currency':'INR',
           'receipt':'SMOKE:ENR:1','notes':deepcopy(payload['notes']),
           'status':'created','amount_paid':0,'amount_due':39900}
    assert validate_order_response(payload,order)['order_id']=='order_smoke123'
    assert validate_order_response(payload,dict(order,status='attempted'),reuse=True)
    cases.append('Unpaid Test Order creation and unchanged replay accepted')
    for bad in (dict(order,amount=79900),dict(order,notes={}),dict(order,status='paid'),
                dict(order,amount_paid=39900,amount_due=0)):
        try:validate_order_response(payload,bad)
        except ValueError:pass
        else:raise AssertionError('Unsafe order accepted: '+str(bad))
    cases.append('Overcharge, mismatched identity, paid order and partially spent order denied')
    try:build_order_payload(dict(pre,expected_gateway_paise=0),'SMOKE:HYB:1')
    except ValueError:cases.append('Wallet-only zero due never sent to Razorpay')
    else:raise AssertionError('Wallet-only gateway order accepted')
    from club100_core.accounting.hybrid_order_service import _guard
    try:_guard(real_order=True)
    except ValueError:cases.append('Without explicit pilot opt-ins real test-order creation is denied')
    else:raise AssertionError('Test order gateway guard unexpectedly enabled')
    after={name:frappe.db.count(name) for name in dt}
    if before!=after:raise AssertionError('Read-only smoke altered record counts')
    return {'outcome':'PASS','site':frappe.local.site,'passed':len(cases),'cases':cases,
            'record_counts_unchanged':True,'gateway_calls':0,'wallet_captures':0,
            'invoice_or_gl_postings':0,'read_only':True,'production_ready':False,
            'note':'Contract-only smoke. Real Test Order creation, provider callbacks, capture and posting remain inactive.'}
