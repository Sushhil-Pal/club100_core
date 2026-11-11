"""Pure source-of-funds and general-ledger invariants. No Frappe imports."""
from decimal import Decimal, InvalidOperation

PAISE = Decimal("0.01")

def money(value):
    """Same exact-to-paise semantics as Stage 5E, kept pure for standalone tests."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid INR amount") from exc
    if not result.is_finite() or result != result.quantize(PAISE):
        raise ValueError("INR amount must be exact to paise")
    return result.quantize(PAISE)


def require_pool_receipt(pool_amount, received, unallocated, already_linked, *, pool_currency='INR',
                         receipt_currency='INR', same_customer=True, same_reference=True,
                         submitted=True, is_real_bank=True):
    pool_amount, received, unallocated, already_linked = map(money, (pool_amount, received, unallocated, already_linked))
    if pool_amount <= 0 or received <= 0 or unallocated < 0 or already_linked < 0:
        raise ValueError('Invalid corporate receipt amounts')
    if pool_currency != 'INR' or receipt_currency != 'INR':
        raise ValueError('Corporate funding must be INR')
    if not (same_customer and same_reference and submitted and is_real_bank):
        raise ValueError('Corporate receipt identity/status/bank evidence mismatch')
    if received - already_linked < pool_amount or unallocated < pool_amount:
        raise ValueError('Corporate pool would exceed received, unallocated cash advance')
    return pool_amount


def require_grant_credit(grant_amount, credit_total, net_ledger, *, available=True,
                         source='Referral', no_prior_spend=True, confirmed_invoice=True):
    amount, credits, net = map(money, (grant_amount, credit_total, net_ledger))
    if source != 'Referral' or not available or not confirmed_invoice or not no_prior_spend:
        raise ValueError('Referral source lacks maturity, invoice, or unspent evidence')
    if amount <= 0 or credits != amount or net != amount:
        raise ValueError('Referral grant amount does not match immutable credit ledger')
    return amount


def check_gl_backing(amount, credit_balance):
    a, balance = map(money, (amount, credit_balance))
    if a <= 0 or balance < a:
        raise ValueError('Insufficient booked liability credit in ERPNext GL')
    return True


def journal_rows_for_corporate(receivable, customer, liability, amount):
    amount = money(amount)
    if amount <= 0 or not receivable or not customer or not liability or receivable == liability:
        raise ValueError('Corporate reclassification accounts are invalid')
    return [
        {'account':receivable, 'party_type':'Customer','party':customer,
         'debit_in_account_currency':float(amount),'credit_in_account_currency':0},
        {'account':liability, 'credit_in_account_currency':float(amount),'debit_in_account_currency':0},
    ]


def journal_rows_for_referral(expense, liability, amount):
    amount = money(amount)
    if amount <= 0 or not expense or not liability or expense == liability:
        raise ValueError('Referral expense/liability accounts are invalid')
    return [
        {'account':expense,'debit_in_account_currency':float(amount),'credit_in_account_currency':0},
        {'account':liability,'credit_in_account_currency':float(amount),'debit_in_account_currency':0},
    ]


def net_event_amount(issued, redeemed, reversed_amount, expired):
    issued, redeemed, reversed_amount, expired = map(money, (issued, redeemed, reversed_amount, expired))
    if min(issued, redeemed, reversed_amount, expired) < 0:
        raise ValueError('Negative movement is not supported')
    remaining = issued - redeemed - reversed_amount - expired
    if remaining < 0:
        raise ValueError('Wallet event balances exceed liability issuance')
    return remaining
