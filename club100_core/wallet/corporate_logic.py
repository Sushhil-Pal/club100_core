"""Pure capacity calculations for employer funded credit tranches."""
from club100_core.wallet.logic import money


def remaining_capacity(funded, allocated):
    funded = money(funded)
    allocated = money(allocated)
    if funded <= 0 or allocated < 0 or allocated > funded:
        raise ValueError("Corporate pool balances violate funding limits")
    return funded - allocated


def require_capacity(funded, allocated, requested):
    requested = money(requested)
    if requested <= 0:
        raise ValueError("Allocation must be positive")
    remaining = remaining_capacity(funded, allocated)
    if requested > remaining:
        raise ValueError("Insufficient funded corporate credit pool balance")
    return remaining - requested
