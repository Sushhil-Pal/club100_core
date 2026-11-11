"""Pure allocation helpers for internal wallet reservations (no Frappe dependency)."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from club100_core.wallet.logic import is_active, money, spend_order_key


def allocate(grants, reserved_by_grant, requested, on_date=None, allowed_sources=None):
    """Return [(grant_name, source, INR amount)] in expiry-first order.

    Caller MUST lock member wallet and re-read grant, ledger and hold rows
    inside the same database transaction before passing the snapshot.
    """
    amount = money(requested)
    if amount <= 0:
        raise ValueError("Reservation amount must be positive")
    day = on_date or date.today()
    sources = set(allowed_sources if allowed_sources is not None else ('Corporate', 'Referral', 'Promotion'))
    if not sources or sources - {'Corporate','Referral','Promotion'}:
        raise ValueError('Invalid permitted funding sources')
    left = amount
    result = []
    for g in sorted(grants, key=spend_order_key):
        if g['source_type'] not in sources or g['status'] != 'Active':
            continue
        if not is_active(g['valid_from'], g['validity_type'], g.get('expires_on'), day):
            continue
        net = money(g['net'])
        held = money(reserved_by_grant.get(g['name'], '0'))
        if net < 0 or net > money(g['amount']) or held < 0 or held > net:
            raise ValueError('Wallet grant or reservation ledger integrity violation')
        available = net - held
        use = min(available, left)
        if use > 0:
            result.append((g['name'], g['source_type'], use))
            left -= use
        if left == 0:
            break
    if left > 0:
        raise ValueError('Insufficient eligible wallet credits')
    return result


def grant_net(grants, ledger_rows):
    net = defaultdict(lambda: Decimal('0.00'))
    for line in ledger_rows:
        net[line['grant']] += money(line['amount'])
    return [dict(g, net=net[g['name']]) for g in grants]
