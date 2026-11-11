"""Pure presentation helpers for member-only wallet/referral activity endpoints."""
from decimal import Decimal, InvalidOperation


def _money(value):
    try:
        amount = Decimal(str(value if value is not None else 0))
        if not amount.is_finite():
            raise ValueError('Invalid financial amount')
        return format(amount.quantize(Decimal('0.01')), '.2f')
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Invalid financial amount') from exc


def validated_page(value):
    # The API is a member-facing activity feed, not an unbounded export.
    if isinstance(value, bool) or not str(value).isdecimal():
        raise ValueError('Page must be a positive integer')
    number = int(value)
    if number < 1 or number > 500:
        raise ValueError('Page out of range')
    return number


def activity_label(entry_type, source):
    source = source if source in ('Corporate', 'Referral', 'Promotion') else 'Wallet'
    names = {
        'Credit': f'{source} credits added',
        'Spend': f'{source} credits used',
        'Refund': f'{source} credits restored',
        'Reversal': f'{source} balance adjusted',
        'Expiry': f'{source} credits expired',
    }
    return names.get(entry_type, 'Wallet adjustment')


def safe_enrollment_reference(entry_type, reference):
    ref = str(reference or '')
    if entry_type in ('Spend', 'Refund') and ref.startswith('C100-ENR-') and len(ref) <= 40 and ref[9:].replace('-', '').isalnum():
        return ref
    return ''
