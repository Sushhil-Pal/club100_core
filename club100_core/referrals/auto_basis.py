"""Automatic referral basis proofs; only called from trusted internal code.

Submitted invoices are strong ERP tax evidence. A *frozen* local GST snapshot
may be used for developer test payments, never for production settlement.
Neither source is accepted from the browser, a referrer, or user-supplied API.
"""
from decimal import Decimal

import frappe
from frappe.utils import cint

from club100_core.referrals.settlement_logic import (
    money, net_of_inclusive_tax, net_gateway_share,
)


def _local_auto_enabled():
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def _self_checkout(enrollment):
    if (enrollment.website_source != 'PWA Commerce'
            or enrollment.pricing_model != 'Online Plan'
            or enrollment.is_trial):
        return False
    plan_code = frappe.db.get_value('Club100 Plan', enrollment.plan, 'plan_code')
    if plan_code != 'self':
        return False
    duration = int(enrollment.membership_duration_months or 0)
    if duration not in (1, 3, 6, 12):
        return False
    # Checkout has *server-owned* total/monthly-equivalent fields; verify the
    # amount cannot be an arbitrary one-off manual enrollment override.
    return (money(enrollment.amount_payable) ==
            money(enrollment.monthly_equivalent) * duration)


def checkout_tax_snapshot(enrollment):
    """Capture local tax configuration during creation of a NEW payment reward."""
    if not _local_auto_enabled() or not _self_checkout(enrollment):
        return None
    from club100_core.accounting.hybrid_gst import current_rate
    return current_rate()  # One tax setting; normal checkouts now post actual invoices.


def _submitted_invoice(enrollment, gateway):
    if not enrollment.sales_invoice:
        return None
    doc = frappe.get_doc('Sales Invoice', enrollment.sales_invoice)
    if doc.docstatus != 1 or doc.is_return or doc.currency != 'INR':
        raise ValueError('Referral invoice must be a submitted INR sales invoice')
    gross = money(enrollment.amount_payable)
    hybrid = frappe.db.get_value('Club100 Hybrid Checkout',
        {'enrollment': enrollment.name},
        ['status','original_price','promotion_discount','funded_wallet_tender','gateway_due'],
        as_dict=True)
    if hybrid:
        if (hybrid.status != 'Captured' or gross != money(hybrid.original_price)
            or gross-money(hybrid.promotion_discount) !=
               money(hybrid.funded_wallet_tender)+money(hybrid.gateway_due)):
            raise ValueError('Hybrid invoice/reward financial identity mismatch')
        gross -= money(hybrid.promotion_discount)
    invoiced_gross = money(doc.grand_total)
    invoiced_net = money(doc.net_total)
    taxes = money(doc.total_taxes_and_charges)
    if invoiced_gross != gross or invoiced_net <= 0 or taxes <= 0:
        raise ValueError('Invoice gross does not match paid membership or positive GST breakdown missing')
    if abs((invoiced_net + taxes) - invoiced_gross) > Decimal('0.01'):
        raise ValueError('Invoice net/tax/gross components do not reconcile')
    if len(doc.items or []) != 1:
        raise ValueError('Invoice has multiple line items; commissionable items need Finance review')
    line = doc.items[0]
    if money(line.net_amount) != invoiced_net or Decimal(str(line.qty or 0)) <= 0:
        raise ValueError('Invoice has additional charges or invalid membership line; Finance review required')
    if money(doc.outstanding_amount) != 0:
        raise ValueError('Invoice payment allocation is not reconciled; outstanding balance remains')
    basis = net_gateway_share(invoiced_net, invoiced_gross, gateway)
    return {'basis': basis, 'type': 'Submitted Invoice',
            'reference': 'AUTO:INVOICE:' + doc.name}


def verified_basis_evidence(enrollment, reward, gateway):
    # Automatic rewards require actual submitted, fully paid invoice evidence.
    # Old local GST-snapshot-only rewards stay On Hold for manual reconciliation.
    if not enrollment.sales_invoice:
        raise ValueError('Submitted and reconciled Sales Invoice required for automatic referral reward')
    return _submitted_invoice(enrollment, gateway)
