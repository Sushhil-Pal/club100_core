"""Stage 5F-D1: read-only expiry/promotional exposure and local-only invoice queue.

No grant expiry ledger debits, promotion GST decisions, refund initiation or
production accounting are implemented here. The sole optional write is the
EXISTING Stage 5E invoice+Payment Entry posting service, for Razorpay-only UAT
purchases, with an explicit flag and local test-mode guard.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import os

import frappe
from frappe.utils import cint, getdate, now_datetime

from club100_core.accounting.lifecycle_rules import (
    money, unused_expired, policy_gate, invoice_queue_classification,
)


def _admin():
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required', frappe.PermissionError)


def _limit(value, max_items=200):
    if type(value) is not int or not 1 <= value <= max_items:
        raise ValueError('limit must be an integer in range 1..' + str(max_items))
    return value


def expiry_exposure_report(as_of=None, limit=100):
    """Unspent, expired ledger exposure; NOT an instruction to release a GL liability."""
    _admin()
    _limit(limit)
    today = getdate(as_of) if as_of is not None else getdate()
    if not isinstance(today, date):
        raise ValueError('Invalid as_of date')
    # Limit eligible expired grants rather than truncating all grants first.
    rows = frappe.get_all('Club100 Wallet Grant',
        filters={'validity_type': 'Expires On', 'status': 'Active', 'expires_on': ['<', str(today)]},
        fields=['name', 'source_type', 'amount', 'validity_type', 'expires_on', 'status'],
        order_by='expires_on asc, creation asc', limit_page_length=limit + 1)
    selected = rows[:limit]
    names = [x.name for x in selected]
    ledger = {}
    held = {}
    if names:
        placeholders = ','.join(['%s'] * len(names))
        sums = frappe.db.sql('SELECT `grant`,COALESCE(SUM(amount),0) AS net FROM `tabClub100 Wallet Ledger` '
            'WHERE `grant` IN (' + placeholders + ') GROUP BY `grant`', tuple(names), as_dict=True)
        ledger = {r.grant: money(r.net) for r in sums}
        # Reservation holds that remain live at expiry are a blocking exception;
        # never release a liability while a conflicting hold exists.
        holds = frappe.db.sql('SELECT i.`grant`, COALESCE(SUM(i.amount),0) AS net '
            'FROM `tabClub100 Wallet Reservation Item` i '
            'JOIN `tabClub100 Wallet Reservation` r ON r.name=i.parent '
            'WHERE i.`grant` IN (' + placeholders + ') AND r.status=%s AND r.expires_at>%s '
            'GROUP BY i.`grant`', tuple(names) + ('Reserved', now_datetime()), as_dict=True)
        held = {r.grant: money(r.net) for r in holds}
    totals = defaultdict(lambda: Decimal('0.00'))
    details = []
    for grant in selected:
        state = unused_expired(grant, ledger.get(grant.name, Decimal('0.00')),
                               today, held.get(grant.name, Decimal('0.00')))
        value = money(state['unspent'])
        totals[grant.source_type] += value
        details.append({'grant': grant.name, 'source_type': grant.source_type,
            'expires_on': str(grant.expires_on), 'unspent': str(value),
            'held': state['held'], 'status': state['status'],
            'posting_allowed': False, 'policy': policy_gate(grant.source_type)['reason']})
    return {'site': frappe.local.site, 'as_of': str(today), 'records_reviewed': len(selected),
        'truncated': len(rows)>limit, 'unspent_expired_by_source':
        {source: str(totals[source]) for source in ('Corporate','Referral','Promotion')},
        'grants': details, 'read_only': True, 'gl_release_posted': False,
        'production_ready': False, 'warning': 'This is wallet ledger exposure, not cash or automatically releasable liability; employer/referral/promotion terms require separate Finance decisions.'}


def operational_backlog(limit=50):
    """Classify new paid purchases and historical refunds, no network or writes."""
    _admin()
    _limit(limit, 100)
    rows = frappe.get_all('Club100 Enrollment',
        filters={'website_source': 'PWA Commerce', 'payment_status': ['in', ['Paid', 'Refunded']]},
        fields=['name', 'payment_status', 'payment_gateway', 'wallet_applied_amount', 'sales_invoice'],
        order_by='creation desc', limit_page_length=limit+1)
    out=[]
    summary=defaultdict(int)
    for row in rows[:limit]:
        mapping=frappe.db.get_value('Club100 Membership Accounting', {'enrollment': row.name}, 'name')
        category=invoice_queue_classification(row.payment_status, mapping,
                                               row.wallet_applied_amount or 0, row.payment_gateway)
        issues=[]
        if category == 'Preview Required':
            from club100_core.accounting.settlement import preview_one
            try:
                preview=preview_one(row.name)
                if preview['status'] == 'Ready' and money(preview['wallet']) == 0:
                    category='Local Pilot Ready: Gateway Only'
                else:
                    category='Invoice Manual Review'
                    issues=preview.get('issues') or [preview.get('status','Unknown preview status')]
            except Exception as exc:
                category='Invoice Manual Review'
                issues=[str(exc)[:240]]
        summary[category]+=1
        out.append({'enrollment': row.name, 'category':category,
                    'accounting_record':mapping, 'issues':issues})
    return {'site':frappe.local.site,'records_reviewed':len(out),'truncated':len(rows)>limit,
        'summary':dict(summary), 'rows':out, 'read_only':True,
        'auto_refunds_enabled':False, 'promotion_posting_enabled':False,
        'production_ready':False}


def _local_ops_enabled():
    from club100_core.accounting.settlement import _local_pilot, _post_allowed
    return (bool(_local_pilot()) and bool(_post_allowed())
        and cint(frappe.conf.get('club100_accounting_ops_enabled', 0)) == 1
        and cint(frappe.conf.get('club100_accounting_scheduler_enabled', 0)) == 0)


def process_local_gateway_invoice_queue(limit=5, dry_run=True):
    """UAT only: reuse Stage 5E posting, never start checkout, refund or Razorpay calls.

    On success one existing verified gateway-only enrollment is accounted at a
    time. No blanket transaction commit of unrelated wallet/referral changes.
    Invocation is restricted to an admin, local site, Test Mode and explicit
    site/env guards. dry_run is the default and performs zero writes.
    """
    _admin()
    _limit(limit, 20)
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be boolean')
    if not dry_run and (not _local_ops_enabled() or
                        os.environ.get('CLUB100_ALLOW_STAGE5FD_LOCAL_POST') != '1'):
        raise ValueError('Local pilot invoice posting requires explicit safeguards and env opt-in')
    rows=frappe.get_all('Club100 Enrollment', filters={
        'website_source':'PWA Commerce', 'pricing_model':'Online Plan',
        'payment_status':'Paid', 'payment_gateway':'Razorpay',
        'wallet_applied_amount':0, 'sales_invoice':['is','not set']},
        pluck='name', order_by='creation asc', limit_page_length=limit)
    from club100_core.accounting.settlement import preview_one, post_one
    results=[]
    for name in rows:
        try:
            preview=preview_one(name)
            if preview['status']!='Ready' or money(preview['wallet'])!=0:
                results.append({'enrollment':name,'action':'Review','issues':preview.get('issues') or []})
                continue
            if dry_run:
                results.append({'enrollment':name,'action':'Ready (dry run)'})
            else:
                posted=post_one(name, commit=True)
                results.append({'enrollment':name,'action':posted['status'],
                    'invoice':posted.get('invoice')})
        except Exception as exc:
            # A failed Stage 5E post rolls back its savepoint; clear any
            # still-open transaction before proceeding to the next row.
            if not dry_run:
                frappe.db.rollback()
            results.append({'enrollment':name,'action':'Review','issues':[str(exc)[:240]]})
    return {'site':frappe.local.site,'dry_run':dry_run,'results':results,
        'production_ready':False,'no_automatic_refunds':True}


def healthcheck():
    _admin()
    return {'site':frappe.local.site, 'local_ops_enabled':_local_ops_enabled(),
        'promotion_accounting_approved':False,
        'automatic_expiry_gl_enabled':False, 'automatic_refund_posting_enabled':False,
        'operational_invoice_queue':'Razorpay-only local UAT, guarded',
        'production_ready':False}
