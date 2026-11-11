"""Admin-only, local Test Mode recovery of an ALREADY captured normal checkout.

Does not create a Razorpay order, capture, refund, or alter the captured amount.
The dry-run is the default and only performs provider GET and ERP read queries.
Real reconciliation reruns the exact existing atomic accounting+activation path
for the enrollment only after independent Razorpay payment verification.
No public endpoints or feature flags.
"""
from uuid import uuid4

import frappe
from frappe.utils import cint


def _gate():
    from club100_core.accounting.settlement import _local_pilot
    if frappe.session.user != 'Administrator' or not _local_pilot():
        raise PermissionError('Administrator on club100.local Razorpay Test Mode required')


def _lock_review(enrollment_id):
    name = str(enrollment_id or '').strip()
    if not name or not frappe.db.exists('Club100 Enrollment', name):
        raise ValueError('Enrollment not found')
    member = frappe.db.get_value('Club100 Enrollment', name, 'member')
    if not member:
        raise ValueError('Enrollment has no member')
    # Preserve lock order used by checkout and webhook handlers.
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', member)
    frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE', name)
    return frappe.get_doc('Club100 Enrollment', name)


def _require_record(doc):
    from club100_core.accounting.rules import money
    if (doc.website_source != 'PWA Commerce' or doc.pricing_model != 'Online Plan'
            or cint(doc.is_trial) or doc.status != 'Registered'
            or doc.payment_status != 'Pending'
            or doc.wallet_settlement_status != 'Review'
            or str(doc.payment_gateway or '') != 'Razorpay'
            or not cint(doc.razorpay_capture_verified)):
        raise ValueError('Only captured, pending, locally reviewed PWA normal payments can be recovered')
    if frappe.db.get_value('Club100 Plan', doc.plan, 'plan_code') != 'self':
        raise ValueError('Non-Self enrollment not supported')
    # Hybrid and funded-wallet review need separate grant/discount reconciliation.
    if (doc.wallet_reservation_key or money(doc.wallet_applied_amount or 0) != 0
            or frappe.db.exists('Club100 Hybrid Checkout', {'enrollment': doc.name})):
        raise ValueError('Wallet/hybrid order cannot use normal payment recovery')
    if (not str(doc.payment_id or '').startswith('pay_')
            or not str(doc.payment_order_id or '').startswith('order_')):
        raise ValueError('Invalid saved payment/order identity')
    if doc.sales_invoice or frappe.db.exists('Club100 Membership Accounting',
                                               {'enrollment': doc.name}):
        raise ValueError('Existing invoice/accounting mapping requires manual reconciliation')
    if frappe.db.get_value('Club100 Enrollment', {
            'payment_id': doc.payment_id, 'name': ['!=', doc.name]}, 'name'):
        raise ValueError('Payment already bound to another enrollment')
    if frappe.db.get_value('Payment Entry', {'reference_no': doc.payment_id,
                                              'docstatus': 1}, 'name'):
        raise ValueError('Submitted Payment Entry already has this payment reference')
    return doc


def _provider_captured(doc):
    from club100_core.api.enrollment import (
        _get_razorpay_config, RAZORPAY_API_BASE, _rupees_to_paise,
    )
    from club100_core.wallet.checkout import expected_gateway_due
    import requests
    cfg = _get_razorpay_config()
    res = requests.get(
        f'{RAZORPAY_API_BASE}/payments/{doc.payment_id}',
        auth=(cfg['key_id'], cfg['key_secret']), timeout=20,
    )
    if not res.ok:
        raise ValueError('Independent Razorpay payment lookup failed')
    payment = res.json()
    amount_paise = _rupees_to_paise(expected_gateway_due(doc))
    if (payment.get('id') != doc.payment_id
            or payment.get('order_id') != doc.payment_order_id
            or str(payment.get('currency') or '') != 'INR'
            or str(payment.get('status') or '').lower() != 'captured'
            or type(payment.get('amount')) is not int
            or payment['amount'] != amount_paise):
        raise ValueError('Razorpay captured payment does not match stored enrollment')
    return amount_paise


def recover(enrollment_id=None, dry_run=1):
    """bench execute ...recover --kwargs '{"enrollment_id":"...","dry_run":1}'.

    Set dry_run=0 only after read-only verification PASS and a DB backup.
    """
    _gate()
    doc = _require_record(_lock_review(enrollment_id))
    verified_paise = _provider_captured(doc)
    # Read-only account/tax-template preflight before either dry-run success or
    # an actual voucher submission. Underlying ERP link validation will still
    # occur during the transaction, under the narrowly scoped accounting user.
    from club100_core.accounting.settlement import _config, _ready
    from club100_core.wallet.checkout import expected_gateway_due
    _ready(_config(), {}, expected_gateway_due(doc))
    report = {
        'enrollment': doc.name, 'payment_id': doc.payment_id,
        'order_id': doc.payment_order_id, 'verified_captured_paise': verified_paise,
        'independent_provider_check': True, 'second_gateway_charge': False,
    }
    if cint(dry_run):
        return {**report, 'outcome': 'READY_FOR_RECONCILIATION',
                'read_only': True, 'posting_attempted': False}

    from club100_core.api.enrollment import _finalize_paid_enrollment_doc
    point = 'club100_recover_' + uuid4().hex[:12]
    frappe.db.savepoint(point)
    try:
        # The Review flag blocked duplicate checkout; remove it *only* inside
        # the same transaction that posts accounting and grants entitlement.
        doc.wallet_settlement_status = None
        doc.wallet_review_reason = None
        doc.payment_status = 'Paid'
        result = _finalize_paid_enrollment_doc(doc)
        invoice = doc.sales_invoice
        if not invoice:
            raise ValueError('Recovery did not attach Sales Invoice')
        mapping = frappe.db.get_value('Club100 Membership Accounting',
            {'enrollment': doc.name}, ['sales_invoice','status','gateway_payment_entry'],
            as_dict=True)
        if (not mapping or mapping.status != 'Posted'
                or mapping.sales_invoice != invoice or not mapping.gateway_payment_entry
                or frappe.db.get_value('Sales Invoice', invoice, 'docstatus') != 1):
            raise ValueError('Posted accounting records do not reconcile')
        from club100_core.accounting.rules import money
        outstanding = frappe.db.get_value('Sales Invoice', invoice, 'outstanding_amount')
        if money(outstanding) != money('0.00'):
            raise ValueError('Recovered invoice has nonzero outstanding')
        frappe.db.commit()
        return {**report, 'outcome': 'RECOVERED', 'invoice': invoice,
                'payment_entry': mapping.gateway_payment_entry,
                'payment_status': 'Paid', 'status': doc.status,
                'invoice_outstanding': '0.00', 'membership_finalizer': result,
                'read_only': False}
    except Exception:
        frappe.db.rollback(save_point=point)
        # Enrollment stays Pending / Registered / Review and payment remains
        # bound. Do not issue another charge on an accounting exception.
        raise
