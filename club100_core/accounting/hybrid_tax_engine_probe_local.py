"""D2C3B2A: isolated, in-memory ERPNext inclusive-GST calculation probe.

No checkout, invoice insert/submit, wallet capture, Razorpay, GL, or membership
changes. This does not establish a legally valid rate or authorize posting.
Only a local Administrator can run it under an explicit environment opt-in.
"""
import hashlib
import json
import os
from decimal import Decimal

import frappe

from club100_core.accounting import settlement
from club100_core.accounting.hybrid_draft_rules import FIELDS, draft_spec, verify_calculated_draft
from club100_core.accounting.hybrid_invoice_contract import money


def _guard():
    if frappe.local.site != 'club100.local':
        raise ValueError('D2C3B2A must run on the isolated club100.local site')
    if str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test':
        raise ValueError('D2C3B2A requires Razorpay TEST mode')
    if frappe.session.user != 'Administrator':
        frappe.throw('Administrator bench session required', frappe.PermissionError)
    if os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2A_PROBE') != '1':
        raise ValueError('In-memory ERPNext probe is disabled by default')


def _fixture_review():
    """Synthetic fixed values; NEVER use arbitrary request or existing credit data."""
    review = {
        'enrollment': 'TEST-ONLY-NOT-AN-ENROLLMENT',
        'member': 'TEST-ONLY-NOT-A-MEMBER',
        'hybrid_checkout': 'TEST-ONLY-NOT-A-HYBRID',
        'snapshot_sha256': 'a' * 64,
        'evidence_name': 'TEST-ONLY-NOT-PAYMENT',
        'evidence_identity_sha256': 'b' * 64,
        'original_price_incl_gst': '799.00',
        'supplier_discount_incl_gst': '100.00',
        'invoice_total_incl_gst': '699.00',
        'taxable_value_projected': '665.71',
        'gst_projected': '33.29',
        'funded_wallet_payment': '300.00',
        'razorpay_payment': '399.00',
        'corporate_source': '200.00',
        'referral_source': '100.00',
        'posting_authorized': False,
        'no_automatic_tax_or_invoice_authorization': True,
        'submitted_invoice_verified': False,
    }
    review['invoice_review_sha256'] = hashlib.sha256(json.dumps(
        {key: review[key] for key in FIELDS}, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    return review


def _numbers(invoice):
    return {key: str(invoice.get(key)) for key in (
        'grand_total', 'net_total', 'total_taxes_and_charges',
        'discount_amount', 'apply_discount_on', 'currency')}


def run():
    """Compute an in-memory Sales Invoice; return PASS / MISMATCH / BLOCKED.

    It deliberately avoids real enrollment evidence and GL balances: this tests
    only ERPNext's tax arithmetic/configuration, NOT transaction eligibility.
    """
    _guard()
    baseline = {
        'site': frappe.local.site,
        'stage': '5F-D2C3B2A',
        'real_customer_charged': False,
        'synthetic_amounts_only': True,
        'invoice_inserted': False,
        'invoice_submitted': False,
        'payment_or_gl_posted': False,
        'wallet_captured': False,
        'membership_activated': False,
        'gateway_called': False,
        'posting_authorized': False,
        'production_ready': False,
    }
    cfg = settlement._config()
    missing = [key for key in ('company', 'customer', 'item', 'tax_template')
               if not str(cfg.get(key) or '').strip()]
    if missing:
        return {**baseline, 'outcome': 'BLOCKED',
                'reason': 'Missing Stage 5E ERPNext configuration',
                'missing_config': missing, 'erpnext_calculation_executed': False}
    try:
        # Validates company, customer, item, GST template and tax accounts.
        tax_rows = settlement._ready(cfg, {}, Decimal('0'))
    except Exception as exc:
        return {**baseline, 'outcome': 'BLOCKED',
                'reason': 'ERPNext accounting setup is incomplete: ' + str(exc),
                'erpnext_calculation_executed': False}

    review = _fixture_review()
    spec = draft_spec(review, cfg, tax_rows, '2026-11-04')
    # No insert or submit: run the ERPNext tax calculation on a NEW unsaved Doc.
    point = 'club100_d2c3b2a_calc'
    frappe.db.savepoint(point)
    try:
        doc = frappe.get_doc(spec)
        doc.calculate_taxes_and_totals()
        computed = _numbers(doc)
        try:
            result = verify_calculated_draft(review, {
                'docstatus': 0,
                'company': doc.company,
                'customer': doc.customer,
                'currency': doc.currency,
                'apply_discount_on': doc.apply_discount_on,
                'discount_amount': doc.discount_amount,
                'items': [{'item_code': item.item_code, 'rate': item.rate}
                          for item in doc.items],
                'grand_total': doc.grand_total,
                'net_total': doc.net_total,
                'total_taxes_and_charges': doc.total_taxes_and_charges,
            }, cfg)
            outcome = {**baseline, 'outcome': 'PASS',
                       'erpnext_calculation_executed': True,
                       'expected': {'invoice': '699.00', 'net': '665.71', 'gst': '33.29'},
                       'calculated': computed, 'verified': result}
        except Exception as exc:
            outcome = {**baseline, 'outcome': 'MISMATCH',
                       'erpnext_calculation_executed': True,
                       'reason': str(exc),
                       'expected': {'invoice': '699.00', 'net': '665.71', 'gst': '33.29'},
                       'calculated': computed}
    except Exception as exc:
        outcome = {**baseline, 'outcome': 'BLOCKED',
                   'erpnext_calculation_executed': False,
                   'reason': 'In-memory ERPNext calculator failed: ' + str(exc)}
    finally:
        frappe.db.rollback(save_point=point)
    # Result is a CALCULATION ONLY and must not authorize accounting or activation.
    return outcome


def healthcheck():
    if frappe.session.user != 'Administrator':
        frappe.throw('Administrator required', frappe.PermissionError)
    cfg = settlement._config()
    return {
        'site': frappe.local.site, 'stage': '5F-D2C3B2A',
        'probe_opted_in': os.environ.get('CLUB100_ALLOW_STAGE5FD2C3B2A_PROBE') == '1',
        'configured': {k: bool(cfg.get(k)) for k in
                       ('company', 'customer', 'item', 'tax_template')},
        'invoice_insert_enabled': False,
        'invoice_submission_enabled': False,
        'wallet_capture_enabled': False,
        'payment_or_gl_posting_enabled': False,
        'production_ready': False,
    }
