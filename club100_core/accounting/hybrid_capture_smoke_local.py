"""Synthetic non-posting wallet ledger contract checks. No database mutations."""
import os
from copy import deepcopy
import frappe
from frappe.utils import cint
from club100_core.accounting.hybrid_capture_rules import planned_spends,verify_captured_ledger

def _guard():
    if (frappe.local.site!='club100.local' or frappe.session.user!='Administrator'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower()!='test'
            or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C2C1_SMOKE')!='1'):
        raise ValueError('Synthetic wallet capture tests disabled: local Administrator and opt-in required')

def fixture():
    context={'enrollment':'C100-ENR-SYNTHETIC','member':'C100-MEM-SYNTHETIC',
        'wallet':'WALLET-SYNTHETIC','reservation':'RES-SYNTHETIC',
        'reservation_key':'HYB:F:C100-ENR-SYNTHETIC',
        'hybrid_checkout':'HYB-SYNTHETIC','evidence':'EVIDENCE-SYNTHETIC',
        'invoice':'SINV-SYNTHETIC','payment_entry':'PE-SYNTHETIC',
        'journal_entry':'JE-SYNTHETIC','hybrid_status':'Reserved',
        'reservation_status':'Reserved','provider_status':'captured',
        'evidence_state':'Verified Pending Invoice','hold_not_expired':True,
        'invoice_docstatus':1,'payment_docstatus':1,'journal_docstatus':1,
        'invoice_member':'C100-MEM-SYNTHETIC','reservation_member':'C100-MEM-SYNTHETIC',
        'reservation_reference':'C100-ENR-SYNTHETIC',
        'payment_invoice':'SINV-SYNTHETIC','journal_invoice':'SINV-SYNTHETIC',
        'invoice_outstanding':'0.00','original_price':'799.00',
        'promotion_discount':'100.00','invoice_amount':'699.00',
        'gateway_amount':'399.00','wallet_amount':'300.00',
        'wallet_journal_amount':'300.00','funding_preflight_verified':True}
    allocations=[{'grant':'CORP-SYNTHETIC','source':'Corporate','amount':'200.00'},
        {'grant':'REF-SYNTHETIC','source':'Referral','amount':'100.00'}]
    backing={'Corporate':'200.00','Referral':'100.00'}
    return context,allocations,backing

def run():
    _guard()
    context,allocations,backing=fixture()
    cases=[]
    def ok(label,check):
        check();cases.append(label)
    def blocked(label,mutation):
        c,a,b=deepcopy((context,allocations,backing));mutation(c,a,b)
        try:planned_spends(c,a,b)
        except ValueError:cases.append(label)
        else:raise ValueError('Unsafe synthetic contract accepted: '+label)
    plan=planned_spends(context,allocations,backing)
    ok('Expected Corporate and Referral spend keys are distinct',lambda:
        (len({x['event_key'] for x in plan['expected_ledger']})==2 or
         (_ for _ in ()).throw(ValueError('Event collision'))))
    ok('Two synthetic spends reconcile to wallet tender',lambda:
        verify_captured_ledger(plan,'Captured',deepcopy(plan['expected_ledger'])))
    blocked('Supplier promotion cannot be used as payment tender',lambda c,a,b:a.append(
        {'grant':'PROMO','source':'Promotion','amount':'0.01'}))
    blocked('Unverified grant backing cannot pass',lambda c,a,b:c.update(funding_preflight_verified=False))
    blocked('Wrong Journal Entry amount rejected',lambda c,a,b:c.update(wallet_journal_amount='299.99'))
    blocked('Nonzero invoice outstanding rejected',lambda c,a,b:c.update(invoice_outstanding='0.01'))
    blocked('Expired wallet reservation rejected',lambda c,a,b:c.update(hold_not_expired=False))
    blocked('Repeated grant rejected',lambda c,a,b:a.append(dict(a[0])))
    bad=deepcopy(plan['expected_ledger']);bad.append(dict(bad[0]))
    try:verify_captured_ledger(plan,'Captured',bad)
    except ValueError:cases.append('Duplicate spend posting rejected')
    else:raise ValueError('Duplicate ledger row accepted')
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C2C1','outcome':'PASS',
        'passed':len(cases),'cases':cases,'synthetic_only':True,'database_writes':0,
        'gateway_calls':0,'ledger_captures':0,'invoice_or_gl_postings':0,
        'membership_activations':0,'posting_authorized':False,'production_ready':False}
