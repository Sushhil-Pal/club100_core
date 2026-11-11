"""Stage 5F-D2A: pure calculations for *supplier-funded pre-supply discounts*.

This is NOT an authority to treat arbitrary wallet value as a taxable discount.
Only a promotional benefit independently confirmed as an eligible invoice discount
may enter this calculation. The real checkout path is intentionally not enabled.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

PAISE = Decimal('0.01')


def rupees(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('A valid INR amount is required')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('A valid INR amount is required') from exc
    if not n.is_finite() or n < 0 or n != n.quantize(PAISE):
        raise ValueError('INR amount must be finite, nonnegative, and in paise')
    return n.quantize(PAISE)


def _rounded_net(gross, tax_rate):
    return (gross / (Decimal('1') + tax_rate / 100)).quantize(PAISE, rounding=ROUND_HALF_UP)


def build_hybrid_plan(original_price, promotional_discount, funded_wallet, gateway,
                      gst_percent='5'):
    """All amounts are gross GST-inclusive INR; discount precedes invoice tax.

    Returns independently checked taxable invoice total, tax and tender split.
    `funded_wallet` includes only Corporate/Referral, NEVER Promotion.
    """
    full = rupees(original_price)
    discount = rupees(promotional_discount)
    wallet = rupees(funded_wallet)
    cash = rupees(gateway)
    try:
        rate = Decimal(str(gst_percent))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Invalid GST rate') from exc
    if not rate.is_finite() or rate <= 0 or rate > 28:
        raise ValueError('Unsupported GST rate')
    if full <= 0 or discount >= full:
        raise ValueError('Full price must be positive and discount less than full price')
    final = full - discount
    if final != wallet + cash:
        raise ValueError('Funded wallet + gateway must equal AFTER-DISCOUNT invoice total')
    if cash > 0 and cash < Decimal('1.00'):
        raise ValueError('Razorpay remainder must be at least INR 1.00')
    # Only compare amounts rounded at invoice-currency precision, never float math.
    before_net = _rounded_net(full, rate)
    after_net = _rounded_net(final, rate)
    return {
        'original_price_incl_gst': str(full),
        'promotion_discount': str(discount),
        'invoice_total_incl_gst': str(final),
        'funded_wallet_tender': str(wallet),
        'razorpay_tender': str(cash),
        'tax_rate_percent': str(rate),
        'invoice_net_excl_gst': str(after_net),
        'invoice_gst': str(final - after_net),
        'comparison_gst_without_discount': str(full - before_net),
        'illustrative_gst_difference': str((full - before_net) - (final - after_net)),
        'treatment': 'Supplier-funded invoice discount before GST',
    }


def verify_source_split(reservations, promotional_discount, funded_wallet):
    """Validate two separate immutable reservations' grant amounts & source types.

    Pass each reservation as iterable of {source, amount, grant}. No mixed-use
    reservation is accepted, and no grant may appear twice across them.
    """
    if not isinstance(reservations, dict) or set(reservations) != {'promotion','funded'}:
        raise ValueError('Both promotion and funded reservation snapshots required')
    seen = set()
    results = {}
    for kind, allowed in (('promotion', {'Promotion'}), ('funded', {'Corporate','Referral'})):
        total = Decimal('0')
        for row in reservations[kind]:
            if not isinstance(row, dict) or row.get('source') not in allowed:
                raise ValueError('Credit source cannot be used in this reservation')
            grant = str(row.get('grant') or '')
            if not grant or grant in seen:
                raise ValueError('Duplicate or missing original grant')
            seen.add(grant)
            amount = rupees(row.get('amount'))
            if amount <= 0:
                raise ValueError('Reservation item must be positive')
            total += amount
        results[kind] = total
    if results['promotion'] != rupees(promotional_discount):
        raise ValueError('Promotion reservation does not match discount')
    if results['funded'] != rupees(funded_wallet):
        raise ValueError('Funded reservation does not match tender')
    return True


def refund_discount_policy(original_discount, original_grant_valid, already_refunded=False):
    """Refund is never allowed to mint new/promoted credit or extend expiry."""
    value = rupees(original_discount)
    if already_refunded:
        return {'restore': '0.00', 'action': 'Already restored'}
    if value == 0:
        return {'restore': '0.00', 'action': 'No promotion'}
    if not original_grant_valid:
        return {'restore': '0.00', 'action': 'Manual review: original grant expired or invalid'}
    return {'restore': str(value), 'action': 'Restore to original grant only'}
