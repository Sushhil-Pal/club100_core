"""Synthetic no-write preflight smoke; reads funding readiness only."""
import os
import frappe
from club100_core.accounting.hybrid_backing_rules import (
    validate_funded_allocations, validate_grant_for_reservation, money)
from club100_core.accounting.hybrid_backing_preflight_local import healthcheck
from club100_core.accounting.settlement import _admin, _local_pilot

def _reject(callback):
    try: callback()
    except ValueError: return True
    raise AssertionError('Expected fail-closed validation')

def run():
    _admin()
    if not _local_pilot() or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2C2B_SMOKE')!='1':
        raise ValueError('Local opt-in required for synthetic source-backing smoke')
    before = frappe.db.count('Club100 Wallet Grant')
    rows=[{'grant':'CORP-SAMPLE','source':'Corporate','amount':'200.00'},
          {'grant':'REF-SAMPLE','source':'Referral','amount':'100.00'}]
    canonical,totals=validate_funded_allocations(rows,'300.00')
    assert totals['Corporate']==money('200') and totals['Referral']==money('100')
    assert _reject(lambda: validate_funded_allocations(rows,'299.99'))
    assert _reject(lambda: validate_funded_allocations(rows+[rows[0]],'500'))
    assert _reject(lambda: validate_funded_allocations(
        [{'grant':'PROMO','source':'Promotion','amount':'100'}],'100'))
    assert _reject(lambda: validate_funded_allocations(rows, '300.001'))
    sample={'name':'CORP-SAMPLE','member':'MEM','source_type':'Corporate',
            'wallet':'WAL','status':'Active','currency':'INR','amount':'200'}
    assert validate_grant_for_reservation(sample,'MEM',rows[0],True)
    assert _reject(lambda: validate_grant_for_reservation(sample,'OTHER',rows[0],True))
    assert _reject(lambda: validate_grant_for_reservation(sample,'MEM',rows[0],False))
    h=healthcheck()
    assert h['wallet_capture_enabled'] is False and h['production_ready'] is False
    assert frappe.db.count('Club100 Wallet Grant')==before
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C2B','outcome':'PASS',
        'passed':8,'synthetic_only':True,'database_writes':0,
        'read_only':True,'bank_statement_reconciled':False,
        'wallet_captures':0,'invoice_or_gl_postings':0,
        'production_ready':False}
