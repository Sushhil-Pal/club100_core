"""Rollback-only Stage 5F integration smoke; does NOT invent receipts or wallet credits."""
import os
import frappe
from club100_core.accounting.funding import (
    _local_pilot, _post_allowed, readiness_report, corporate_receipt_preview,
    referral_issuance_preview, post_corporate_receipt, post_referral_issuance,
    EVENT_DT,
)


def run(pool_name=None, payment_entry_name=None, referral_grant_name=None):
    if not _local_pilot() or os.environ.get('CLUB100_ALLOW_STAGE5F_SMOKE')!='1':
        return {'outcome':'BLOCKED', 'reason':'Explicit local rollback-only smoke flag required'}
    before=frappe.db.count(EVENT_DT)
    try:
        report=readiness_report(limit=100)
        result={'site':frappe.local.site,'report_read_only':True,
                'pool_rows':report['pools_reviewed'],'grant_rows':report['grants_reviewed'],
                'unbooked_pool_count':len(report['unbooked_corporate_pools']),
                'unbooked_grant_count':len(report['unbooked_wallet_grants']),
                'no_external_calls':True,'rollback_on_return':True}
        if pool_name and payment_entry_name:
            pre=corporate_receipt_preview(pool_name,payment_entry_name)
            result['corporate_preview']=pre
            if pre['status']=='Ready for controlled UAT' and _post_allowed():
                posted=post_corporate_receipt(pool_name,payment_entry_name,commit=False)
                assert posted['status']=='Posted'
                assert frappe.db.get_value('Journal Entry',posted['journal_entry'],'docstatus')==1
                result['corporate_post_test']='PASS'
            else:
                result['corporate_post_test']='SKIP: evidence or flags not ready'
        if referral_grant_name:
            pre=referral_issuance_preview(referral_grant_name)
            result['referral_preview']=pre
            if pre['status']=='Ready for controlled UAT' and _post_allowed():
                posted=post_referral_issuance(referral_grant_name,commit=False)
                assert posted['status']=='Posted'
                assert frappe.db.get_value('Journal Entry',posted['journal_entry'],'docstatus')==1
                result['referral_post_test']='PASS'
            else:
                result['referral_post_test']='SKIP: evidence or flags not ready'
        assert frappe.db.count(EVENT_DT)>=before
        result['outcome']='PASS'
        return result
    finally:
        frappe.db.rollback()
