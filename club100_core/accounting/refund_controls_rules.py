"""Pure refund-accounting eligibility rules. No accounting postings or API calls.

Credit notes and wallet reversals cannot be treated as the same journal event:
member credits are a tender mechanism, and their original funding source must
be preserved on restoration. Finance must validate tax/discount treatment.
"""
from decimal import Decimal
from club100_core.accounting.rules import money

SOURCES = ('Corporate', 'Referral', 'Promotion')


def refund_plan(gross, wallet, gateway, sources, status):
    gross, wallet, gateway = (money(v) for v in (gross, wallet, gateway))
    if gross <= 0 or wallet < 0 or gateway < 0 or gross != wallet + gateway:
        raise ValueError('Refund tender identity mismatch')
    if status not in ('Posted', 'Refunded', 'Review'):
        raise ValueError('Unknown membership accounting status')
    if not isinstance(sources, dict):
        raise ValueError('Original wallet source allocation is missing')
    source_sum = Decimal('0.00')
    amounts = {}
    for source, amount in sources.items():
        if source not in SOURCES:
            raise ValueError('Unsupported source on original accounting mapping')
        value = money(amount)
        if value <= 0:
            raise ValueError('Wallet source must be positive')
        amounts[source] = str(value)
        source_sum += value
    if source_sum != wallet:
        raise ValueError('Original source allocations do not match wallet tender')
    return {'credit_note_gross': str(gross),
            'gateway_refund': str(gateway), 'wallet_restoration': str(wallet),
            'restore_sources': dict(sorted(amounts.items())),
            'required': (['GST credit note'] + (['Razorpay refund reconciliation and clearing reversal'] if gateway else [])
                         + (['Original-grant wallet restoration and liability reversal'] if wallet else [])),
            'already_refunded_mapping': status == 'Refunded'}
