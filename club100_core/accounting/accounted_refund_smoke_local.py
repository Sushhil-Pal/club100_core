"""Local-only, rollback-only Stage 5F-C2 test. All external refund evidence mocked.

This is NOT proof a real Razorpay refund has occurred. The test must never
initiate gateway refunds and must not leave any real financial voucher behind.
"""
import os
import frappe
from frappe.utils import cint
from club100_core.accounting.settlement import preview_one, post_one, _local_pilot
from club100_core.accounting import accounted_refunds as stage


def run(enrollment_id=None):
    if (not _local_pilot() or os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND_SMOKE') != '1'
        or not cint(frappe.conf.get('club100_accounting_post_enabled',0))
        or not cint(frappe.conf.get('club100_accounting_refund_post_enabled',0))
        or not cint(frappe.conf.get('club100_round4b_enabled',0))):
        return {'outcome':'BLOCKED','reason':'Local smoke, refund, invoice and recovery flags must be enabled'}
    if frappe.session.user not in ('Administrator',) and 'System Manager' not in frappe.get_roles(frappe.session.user):
        return {'outcome':'BLOCKED','reason':'System Manager required'}
    names=[enrollment_id] if enrollment_id else frappe.get_all('Club100 Enrollment',
        filters={'website_source':'PWA Commerce','payment_status':'Paid',
                 'sales_invoice':['is','not set']},
        pluck='name',order_by='creation desc', limit_page_length=20)
    old_fetch=stage._read_refund
    try:
        for name in names:
            try:
                candidate=preview_one(name)
            except Exception:
                continue
            if candidate['status'] != 'Ready' or candidate['sources'].get('Promotion'):
                continue
            # Do not simulate a refund for an enrollment with a matured, issued reward.
            if frappe.db.exists('Club100 Referral Reward',{'enrollment':name}):
                statuses=frappe.get_all('Club100 Referral Reward',filters={'enrollment':name},pluck='status')
                if any(s not in ('On Hold','Pending','Approved','Reversed') for s in statuses):
                    continue
                if not cint(frappe.conf.get('club100_referral_settlement_enabled',0)):
                    continue
            try:
                invoice=post_one(name,commit=False)
                assert invoice['status']=='Posted'
                enrollment=frappe.get_doc('Club100 Enrollment',name)
                gateway = float(candidate['razorpay'])
                refund_id = 'rfnd_STAGE5FC2SMOKE001' if gateway else None
                if gateway:
                    def simulated_read(rid):
                        assert rid == refund_id
                        return {'id':rid,'payment_id':enrollment.payment_id,
                                'status':'processed', 'currency':'INR',
                                'amount':round(gateway*100)}
                    stage._read_refund=simulated_read
                result=stage.post_accounted_full_refund(name,refund_id,commit=False)
                assert result['status']=='Simulation Passed' and result['rolled_back']
                # This refund function rolls its own savepoint back after checking
                # vouchers; invoice remains until the outer finally rollback.
                assert frappe.db.get_value('Club100 Enrollment',name,'payment_status')=='Paid'
                assert frappe.db.get_value('Club100 Membership Accounting',name,'status')=='Posted'
                return {'outcome':'PASS','enrollment':name,'gateway_mode':'mocked GET-only' if gateway else 'not needed',
                        'simulated_credit_note':result['credit_note'],
                        'simulated_gateway_payment_entry':result['gateway_refund_entry'],
                        'simulated_wallet_journal':result['wallet_refund_entry'],
                        'refund_simulation_rolled_back':True,
                        'invoice_test_rolled_back_on_return':True,
                        'no_gateway_requests':True}
            except Exception as exc:
                # Report the first *eligible* runtime failure for targeted fixing,
                # rather than hiding it by scanning additional enrollments.
                return {'outcome':'FAIL','enrollment':name,'error_type':type(exc).__name__,
                        'detail':str(exc)[:500], 'all_records_rolled_back_on_return':True}
        return {'outcome':'SKIP','candidates_reviewed':len(names),
                'reason':'No future Scheduled unaccounted Self test enrollment with supported sources and referral flag'}
    finally:
        stage._read_refund=old_fetch
        frappe.db.rollback()
