"""Pure, deterministic referral settlement rules. No Frappe or gateway access."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PAISE = Decimal('0.01')


def money(value):
    try:
        number = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError('Invalid INR amount') from exc
    if not number.is_finite() or number != number.quantize(PAISE):
        raise ValueError('Amount must be finite INR with at most two decimals')
    return number.quantize(PAISE)


def date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def verified_amount(basis, rate, gateway_paid):
    """Reward basis cannot include wallet tender or exceed verified gateway tender."""
    value = money(basis)
    gateway = money(gateway_paid)
    pct = Decimal(str(rate))
    if not pct.is_finite() or pct <= 0 or pct > 100:
        raise ValueError('Reward rate must be greater than zero and at most 100%')
    if value <= 0 or gateway <= 0 or value > gateway:
        raise ValueError('Verified net-of-tax basis must be positive and <= gateway-paid amount')
    reward = (value * pct / Decimal('100')).quantize(PAISE, rounding=ROUND_HALF_UP)
    if reward <= 0:
        raise ValueError('Verified reward rounds to zero')
    return reward


def maturity_day(payment_date, start_date, hold_days=30):
    days = int(hold_days)
    if str(days) != str(hold_days) or days < 14 or days > 180:
        raise ValueError('Referral hold must be 14–180 whole days')
    return max(date_value(payment_date), date_value(start_date)) + timedelta(days=days)


def credit_validity(validity, expiry, eligible):
    if validity == 'Lifetime':
        if expiry:
            raise ValueError('Lifetime reward credits cannot expire')
        return None
    if validity != 'Expires On' or not expiry:
        raise ValueError('Specify Lifetime or an expiry date')
    date_expiry = date_value(expiry)
    if date_expiry < date_value(eligible):
        raise ValueError('Referral credit cannot expire before maturity')
    return str(date_expiry)


def can_mature(today, eligible_on):
    return date_value(today) >= date_value(eligible_on)


def recoverable_grant(issued, balance, reserved):
    original = money(issued)
    available = money(balance)
    held = money(reserved)
    if original <= 0 or available < 0 or held < 0 or available > original or held > available:
        raise ValueError('Referral grant ledger or hold integrity failure')
    if available != original or held != 0:
        raise ValueError('Referral credit already spent or reserved: manual recovery required')
    return True


def net_of_inclusive_tax(gateway_paid, gst_percent):
    """Split the verified gateway tender into INR net value and GST.

    The rate MUST come from a trusted, frozen server-owned checkout snapshot.
    This calculation alone is not a tax invoice or GST classification proof.
    """
    gross = money(gateway_paid)
    rate = Decimal(str(gst_percent))
    if not rate.is_finite() or rate <= 0 or rate > 28 or gross <= 0:
        raise ValueError('Invalid inclusive GST rate or gateway tender')
    net = (gross * Decimal('100') / (Decimal('100') + rate)).quantize(
        PAISE, rounding=ROUND_HALF_UP)
    if net <= 0 or net >= gross:
        raise ValueError('GST-exclusive basis is invalid')
    return net


def net_gateway_share(invoice_net_total, invoice_gross_total, gateway_paid):
    """Allocate invoiced net membership value to the Razorpay-paid tender only."""
    net = money(invoice_net_total)
    gross = money(invoice_gross_total)
    gateway = money(gateway_paid)
    if gross <= 0 or net <= 0 or net >= gross or gateway <= 0 or gateway > gross:
        raise ValueError('Invalid submitted invoice net/gross/tender combination')
    return (net * gateway / gross).quantize(PAISE, rounding=ROUND_HALF_UP)
