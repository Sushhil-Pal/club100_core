"""No-DB smoke of synthetic voucher blueprint. NEVER executes trial()."""
import os
import frappe
from club100_core.accounting import settlement
from club100_core.accounting.hybrid_transaction_uat_local import _fixture
from club100_core.accounting.hybrid_transaction_uat_rules import check_gateway_only_plan


def run():
    if frappe.local.site != 'club100.local' or frappe.session.user != 'Administrator':
        raise ValueError('Local Administrator required')
    if os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C1_SMOKE') != '1':
        raise ValueError('Read-only smoke not enabled')
    review,evidence=_fixture()
    cfg={**{k:'' for k in ('company','customer','razorpay_clearing')},
        'company':'TEST COMPANY','customer':'TEST CUSTOMER',
        'razorpay_clearing':'TEST CLEARING'}
    plan=check_gateway_only_plan(review,cfg,evidence)
    assert plan['gateway_amount']=='699.00' and plan['funded_amount']=='0.00'
    assert plan['supplier_discount']=='100.00'
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C1','outcome':'PASS',
         'passed':5,'synthetic_only':True,'database_writes':0,
         'gateway_calls':0,'voucher_submissions':0,'wallet_captures':0,
         'membership_activations':0,'posting_authorized':False,
         'production_ready':False}
