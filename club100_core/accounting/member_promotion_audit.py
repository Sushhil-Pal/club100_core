"""Read-only Club100 member, membership checkout and promotion-credit UAT audit.

Place at apps/club100_core/club100_core/accounting/member_promotion_audit.py.
Run as Administrator with `bench --site club100.local execute
  club100_core.accounting.member_promotion_audit.report
  --kwargs '{"member_id":"C100-MEM-00238"}'`.

No inserts, updates, submits, commits, locks or gateway calls.
Uses the existing, installed checkout_audit for financial transaction checks.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import frappe
from frappe.utils import cint

CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def amount(value):
    try:
        return Decimal(str(value if value is not None else 0)).quantize(
            CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Invalid monetary value in accounting data')


def fmt(value):
    return str(amount(value))


def report(member_id, recent_limit=10, audit_paid=3, include_full=0):
    """Read-only summary. include_full=1 embeds each complete checkout audit."""
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('Administrator or System Manager required', frappe.PermissionError)

    member_id = str(member_id or '').strip()
    if not member_id:
        raise ValueError('member_id is required')
    if not frappe.db.exists('Club100 Member', member_id):
        raise ValueError('Member not found: ' + member_id)

    recent_limit = max(1, min(cint(recent_limit), 25))
    audit_paid = max(0, min(cint(audit_paid), 10))
    include_full = bool(cint(include_full))

    from club100_core.wallet.core import wallet_balance
    from club100_core.accounting.promotion_uat_local import report as promotion_readiness
    from club100_core.accounting.checkout_audit import audit as audit_checkout

    member = frappe.db.get_value('Club100 Member', member_id,
                                 ['name', 'full_name', 'status', 'customer', 'contact'], as_dict=True)
    output = {
        'member': {
            'name': member.name, 'full_name': member.full_name, 'status': member.status,
            'customer': member.customer, 'contact': member.contact,
        },
        'read_only': True,
        'promotion': {},
        'enrollments': [],
        'checkout_audits': [],
        'issues': [],
        'notes': [
            'No records or financial vouchers are changed. Razorpay is not queried.',
            'Promotion credit is a price discount, not wallet cash tender.',
            'Figures reflect current grant balances; a past pre-checkout balance cannot be inferred from the current wallet alone.',
        ],
    }

    wallet = wallet_balance(member_id)  # DB reads only; uses ledger-derived balances and holds
    promotion_wallet = {g['grant']: g for g in wallet.get('grants', [])
                        if g.get('source') == 'Promotion'}
    promotion_data = promotion_readiness(member_id)  # authorized, read-only eligibility
    eligible = {g['grant'] for g in promotion_data.get('grants', [])
                if g.get('status') == 'Eligible'}

    rows = frappe.get_all('Club100 Wallet Grant',
        filters={'member': member_id, 'source_type': 'Promotion'},
        fields=['name', 'amount', 'status', 'validity_type', 'valid_from', 'expires_on',
                'source_event_key', 'funding_reference', 'notes', 'owner'],
        order_by='creation desc', limit_page_length=1000)
    grants = []
    total_issued = total_spent = total_remaining = ZERO
    for g in rows:
        ledger = frappe.get_all('Club100 Wallet Ledger',
            filters={'grant': g.name},
            fields=['name', 'entry_type', 'amount', 'event_key'],
            order_by='creation asc', limit_page_length=1000)
        credits = sum((amount(x.amount) for x in ledger if x.entry_type == 'Credit'), ZERO)
        spends = sum((-amount(x.amount) for x in ledger if x.entry_type == 'Spend'), ZERO)
        net = sum((amount(x.amount) for x in ledger), ZERO)
        total_issued += credits
        total_spent += spends
        total_remaining += net
        live = promotion_wallet.get(g.name, {})
        is_eligible = g.name in eligible
        if credits != amount(g.amount):
            output['issues'].append({'severity': 'FAIL', 'check': 'Grant issued Credit ledger matches grant',
                                     'grant': g.name, 'credit': fmt(credits), 'grant_amount': fmt(g.amount)})
        if net < ZERO or net > amount(g.amount):
            output['issues'].append({'severity': 'FAIL', 'check': 'Promotion grant ledger balance valid',
                                     'grant': g.name, 'net': fmt(net)})
        grants.append({
            'grant': g.name, 'status': g.status,
            'issued_amount': fmt(g.amount), 'credited_ledger': fmt(credits),
            'spent_ledger': fmt(spends), 'remaining_ledger': fmt(net),
            'currently_spendable': fmt(live.get('spendable_remaining', 0) if is_eligible else 0),
            'eligible_for_checkout_now': is_eligible,
            'validity': g.validity_type,
            'valid_from': str(g.valid_from) if g.valid_from else None,
            'expires_on': str(g.expires_on) if g.expires_on else None,
            'issued_by': g.owner, 'reason': g.notes,
            'ledger_events': [{'name': x.name, 'type': x.entry_type,
                               'amount': fmt(x.amount), 'event_key': x.event_key}
                              for x in ledger],
        })

    output['promotion'] = {
        'issued_total': fmt(total_issued),
        'spent_total': fmt(total_spent),
        'remaining_ledger_total': fmt(total_remaining),
        'spendable_discount_now': fmt(promotion_data.get('spendable_promotion_discount', 0)),
        'grants': grants,
    }

    enrollments = frappe.get_all('Club100 Enrollment',
        filters={'member': member_id},
        fields=['name', 'status', 'payment_status', 'program', 'plan',
                'amount_payable', 'sales_invoice', 'start_date', 'end_date'],
        order_by='creation desc', limit_page_length=recent_limit)
    output['enrollments'] = [
        {'name': e.name, 'status': e.status, 'payment_status': e.payment_status,
         'program': e.program, 'plan': e.plan, 'membership_price': fmt(e.amount_payable),
         'sales_invoice': e.sales_invoice,
         'start_date': str(e.start_date) if e.start_date else None,
         'end_date': str(e.end_date) if e.end_date else None}
        for e in enrollments
    ]

    paid = [e for e in enrollments if e.payment_status == 'Paid'][:audit_paid]
    for e in paid:
        result = audit_checkout(e.name)  # follows Hybrid Checkout and promotion reservation/Spend ledger
        hybrid = result.get('documents', {}).get('hybrid_checkout') or {}
        invoice = result.get('documents', {}).get('sales_invoice') or {}
        mapping = result.get('documents', {}).get('membership_accounting') or {}
        problems = [c for c in result.get('checks', []) if c.get('status') != 'PASS']
        row = {
            'enrollment': e.name,
            'audit_status': result.get('overall'),
            'counts': result.get('counts'),
            'hybrid_checkout': hybrid.get('name'),
            'promotion_discount': hybrid.get('promotion_discount', '0.00'),
            'wallet_tender': hybrid.get('wallet_tender', '0.00'),
            'razorpay_tender': mapping.get('gateway_paid'),
            'invoice': invoice.get('name'),
            'invoice_customer': invoice.get('customer'),
            'invoice_total': invoice.get('grand_total'),
            'invoice_outstanding': invoice.get('outstanding'),
            'payment_entry': (result.get('documents', {}).get('payment_entry') or {}).get('name'),
            'wallet_journal_entry': (result.get('documents', {}).get('wallet_journal_entry') or {}).get('name'),
            'promotion_reservation': (result.get('documents', {}).get('promotion_reservation') or {}).get('name'),
            'warnings_and_failures': problems,
        }
        if include_full:
            row['full_checkout_audit'] = result
        output['checkout_audits'].append(row)
        if result.get('counts', {}).get('FAIL'):
            output['issues'].append({'severity': 'FAIL', 'enrollment': e.name,
                                    'check': 'Checkout accounting audit',
                                    'details': problems})
        elif result.get('counts', {}).get('WARN'):
            output['issues'].append({'severity': 'WARN', 'enrollment': e.name,
                                    'check': 'Checkout accounting audit',
                                    'details': problems})

    if not paid:
        output['issues'].append({'severity': 'WARN', 'check': 'No paid enrollment audited',
                                 'detail': 'No Paid enrollment in the recent selection or audit_paid=0'})
    output['overall'] = ('FAIL' if any(i['severity'] == 'FAIL' for i in output['issues'])
                         else 'WARN' if output['issues'] else 'PASS')
    return output
