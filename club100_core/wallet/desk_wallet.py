"""Admin-only, read-only wallet overview for ERPNext Desk.

No ledger posts, balance snapshots, or persisted totals. Uses the same
expiry- and hold-aware wallet_balance function as checkout.
"""
import frappe
from frappe.utils import getdate

from club100_core.wallet.core import wallet_balance
from club100_core.wallet.desk_logic import pack_balances, pack_grant
from club100_core.wallet.logic import money

GRANT_PAGE = 50
TRANSACTION_PAGE = 25


def _wallet_doc(wallet_name):
    if frappe.session.user == 'Guest' or 'System Manager' not in frappe.get_roles():
        frappe.throw('System Manager access required to view this wallet.', frappe.PermissionError)
    if not wallet_name or not isinstance(wallet_name, str) or len(wallet_name) > 140:
        frappe.throw('Valid wallet name required.', frappe.ValidationError)
    doc = frappe.get_doc('Club100 Wallet', wallet_name)
    if not frappe.has_permission('Club100 Wallet', ptype='read', doc=doc):
        frappe.throw('Not permitted to read this wallet.', frappe.PermissionError)
    return doc


@frappe.whitelist(methods=['GET'])
def wallet_desk_summary(wallet_name):
    """Return a limited, read-only transaction/grant snapshot to a Desk admin.

    Full wallet totals include ALL grants and ledger postings regardless
    of the 50-grant and 25-transaction display pagination.
    """
    wallet = _wallet_doc(wallet_name)
    if wallet.member != wallet.name:
        # Wallet DocType uses field:member autoname.
        frappe.throw('Wallet/member integrity mismatch.', frappe.ValidationError)
    balance = wallet_balance(wallet.member)
    if balance['wallet'] != wallet.name:
        frappe.throw('Wallet identity mismatch.', frappe.ValidationError)

    today = getdate()
    # wallet_balance() already validates all active grant balances and deducts
    # current reservation holds. Metadata fetch is only for display/enrichment.
    display_grants = frappe.get_all(
        'Club100 Wallet Grant',
        filters={'wallet': wallet.name, 'status': 'Active'},
        fields=['name', 'amount', 'corporate_pool'],
        order_by='creation desc', limit_page_length=GRANT_PAGE,
    )
    balances_by_grant = {row['grant']: row for row in balance['grants']}
    grant_rows = []
    for row in display_grants:
        snapshot = balances_by_grant.get(row.name)
        if not snapshot:
            frappe.throw('Missing wallet grant balance snapshot.', frappe.ValidationError)
        grant_rows.append(pack_grant(snapshot, row, today, wallet.status))

    ledger_rows = frappe.get_all(
        'Club100 Wallet Ledger',
        filters={'wallet': wallet.name},
        fields=['name', 'creation', 'entry_type', 'amount', 'grant', 'reference'],
        order_by='creation desc', limit_page_length=TRANSACTION_PAGE,
    )
    transactions = [
        {'id': row.name, 'created_at': str(row.creation),
         'type': row.entry_type, 'amount': str(money(row.amount)),
         'grant': row.grant, 'reference': row.reference or ''}
        for row in ledger_rows
    ]

    return {
        'wallet': wallet.name,
        'member': wallet.member,
        'status': wallet.status,
        'currency': wallet.currency,
        'as_of': str(today),
        'balances': pack_balances(balance),
        'grants': grant_rows,
        'grant_count': frappe.db.count('Club100 Wallet Grant',
                                       {'wallet': wallet.name, 'status': 'Active'}),
        'grant_page_size': GRANT_PAGE,
        'transactions': transactions,
        'transaction_count': frappe.db.count('Club100 Wallet Ledger', {'wallet': wallet.name}),
        'transaction_page_size': TRANSACTION_PAGE,
    }


def wallet_desk_healthcheck():
    """Read-only bench check; no permissions bypass or balance mutation."""
    return {
        'site': frappe.local.site,
        'wallet_dashboard_field': bool(frappe.get_meta('Club100 Wallet').has_field('wallet_dashboard')),
        'wallets': frappe.db.count('Club100 Wallet'),
        'service': 'club100_core.wallet.desk_wallet.wallet_desk_summary',
        'read_only': True,
    }
