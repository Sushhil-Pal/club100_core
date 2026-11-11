"""D2C3B1: guarded ERPNext DRAFT-invoice computation experiment, local only.

Never submit invoices, make payment entries, redeem wallet, change Enrollment,
mark evidence Settled, call Razorpay or post GL. Optional draft insertion always
rolls back by savepoint and is disabled unless an Administrator explicitly opts
in from a local bench command. No public whitelisted endpoints or hooks.
"""
import os

import frappe
from frappe.utils import cint, getdate, nowdate
from club100_core.accounting.hybrid_draft_rules import draft_spec, verify_calculated_draft
from club100_core.accounting.hybrid_invoice_contract import money
from club100_core.accounting.hybrid_invoice_review_local import preview_one
from club100_core.accounting import settlement


def _admin():
    if frappe.session.user != 'Administrator':
        frappe.throw('D2C3B1 requires Administrator via local bench', frappe.PermissionError)


def _local():
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def _pilot():
    return (_local() and cint(frappe.conf.get('club100_stage5fd2c3b1_local_enabled',0)) == 1
            and os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B1_LOCAL') == '1')


def _draft_insert_enabled():
    return (_pilot()
            and cint(frappe.conf.get('club100_stage5fd2c3b1_draft_insert_enabled',0)) == 1
            and os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B1_DRAFT_INSERT') == '1')


def _inputs(enrollment_id):
    report=preview_one(enrollment_id)
    if report['issues'] or not report['finance_preflight_passed']:
        raise ValueError('Financial preflight BLOCKED: '+ '; '.join(report['issues']))
    review=report['review']
    cfg=settlement._config()
    sources={s: money(review[k]) for s,k in (('Corporate','corporate_source'),('Referral','referral_source'))
             if money(review[k]) > 0}
    taxes=settlement._ready(cfg, sources, money(review['razorpay_payment']))
    # D2C3A preview_one has already checked GL backing, including allocations.
    # Do not call the legacy two-argument backing helper: newer versions require
    # a third original-allocation parameter and it is validated in D2C3A.
    doc=frappe.get_doc('Club100 Enrollment', report['enrollment'])
    if frappe.db.get_value('Club100 Plan',doc.plan,'plan_code') != 'self':
        raise ValueError('D2C3B1 trial is limited to the Self plan')
    return report, cfg, taxes, getdate(doc.payment_date or nowdate()).isoformat()


def check_one(enrollment_id, insert_draft=False):
    """Default: read-only review. Explicitly enabled draft insert rolls back.

    Not a production payment callback. Not a greenlight to issue GST invoices.
    ERPNext document hooks could have non-transactional effects: never run
    the draft-insert option on a production/shared site.
    """
    _admin()
    if not _pilot():
        raise ValueError('D2C3B1 review local-pilot flags are disabled')
    if type(insert_draft) is not bool:
        raise ValueError('insert_draft must be a boolean')
    if insert_draft and not _draft_insert_enabled():
        raise ValueError('D2C3B1 draft insertion is separately disabled')
    report, cfg, taxes, posting_date = _inputs(enrollment_id)
    spec=draft_spec(report['review'],cfg,taxes,posting_date)
    baseline={'site':frappe.local.site, 'stage':'5F-D2C3B1',
              'enrollment':report['enrollment'],
              'invoice_review_sha256':report['review']['invoice_review_sha256'],
              'expected_discount':report['review']['supplier_discount_incl_gst'],
              'expected_invoice_total':report['review']['invoice_total_incl_gst'],
              'expected_gst':report['review']['gst_projected'],
              'invoice_draft_only':True,'wallet_captured':False,
              'member_activated':False,'posting_authorized':False,
              'production_ready':False}
    if not insert_draft:
        return {**baseline,'status':'Prepared','database_writes':0,
                'erpnext_calculation_executed':False,'read_only':True}
    # Locks ensure that any change to grant holds/evidence blocks the attempt;
    # a second preflight is required after entering this transaction.
    point='club100_d2c3b1_draft'
    frappe.db.savepoint(point)
    original_count=frappe.db.count('Sales Invoice')
    try:
        member=frappe.db.get_value('Club100 Enrollment',report['enrollment'],'member')
        if not member:
            raise ValueError('Enrollment member vanished')
        frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE',member)
        frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE',report['enrollment'])
        frc=frappe.db.get_value('Club100 Hybrid Checkout',{'enrollment':report['enrollment']},'name')
        if not frc:
            raise ValueError('Hybrid checkout vanished')
        frappe.db.sql('SELECT name FROM `tabClub100 Hybrid Checkout` WHERE name=%s FOR UPDATE',frc)
        evid=frappe.db.get_value('Club100 Hybrid Payment Evidence',{'enrollment':report['enrollment']},'name')
        if not evid:
            raise ValueError('Payment evidence vanished')
        frappe.db.sql('SELECT name FROM `tabClub100 Hybrid Payment Evidence` WHERE name=%s FOR UPDATE',evid)
        repeat, repeat_cfg, repeat_taxes, repeat_date=_inputs(enrollment_id)
        if repeat['review']['invoice_review_sha256'] != report['review']['invoice_review_sha256']:
            raise ValueError('Invoice changed during local calculation')
        if (repeat_cfg != cfg or repeat_taxes != taxes or repeat_date != posting_date):
            raise ValueError('Accounting configuration or transaction date changed')
        invoice=frappe.get_doc(spec)
        invoice.insert(ignore_permissions=True)
        result=verify_calculated_draft(report['review'],{
            'docstatus': invoice.docstatus, 'company':invoice.company,
            'customer':invoice.customer, 'currency':invoice.currency,
            'apply_discount_on':invoice.apply_discount_on,
            'discount_amount':invoice.discount_amount,
            'items':[{'item_code':i.item_code,'rate':i.rate} for i in invoice.items],
            'grand_total':invoice.grand_total,'net_total':invoice.net_total,
            'total_taxes_and_charges':invoice.total_taxes_and_charges},cfg)
        result={**baseline, 'status':'Calculated and rolled back',
            'draft_calculation':result, 'erpnext_calculation_executed':True,
            'draft_submitted':False, 'invoice_or_gl_postings':0,
            'gateway_calls':0, 'wallet_capture_count':0}
    finally:
        # NEVER commit this draft, regardless of success or exception.
        frappe.db.rollback(save_point=point)
    if frappe.db.count('Sales Invoice') != original_count:
        raise ValueError('Invoice count changed despite rollback; investigate immediately')
    return {**result,'record_counts_unchanged':True,'database_writes_persisted':0}


def healthcheck():
    _admin()
    return {'site':frappe.local.site,'stage':'5F-D2C3B1',
            'rules_installed':True,'local_pilot_enabled':_pilot(),
            'draft_insert_enabled':_draft_insert_enabled(),
            'invoice_submission_enabled':False,'wallet_capture_enabled':False,
            'payment_and_gl_posting_enabled':False,'production_ready':False}
