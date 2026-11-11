"""D2C3B2C2C1: READ-ONLY local capture-contract health and backing review.

Never captures reservation or posts Journal Entries. Review is informational
and deliberately cannot become an authorization token for later settlement.
"""
import frappe
from club100_core.accounting.hybrid_backing_preflight_local import _guard,review_enrollment

def healthcheck():
    _guard()
    return {'site':frappe.local.site,'stage':'5F-D2C3B2C2C1',
       'capture_contract_installed':True,'backing_review_available':True,
       'live_capture_wired':False,'wallet_capture_enabled':False,
       'invoice_or_gl_posting_enabled':False,'membership_activation_enabled':False,
       'bank_statement_reconciled':False,'read_only':True,'production_ready':False}

def review_enrollment_capture(enrollment_id):
    _guard()
    report=review_enrollment(enrollment_id)
    return {**report,'stage':'5F-D2C3B2C2C1',
       'capture_contract_installed':True,'ledger_capture_ready':False,
       'bank_statement_reconciled':False,'posting_authorized':False,
       'capture_authorized':False,'production_ready':False,
       'note':'Point-in-time read-only verification. Future transaction MUST recheck under locks and cannot use this as permission.'}
