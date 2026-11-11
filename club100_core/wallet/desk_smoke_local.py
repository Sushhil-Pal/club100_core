"""Read-only ERPNext check of actual wallet Desk summary with existing site data."""
from decimal import Decimal

import frappe

from club100_core.wallet.core import wallet_balance
from club100_core.wallet.desk_wallet import wallet_desk_summary


def run():
    # Bench execute is run as Administrator on the local site.
    if frappe.local.site != 'club100.local':
        frappe.throw('Local-only smoke test')
    rows = frappe.get_all('Club100 Wallet', fields=['name', 'member'], limit_page_length=1)
    if not rows:
        return {'outcome': 'SKIP', 'reason': 'No existing wallet to inspect', 'read_only': True}
    target = rows[0]
    before = (frappe.db.count('Club100 Wallet Grant'), frappe.db.count('Club100 Wallet Ledger'))
    data = wallet_desk_summary(target.name)
    raw = wallet_balance(target.member)
    assert data['wallet'] == target.name
    assert data['member'] == target.member
    assert Decimal(data['balances']['available']) == Decimal(raw['available'])
    assert Decimal(data['balances']['reserved']) == Decimal(raw.get('reserved', '0'))
    for src in ('Corporate', 'Promotion', 'Referral'):
        assert Decimal(data['balances']['by_source'][src]) == Decimal(raw['by_source'][src])
    assert len(data['grants']) <= data['grant_page_size']
    assert len(data['transactions']) <= data['transaction_page_size']
    after = (frappe.db.count('Club100 Wallet Grant'), frappe.db.count('Club100 Wallet Ledger'))
    assert before == after
    return {'site': frappe.local.site, 'outcome': 'PASS',
            'wallet': target.name, 'member': target.member,
            'available': data['balances']['available'],
            'reserved': data['balances']['reserved'],
            'grants_displayed': len(data['grants']),
            'transactions_displayed': len(data['transactions']),
            'read_only': True, 'counts_unchanged': True}
