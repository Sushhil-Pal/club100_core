"""Pure Stage 5F-D rules. No Frappe, no tax-policy inference, no accounting writes.

Expiry is inclusive: the unused amount becomes unavailable on the *next* day.
An expired grant's original ledger remains immutable until a separately approved
accounting release is posted, if appropriate for that grant and contract.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

CENT = Decimal('0.01')
SOURCES = ('Corporate', 'Referral', 'Promotion')


def money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Invalid monetary value') from exc
    if not amount.is_finite() or amount != amount.quantize(CENT):
        raise ValueError('Amount must be finite and have at most two decimals')
    return amount.quantize(CENT)


def day(value):
    if isinstance(value, date) and not hasattr(value, 'hour'):
        return value
    return date.fromisoformat(str(value)[:10])


def unused_expired(grant, ledger_net, as_of, reserved=0):
    """Assess a single grant without posting a debit or changing expiry fields."""
    if grant['source_type'] not in SOURCES:
        raise ValueError('Unsupported grant source')
    amount = money(grant['amount'])
    net = money(ledger_net)
    held = money(reserved)
    if amount <= 0 or not Decimal('0.00') <= net <= amount or held < 0 or held > net:
        raise ValueError('Grant amount, ledger or reservation integrity mismatch')
    validity = grant['validity_type']
    expires = grant.get('expires_on')
    if validity == 'Lifetime':
        if expires not in (None, ''):
            raise ValueError('Lifetime grant has unexpected expiry')
        return {'expired': False, 'unspent': '0.00', 'held': str(held), 'status': 'Lifetime'}
    if validity != 'Expires On' or not expires:
        raise ValueError('Grant validity is inconsistent')
    if grant.get('status') != 'Active':
        return {'expired': False, 'unspent': '0.00', 'held': str(held), 'status': 'Inactive'}
    expired = day(as_of) > day(expires)
    return {'expired': expired, 'unspent': str(net if expired else Decimal('0.00')),
            'held': str(held), 'status': ('Held: Manual Review' if expired and held else
                'Expired Unspent' if expired and net else 'Expired Empty' if expired else 'Not Expired')}


def policy_gate(source, proposed_treatment=None):
    """No tax classification is implied by the word 'Promotion'."""
    if source not in SOURCES:
        raise ValueError('Unknown wallet source')
    if source == 'Promotion':
        # Even a nominated treatment does not prove that the member contract,
        # tax invoice, or funding source supports it. Finance approval required.
        return {'auto_post': False, 'reason': 'Promotion requires approved consideration and GST treatment',
                'proposed_treatment': proposed_treatment or None}
    if source == 'Corporate':
        return {'auto_post': False, 'reason': 'Expiry requires employer contract / refund-or-rollover decision'}
    return {'auto_post': False, 'reason': 'Expiry requires approved referral-credit breakage / reversal policy'}


def invoice_queue_classification(payment_status, accounting_record, wallet_amount, gateway):
    """Only a paid, gateway-only, as-yet-unaccounted enrollment can enter pilot auto-post."""
    if payment_status == 'Refunded':
        return 'Refund Review' if accounting_record else 'Legacy Refund Review'
    if payment_status != 'Paid':
        return 'Not Paid'
    if accounting_record:
        return 'Already Accounted'
    if money(wallet_amount) > 0:
        return 'Wallet: Source Backing Review'
    if gateway == 'Razorpay':
        return 'Preview Required'
    return 'Unsupported Payment'
