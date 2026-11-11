"""Shared pure tender allocation checks; money is INR in 2 decimal places."""
from decimal import Decimal
from club100_core.wallet.logic import money


def split_total(total, wallet_amount):
    gross = money(total)
    wallet = money(wallet_amount)
    if gross <= 0 or wallet < 0 or wallet > gross:
        raise ValueError('Invalid checkout tender amount')
    gateway = gross - wallet
    if 0 < gateway < Decimal('1.00'):
        raise ValueError('Razorpay remainder must be at least INR 1 or zero')
    return gross, wallet, gateway


def ensure_tender_integrity(gross, wallet, gateway):
    a, b, c = split_total(gross, wallet)
    if c != money(gateway):
        raise ValueError('Enrollment wallet and Razorpay amounts do not reconcile')
    return a, b, c
