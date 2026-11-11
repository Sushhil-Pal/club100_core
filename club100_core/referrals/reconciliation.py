"""Read-only member referral wallet liability reconciliation, not a GL posting.

Use from bench as System Manager. Anomalies deliberately block a confident
production-readiness statement; this does NOT reconcile ERPNext GL or taxes.
"""
from decimal import Decimal

import frappe
from club100_core.referrals.settlement_logic import money
from club100_core.referrals.settlement import REFERRAL_KEY_PREFIX


def member_referral_liability_report(limit=1000):
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required', frappe.PermissionError)
    limit = int(limit)
    if limit < 1 or limit > 1000:
        raise ValueError('Limit must be from 1 to 1000')
    names = frappe.get_all('Club100 Referral Reward', filters={
        'status': ['in', ['Available', 'Approved', 'On Hold', 'Reversed']],
        'reward_track': 'Member Credit',
    }, pluck='name', limit_page_length=limit + 1, order_by='creation desc')
    truncated = len(names) > limit
    outstanding = Decimal('0.00')
    credited = 0
    anomalies = []
    for name in names[:limit]:
        reward = frappe.get_doc('Club100 Referral Reward', name)
        grant_name = frappe.db.get_value('Club100 Wallet Grant', {
            'source_event_key': REFERRAL_KEY_PREFIX + name}, 'name')
        if reward.status == 'Available':
            credited += 1
            if not grant_name or grant_name != reward.settlement_reference:
                anomalies.append({'reward': name, 'issue': 'Available reward missing linked grant'})
                continue
            grant = frappe.get_doc('Club100 Wallet Grant', grant_name)
            if (grant.referral_reward != name or grant.source_type != 'Referral'
                    or money(grant.amount) != money(reward.verified_reward_amount)):
                anomalies.append({'reward': name, 'issue': 'Grant identity or amount mismatch'})
                continue
            balance = frappe.db.sql('''SELECT COALESCE(SUM(amount),0) AS amount
                FROM `tabClub100 Wallet Ledger` WHERE `grant`=%s''', grant_name, as_dict=True)[0].amount
            balance = money(balance)
            if balance < 0 or balance > money(grant.amount):
                anomalies.append({'reward': name, 'issue': 'Invalid grant remaining ledger balance'})
            else:
                outstanding += balance
        elif grant_name and reward.status in ('On Hold', 'Approved'):
            anomalies.append({'reward': name, 'issue': 'Unmatured reward already has a grant'})
        elif grant_name and reward.status == 'Reversed':
            net = frappe.db.sql('''SELECT COALESCE(SUM(amount),0) AS amount
                FROM `tabClub100 Wallet Ledger` WHERE `grant`=%s''', grant_name, as_dict=True)[0].amount
            if money(net) != 0:
                anomalies.append({'reward': name, 'issue': 'Reversed reward retains wallet liability'})
    return {
        'records_reviewed': min(len(names), limit),
        'truncated': truncated,
        'posted_referral_rewards': credited,
        'remaining_referral_wallet_liability_inr': str(outstanding),
        'anomalies': anomalies[:100],
        'anomaly_count_in_sample': len(anomalies),
        'erpnext_gl_reconciled': False,
        'caution': 'Wallet subledger only. ERPNext GL, taxable treatment and corporate pool liability require separate reconciliation.',
    }
