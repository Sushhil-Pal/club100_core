"""Configurable GST for NEW Club100 hybrid checkouts.

Historical hybrid records freeze gst_percent; a changed current template cannot
silently alter a pending order. This is configuration, not a tax ruling.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PAISE = Decimal('0.01')


def normalized_rate(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('GST rate must be an exact decimal setting')
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Invalid GST rate') from exc
    if not rate.is_finite() or not (Decimal('0') < rate <= Decimal('28')) or rate.quantize(PAISE) != rate:
        raise ValueError('GST rate must be greater than 0, up to 28, with at most 2 decimals')
    return rate


def current_rate():
    import frappe
    configured = frappe.conf.get('club100_accounting_expected_gst_percent')
    # Site config may deserialize JSON numbers as floats; parse their decimal text
    # at this trusted configuration boundary, never from user-supplied amounts.
    return normalized_rate(str(configured) if configured not in (None, '') else '5')


def inclusive_breakdown(original, discount, wallet, rate):
    from club100_core.accounting.promotion_rules import rupees
    original, discount, wallet = (rupees(v) for v in (original, discount, wallet))
    tax = normalized_rate(rate)
    if original <= 0 or discount < 0 or discount >= original:
        raise ValueError('Invalid original price or supplier discount')
    invoice = original - discount
    if wallet < 0 or wallet >= invoice:
        raise ValueError('Wallet cannot exceed the invoice or eliminate gateway payment in this pilot')
    gateway = invoice - wallet
    if gateway < Decimal('1.00'):
        raise ValueError('Razorpay payment must be at least INR 1.00')
    net = (invoice / (Decimal('1') + tax / 100)).quantize(PAISE, rounding=ROUND_HALF_UP)
    return {'original': str(original), 'discount': str(discount), 'invoice': str(invoice),
            'wallet': str(wallet), 'gateway': str(gateway), 'gst_percent': str(tax),
            'net': str(net), 'gst': str(invoice - net)}


def verify_tax_rows(rows, frozen_rate):
    expected = normalized_rate(frozen_rate)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError('Expected one or two inclusive tax rows')
    total = Decimal('0')
    for row in rows:
        if (row.get('charge_type') != 'On Net Total'
                or row.get('included_in_print_rate') not in (1, True)
                or not row.get('account_head')):
            raise ValueError('Tax template must use inclusive On Net Total rows')
        total += normalized_rate(str(row.get('rate')))
    if total != expected:
        raise ValueError('Configured ERPNext GST template differs from frozen checkout rate')
    return total
