"""Read-only, member-scoped Frappe smoke check. Never issues credits or rewards."""
import os
import frappe

from club100_core.api.member_insights import my_wallet_activity, my_referral_progress
from club100_core.wallet.core import my_wallet


def run():
    if os.environ.get('CLUB100_ALLOW_INSIGHTS_TEST') != '1' or frappe.local.site != 'club100.local':
        frappe.throw('Local-only insights test; explicit environment flag required')
    original = frappe.session.user
    selected = None
    for row in frappe.get_all('Club100 Wallet', fields=['member'], limit_page_length=100):
        user = frappe.db.get_value('Club100 Member', row.member, 'user')
        if user and user not in ('Administrator', 'Guest'):
            selected = (row.member, user)
            break
    if not selected:
        return {'outcome': 'SKIP', 'reason': 'No wallet with a member login is available',
                'read_only': True}
    try:
        frappe.set_user(selected[1])
        balance = my_wallet()
        activity = my_wallet_activity(1)
        progress = my_referral_progress()
        assert balance['wallet'] == frappe.db.get_value('Club100 Wallet', {'member': selected[0]}, 'name')
        assert len(activity['entries']) <= 20
        assert all('event_key' not in e and 'funding_reference' not in e for e in activity['entries'])
        assert isinstance(progress['recorded_referrals'], int)
        assert isinstance(progress['paid_referrals'], int)
        assert isinstance(progress['provisional_reward_events'], int)
        assert progress['paid_referrals'] <= progress['recorded_referrals']
        return {'outcome': 'PASS', 'site': frappe.local.site,
                'member': selected[0], 'available': balance['available'],
                'activity_rows': len(activity['entries']), 'has_more': activity['has_more'],
                'referral_summary': progress, 'read_only': True,
                'notes': 'No grants, ledger entries, attribution or reward records created.'}
    finally:
        frappe.set_user(original)
