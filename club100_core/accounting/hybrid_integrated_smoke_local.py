"""Non-posting, local-only integrity checks for the consolidated integration.

Does not create orders or invoke checkout, payment handlers, invoice submission,
wallet capture, referral reward posting, or provider GET. It is NOT E2E UAT.
"""
import os
from decimal import Decimal
import frappe
from frappe.utils import cint
from club100_core.accounting.hybrid_gst import inclusive_breakdown, verify_tax_rows
from club100_core.accounting.hybrid_runtime import enabled, financial_enabled


def run():
    if (frappe.local.site!='club100.local'
            or os.environ.get('CLUB100_ALLOW_HYBRID_E2E_SMOKE')!='1'
            or frappe.session.user!='Administrator'):
        raise ValueError('Synthetic integrity checks require local Administrator opt-in')
    results=[]
    p=inclusive_breakdown('799','100','300','5')
    assert (p['invoice'],p['net'],p['gst'],p['gateway'])==('699.00','665.71','33.29','399.00')
    results.append('Frozen ₹799 price and ₹100 discount yield ₹699 invoice')
    p2=inclusive_breakdown('799','100','300','12')
    assert (p2['net'],p2['gst'],p2['gateway'])==('624.11','74.89','399.00')
    results.append('Configurable GST rate recalculates taxable value without changing tender')
    assert verify_tax_rows([{'charge_type':'On Net Total','included_in_print_rate':1,
            'account_head':'Output Tax','rate':'5'}], '5')==Decimal('5')
    results.append('GST-inclusive ERPNext tax row must equal frozen rate')
    try:
        verify_tax_rows([{'charge_type':'On Net Total','included_in_print_rate':1,
            'account_head':'Output Tax','rate':'5'}], '12')
    except ValueError: results.append('Mismatched ERPNext tax template rejected')
    else:raise ValueError('Failed to reject mismatched GST row')
    assert p['invoice']==str(Decimal(p['wallet'])+Decimal(p['gateway']))
    results.append('Corporate/Referral wallet credit remains an invoice payment, not GST discount')
    return {'site':frappe.local.site,'stage':'HYBRID-E2E-INTEGRATION',
        'outcome':'PASS','passed':len(results),'cases':results,
        'checkout_enabled':enabled(),'finance_enabled':financial_enabled(),
        'synthetic_only':True,'database_writes':0,'gateway_calls':0,
        'invoice_or_gl_postings':0,'wallet_captures':0,
        'membership_activations':0,'production_ready':False}
