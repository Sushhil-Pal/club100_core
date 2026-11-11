"""Pure batch allocation rules; used before posting any money."""
from decimal import Decimal
from datetime import date
from club100_core.wallet.logic import money

MAX_ROWS = 200


def prepare_lines(lines, default_from, default_type, default_expiry):
    """Normalize and validate a whole batch prior to any grant issuance."""
    if not lines or len(lines) > MAX_ROWS:
        raise ValueError(f"Batch must contain 1 to {MAX_ROWS} employee allocations")
    seen = set()
    output = []
    total = Decimal('0.00')
    for idx, row in enumerate(lines, 1):
        member = str(row.get('member') or '').strip()
        if not member or member in seen:
            raise ValueError(f"Row {idx}: member missing or duplicated in this batch")
        seen.add(member)
        amount = money(row.get('amount'))
        if amount <= 0:
            raise ValueError(f"Row {idx}: amount must be positive")
        valid_from = str(row.get('valid_from') or default_from or '').strip()
        validity = str(row.get('validity_type') or default_type or '').strip()
        expiry = str(row.get('expires_on') or default_expiry or '').strip()
        if not valid_from:
            raise ValueError(f"Row {idx}: valid-from date missing")
        try:
            vf = date.fromisoformat(valid_from[:10])
        except ValueError as exc:
            raise ValueError(f"Row {idx}: invalid valid-from date") from exc
        if validity == 'Lifetime':
            if row.get('expires_on'):
                raise ValueError(f"Row {idx}: lifetime credit must not have an expiry")
            expiry = ''
        elif validity == 'Expires On':
            if not expiry:
                raise ValueError(f"Row {idx}: expiry date required")
            try:
                exp = date.fromisoformat(expiry[:10])
            except ValueError as exc:
                raise ValueError(f"Row {idx}: invalid expiry date") from exc
            if exp < vf:
                raise ValueError(f"Row {idx}: expiry before valid-from date")
        else:
            raise ValueError(f"Row {idx}: invalid credit validity")
        total += amount
        output.append({'member':member,'amount':amount,'valid_from':str(vf),'validity_type':validity,'expires_on':expiry or None,'notes':row.get('notes') or ''})
    return output, money(total)
