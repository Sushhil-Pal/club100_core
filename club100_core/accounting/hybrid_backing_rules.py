"""Pure, fail-closed grant-allocation rules for D2C3B2C2B.

This module NEVER authorizes wallet capture or financial posting.
"""
from decimal import Decimal, InvalidOperation

CENT = Decimal('0.01')
FUNDED = frozenset(('Corporate', 'Referral'))

def money(value):
    if isinstance(value, (bool, float)) or value is None:
        raise ValueError('Funding amounts must be exact paise, not float/bool/None')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid funding amount') from None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        raise ValueError('Funding amount must be finite nonnegative paise')
    return amount

def validate_funded_allocations(allocations, expected_total):
    """Verify unique original grants, Corporate/Referral-only, positive amounts."""
    target = money(expected_total)
    if not isinstance(allocations, list) or len(allocations) > 200:
        raise ValueError('Funded allocations must be a bounded list')
    if target == 0 and allocations:
        raise ValueError('Zero funded tender cannot have allocations')
    if target > 0 and not allocations:
        raise ValueError('Nonzero funded tender requires grant allocations')
    seen = set()
    totals = {}
    canonical = []
    for row in allocations:
        if not isinstance(row, dict):
            raise ValueError('Grant allocation must be a dictionary')
        grant = row.get('grant')
        source = row.get('source')
        if not isinstance(grant, str) or not grant.strip() or grant.strip() != grant or grant in seen:
            raise ValueError('Missing or repeated original grant')
        if source not in FUNDED:
            raise ValueError('Only Corporate/Referral can settle a membership invoice')
        amount = money(row.get('amount'))
        if amount <= 0:
            raise ValueError('Funding allocation must be positive')
        seen.add(grant)
        totals[source] = totals.get(source, Decimal('0.00')) + amount
        canonical.append({'grant':grant,'source':source,'amount':str(amount)})
    if sum(totals.values(),Decimal('0.00')) != target:
        raise ValueError('Funded allocations do not equal immutable wallet tender')
    return canonical, totals

def validate_grant_for_reservation(grant, expected_member, allocation, active_today):
    """Do not rely exclusively on accounting voucher provenance for grant eligibility."""
    if not isinstance(grant, dict):
        grant = dict(grant) if grant is not None else None
    if not grant or grant.get('name') != allocation['grant']:
        raise ValueError('Funded grant no longer exists')
    if (grant.get('member') != expected_member or grant.get('source_type') != allocation['source']
            or grant.get('status') != 'Active' or grant.get('currency') != 'INR'
            or not grant.get('wallet')):
        raise ValueError('Funded grant owner, source, status, currency, or wallet invalid')
    if not active_today:
        raise ValueError('Funded grant is not within its original validity window')
    if money(grant.get('amount')) < money(allocation['amount']):
        raise ValueError('Funded allocation exceeds grant face value')
    return True

def validate_reservation_hold(record, hybrid, now):
    if not record or record.get('status') != 'Reserved':
        raise ValueError('Funded wallet reservation is not Reserved')
    if record.get('member') != hybrid['member'] or record.get('reference') != hybrid['enrollment']:
        raise ValueError('Funded reservation member/enrollment identity changed')
    if money(record.get('total_amount')) != money(hybrid['funded_wallet_tender']):
        raise ValueError('Funded reservation total differs from frozen checkout')
    if not record.get('expires_at') or record['expires_at'] <= now:
        raise ValueError('Funded wallet hold expired')
    return True
