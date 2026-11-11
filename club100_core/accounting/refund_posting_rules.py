"""Stage 5F-C2: pure full-refund invariants; no DB/financial side effects."""
from decimal import Decimal
from club100_core.accounting.rules import money


def refund_identity(gross, wallet, gateway, sources, *, original_status, enrollment_status,
                    payment_status, refund_case_status=None):
    g, w, r = (money(v) for v in (gross, wallet, gateway))
    if g <= 0 or min(w, r) < 0 or w + r != g:
        raise ValueError('Refund gross and original tenders do not reconcile')
    if original_status != 'Posted' or enrollment_status != 'Scheduled' or payment_status != 'Paid':
        raise ValueError('Only paid, future Scheduled, fully accounted memberships can be refunded')
    if refund_case_status and refund_case_status not in ('Prepared', 'Gateway Pending'):
        raise ValueError('Existing refund case requires reconciliation')
    if not isinstance(sources, dict):
        raise ValueError('Missing original funding-source split')
    net = Decimal('0.00')
    for source, raw in sources.items():
        if source not in ('Corporate', 'Referral'):
            # Promotion treatment and tax classification remain unapproved.
            raise ValueError('Unsupported source for automatic refund: ' + str(source))
        a = money(raw)
        if a <= 0:
            raise ValueError('Original source amounts must be positive')
        net += a
    if net != w:
        raise ValueError('Original grant source amounts differ from wallet tender')
    return {'gross': g, 'wallet': w, 'gateway': r,
            'sources': {s: money(a) for s,a in sorted(sources.items())}}


def refundable_grant(validity_type, expiry, today):
    """No automatic restoration of already expired credits into a booked liability."""
    if validity_type == 'Lifetime':
        return True
    if validity_type != 'Expires On' or not expiry:
        raise ValueError('Unknown credit validity on original grant')
    from datetime import date
    d = lambda value: value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if d(expiry) < d(today):
        raise ValueError('Original wallet grant already expired; Finance review required')
    return True
