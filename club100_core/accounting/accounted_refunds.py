"""Stage 5F-C2: controlled, local-only full refund of previously ACCOUNTED Self orders.

An external Razorpay Test Mode refund must already be processed; this module
uses GET-only confirmation. A successful operation creates one ERPNext return
Sales Invoice, one customer refund Payment Entry (if gateway-funded), one
wallet-liability restore Journal Entry (if wallet-funded), and reverses the
original wallet grant ledger plus associated provisional referral rewards.

NEVER exposes a whitelisted method, never issues a Razorpay refund, and never
runs via scheduler. Explicit opt-in env var plus local site flags are required.
Do not use in production until real tax templates, source-backed wallet flows,
refund approval, concurrency, and external bank reconciliation are validated.
"""
import json
import os
import re
from decimal import Decimal

import frappe
from frappe.utils import cint, getdate, nowdate

from club100_core.accounting.rules import money
from club100_core.accounting.refund_posting_rules import refund_identity, refundable_grant
from club100_core.accounting.settlement import _admin, _config, _local_pilot, _internal_write
from club100_core.wallet.recovery import (
    _lock_enrollment, _lock_trial_for_refund, _trial_for_refund,
    _reconcile_trial_after_refund, _authorized_case_write, _get_gateway_refund,
)
from club100_core.wallet.recovery_rules import check_refund
from club100_core.wallet.spending import refund_full

MAPPING = 'Club100 Membership Accounting'
CASE = 'Club100 Wallet Refund Case'


def _allowed():
    return (bool(_local_pilot())
            and cint(frappe.conf.get('club100_accounting_post_enabled', 0)) == 1
            and cint(frappe.conf.get('club100_accounting_refund_post_enabled', 0)) == 1
            and cint(frappe.conf.get('club100_round4b_enabled', 0)) == 1
            and os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND') == '1')


def _require():
    _admin()
    if not _allowed():
        raise ValueError('Accounted refunds disabled: local Test Mode, three site flags and env guard required')


def _read_refund(refund_id):
    """Only a Razorpay TEST credential and GET; never initiate a refund."""
    from club100_core.api.enrollment import _get_razorpay_config
    config = _get_razorpay_config()
    if not str(config.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Refuse live Razorpay credentials in local refund pilot')
    return _get_gateway_refund(refund_id)


def _fetch_record(enrollment):
    name = frappe.db.get_value(MAPPING, {'enrollment': enrollment.name}, 'name')
    if not name:
        raise ValueError('Not an accounted membership; original non-invoiced refund flow applies')
    frappe.db.sql('SELECT name FROM `tabClub100 Membership Accounting` WHERE name=%s FOR UPDATE', name)
    return frappe.get_doc(MAPPING, name)


def _validate_accounted(enrollment, rec, refund_id, *, dry_run=False):
    if rec.status == 'Refunded':
        if (not rec.credit_note or not frappe.db.get_value('Sales Invoice', rec.credit_note, 'docstatus') == 1):
            raise ValueError('Refunded mapping has missing or unsubmitted credit note')
        if (rec.gateway_paid and not rec.gateway_refund_entry) or (rec.wallet_paid and not rec.wallet_refund_entry):
            raise ValueError('Refunded mapping has missing refund tender entries')
        case = frappe.db.get_value(CASE, {'enrollment':enrollment.name},
                ['status','gateway_refund_id'], as_dict=True)
        if not case or case.status != 'Completed' or (refund_id and case.gateway_refund_id != refund_id):
            raise ValueError('Refunded accounting mapping and refund case disagree')
        for doctype, field in (('Payment Entry','gateway_refund_entry'), ('Journal Entry','wallet_refund_entry')):
            voucher = rec.get(field)
            if voucher and frappe.db.get_value(doctype, voucher, 'docstatus') != 1:
                raise ValueError('Refund tender voucher cancelled or missing')
        return {'already_refunded':True}
    if rec.status != 'Posted' or rec.credit_note or rec.gateway_refund_entry or rec.wallet_refund_entry:
        raise ValueError('Accounting record already partially refunded or in review')
    case = frappe.db.get_value(CASE, {'enrollment':enrollment.name},
               ['name','status','gateway_refund_id','wallet_to_restore','gateway_to_refund'],as_dict=True)
    sources = json.loads(rec.wallet_sources_json or '{}')
    plan = refund_identity(rec.gross, rec.wallet_paid, rec.gateway_paid, sources,
        original_status=rec.status, enrollment_status=enrollment.status,
        payment_status=enrollment.payment_status, refund_case_status=case.status if case else None)
    if (enrollment.website_source != 'PWA Commerce' or enrollment.pricing_model != 'Online Plan'
            or cint(enrollment.is_trial) or not enrollment.start_date
            or getdate(enrollment.start_date) <= getdate(nowdate())):
        raise ValueError('Refund only allowed before future paid membership starts')
    if (enrollment.name != rec.enrollment or rec.member != enrollment.member
            or rec.currency != 'INR' or enrollment.sales_invoice != rec.sales_invoice
            or money(enrollment.amount_payable) != plan['gross']
            or money(enrollment.wallet_applied_amount or 0) != plan['wallet']):
        raise ValueError('Accounting-to-enrollment identity or gross/wallet amounts differ')
    if enrollment.get('converted_enrollment'):
        raise ValueError('Trial origin refunds require manual entitlement review')
    _trial_for_refund(enrollment, 'Paid')
    from club100_core.wallet.checkout import expected_gateway_due
    if money(expected_gateway_due(enrollment)) != plan['gateway']:
        raise ValueError('Gateway tender does not reconcile to enrollment')
    inv=frappe.db.get_value('Sales Invoice', rec.sales_invoice,
        ['docstatus','company','customer','is_return','grand_total','outstanding_amount','debit_to'],as_dict=True)
    cfg=_config()
    if (not inv or inv.docstatus != 1 or inv.is_return or inv.customer != rec.customer
            or inv.company != cfg['company'] or money(inv.grand_total) != plan['gross']
            or money(inv.outstanding_amount) != 0):
        raise ValueError('Original submitted invoice and receivable settlement do not reconcile')
    if frappe.db.exists('Sales Invoice', {'return_against':rec.sales_invoice, 'is_return':1,
                                         'docstatus':['!=', 2]}):
        raise ValueError('An existing credit note (including draft) requires manual reconciliation')
    for doctype, field, amount in (('Payment Entry','gateway_payment_entry',plan['gateway']),
                                    ('Journal Entry','wallet_journal_entry',plan['wallet'])):
        voucher=rec.get(field)
        if amount and (not voucher or frappe.db.get_value(doctype, voucher, 'docstatus') != 1):
            raise ValueError('Original payment or wallet GL voucher missing/not submitted')
        if not amount and voucher:
            raise ValueError('Accounting mapping has unexpected zero-tender voucher')
    allocations = []
    if plan['wallet']:
        if not enrollment.wallet_reservation_key or enrollment.wallet_settlement_status != 'Settled':
            raise ValueError('Captured wallet settlement identity required')
        from club100_core.accounting.settlement import _reservation_allocations
        allocation = _reservation_allocations(enrollment)
        allocations = allocation
        if not allocation:
            raise ValueError('Captured wallet reservations not found')
        from collections import defaultdict
        allocated=defaultdict(lambda:Decimal('0.00'))
        for row in allocation:
            allocated[row['source']] += money(row['amount'])
            g=frappe.db.get_value('Club100 Wallet Grant', row['grant'],
                    ['validity_type','expires_on','source_type','member','status'],as_dict=True)
            if (not g or g.member != enrollment.member or g.source_type != row['source']
                    or g.status != 'Active'):
                raise ValueError('Wallet grant provenance or status changed after purchase')
            refundable_grant(g.validity_type, g.expires_on, nowdate())
        if dict(allocated) != plan['sources']:
            raise ValueError('Original booked grant allocations differ from accounting mapping')
    if case and (money(case.wallet_to_restore) != plan['wallet']
                 or money(case.gateway_to_refund) != plan['gateway']
                 or frappe.db.get_value(CASE,case.name,'member') != enrollment.member
                 or frappe.db.get_value(CASE,case.name,'wallet_reservation_key') != (enrollment.wallet_reservation_key or '')):
        raise ValueError('Existing refund case amount differs from accounting mapping')
    if plan['gateway']:
        if enrollment.payment_gateway != 'Razorpay' or not cint(enrollment.get('razorpay_capture_verified')):
            raise ValueError('Original Razorpay capture not verified')
        if not re.fullmatch(r'rfnd_[A-Za-z0-9]+',str(refund_id or '')):
            raise ValueError('Actual Razorpay Test Mode refund ID required')
        if frappe.db.exists('Payment Entry', {'reference_no':refund_id,'docstatus':1}):
            raise ValueError('Razorpay refund ID already recorded in an ERPNext Payment Entry')
        if case and case.gateway_refund_id and case.gateway_refund_id != refund_id:
            raise ValueError('Refund ID changed from prepared case')
        if frappe.db.exists(CASE, {'gateway_refund_id': refund_id, 'enrollment':['!=',enrollment.name]}):
            raise ValueError('Razorpay refund ID already used for another enrollment')
        data = _read_refund(refund_id)
        if check_refund(data, enrollment.payment_id, int(plan['gateway']*100), refund_id) != 'processed':
            raise ValueError('Razorpay refund not independently verified as processed')
    elif refund_id:
        raise ValueError('Wallet-only purchase must not specify Razorpay refund ID')
    if frappe.db.exists('Club100 Referral Reward',{'enrollment':enrollment.name}):
        # Block issued or already-spent rewards: reversal needs separate booked
        # referral-expense/liability adjustment. Provisional rewards can reverse.
        rewards=frappe.get_all('Club100 Referral Reward',filters={'enrollment':enrollment.name},fields=['status'])
        if any(r.status not in ('On Hold','Pending','Approved','Reversed') for r in rewards):
            raise ValueError('Issued/cash referral reward requires manual GL reversal')
        from club100_core.referrals.settlement import refund_preflight
        refund_preflight(enrollment.name)
    return {'already_refunded':False,'plan':plan,'config':cfg,'invoice':inv,
            'case':case,'allocations':allocations}


def _make_credit_note(rec, plan):
    from erpnext.controllers.sales_and_purchase_return import make_return_doc
    note=make_return_doc('Sales Invoice',rec.sales_invoice)
    note.set('set_posting_time',1)
    note.set('posting_date',getdate(nowdate()))
    note.set('posting_time',frappe.utils.nowtime())
    note.set('due_date',getdate(nowdate()))
    note.set('update_stock',0)
    note.set('is_pos',0)
    note.set('payments',[])
    note.insert(ignore_permissions=True)
    original = frappe.db.get_value('Sales Invoice',rec.sales_invoice,
           ['net_total','total_taxes_and_charges'],as_dict=True)
    if (not cint(note.is_return) or note.return_against != rec.sales_invoice
           or money(note.grand_total) != -plan['gross'] or note.customer != rec.customer
           or not original or money(note.net_total) != -money(original.net_total)
           or money(note.total_taxes_and_charges) != -money(original.total_taxes_and_charges)):
        raise ValueError('ERPNext return is not an exact full credit note')
    note.submit()
    if note.docstatus != 1:
        raise ValueError('GST credit note failed to submit')
    return note


def _refund_gateway_entry(rec, cfg, note, amount, refund_id):
    if amount == 0:
        return None
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    pay=get_payment_entry('Sales Invoice',note.name)
    # A fully paid Sales Invoice return must produce a customer Pay Payment Entry.
    if pay.payment_type != 'Pay' or pay.party_type != 'Customer' or pay.party != rec.customer:
        raise ValueError('ERPNext returned unexpected refund Payment Entry direction')
    pay.paid_from = cfg['razorpay_clearing']
    pay.paid_to = note.debit_to
    pay.paid_amount = float(amount)
    pay.received_amount = float(amount)
    pay.reference_no = refund_id
    pay.reference_date = getdate(nowdate())
    refs=[row for row in pay.references if row.reference_doctype == 'Sales Invoice' and row.reference_name == note.name]
    if len(refs) != 1:
        raise ValueError('Return payment must reference exactly the credit note')
    for row in pay.references:
        row.allocated_amount = -float(amount) if row is refs[0] else 0
    pay.insert(ignore_permissions=True)
    pay.submit()
    if pay.docstatus != 1:
        raise ValueError('Razorpay clearing refund Payment Entry not submitted')
    return pay


def _restore_wallet_liability(rec, cfg, note, sources, wallet):
    if wallet == 0:
        return None
    rows=[{'account':note.debit_to, 'party_type':'Customer', 'party':note.customer,
           'reference_type':'Sales Invoice', 'reference_name':note.name,
           'debit_in_account_currency':float(wallet), 'credit_in_account_currency':0}]
    for source, amount in sources.items():
        rows.append({'account':cfg[source], 'credit_in_account_currency':float(amount),
                     'debit_in_account_currency':0})
    je=frappe.get_doc({'doctype':'Journal Entry','company':cfg['company'],
         'voucher_type':'Journal Entry','posting_date':getdate(nowdate()),
         'user_remark':'Club100 accounting refund wallet restore '+rec.enrollment,
         'accounts':rows})
    je.insert(ignore_permissions=True)
    je.submit()
    if je.docstatus != 1:
        raise ValueError('Wallet liability refund Journal Entry not submitted')
    return je


def _write_case(enrollment, plan, refund_id):
    case_name=frappe.db.get_value(CASE,{'enrollment':enrollment.name},'name')
    if case_name:
        case=frappe.get_doc(CASE,case_name)
    else:
        case=frappe.get_doc({'doctype':CASE,'enrollment':enrollment.name,
             'member':enrollment.member,'case_kind':'Paid', 'status':'Prepared',
             'payment_id':enrollment.payment_id or '',
             'wallet_reservation_key':enrollment.wallet_reservation_key or '',
             'wallet_to_restore':float(plan['wallet']),
             'gateway_to_refund':float(plan['gateway']), 'currency':'INR'})
        with _authorized_case_write():
            case.insert(ignore_permissions=True)
    if case.status not in ('Prepared','Gateway Pending') or case.payment_id != enrollment.payment_id:
        raise ValueError('Refund case conflicts with existing refund workflow')
    with _authorized_case_write():
        case.gateway_refund_id=refund_id or ''
        case.last_gateway_status='processed' if refund_id else 'not_applicable'
        case.status='Completed'
        case.confirmed_at=frappe.utils.now_datetime()
        case.save(ignore_permissions=True)
    return case


def post_accounted_full_refund(enrollment_id, gateway_refund_id=None, *, commit=False):
    """System-Manager-only local pilot. commit=True requires separate env flag.

    All source subledger, credit note and tender postings share the SAME DB
    transaction. No gateway refund POST. If the actual gateway refund was
    already processed, retry is safe after a database failure.
    """
    _require()
    if not commit and os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND_SMOKE') != '1':
        raise ValueError('Dry-run refund is restricted to the rollback-only integration smoke')
    if commit and os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND_COMMIT') != '1':
        raise ValueError('Persistent posting needs an additional explicit environment guard')
    enrollment_id=str(enrollment_id or '').strip()
    enrollment=_lock_enrollment(enrollment_id)
    _lock_trial_for_refund(enrollment)
    rec=_fetch_record(enrollment)
    proof=str(gateway_refund_id or '').strip()
    result=_validate_accounted(enrollment,rec,proof)
    if result.get('already_refunded'):
        return {'status':'Already Refunded','enrollment':enrollment.name,
                'credit_note':rec.credit_note,'gateway_refund_entry':rec.gateway_refund_entry or None,
                'wallet_refund_entry':rec.wallet_refund_entry or None}
    plan=result['plan']
    cfg=result['config']
    frappe.db.savepoint('club100_accounted_refund')
    try:
        note=_make_credit_note(rec,plan)
        gateway=_refund_gateway_entry(rec,cfg,note,plan['gateway'],proof)
        wallet=_restore_wallet_liability(rec,cfg,note,plan['sources'],plan['wallet'])
        if plan['wallet']:
            restored=refund_full(enrollment.wallet_reservation_key)
            if restored['status'] != 'Refunded':
                raise ValueError('Wallet refund did not settle to original grants')
        # Verify both real ERPNext voucher legs and the exact original-grant
        # ledger restoration before marking the membership/refund case complete.
        from club100_core.accounting.refund_reconciliation import verify_full_refund
        audit = verify_full_refund(enrollment, rec, cfg, plan, result['invoice'],
                  note, gateway, wallet, result['allocations'])
        if frappe.db.exists('Club100 Referral Reward',{'enrollment':enrollment.name}):
            from club100_core.referrals.settlement import reverse_for_enrollment
            reverse_for_enrollment(enrollment.name)
        case=_write_case(enrollment,plan,proof)
        enrollment.status='Cancelled'
        enrollment.payment_status='Refunded'
        if plan['wallet']:
            enrollment.wallet_settlement_status='Refunded'
        enrollment.save(ignore_permissions=True)
        _reconcile_trial_after_refund(enrollment,'Paid')
        with _internal_write():
            rec.credit_note=note.name
            rec.gateway_refund_entry=gateway.name if gateway else None
            rec.wallet_refund_entry=wallet.name if wallet else None
            rec.status='Refunded'
            rec.save(ignore_permissions=True)
        # A return invoice may carry residual credit if ERPNext does not
        # allocate either reversal correctly. Reject instead of silently
        # leaving an outstanding customer credit.
        remaining=money(frappe.db.get_value('Sales Invoice',note.name,'outstanding_amount'))
        if remaining != 0:
            raise ValueError('Refund credit note has residual outstanding: '+str(remaining))
        out={'status':'Refunded','enrollment':enrollment.name,'credit_note':note.name,
             'gateway_refund_entry':gateway.name if gateway else None,
             'wallet_refund_entry':wallet.name if wallet else None,
             'wallet_restored':str(plan['wallet']),'gateway_verified':str(plan['gateway']),
             'refund_case':case.name,'no_gateway_refund_post':True,
             'voucher_reconciliation':audit}
        if commit:
            frappe.db.commit()
        else:
            frappe.db.rollback(save_point='club100_accounted_refund')
            out['rolled_back'] = True
            out['status'] = 'Simulation Passed'
        return out
    except Exception:
        frappe.db.rollback(save_point='club100_accounted_refund')
        raise


def healthcheck():
    _admin()
    return {'pilot_site':_local_pilot(), 'posting_enabled':_allowed(),
            'persistent_refunds_not_default':True,'external_gateway_refund_required':True,
            'refund_auto_scheduler':False,'production_ready':False}
