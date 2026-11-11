"""Read-only, best-effort backed wallet balance for member UI and checkout quote.

Reservation and posting still revalidate under DB locks; this is NOT a
financial posting authorization. Promotional balance is not cash tender.
"""
from decimal import Decimal
from club100_core.accounting.rules import money
from club100_core.accounting.settlement import _config, _require_wallet_liability_backing


def verified_wallet_quote(balance):
    cfg = _config()
    totals = {'Corporate': Decimal('0.00'), 'Referral': Decimal('0.00')}
    accepted = []
    rejected = {'Corporate': Decimal('0.00'), 'Referral': Decimal('0.00')}
    for grant in balance.get('grants', []):
        source = grant.get('source')
        if source not in totals or not grant.get('spendable'):
            continue
        amount = money(grant.get('spendable_remaining') or 0)
        if amount <= 0:
            continue
        entry = {'grant': grant['grant'], 'source': source, 'amount': amount}
        candidate = accepted + [entry]
        proposal = dict(totals)
        proposal[source] += amount
        try:
            # Checks grant-specific source event, submitted source JE, GL,
            # correct funding receipt, and current liability headroom.
            _require_wallet_liability_backing(cfg, {s: a for s, a in proposal.items() if a > 0}, candidate)
        except (ValueError, RuntimeError):
            rejected[source] += amount
            continue
        accepted = candidate
        totals = proposal
    return {
        'by_source': {s: str(v) for s, v in totals.items()},
        'unbacked_by_source': {s: str(v) for s, v in rejected.items()},
        'total': str(sum(totals.values(), Decimal('0.00'))),
        'funding_verification_required': True,
    }
