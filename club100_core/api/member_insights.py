"""Read-only insights for the *logged-in member*; no member-id parameters.

Credit availability is always sourced from the established wallet balance API.
No financial postings, reward approval, or commission payouts are performed.
"""
import frappe

from club100_core.api.member_insights_rules import (
    _money, activity_label, safe_enrollment_reference, validated_page,
)
from club100_core.referrals.service import _member_for_request

ACTIVITY_PAGE_SIZE = 20


@frappe.whitelist(methods=['GET'])
def my_wallet_activity(page=1):
    """Paginated ledger history for caller's own wallet; no raw funding proofs."""
    member = _member_for_request()
    page = validated_page(page)
    wallet = frappe.db.get_value('Club100 Wallet', {'member': member}, 'name')
    if not wallet:
        return {'page': page, 'has_more': False, 'entries': []}
    offset = (page - 1) * ACTIVITY_PAGE_SIZE
    entries = frappe.get_all(
        'Club100 Wallet Ledger', filters={'wallet': wallet, 'member': member},
        fields=['name', 'creation', 'entry_type', 'amount', 'grant', 'reference'],
        order_by='creation desc, name desc',
        limit_start=offset, limit_page_length=ACTIVITY_PAGE_SIZE + 1,
    )
    visible = entries[:ACTIVITY_PAGE_SIZE]
    grant_ids = sorted({e.grant for e in visible if e.grant})
    grant_sources = {}
    if grant_ids:
        grant_rows = frappe.get_all(
            'Club100 Wallet Grant', filters={'name': ['in', grant_ids], 'member': member, 'wallet': wallet},
            fields=['name', 'source_type'], limit_page_length=ACTIVITY_PAGE_SIZE,
        )
        grant_sources = {g.name: g.source_type for g in grant_rows}
    result = []
    for row in visible:
        # A ledger line linked to an unknown/foreign grant indicates corruption.
        if row.grant and row.grant not in grant_sources:
            frappe.throw('Wallet ledger grant mismatch.', frappe.ValidationError)
        source = grant_sources.get(row.grant, 'Wallet')
        result.append({
            'id': row.name,
            'created_at': str(row.creation),
            'type': row.entry_type,
            'source': source,
            'amount': _money(row.amount),
            'description': activity_label(row.entry_type, source),
            'enrollment': safe_enrollment_reference(row.entry_type, row.reference),
        })
    return {'page': page, 'has_more': len(entries) > ACTIVITY_PAGE_SIZE, 'entries': result}


@frappe.whitelist(methods=['GET'])
def my_referral_progress():
    """Aggregate direct-referral progress. Never disclose referred identities.

    Paid referral count is *not* a reward entitlement or approval. Earned credit
    totals count only actual Referral-source wallet CREDIT ledger postings.
    """
    member = _member_for_request()
    partner = frappe.db.get_value('Club100 Referral Partner', {'member': member}, 'name')
    if not partner:
        return {'recorded_referrals': 0, 'paid_referrals': 0,
                'provisional_reward_events': 0, 'credited_total': '0.00'}

    recorded = frappe.db.count('Club100 Referral Attribution',
                               {'partner': partner, 'status': 'Captured'})
    # DISTINCT via EXISTS means renewals cannot inflate the number of people.
    # A membership must be paid and capture-verified or wallet-settled.
    paid = frappe.db.sql('''
        SELECT COUNT(*) AS count FROM `tabClub100 Referral Attribution` a
        WHERE a.partner=%s AND a.status='Captured'
          AND EXISTS (
            SELECT 1 FROM `tabClub100 Enrollment` e
            WHERE e.member=a.referred_member
              AND e.payment_status='Paid' AND e.status!='Cancelled'
              AND COALESCE(e.membership_duration_months,0) >= COALESCE(a.minimum_duration_months,0)
              AND (COALESCE(e.razorpay_capture_verified,0)=1
                   OR (e.wallet_settlement_status='Settled'
                       AND COALESCE(e.gateway_due_amount,0)=0))
          )''', (partner,), as_dict=True)
    pending = frappe.db.count('Club100 Referral Reward',
                              {'partner': partner, 'status': ['in', ['On Hold', 'Pending', 'Approved']]})
    # This is the total amount actually issued as Referral wallet credits,
    # not provisional reward estimates (which remain in On Hold).
    issued = frappe.db.sql('''
        SELECT COALESCE(SUM(l.amount),0) AS total
        FROM `tabClub100 Wallet Ledger` l
        JOIN `tabClub100 Wallet Grant` g ON g.name=l.`grant`
        WHERE l.member=%s AND g.member=%s
          AND g.source_type='Referral' AND l.entry_type='Credit'
    ''', (member, member), as_dict=True)
    return {'recorded_referrals': int(recorded),
            'paid_referrals': int(paid[0].count) if paid else 0,
            'provisional_reward_events': int(pending),
            'credited_total': _money(issued[0].total if issued else 0)}
