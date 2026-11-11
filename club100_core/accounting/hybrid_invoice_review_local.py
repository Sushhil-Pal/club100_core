"""D2C3A: STRICT read-only local review of verified hybrid payment and invoice quote.

Never calls Razorpay, changes wallet state, creates a draft/submitted invoice,
creates accounting documents, or marks an enrollment Paid/Active. Even an empty
issues list is NOT permission to post. This is exclusively a diagnostic.
"""
import os

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from club100_core.accounting import hybrid_reservations as reserves
from club100_core.accounting.hybrid_integration_rules import (
    validate_frozen_checkout, invoice_preflight, example_projection)
from club100_core.accounting.hybrid_invoice_contract import (
    build_invoice_review, validate_tax_template_rows, money)

DT_EVIDENCE = 'Club100 Hybrid Payment Evidence'


def _admin():
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)


def _site():
    if frappe.local.site != 'club100.local' or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test':
        raise ValueError('D2C3A review is local TEST site only')


def _snapshot(hybrid):
    keys = ('enrollment', 'member', 'status', 'policy_version', 'original_price',
            'promotion_discount', 'funded_wallet_tender', 'gateway_due',
            'gst_percent', 'promotion_reservation_key', 'funded_reservation_key',
            'plan_snapshot_json')
    return {k: hybrid.get(k) for k in keys}


def preview_one(enrollment_id):
    """Review existing D2C2C verified evidence; no transactions or side effects.

    Missing evidence raises, and manual-review evidence cannot be promoted.
    Read-only scans are NOT a substitute for locked, server-refetched evidence
    in the eventual posting transaction.
    """
    _admin(); _site()
    enrollment_id = str(enrollment_id or '').strip()
    if not enrollment_id or len(enrollment_id) > 140:
        raise ValueError('Enrollment ID required')
    enrollment = frappe.get_doc('Club100 Enrollment', enrollment_id)
    name = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment': enrollment.name}, 'name')
    if not name:
        raise ValueError('Hybrid Checkout does not exist for enrollment')
    hybrid = frappe.get_doc('Club100 Hybrid Checkout', name)
    if hybrid.member != enrollment.member or hybrid.enrollment != enrollment.name:
        raise ValueError('Member/hybrid/enrollment mismatch')
    evidence_name = frappe.db.get_value(DT_EVIDENCE, {'enrollment': enrollment.name}, 'name')
    if not evidence_name:
        raise ValueError('No verified D2C2C payment evidence for enrollment')
    evidence = frappe.get_doc(DT_EVIDENCE, evidence_name)
    if evidence.state != 'Verified Pending Invoice':
        raise ValueError('Evidence requires manual review or has already settled')
    # D2B checks wallet reservation owner, amount, grant source and frozen
    # allocation lists; it makes no changes, but data can change later.
    allocation = reserves._pair(hybrid, ('Reserved',))
    pre = validate_frozen_checkout(_snapshot(hybrid), allocation['promotion'], allocation['funded'])
    invoice_preflight(pre, example_projection(pre))
    ev = {k: evidence.get(k) for k in ('name', 'enrollment', 'member',
         'hybrid_checkout', 'razorpay_order_id', 'razorpay_payment_id',
         'snapshot_sha256', 'identity_sha256', 'provider_status', 'state',
         'amount_paise')}
    review = build_invoice_review(pre, ev, hybrid.name,
                                  allocation['promotion'], allocation['funded'])
    issues = []
    if (enrollment.website_source != 'PWA Commerce' or enrollment.pricing_model != 'Online Plan'
            or cint(enrollment.is_trial) or enrollment.payment_status != 'Pending'
            or enrollment.status != 'Registered' or enrollment.sales_invoice
            or enrollment.payment_id or enrollment.wallet_settlement_status == 'Settled'):
        issues.append('Enrollment is not a pristine pending hybrid checkout')
    if enrollment.payment_order_id != evidence.razorpay_order_id:
        issues.append('Stored Razorpay order has changed')
    if money(str(enrollment.amount_payable)) != money(str(hybrid.original_price)):
        issues.append('Enrollment original price changed since reservation')
    if hybrid.status != 'Reserved' or get_datetime(hybrid.expires_at) <= now_datetime():
        issues.append('Hybrid checkout expired or is not Reserved')
    for reservation_key in (hybrid.promotion_reservation_key, hybrid.funded_reservation_key):
        if not reservation_key:
            continue
        hold = frappe.db.get_value('Club100 Wallet Reservation',
            {'reservation_key': reservation_key}, ['status', 'expires_at'], as_dict=True)
        if not hold or hold.status != 'Reserved' or get_datetime(hold.expires_at) <= now_datetime():
            issues.append('Wallet hold expired or changed; manual payment review required')
    if not cint(frappe.conf.get('club100_hybrid_gst_scheme_approved', 0)) and money(review['supplier_discount_incl_gst']) > 0:
        issues.append('Promotional GST scheme not approved; cannot invoice discounted amount')
    if allocation['promotion']:
        approved = set(reserves._eligible_promotion_grants(hybrid.member, hybrid.policy_version))
        if any(row['grant'] not in approved for row in allocation['promotion']):
            issues.append('Specific promotional grant/policy tax eligibility not currently approved')

    # Existing Stage 5E/F accounting configuration and actual GL backing are
    # checked READ-ONLY. Legacy settlement.post_one must NOT be called.
    taxes = []
    try:
        from club100_core.accounting import settlement
        cfg = settlement._config()
        sources = {kind: money(review[k]) for kind, k in (
            ('Corporate', 'corporate_source'), ('Referral', 'referral_source'))
                   if money(review[k]) > 0}
        taxes = settlement._ready(cfg, sources, money(review['razorpay_payment']))
        validate_tax_template_rows(taxes, '5')
        if sources:
            settlement._require_wallet_liability_backing(cfg, sources, allocation['funded'])
    except Exception as exc:
        issues.append('ERPNext tax/account/source-backing preflight blocked: ' + str(exc))
    # Even with zero issues, we do NOT verify actual ERPNext tax line rounding,
    # independent provider capture freshness or posted GL. These are next stage.
    return {
        'site': frappe.local.site, 'stage': '5F-D2C3A',
        'enrollment': enrollment.name, 'evidence': evidence.name,
        'review': review, 'issues': issues,
        'projection_contract_valid': True,
        'finance_preflight_passed': not issues,
        'legal_tax_approval_independently_required': True,
        'fresh_provider_get_required_before_posting': True,
        'submitted_invoice_and_gl_verification_pending': True,
        'wallet_capture_enabled': False, 'membership_activation_enabled': False,
        'invoice_or_gl_posting_enabled': False,
        'read_only': True, 'posting_authorized': False,
        'production_ready': False,
    }


def healthcheck():
    _admin()
    return {'site': frappe.local.site, 'stage': '5F-D2C3A',
        'invoice_review_installed': True,
        'evidence_doctype_present': bool(frappe.db.exists('DocType', DT_EVIDENCE)),
        'hybrid_doctype_present': bool(frappe.db.exists('DocType', 'Club100 Hybrid Checkout')),
        'new_invoice_posting_enabled': False,
        'wallet_capture_enabled': False,
        'membership_activation_enabled': False,
        'production_ready': False, 'read_only': True}
