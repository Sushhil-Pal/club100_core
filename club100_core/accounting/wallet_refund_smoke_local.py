"""Strict rollback-only wallet-only/split refund *voucher* integration smoke.

Uses ONLY a genuinely source-backed existing paid enrollment. It never creates
fake corporate receipts, backdates grants, reclassifies promotional credits,
or initiates a Razorpay refund. All invoices/vouchers/refunds are rolled back.
Until a fresh source-backed eligible enrollment exists, correctly returns SKIP.
"""
import os

import frappe
from frappe.utils import cint

from club100_core.accounting.settlement import _admin, _local_pilot, preview_one, post_one
from club100_core.accounting import accounted_refunds as refunds


def _guard():
    _admin()
    if (not _local_pilot() or os.environ.get('CLUB100_ALLOW_WALLET_REFUND_SMOKE')!='1'
        or os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND')!='1'
        or not cint(frappe.conf.get('club100_accounting_post_enabled',0))
        or not cint(frappe.conf.get('club100_accounting_refund_post_enabled',0))
        or not cint(frappe.conf.get('club100_round4b_enabled',0))):
        raise ValueError('Stage5F-C3 requires local-only smoke env guards and posting/refund flags')


def _candidate(name, mode):
    preview=preview_one(name)
    wallet=float(preview['wallet'])
    gateway=float(preview['razorpay'])
    if mode=='wallet' and (wallet<=0 or gateway!=0):
        return False, 'Not a wallet-only purchase'
    if mode=='split' and (wallet<=0 or gateway<=0):
        return False, 'Not a split wallet + Razorpay purchase'
    if preview['status']!='Ready':
        return False, '; '.join(preview.get('issues') or ['Not ready for accounting'])
    enrollment=frappe.db.get_value('Club100 Enrollment',name,
            ['status','start_date','origin_trial_enrollment','converted_enrollment'],as_dict=True)
    from frappe.utils import getdate, nowdate
    if (not enrollment or enrollment.status!='Scheduled' or not enrollment.start_date
            or getdate(enrollment.start_date)<=getdate(nowdate())):
        return False, 'Paid membership must be Scheduled and start in future'
    if enrollment.converted_enrollment:
        return False, 'Trial-origin enrollment needs manual review'
    if frappe.db.exists('Club100 Referral Reward',{'enrollment':name}):
        statuses=frappe.get_all('Club100 Referral Reward',
                     filters={'enrollment':name},pluck='status')
        if any(status not in ('On Hold','Pending','Approved','Reversed') for status in statuses):
            return False, 'Issued referral reward requires separate GL recovery'
        if not cint(frappe.conf.get('club100_referral_settlement_enabled',0)):
            return False, 'Enable local referral settlement flag for provisional reward reversal test'
    return True, ''


def run(mode='wallet', enrollment_id=None):
    _guard()
    if mode not in ('wallet','split'):
        raise ValueError('mode must be wallet or split')
    names=[str(enrollment_id)] if enrollment_id else frappe.get_all('Club100 Enrollment',
        filters={'website_source':'PWA Commerce','payment_status':'Paid',
                 'sales_invoice':['is','not set']},
        pluck='name',order_by='creation desc',limit_page_length=40)
    reasons=[]
    original=refunds._read_refund
    try:
        for name in names:
            try:
                ready,reason=_candidate(name,mode)
            except Exception as exc:
                reasons.append({'enrollment':name,'reason':type(exc).__name__+': '+str(exc)[:180]})
                continue
            if not ready:
                reasons.append({'enrollment':name,'reason':reason[:180]})
                continue
            try:
                # Stage5E posts vouchers in the current transaction. Never commit.
                initial=post_one(name,commit=False)
                if initial['status']!='Posted':
                    raise ValueError('Invoice was not newly posted in rollback transaction')
                enrollment=frappe.get_doc('Club100 Enrollment',name)
                from decimal import Decimal
                gateway=Decimal(str(initial['razorpay']))
                rid=('rfnd_STAGE5FC3SPLIT001' if mode=='split' else None)
                if gateway:
                    def mocked_get(provided):
                        if provided!=rid:raise ValueError('Unexpected simulated refund identity')
                        return {'id':rid,'payment_id':enrollment.payment_id,'status':'processed',
                                'currency':'INR','amount':int(gateway*100)}
                    refunds._read_refund=mocked_get
                result=refunds.post_accounted_full_refund(name,rid,commit=False)
                if result['status']!='Simulation Passed' or not result.get('rolled_back'):
                    raise ValueError('Accounted refund did not roll back its savepoint')
                proof=result.get('voucher_reconciliation')
                if (not proof or Decimal(proof['wallet_reconciled']) <= 0 or
                   Decimal(proof['gateway_reconciled']) != gateway or
                   int(proof['original_grants_verified'])<1):
                    raise ValueError('Wallet/split refund GL + same-grant audit failed')
                return {'outcome':'PASS','mode':mode,'enrollment':name,
                        'source_backed_initial_preview':True,
                        'voucher_reconciliation':proof,
                        'credit_note':result['credit_note'],
                        'gateway_entry':result['gateway_refund_entry'],
                        'wallet_entry':result['wallet_refund_entry'],
                        'refund_internally_rolled_back':True,
                        'outer_vouchers_rolled_back_on_return':True,
                        'razorpay_refund_verification':'mocked GET-only' if gateway else 'not applicable',
                        'real_razorpay_requests':0}
            except Exception as exc:
                return {'outcome':'FAIL','mode':mode,'enrollment':name,
                        'error_type':type(exc).__name__,'detail':str(exc)[:350],
                        'all_records_rolled_back_on_return':True}
        return {'outcome':'SKIP','mode':mode,'candidate_count':len(names),
                'sample_reasons':reasons[:5],
                'reason':'No eligible source-GL-backed future Scheduled enrollment. No fake receipt or grant was created.'}
    finally:
        refunds._read_refund=original
        frappe.db.rollback()
