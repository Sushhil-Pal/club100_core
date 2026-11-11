"""One guarded Club100 hybrid checkout -> verified evidence -> settled membership flow.

This is an INTEGRATION candidate for local Razorpay TEST-mode UAT. Real checkout
routes and financial posting both require separate site+process opt-ins.
No bank, CA, grant-funding, or refund prerequisites are silently waived.

Public API input is LIMITED to member-owned new enrollment preferences. Invoice,
wallet funding, tax rate, amount, order identity and settlement evidence are
always derived from server state and independently authenticated provider data.
"""
import json
import os
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe.utils import cint, getdate, nowdate, now_datetime, get_datetime, add_months, add_days

from club100_core.accounting import settlement, hybrid_reservations as reservations
from club100_core.accounting.hybrid_gst import current_rate, inclusive_breakdown, verify_tax_rows
from club100_core.accounting.promotion_rules import rupees
from club100_core.accounting.hybrid_integration_rules import validate_frozen_checkout, verify_razorpay_evidence
from club100_core.accounting.hybrid_invoice_contract import build_invoice_review
from club100_core.accounting.wallet_backing import verify_wallet_grant_backing
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.spending import capture

EVIDENCE = 'Club100 Hybrid Payment Evidence'
ACCOUNTING = 'Club100 Membership Accounting'


def enabled():
    """Hybrid commerce is available automatically in local Razorpay TEST development."""
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def financial_enabled():
    # Not a feature switch. Source GL, tax, payment and entitlement checks
    # remain enforced at transaction boundaries; checkout cannot charge first.
    return enabled()


def require_enabled():
    if not enabled():
        raise ValueError('This development integration requires club100.local / Razorpay TEST mode')


def require_financial_enabled():
    require_enabled()


@contextmanager
def _flag(name):
    old = getattr(frappe.flags, name, False)
    setattr(frappe.flags, name, True)
    try:
        yield
    finally:
        setattr(frappe.flags, name, old)




@contextmanager
def _verified_voucher_user():
    """ERPNext account/link permissions only for authenticated hybrid vouchers.

    Public API caller's member session is restored even when a voucher throws.
    This cannot be invoked without the independently verified Razorpay bridge.
    Wallet reservation, provider validation and quote decisions happen outside.
    """
    if not getattr(frappe.flags, 'club100_hybrid_authenticated_bridge', False):
        raise PermissionError('Unverified hybrid request cannot post as accounting user')
    require_financial_enabled()
    prior_user = frappe.session.user
    try:
        if prior_user != 'Administrator':
            frappe.set_user('Administrator')
        yield
    finally:
        if frappe.session.user != prior_user:
            frappe.set_user(prior_user)

def _funding_plan(hybrid):
    """Requires per-grant source evidence AND sufficient actual GL liability."""
    pairs = reservations._pair(hybrid, ('Reserved',))
    funded = pairs['funded']
    if not funded:
        return {}, pairs
    from club100_core.accounting.hybrid_backing_rules import validate_funded_allocations
    _, by_source = validate_funded_allocations(funded, str(rupees(hybrid.funded_wallet_tender)))
    cfg = settlement._config()
    # Serialize source-liability headroom checks across members: a second
    # simultaneous checkout must not spend the same accounting credit twice.
    for account in sorted({cfg[source] for source in by_source}):
        frappe.db.sql('SELECT name FROM `tabAccount` WHERE name=%s FOR UPDATE', account)
    verified = verify_wallet_grant_backing(cfg, funded)
    if verified != by_source:
        raise ValueError('Source-specific wallet backing does not match held allocations')
    settlement._require_wallet_liability_backing(cfg, by_source, funded)
    return by_source, pairs


def _quote_for_member(member, program, months):
    from club100_core.api.commerce_checkout import _quote
    base = _quote(member, program, months)
    balance = wallet_balance(member.name)
    # Only GL-backed grants are offered as payment tender. The final order
    # creation rechecks under locks; this is just read-only UI eligibility.
    from club100_core.accounting.wallet_quote import verified_wallet_quote
    backed = verified_wallet_quote(balance)
    approved = set()
    policy = str(frappe.conf.get('club100_hybrid_promotion_policy_version') or 'member-funded-v1').strip()
    if policy:
        approved = set(reservations._eligible_promotion_grants(member.name, policy))
    allowed_promo = sum((rupees(g['spendable_remaining']) for g in balance['grants']
                         if g['grant'] in approved and g['source'] == 'Promotion' and g['spendable']),
                        Decimal('0.00'))
    rate = current_rate()
    gross = rupees(base['price']['amount_payable'])
    return base, {'enabled': financial_enabled(), 'original': str(gross), 'gstPercent': str(rate),
                  'displayedFundedBalance': backed['total'],
                  'approvedPromotionBalance': str(allowed_promo),
                  'bySource': {**balance['by_source'], **backed['by_source']},
                  'unbackedBySource': backed['unbacked_by_source'],
                  'currency': 'INR',
                  'fundingVerificationRequired': True,
                  'paidStartDate': str(base['start'])}


def quote(program, months):
    require_enabled()
    from club100_core.api.member import _get_current_member
    member = _get_current_member()
    if member.status != 'Active' or member.onboarding_status != 'Completed':
        raise ValueError('Complete your member profile first')
    _, result = _quote_for_member(member, program, months)
    return result


def _frozen(hybrid):
    keys = ('enrollment', 'member', 'status', 'policy_version', 'original_price',
            'promotion_discount', 'funded_wallet_tender', 'gateway_due', 'gst_percent',
            'promotion_reservation_key', 'funded_reservation_key', 'plan_snapshot_json')
    return {key: hybrid.get(key) for key in keys}


def _calculated_tax(plan, cfg, tax_rows, description):
    """Run the actual unsaved ERPNext tax engine before charging or submitting."""
    expected = inclusive_breakdown(plan['original_price_incl_gst'],
            plan['promotion_discount'], plan['funded_wallet_tender'], plan['tax_rate_percent'])
    verify_tax_rows(tax_rows, plan['tax_rate_percent'])
    spec = {'doctype': 'Sales Invoice', 'company': cfg['company'],
            'customer': cfg['customer'], 'currency': 'INR', 'update_stock': 0,
            'taxes_and_charges': cfg['tax_template'], 'taxes': tax_rows,
            'posting_date': nowdate(), 'due_date': nowdate(),
            'apply_discount_on': 'Grand Total',
            'discount_amount': float(rupees(plan['promotion_discount'])),
            'items': [{'item_code': cfg['item'], 'qty': 1,
                       'rate': float(rupees(plan['original_price_incl_gst'])),
                       'description': description}]}
    doc = frappe.get_doc(spec)
    doc.calculate_taxes_and_totals()
    for key, amount in (('grand_total', expected['invoice']),
                        ('net_total', expected['net']),
                        ('total_taxes_and_charges', expected['gst'])):
        if rupees(str(doc.get(key))) != rupees(amount):
            raise ValueError('ERPNext GST calculation mismatch (' + key + '); no further posting')
    if rupees(str(doc.discount_amount or 0)) != rupees(expected['discount']):
        raise ValueError('ERPNext supplier discount does not match frozen promotion')
    return spec, expected


def _new_enrollment(member, base):
    p = base['price']
    doc = frappe.get_doc({'doctype': 'Club100 Enrollment',
        'member': member.name, 'full_name': member.full_name or member.name,
        'mobile': member.mobile, 'email': member.email,
        'program': base['program'], 'plan': base['plan'],
        'pricing_model': 'Online Plan', 'enrollment_type': 'Individual',
        'enrollment_date': nowdate(), 'status': 'Registered', 'payment_status': 'Pending',
        'start_date': base['start'], 'membership_duration_months': p['duration_months'],
        'monthly_equivalent': p['monthly_equivalent'], 'amount_payable': p['amount_payable'],
        'website_source': 'PWA Commerce',
        'origin_trial_enrollment': base['trial'].name if base['trial'] else None})
    doc.insert(ignore_permissions=True)
    return doc


def _new_order(enrollment_id):
    """Reuse pinned D2C2B order construction/response checks, not its admin gate."""
    from club100_core.accounting.hybrid_order_service import (
        _locked_plan, _http_request, URL)
    from club100_core.accounting.hybrid_order_contract import validate_order_response
    from club100_core.api.enrollment import _get_razorpay_config
    doc, hybrid, pre, payload = _locked_plan(enrollment_id)
    cfg = _get_razorpay_config()
    if cfg.get('mode') != 'test' or not str(cfg.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Only Razorpay TEST credentials are accepted')
    if doc.payment_order_id:
        order = _http_request('GET', URL + '/' + doc.payment_order_id, cfg)
        checked = validate_order_response(payload, order, reuse=True)
        if checked['order_id'] != doc.payment_order_id:
            raise ValueError('Razorpay stored order identity mismatch')
    else:
        response = _http_request('POST', URL, cfg, payload)
        checked = validate_order_response(payload, response)
        if get_datetime(hybrid.expires_at) <= now_datetime():
            raise ValueError('Payment hold expired during Razorpay order creation')
        doc.payment_gateway = 'Razorpay'
        doc.payment_order_id = checked['order_id']
        doc.save(ignore_permissions=True)
    return {'success': True, 'enrollment': doc.name, 'key_id': cfg['key_id'],
        'mode': cfg['mode'], 'order_id': checked['order_id'],
        'amount': checked['amount'], 'currency': 'INR',
        'program': doc.program, 'durationMonths': cint(doc.membership_duration_months),
        'paidStartDate': str(doc.start_date), 'hybrid': True,
        'checkoutStatus': 'pending', 'do_not_retry_payment_if_charged': True}


def create(program, duration_months, promotion_discount='0', wallet_amount='0'):
    """Member-owned, single-flow checkout. No client-controlled prices or rates."""
    require_financial_enabled()  # Never take Razorpay money if settlement is disabled.
    from club100_core.api.commerce_checkout import _member_locked
    member = _member_locked()
    # Decimal-source validation. Never trust browser strings as settled amounts.
    discount = rupees(promotion_discount)
    wallet = rupees(wallet_amount)
    if discount == 0 and wallet == 0:
        raise ValueError('Use the ordinary checkout if no hybrid tender is selected')
    base, _ = _quote_for_member(member, program, duration_months)
    rate = current_rate()
    plan = inclusive_breakdown(base['price']['amount_payable'], discount, wallet, rate)
    # Provision and validate member-specific ERPNext Customer BEFORE an order.
    # Savepoint includes new Customer/Contact, so a failed checkout leaves no
    # partially created billing party.
    point = 'club100_hybrid_new_' + uuid4().hex[:12]
    frappe.db.savepoint(point)
    try:
        from club100_core.accounting.billing_customer import ensure_customer_for_member
        cfg = settlement._config()
        cfg['customer'] = ensure_customer_for_member(member.name, cfg['company'])
        taxes = settlement._ready(cfg, {}, rupees(plan['gateway']))
        _calculated_tax({'original_price_incl_gst': plan['original'],
             'promotion_discount': plan['discount'], 'funded_wallet_tender': plan['wallet'],
             'tax_rate_percent': plan['gst_percent']}, cfg, taxes, 'Club100 Self Membership')
        doc = _new_enrollment(member, base)
        policy = str(frappe.conf.get('club100_hybrid_promotion_policy_version') or 'member-funded-v1')
        with _flag('club100_hybrid_e2e_checkout'):
            reservations.reserve_hybrid(doc.name, str(discount), str(wallet), policy)
        hybrid_id = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment': doc.name}, 'name')
        hybrid = frappe.get_doc('Club100 Hybrid Checkout', hybrid_id)
        _funding_plan(hybrid)  # Recheck under reservation locks before any payment order.
        return _new_order(doc.name)
    except Exception:
        frappe.db.rollback(save_point=point)
        raise


def _owned_hybrid(enrollment_id):
    from club100_core.api.member import _get_current_member
    member = _get_current_member()
    name = str(enrollment_id or '').strip()
    row = frappe.db.get_value('Club100 Enrollment', name,
          ['name','member','website_source','payment_status','status'], as_dict=True)
    if not row or row.member != member.name or row.website_source != 'PWA Commerce':
        frappe.throw('Checkout does not belong to your member account', frappe.PermissionError)
    hybrid = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment': name},
          ['name','status','expires_at'], as_dict=True)
    if not hybrid:
        raise ValueError('This is not a hybrid checkout')
    return row, hybrid


def resume(enrollment_id):
    require_financial_enabled()
    row, hybrid = _owned_hybrid(enrollment_id)
    if row.payment_status != 'Pending' or row.status != 'Registered' or hybrid.status != 'Reserved':
        raise ValueError('Payment already received or held for review; do not pay again')
    if get_datetime(hybrid.expires_at) <= now_datetime():
        raise ValueError('This wallet hold has expired; contact support before retrying')
    if frappe.db.exists(EVIDENCE, {'enrollment': row.name}):
        raise ValueError('Verified payment evidence already exists; do not pay again')
    from club100_core.accounting.billing_customer import ensure_customer_for_member
    from club100_core.api.member import _get_current_member
    member = _get_current_member()
    ensure_customer_for_member(member.name, settlement._config()['company'])
    return _new_order(row.name)


def state(program):
    require_enabled()
    from club100_core.api.member import _get_current_member
    member = _get_current_member()
    plan = frappe.db.get_value('Club100 Plan', {'plan_code': 'self', 'status':'Active'}, 'name')
    if not plan or not frappe.db.exists('Club100 Program', str(program or '')):
        raise ValueError('Valid Self plan and program required')
    rows = frappe.get_all('Club100 Enrollment',
        filters={'member':member.name, 'program':program,'plan':plan,
                 'website_source':'PWA Commerce','is_trial':0},
        fields=['name','status','payment_status','start_date','amount_payable'],
        order_by='creation desc', limit_page_length=12)
    for row in rows:
        # A captured Active/Completed purchase is historical. It must not
        # suppress the renewal checkout for the member's current paid term.
        if row.payment_status == 'Paid' and row.status in ('Active', 'Completed'):
            continue
        h = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment':row.name},
               ['status','expires_at'],as_dict=True)
        if not h:
            continue
        e = frappe.db.get_value(EVIDENCE, {'enrollment':row.name}, ['state','review_reason'],as_dict=True)
        status = ('paid' if row.payment_status == 'Paid' and h.status == 'Captured'
                  else 'review' if e or h.status == 'Review'
                  else 'expired' if get_datetime(h.expires_at) <= now_datetime()
                  else 'pending')
        return {'state':status, 'hybrid':True, 'enrollment':row.name,
                'status':row.status,'paymentStatus':row.payment_status,
                'amountPayable':float(row.amount_payable), 'paidStartDate':str(row.start_date),
                'doNotRetry':bool(e), 'reviewReason':e.review_reason if e else None}
    return {'state':'none','hybrid':False}


def _ledger_rows(reservation):
    names = ['WALLET:SPEND:' + reservation.name + ':' + row.grant for row in reservation.items]
    if not names:
        return []
    raw = frappe.get_all('Club100 Wallet Ledger', filters={'event_key':['in',names]},
        fields=['event_key','wallet','member','grant','entry_type','amount','currency','reference'],
        limit_page_length=len(names)+1)
    return [{**dict(item), 'amount':str(item.amount)} for item in raw]


def _settle(enrollment_id, signed_order, signed_payment):
    """Called only after the existing bridge validates HMAC and fetches Razorpay.

    This re-verifies the stored evidence, source GL, hold, tax, submitted vouchers,
    ledger and zero outstanding under member->wallet->enrollment locks.
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    from club100_core.accounting.hybrid_capture_rules import planned_spends, verify_captured_ledger
    from club100_core.api.enrollment import _finalize_paid_enrollment_doc
    doc, hybrid = reservations._get_locked(enrollment_id)
    ev_name = frappe.db.get_value(EVIDENCE, {'enrollment':doc.name}, 'name')
    if not ev_name:
        raise ValueError('Verified payment evidence missing')
    frappe.db.sql('SELECT name FROM `tabClub100 Hybrid Payment Evidence` WHERE name=%s FOR UPDATE', ev_name)
    evidence = frappe.get_doc(EVIDENCE, ev_name)
    prior = frappe.db.get_value(ACCOUNTING, {'enrollment':doc.name}, 'name')
    if prior:
        mapping = frappe.get_doc(ACCOUNTING, prior)
        if (evidence.state != 'Settled' or hybrid.status != 'Captured'
                or doc.payment_status != 'Paid' or doc.sales_invoice != mapping.sales_invoice
                or evidence.razorpay_payment_id != doc.payment_id):
            raise ValueError('Existing accounting mapping conflicts with hybrid state')
        return {'success':True, 'enrollment':doc.name, 'status':doc.status,
                'payment_status':'Paid', 'invoice':mapping.sales_invoice, 'idempotent':True}
    if evidence.state != 'Verified Pending Invoice' or evidence.provider_status != 'captured':
        raise ValueError('Evidence is not eligible for settlement')
    if (doc.payment_status != 'Pending' or doc.status != 'Registered' or doc.payment_id
            or doc.sales_invoice or hybrid.status != 'Reserved'
            or get_datetime(hybrid.expires_at) <= now_datetime()):
        raise ValueError('Enrollment or hybrid hold requires manual review')
    pairs = reservations._pair(hybrid, ('Reserved',))
    pre = validate_frozen_checkout(_frozen(hybrid), pairs['promotion'], pairs['funded'])
    verify_razorpay_evidence(pre, doc.payment_order_id, signed_order, signed_payment)
    e = {key:evidence.get(key) for key in ('name','enrollment','member','hybrid_checkout',
         'razorpay_order_id','razorpay_payment_id','amount_paise','snapshot_sha256',
         'identity_sha256','provider_status','state')}
    review = build_invoice_review(pre, e, hybrid.name, pairs['promotion'], pairs['funded'])
    # Snapshot + currently configured GST must still agree. A rate change after
    # order creation fails closed for manual finance review (never reprice paid order).
    if rupees(str(hybrid.gst_percent)) != current_rate():
        raise ValueError('GST configuration changed since order: manual invoice review required')
    if rupees(doc.amount_payable) != rupees(hybrid.original_price):
        raise ValueError('Enrollment original price changed since order')
    if doc.payment_order_id != evidence.razorpay_order_id or evidence.razorpay_payment_id != signed_payment['id']:
        raise ValueError('Stored payment identity mismatch')
    if rupees(hybrid.promotion_discount or 0):
        approved = set(reservations._eligible_promotion_grants(hybrid.member,hybrid.policy_version))
        if any(a['grant'] not in approved for a in pairs['promotion']):
            raise ValueError('Promotion approval changed since checkout')
    sources, _ = _funding_plan(hybrid)
    cfg = settlement._config()
    from club100_core.accounting.billing_customer import require_existing_for_member
    cfg['customer'] = require_existing_for_member(doc.member, cfg['company'])
    gateway = rupees(hybrid.gateway_due)
    # Verified order, evidence, frozen price and GL backing precede elevation.
    with _verified_voucher_user():
        tax_rows = settlement._ready(cfg, sources, gateway)
        plan = pre['plan']
        service_end = add_days(add_months(getdate(doc.start_date), cint(doc.membership_duration_months)), -1)
        spec, calculated = _calculated_tax(plan, cfg, tax_rows, ('Club100 membership ' + doc.name + ' | ' + str(doc.program or '')
             + ' | ' + str(doc.membership_duration_months) + ' months | '
             + str(doc.start_date) + ' to ' + str(service_end)))
        invoice_date = getdate(doc.payment_date or nowdate())
        spec.update({'posting_date':invoice_date, 'due_date':invoice_date,
            'set_posting_time':1,'posting_time':now_datetime().strftime('%H:%M:%S'),
            'remarks':'Club100 hybrid: '+doc.name+'; verified payment '+evidence.razorpay_payment_id})
        invoice = frappe.get_doc(spec)
        invoice.insert(ignore_permissions=True)
        for key, amount in (('grand_total',calculated['invoice']),
                            ('net_total',calculated['net']),('total_taxes_and_charges',calculated['gst'])):
            if rupees(str(invoice.get(key))) != rupees(amount):
                raise ValueError('ERPNext inserted invoice mismatched frozen quote')
        invoice.submit()
        if cint(invoice.docstatus) != 1:
            raise ValueError('Invoice submission failed')
        payment = get_payment_entry('Sales Invoice',invoice.name, party_amount=float(gateway))
        payment.paid_to = cfg['razorpay_clearing']
        payment.paid_amount = float(gateway)
        payment.received_amount = float(gateway)
        payment.reference_no = evidence.razorpay_payment_id
        payment.reference_date = invoice_date
        refs = [r for r in payment.references if r.reference_doctype=='Sales Invoice' and r.reference_name==invoice.name]
        if len(refs)!=1 or len(payment.references)!=1:
            raise ValueError('Payment Entry must reference exactly one invoice')
        payment.references[0].allocated_amount = float(gateway)
        payment.insert(ignore_permissions=True);payment.submit()
        if cint(payment.docstatus)!=1:
            raise ValueError('Gateway clearing Payment Entry not submitted')
        journal = None
        if sources:
            rows=[]
            for source in sorted(sources):
                rows.append({'account':cfg[source], 'debit_in_account_currency':float(sources[source]),
                             'credit_in_account_currency':0})
            rows.append({'account':invoice.debit_to,'party_type':'Customer','party':invoice.customer,
                 'reference_type':'Sales Invoice','reference_name':invoice.name,
                 'credit_in_account_currency':float(rupees(hybrid.funded_wallet_tender)),
                 'debit_in_account_currency':0})
            journal=frappe.get_doc({'doctype':'Journal Entry', 'company':cfg['company'],
                 'voucher_type':'Journal Entry','posting_date':invoice_date,
                 'user_remark':'Club100 source-backed wallet redemption '+doc.name,
                 'accounts':rows})
            journal.insert(ignore_permissions=True);journal.submit()
            if cint(journal.docstatus)!=1:
                raise ValueError('Wallet liability Journal Entry not submitted')
    if rupees(str(frappe.db.get_value('Sales Invoice',invoice.name,'outstanding_amount'))) != Decimal('0.00'):
        raise ValueError('Invoice outstanding is nonzero after payment and wallet posting')
    if sources:
        funded_name = frappe.db.get_value('Club100 Wallet Reservation',
             {'reservation_key':hybrid.funded_reservation_key}, 'name')
        if not funded_name:
            raise ValueError('Funded wallet reservation not found')
        funded_hold = frappe.get_doc('Club100 Wallet Reservation', funded_name)
        ctx={'enrollment':doc.name,'member':doc.member,'wallet':funded_hold.wallet,
            'reservation':funded_hold.name,'reservation_key':hybrid.funded_reservation_key,
            'invoice':invoice.name,'payment_entry':payment.name,'journal_entry':journal.name,
            'evidence':evidence.name,'hybrid_checkout':hybrid.name,
            'hybrid_status':'Reserved','reservation_status':'Reserved',
            'provider_status':'captured','evidence_state':'Verified Pending Invoice',
            'hold_not_expired':get_datetime(funded_hold.expires_at)>now_datetime(),
            'invoice_docstatus':1,'payment_docstatus':1,'journal_docstatus':1,
            'invoice_member':doc.member,'reservation_member':funded_hold.member,
            'reservation_reference':doc.name,'payment_invoice':invoice.name,
            'journal_invoice':invoice.name,'invoice_outstanding':'0.00',
            'original_price':str(rupees(hybrid.original_price)),
            'promotion_discount':str(rupees(hybrid.promotion_discount or 0)),
            'invoice_amount':calculated['invoice'],'gateway_amount':str(gateway),
            'wallet_amount':str(rupees(hybrid.funded_wallet_tender)),
            'funding_preflight_verified':True,
            'wallet_journal_amount':str(rupees(hybrid.funded_wallet_tender))}
        capture_plan=planned_spends(ctx,pairs['funded'],
              {s:str(v) for s,v in sources.items()})
        capture(hybrid.funded_reservation_key,doc.name)
        updated=frappe.get_doc('Club100 Wallet Reservation',funded_hold.name)
        verify_captured_ledger(capture_plan,updated.status,_ledger_rows(updated))
    if hybrid.promotion_reservation_key:
        # Promotion uses its own grant allowance. No GL tender is posted for it.
        capture(hybrid.promotion_reservation_key,doc.name)
    with settlement._internal_write():
        mapping=frappe.get_doc({'doctype':ACCOUNTING,
             'enrollment':doc.name,'member':doc.member,'customer':invoice.customer,
             'sales_invoice':invoice.name,'gateway_payment_entry':payment.name,
             'wallet_journal_entry':journal.name if journal else '',
             'gross':calculated['invoice'],'wallet_paid':hybrid.funded_wallet_tender,
             'gateway_paid':gateway,'currency':'INR','status':'Posted',
             'wallet_sources_json':json.dumps({s:str(v) for s,v in sources.items()},sort_keys=True)})
        mapping.insert(ignore_permissions=True)
    hybrid.status='Captured';hybrid.payment_reference=evidence.razorpay_payment_id
    hybrid.captured_at=now_datetime()
    with reservations._pilot_write():hybrid.save(ignore_permissions=True)
    evidence.state='Settled';evidence.review_reason=''
    with _flag('club100_hybrid_evidence_write'):evidence.save(ignore_permissions=True)
    doc.sales_invoice=invoice.name
    doc.payment_gateway='Razorpay';doc.payment_id=evidence.razorpay_payment_id
    doc.payment_date=now_datetime();doc.razorpay_capture_verified=1
    doc.wallet_settlement_status='Settled' if sources else ''
    doc.payment_status='Paid';doc.save(ignore_permissions=True)
    final=_finalize_paid_enrollment_doc(doc)
    return {'success':True,'enrollment':doc.name,'status':doc.status,
       'payment_status':'Paid','invoice':invoice.name,
       'wallet_captured':bool(sources),'membership_activated':True,
       'payment_entry':payment.name,'wallet_journal':journal.name if journal else None,
       **final}


def settle_verified_from_bridge(enrollment_id, order, payment):
    """Only bridge calls this after verified HMAC and server-fetched captured payment.

    Errors become durable manual-review evidence, NEVER a second-charge prompt.
    This function never commits: Frappe POST request owns the outer transaction.
    """
    require_financial_enabled()
    if not getattr(frappe.flags,'club100_hybrid_authenticated_bridge',False):
        raise ValueError('Only independently authenticated payment callback may settle')
    point='club100_hybrid_post_' + uuid4().hex[:12]
    frappe.db.savepoint(point)
    try:
        return _settle(enrollment_id, order, payment)
    except Exception as exc:
        frappe.db.rollback(save_point=point)
        # Evidence already exists, as it was captured BEFORE the settlement
        # savepoint in the authenticated bridge. Leave member Pending/Registered.
        e_name=frappe.db.get_value(EVIDENCE,{'enrollment':enrollment_id},'name')
        if not e_name:
            raise
        evidence=frappe.get_doc(EVIDENCE,e_name)
        if evidence.state!='Settled':
            evidence.state='Manual Review'
            evidence.review_reason=str(exc)[:400]
            with _flag('club100_hybrid_evidence_write'):evidence.save(ignore_permissions=True)
        h_name=frappe.db.get_value('Club100 Hybrid Checkout',{'enrollment':enrollment_id},'name')
        if h_name:
            hybrid=frappe.get_doc('Club100 Hybrid Checkout',h_name)
            if hybrid.status=='Reserved':
                hybrid.status='Review';hybrid.review_reason=str(exc)[:400]
                with reservations._pilot_write():hybrid.save(ignore_permissions=True)
        enrollment=frappe.get_doc('Club100 Enrollment',enrollment_id)
        if enrollment.payment_status=='Pending' and enrollment.status=='Registered':
            enrollment.wallet_settlement_status='Review'
            enrollment.wallet_review_reason=str(exc)[:400]
            enrollment.save(ignore_permissions=True)
        return {'success':False,'manual_review':True,'enrollment':enrollment_id,
                'do_not_retry_payment':True,'payment_verified':True,
                'message':'Payment was captured. Accounting settlement requires support review; do not pay again.'}


def healthcheck():
    return {'site':frappe.local.site,'stage':'HYBRID-E2E-INTEGRATION',
        'checkout_enabled':enabled(),'finance_enabled':financial_enabled(),
        'local_test_mode_only':True,'production_ready':False,
        'real_wallet_backing_required':True,'gst_percent':str(current_rate()),
        'bank_reconciliation_approval_not_inferred':True}
