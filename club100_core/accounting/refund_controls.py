"""Stage 5F-C: Administrator-only, READ-ONLY refund/accounting preflight.

Does NOT create Sales Invoice returns, Payment Entries, Journal Entries,
Wallet Ledger records or Razorpay refunds. It exists to stop unsafe partial
financial recovery until a tested atomic credit-note orchestration is ready.
"""
import json
import frappe
from club100_core.accounting.settlement import _admin
from club100_core.accounting.refund_controls_rules import refund_plan
from club100_core.accounting.rules import money

DT = 'Club100 Membership Accounting'


def preview_refund(enrollment_id):
    _admin()
    name = str(enrollment_id or '').strip()
    if not name:
        raise ValueError('Enrollment is required')
    enrollment = frappe.db.get_value('Club100 Enrollment', name,
        ['name','member','status','payment_status','amount_payable','wallet_applied_amount',
         'payment_id','sales_invoice'], as_dict=True)
    if not enrollment:
        raise ValueError('Enrollment not found')
    rec_name = frappe.db.get_value(DT, {'enrollment': name}, 'name')
    if not rec_name:
        return {'enrollment':name, 'status':'Not Accounted',
                'issues':['No Club100 Membership Accounting mapping; use original non-invoiced refund controls only after independent payment checks'],
                'ready_for_refund_posting':False, 'read_only':True}
    rec = frappe.db.get_value(DT, rec_name,
        ['name','member','customer','status','sales_invoice','gateway_payment_entry','wallet_journal_entry',
         'credit_note','gateway_refund_entry','wallet_refund_entry','gross','gateway_paid','wallet_paid',
         'wallet_sources_json','currency'], as_dict=True)
    issues=[]
    plan=None
    try:
        sources = json.loads(rec.wallet_sources_json or '{}')
        plan = refund_plan(rec.gross, rec.wallet_paid, rec.gateway_paid, sources, rec.status)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        issues.append('Original payment split: ' + str(exc))
    if rec.currency != 'INR' or rec.member != enrollment.member or money(rec.gross) != money(enrollment.amount_payable):
        issues.append('Accounting mapping currency, member or amount differs from enrollment')
    if enrollment.sales_invoice != rec.sales_invoice:
        issues.append('Enrollment and accounting Sales Invoice link differ')
    inv = frappe.db.get_value('Sales Invoice', rec.sales_invoice,
        ['name','docstatus','company','customer','is_return','grand_total','outstanding_amount'], as_dict=True) if rec.sales_invoice else None
    if not inv or inv.docstatus != 1 or inv.is_return or inv.customer != rec.customer or money(inv.grand_total) != money(rec.gross):
        issues.append('Original Sales Invoice is missing, cancelled, returned or mismatched')
    elif money(inv.outstanding_amount) != 0:
        issues.append('Original Sales Invoice is not fully settled')
    for doctype, link, amount in (
        ('Payment Entry', 'gateway_payment_entry', rec.gateway_paid),
        ('Journal Entry', 'wallet_journal_entry', rec.wallet_paid),
    ):
        voucher = rec.get(link)
        if money(amount) > 0 and (not voucher or frappe.db.get_value(doctype, voucher, 'docstatus') != 1):
            issues.append('Original ' + doctype + ' is missing or not submitted')
    refund_case = frappe.db.get_value('Club100 Wallet Refund Case', {'enrollment':name},
        ['name','status','gateway_refund_id','wallet_to_restore','gateway_to_refund'],as_dict=True)
    if refund_case:
        if plan and (money(refund_case.wallet_to_restore) != money(rec.wallet_paid)
                     or money(refund_case.gateway_to_refund) != money(rec.gateway_paid)):
            issues.append('Original refund case tender amounts disagree with accounting')
        if refund_case.status == 'Completed' and not rec.credit_note:
            issues.append('Wallet refund completed but submitted accounting credit note is missing')
    if rec.credit_note:
        note=frappe.db.get_value('Sales Invoice',rec.credit_note,
            ['docstatus','is_return','return_against'],as_dict=True)
        if not note or note.docstatus != 1 or not note.is_return or note.return_against != rec.sales_invoice:
            issues.append('Refund credit note is absent or does not reference original invoice')
    if rec.status == 'Refunded' and not rec.credit_note:
        issues.append('Accounting mapping marked Refunded without credit note')
    if rec.status == 'Review':
        issues.append('Accounting record already requires manual review')
    if enrollment.status not in ('Scheduled','Cancelled'):
        issues.append('Membership already started or entitlement status requires manual review')
    # This code intentionally NEVER returns Ready for posting: no tested atomic
    # invoice-return/Razorpay/credit-liability reverse flow exists yet.
    return {'enrollment':name,'record':rec_name,'status':'Review Required',
            'invoice':rec.sales_invoice,'credit_note':rec.credit_note or None,
            'tender_plan':plan,'refund_case_status':refund_case.status if refund_case else None,
            'issues':issues, 'ready_for_refund_posting':False,'read_only':True,
            'missing_implementation':['Credit-note posting and GST reversal',
                'Gateway refund clearing entry after independent Razorpay proof',
                'Return wallet credits to original grants with matching GL liability',
                'Atomic audit and duplicate protection']}


def refund_exposure_report(limit=100):
    _admin()
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError('limit must be an integer in range 1..200')
    ids=frappe.get_all(DT,fields=['enrollment'],order_by='creation desc',limit_page_length=limit+1)
    results=[]
    for row in ids[:limit]:
        result=preview_refund(row.enrollment)
        results.append({'enrollment':row.enrollment,'mapping_status':result['status'],
                        'invoice':result.get('invoice'),'issues':result.get('issues') or [],
                        'refund_case_status':result.get('refund_case_status')})
    return {'records_reviewed':min(len(ids),limit), 'truncated':len(ids)>limit,
            'accounted_enrollments':results,'posting_enabled':False,
            'tax_credit_notes_automated':False, 'wallet_refund_gl_automated':False,
            'production_ready':False, 'read_only':True}
