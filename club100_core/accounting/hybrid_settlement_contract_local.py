"""D2C3B2B: strictly non-posting hybrid settlement blueprint and diagnostics.

Prepares a frozen ledger contract from D2C3A review. Never performs invoice
submission, Razorpay requests, wallet capture, GL posting or enrollment updates.
The future posting stage must re-verify payment with fresh Razorpay GET under
Member -> Wallet -> Enrollment -> Hybrid locks and atomically reconcile.
"""
import os
import frappe
from frappe.utils import cint
from club100_core.accounting.hybrid_posting_rules import build_posting_plan
from club100_core.accounting.hybrid_invoice_review_local import preview_one
from club100_core.accounting import settlement


def _admin():
    if frappe.session.user != 'Administrator':
        frappe.throw('Administrator required for local settlement blueprint',frappe.PermissionError)


def _local():
    return (frappe.local.site == 'club100.local'
        and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def preview(enrollment_id):
    """Review an existing verified, unposted hybrid transaction; no writes."""
    _admin()
    if not _local() or os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2B_REVIEW') != '1':
        raise ValueError('D2C3B2B local-only review is disabled')
    report=preview_one(enrollment_id)
    if report['issues'] or not report['finance_preflight_passed']:
        return {'site':frappe.local.site,'stage':'5F-D2C3B2B',
          'outcome':'BLOCKED','issues':report['issues'],
          'posting_authorized':False,'production_ready':False,'read_only':True}
    review=report['review']
    evidence=frappe.db.get_value('Club100 Hybrid Payment Evidence',report['evidence'],
        ['name','identity_sha256','enrollment','member','hybrid_checkout',
         'state','provider_status','razorpay_order_id','razorpay_payment_id',
         'amount_paise','snapshot_sha256'],as_dict=True)
    if not evidence:raise ValueError('Verified payment evidence vanished')
    plan=build_posting_plan(review,settlement._config(),dict(evidence))
    return {'site':frappe.local.site,'stage':'5F-D2C3B2B',
        'outcome':'PLAN_VALIDATED','plan':plan,'posting_authorized':False,
        'no_fresh_provider_get':True,'invoice_submitted':False,
        'wallet_captured':False,'membership_activated':False,
        'production_ready':False,'read_only':True}


def healthcheck():
    _admin()
    return {'site':frappe.local.site,'stage':'5F-D2C3B2B',
      'contract_installed':True,'review_opted_in':os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2B_REVIEW')=='1',
      'new_invoice_submission_enabled':False,'wallet_capture_enabled':False,
      'payment_entry_or_gl_posting_enabled':False,'membership_activation_enabled':False,
      'production_ready':False}
