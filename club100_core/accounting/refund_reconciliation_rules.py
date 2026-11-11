"""Pure exact-paise refund voucher and same-grant invariants. No DB access."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

Q = Decimal('0.01')


def amount(value):
    try:
        n = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError('Invalid INR amount') from exc
    if not n.is_finite() or n != n.quantize(Q):
        raise ValueError('INR amount must be finite and exact to paise')
    return n


def exact_offset(original_net, refund_net, expected, name):
    """Debit-positive: original net = amount, reversal net = -amount."""
    original, reverse, expected = map(amount, (original_net, refund_net, expected))
    if expected <= 0 or original != expected or reverse != -expected:
        raise ValueError('Refund does not exactly offset original ' + name)
    return True


def check_source_legs(sources, original_nets, refund_nets, account_names, receivable, clearing=None):
    if not isinstance(sources, dict) or not sources:
        raise ValueError('Wallet source split is missing')
    if set(sources) - {'Corporate', 'Referral'}:
        raise ValueError('Unsupported wallet source in refund')
    active_accounts = [account_names.get(s) for s in sources]
    if any(not name for name in active_accounts) or len(set(active_accounts)) != len(active_accounts):
        raise ValueError('Wallet source account mapping missing or duplicated')
    if receivable in active_accounts or (clearing and clearing in active_accounts):
        raise ValueError('Wallet source liability account overlaps tender account')
    for source, value in sources.items():
        exact_offset(original_nets.get(source), refund_nets.get(source), value, source)
    return sum((amount(a) for a in sources.values()), Decimal('0.00'))


def check_grant_restoration(allocations, spend_rows, refund_rows):
    """Each original grant has exactly one Spend and one equal Refund ledger event."""
    if not allocations:
        raise ValueError('Missing original wallet allocations')
    seen = set()
    total = Decimal('0.00')
    for allocation in allocations:
        grant = allocation['grant']
        value = amount(allocation['amount'])
        if not grant or grant in seen or value <= 0:
            raise ValueError('Duplicate, missing, or nonpositive grant allocation')
        seen.add(grant)
        sp, ref = spend_rows.get(grant, []), refund_rows.get(grant, [])
        if len(sp) != 1 or len(ref) != 1 or amount(sp[0]) != -value or amount(ref[0]) != value:
            raise ValueError('Original-grant refund ledger does not exactly restore ' + grant)
        total += value
    if set(spend_rows) != seen or set(refund_rows) != seen:
        raise ValueError('Unexpected grant was spent or restored under reservation')
    return total
