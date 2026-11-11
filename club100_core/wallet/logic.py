"""Pure helpers for Club100 Wallet. Does not import Frappe.

Expiry date is inclusive; Lifetime always means expires_on=None.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def money(value):
    result = Decimal(str(value or "0"))
    if not result.is_finite():
        raise ValueError("Amount must be finite")
    rounded = result.quantize(CENT, rounding=ROUND_HALF_UP)
    if rounded != result:
        raise ValueError("Amounts must have at most two decimal places")
    return rounded


def is_active(valid_from, validity_type, expires_on=None, on_date=None):
    day = on_date or date.today()
    if not isinstance(day, date):
        day = date.fromisoformat(str(day)[:10])
    start = valid_from if isinstance(valid_from, date) else date.fromisoformat(str(valid_from)[:10])
    if day < start:
        return False
    if validity_type == "Lifetime":
        return expires_on in (None, "")
    if validity_type != "Expires On" or not expires_on:
        return False
    end = expires_on if isinstance(expires_on, date) else date.fromisoformat(str(expires_on)[:10])
    return day <= end


def spend_order_key(grant):
    """Earliest-expiring credits first; Lifetime credits always last."""
    expiry = grant.get("expires_on") if grant.get("validity_type") == "Expires On" else None
    return (expiry is None, str(expiry or "9999-12-31"), str(grant.get("valid_from") or ""), str(grant.get("name") or ""))
