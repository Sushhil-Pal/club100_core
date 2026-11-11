"""Pure, read-only formatting helpers for ERPNext wallet detail presentation."""
from datetime import date
from decimal import Decimal

from club100_core.wallet.logic import money


def classify_grant(g, today, wallet_status):
    """Informational grant state; never used to authorize wallet spending."""
    day = date.fromisoformat(str(today)[:10])
    start = date.fromisoformat(str(g['valid_from'])[:10])
    remaining = money(g['remaining'])
    if wallet_status != 'Active':
        return 'Wallet Frozen'
    if day < start:
        return 'Future'
    if g['validity'] != 'Lifetime':
        expiry = g.get('expires_on')
        if not expiry or day > date.fromisoformat(str(expiry)[:10]):
            return 'Expired'
    if remaining <= 0:
        return 'Used'
    if money(g.get('spendable_remaining', '0')) <= 0:
        return 'Fully Reserved'
    return 'Available'


def pack_balances(balance):
    """Use authoritative ledger-derived service values; don't recompute totals."""
    by_source = balance.get('by_source') or {}
    return {
        'available': str(money(balance['available'])),
        'reserved': str(money(balance.get('reserved', 0))),
        'expired_unspent': str(money(balance.get('expired_unspent', 0))),
        'future_unvested': str(money(balance.get('future_unvested', 0))),
        'by_source': {source: str(money(by_source.get(source, 0)))
                      for source in ('Corporate', 'Referral', 'Promotion')},
    }


def pack_grant(g, metadata, today, wallet_status):
    """Input g is from wallet_balance; metadata is from the Grant DocType."""
    state = classify_grant(g, today, wallet_status)
    return {
        'grant': g['grant'], 'source': g['source'],
        'original_amount': str(money(metadata['amount'])),
        'remaining': str(money(g['remaining'])),
        'reserved': str(money(g.get('reserved', 0))),
        'available': str(money(g.get('spendable_remaining', 0))) if state == 'Available' else '0.00',
        'validity': g['validity'], 'valid_from': str(g['valid_from']),
        'expires_on': str(g['expires_on']) if g.get('expires_on') else None,
        'corporate_pool': metadata.get('corporate_pool') or None,
        'state': state,
    }
