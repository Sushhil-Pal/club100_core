"""D2C3B2C1: pure voucher invariants for synthetic, gateway-only ERPNext UAT.

Does NOT prove a real payment, authorize posting, or change memberships.
Funding-source wallet journal/capture is deliberately NOT simulated here.
"""
from decimal import Decimal
from club100_core.accounting.hybrid_invoice_contract import money
from club100_core.accounting.hybrid_posting_rules import (
    build_posting_plan, validate_submitted_invoice, validate_gateway_entry,
)


def check_gateway_only_plan(review, cfg, evidence):
    plan = build_posting_plan(review, cfg, evidence)
    if (money(plan['gateway_amount']) != money(plan['invoice_amount'])
        or money(plan['funded_amount']) != Decimal('0.00')
        or plan['source_amounts'] or plan['liability_accounts']):
        raise ValueError('Synthetic B2C1 must be gateway-only; funded wallet settlement is reserved for B2C2')
    if money(plan['supplier_discount']) <= 0:
        raise ValueError('B2C1 must check the hybrid promotional-discount invoice')
    if not plan['promotion_is_not_tender'] or plan['posting_authorized'] is not False:
        raise ValueError('Untrusted posting authorization')
    return plan


def check_vouchers(review, cfg, plan, invoice, payment, outstanding):
    """Validate submitted ERPNext docs via B2B contract; no DB calls here."""
    if plan['enrollment'] != review['enrollment'] or plan['invoice_review_sha256'] != review['invoice_review_sha256']:
        raise ValueError('Posted-document contract refers to different synthetic plan')
    validate_submitted_invoice(review, cfg, invoice)
    validate_gateway_entry(plan, invoice['name'], payment)
    if money(str(outstanding)) != Decimal('0.00'):
        raise ValueError('Payment Entry did not fully settle the invoice')
    return {'invoice':invoice['name'], 'payment_entry_validated':True,
        'invoice_zero_outstanding':True, 'wallet_journal_created':False,
        'membership_activated':False, 'posting_authorized':False}
