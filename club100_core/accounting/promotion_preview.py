"""Local-only, read-only Stage 5F-D2 hybrid promotional-discount preview.

This is a design/integration gate, NOT a checkout endpoint or GST approval.
No credit reservation, payment, invoice or GL operation occurs here.
"""
import frappe
from frappe.utils import cint
from club100_core.accounting.promotion_rules import rupees, build_hybrid_plan


def _admin_local():
    if frappe.local.site != 'club100.local':
        frappe.throw('Pilot promotion preview is local only')
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)


def preview_hybrid(member_id, original_price, promotional_discount,
                   funded_wallet='0.00', gst_percent='5'):
    """For modeling only: balances are indicative, not tax-eligibility proof."""
    _admin_local()
    from club100_core.wallet.core import wallet_balance
    member_id = str(member_id or '').strip()
    balance = wallet_balance(member_id)
    available_promo = rupees(balance.get('by_source', {}).get('Promotion','0'))
    discount = rupees(promotional_discount)
    funded = rupees(funded_wallet)
    available_funded = sum((rupees(balance.get('by_source', {}).get(source,'0'))
        for source in ('Corporate','Referral')), rupees('0'))
    invoice_total = rupees(original_price) - discount
    if discount > available_promo:
        raise ValueError('Proposed discount exceeds available promotional credits')
    if funded > available_funded:
        raise ValueError('Proposed funded tender exceeds Corporate+Referral credits')
    if invoice_total < funded:
        raise ValueError('Funded tender exceeds discounted invoice')
    plan = build_hybrid_plan(original_price, discount, funded, invoice_total-funded, gst_percent)
    return {
        'site': frappe.local.site, 'member': member_id,
        'preview_only': True, 'checkout_enabled': False,
        'financial_postings': False, 'gst_policy_approved': False,
        'available_promotion': str(available_promo),
        'available_funded_wallet': str(available_funded),
        'plan': plan,
        'blocked_until': [
            'Approved promotion scheme and GST invoice treatment',
            'Server-side dual-reservation capture and refund orchestration',
            'Checkout and Razorpay callback updates with original price snapshot',
            'Submitted invoice discount verification and recovery checks',
            'Current PWA quote/checkout integration',
        ],
        'note': 'Read-only simulation; do not classify prior Promotion grants as automatically GST-discount-eligible.',
    }


def healthcheck():
    _admin_local()
    return {'site':frappe.local.site, 'promotion_discounts_active':False,
            'dual_reservation_capture_active':False, 'invoice_discount_posting_active':False,
            'tax_policy_approval_required':True, 'legacy_promotion_grants_reclassified':False,
            'read_only':True}
