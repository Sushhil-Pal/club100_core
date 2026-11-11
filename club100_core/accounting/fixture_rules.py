"""Pure UAT-only scenario definitions. These do not authorize a real receipt."""
from decimal import Decimal, InvalidOperation


SCENARIOS = {
    'wallet': ('799.00', '799.00', '0.00'),
    'split': ('799.00', '300.00', '499.00'),
}


def scenario(mode):
    if mode not in SCENARIOS:
        raise ValueError('Only wallet and split modes are supported')
    gross, wallet, gateway = (Decimal(x) for x in SCENARIOS[mode])
    if wallet <= 0 or gateway < 0 or wallet + gateway != gross:
        raise ValueError('UAT fixture tender does not reconcile')
    return {'mode': mode, 'gross': gross, 'wallet': wallet, 'gateway': gateway}


def check_refund_proof(result, expected):
    if result.get('status') != 'Simulation Passed' or result.get('rolled_back') is not True:
        raise ValueError('Refund did not roll back its inner savepoint')
    audit = result.get('voucher_reconciliation') or {}
    try:
        wallet = Decimal(str(audit['wallet_reconciled']))
        gateway = Decimal(str(audit['gateway_reconciled']))
        grants = int(audit['original_grants_verified'])
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError('Refund reconciliation result missing or invalid') from exc
    if not (wallet.is_finite() and gateway.is_finite()):
        raise ValueError('Invalid refund reconciliation amounts')
    if wallet != expected['wallet'] or gateway != expected['gateway'] or grants != 1:
        raise ValueError('Incorrect voucher offset or original-grant refund')
    return audit
