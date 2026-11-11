"""Pure financial invariants for Club100 PWA Self membership accounting.

Amounts are INR, rounded to paise; never trust browser totals, guessed GST, or
claim that wallet credits are bank receipts.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PAISE = Decimal('0.01')


def money(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Invalid INR amount') from exc
    if not d.is_finite() or d != d.quantize(PAISE):
        raise ValueError('INR amount must be exact to paise')
    return d.quantize(PAISE)


def breakdown(gross, wallet, gateway, allocations):
    gross, wallet, gateway = money(gross), money(wallet), money(gateway)
    if gross <= 0 or wallet < 0 or gateway < 0 or gross != wallet + gateway:
        raise ValueError('Checkout tenders do not equal the invoice gross')
    totals = defaultdict(lambda: Decimal('0.00'))
    for row in allocations:
        source = row['source']
        if source not in ('Corporate', 'Referral', 'Promotion'):
            raise ValueError('Unrecognized wallet funding source')
        amount = money(row['amount'])
        if amount <= 0:
            raise ValueError('Wallet reservation allocations must be positive')
        totals[source] += amount
    if sum(totals.values(), Decimal('0.00')) != wallet:
        raise ValueError('Wallet grant allocations do not match settled amount')
    return {'gross': gross, 'wallet': wallet, 'gateway': gateway,
            'sources': dict(sorted(totals.items()))}


def invoice_integrity(gross, net, taxes, actual_total, tolerance='0.01'):
    gross, net, taxes, actual_total = [money(x) for x in (gross, net, taxes, actual_total)]
    if gross != actual_total or net <= 0 or taxes < 0 or abs(net + taxes - gross) > money(tolerance):
        raise ValueError('Posted invoice does not reconcile to enrollment and GST')
    return True


def expected_net_from_gst(gross, rate):
    """Test calculator only. Actual invoice must provide authoritative tax split."""
    gross = money(gross)
    rate = Decimal(str(rate))
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError('Invalid GST rate')
    return (gross / (Decimal(1) + rate/100)).quantize(PAISE, rounding=ROUND_HALF_UP)


def refund_accounting(gross, wallet, gateway):
    x = breakdown(gross, wallet, gateway, []) if money(wallet) == 0 else None
    if x is None:
        if money(gross) <= 0 or money(wallet) < 0 or money(gateway) < 0 or money(gross) != money(wallet) + money(gateway):
            raise ValueError('Refund totals do not match original purchase')
    return {'credit_note_total': str(money(gross)),
            'razorpay_refund_to_record': str(money(gateway)),
            'wallet_liability_to_restore': str(money(wallet))}
