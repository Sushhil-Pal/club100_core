"""Local read-only smoke: no Sales Invoice, Payment Entry, refunds or wallet writes."""
import os
import frappe
from club100_core.accounting.refund_controls import preview_refund, refund_exposure_report
from club100_core.accounting.settlement import _local_pilot


def run(enrollment_id='C100-ENR-00197'):
    if not _local_pilot() or os.environ.get('CLUB100_ALLOW_STAGE5FC_SMOKE') != '1':
        return {'outcome':'BLOCKED','reason':'Only explicit local Test Mode smoke is permitted'}
    before = frappe.db.count('Club100 Membership Accounting')
    try:
        report = refund_exposure_report(limit=20)
        preview = preview_refund(str(enrollment_id))
        assert preview['ready_for_refund_posting'] is False
        assert report['read_only'] is True
        assert frappe.db.count('Club100 Membership Accounting') == before
        return {'outcome':'PASS', 'read_only':True,'enrollment':str(enrollment_id),
                'refund_status':preview['status'],'report_rows':report['records_reviewed'],
                'financial_postings':0,'rollback_on_return':True}
    finally:
        frappe.db.rollback()
