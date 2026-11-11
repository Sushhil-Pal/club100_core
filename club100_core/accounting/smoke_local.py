"""Rollback-only local ERPNext integration smoke for submitted invoice and tender posting.

Use only on a disposable Club100 test site with configured accounting test
Customer/Item/tax/accounts. No Razorpay requests. No production execution.
"""
import os
import frappe
from frappe.utils import cint
from club100_core.accounting.settlement import preview_one, post_one, _local_pilot


def run(enrollment_id=None):
    if (not _local_pilot() or os.environ.get('CLUB100_ALLOW_ACCOUNTING_SMOKE') != '1'
            or not cint(frappe.conf.get('club100_accounting_post_enabled',0))):
        return {'outcome':'BLOCKED', 'reason':'Local test site/flag/explicit env var required'}
    if enrollment_id:
        names=[str(enrollment_id)]
    else:
        names=frappe.get_all('Club100 Enrollment',filters={
            'website_source':'PWA Commerce','payment_status':'Paid',
            'sales_invoice':['is','not set']},pluck='name',limit_page_length=25,
            order_by='creation desc')
    try:
        for name in names:
            try:
                preview=preview_one(name)
            except Exception:
                continue
            if preview['status']!='Ready':
                continue
            before=frappe.db.count('Club100 Membership Accounting')
            result=post_one(name,commit=False)
            assert result['status']=='Posted'
            assert frappe.db.get_value('Sales Invoice',result['invoice'],'docstatus')==1
            assert frappe.db.get_value('Sales Invoice',result['invoice'],'outstanding_amount')==0
            assert frappe.db.count('Club100 Membership Accounting')==before+1
            return {'outcome':'PASS', 'cases':4,'enrollment':name,
                    'invoice_created':True,'gateway_payment_posted':bool(result['razorpay_entry']),
                    'wallet_redemption_posted':bool(result['wallet_entry']),
                    'no_gateway_requests':True,'rollback_on_return':True}
        return {'outcome':'SKIP','reason':'No eligible paid uninvoiced Self test enrollment with complete accounting configuration',
                'candidates_reviewed':len(names)}
    finally:
        frappe.db.rollback()
