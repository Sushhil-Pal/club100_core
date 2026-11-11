"""Local-development automatic ERPNext invoice and tender settlement.

Creates one submitted GST-inclusive membership Sales Invoice, a Razorpay
clearing-account Payment Entry for captured gateway cash, and an invoice-linked
Journal Entry for previously captured wallet credits. The verified PWA checkout
finalizer invokes this service before activating the membership. This internal service is not whitelisted.

Do not turn this on in production until finance validates company/customer/tax
mapping, opening liability GL balances, source grants, and refund treatment.
"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import json

import frappe
from frappe.utils import cint, getdate, get_datetime, nowdate, now_datetime

from club100_core.accounting.rules import money, breakdown, invoice_integrity


CONFIG_KEYS = {
    'company': 'club100_accounting_company',
    'customer': 'club100_accounting_test_customer',
    'item': 'club100_accounting_self_item',
    'tax_template': 'club100_accounting_inclusive_tax_template',
    'razorpay_clearing': 'club100_accounting_razorpay_clearing_account',
    'Corporate': 'club100_accounting_corporate_liability_account',
    'Referral': 'club100_accounting_referral_liability_account',
    'Promotion': 'club100_accounting_promotion_liability_account',
}


def _admin():
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager permission required', frappe.PermissionError)


def _local_pilot():
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def _post_allowed():
    # Accounting runs by default for verified local TEST checkouts; no feature flag.
    return _local_pilot()


def _config():
    return {key: str(frappe.conf.get(val) or '').strip() for key, val in CONFIG_KEYS.items()}


def _company_check(kind, name, company):
    if not name or not frappe.db.exists(kind, name):
        raise ValueError(f'Configured {kind} not found: {name or "<unset>"}')
    if kind == 'Account':
        row = frappe.db.get_value('Account', name, ['company', 'is_group', 'disabled'], as_dict=True)
        if not row or row.company != company or cint(row.is_group) or cint(row.disabled):
            raise ValueError('Account must be enabled, non-group, and belong to configured Company: ' + name)


def _tax_rows(template_name, company):
    template = frappe.get_doc('Sales Taxes and Charges Template', template_name)
    if template.company != company or cint(template.disabled):
        raise ValueError('GST tax template is disabled or belongs to another Company')
    rows = list(template.taxes or [])
    if not 1 <= len(rows) <= 2:
        raise ValueError('Only a 1- or 2-component GST tax template is allowed in pilot')
    percent = Decimal('0')
    result = []
    for r in rows:
        if (r.charge_type != 'On Net Total' or not cint(r.included_in_print_rate)
                or not r.account_head or Decimal(str(r.rate or 0)) <= 0):
            raise ValueError('GST template must contain positive inclusive On Net Total tax rows')
        _company_check('Account', r.account_head, company)
        percent += Decimal(str(r.rate))
        result.append({'charge_type': 'On Net Total', 'account_head': r.account_head,
                       'description': r.description, 'rate': float(r.rate),
                       'included_in_print_rate': 1})
    from club100_core.accounting.hybrid_gst import current_rate
    expected = current_rate()
    if percent != expected:
        raise ValueError('ERPNext inclusive GST tax template must match configured rate '+str(expected)+'%')
    return result


def _require_wallet_liability_backing(cfg, amounts, allocations=None):
    """Fail closed unless *each* spent grant is linked to a posted source GL event.

    A legacy aggregate opening balance / manual attestation is deliberately NOT
    a substitute for source-specific accounting proof. The final aggregate GL
    balance is also checked, to prevent spending liabilities already depleted
    by prior invoices. No source records or GL entries are created here.
    """
    if not amounts:
        return
    if allocations is None:
        raise ValueError('Wallet GL blocked: grant-level allocations required')
    from club100_core.accounting.wallet_backing import verify_wallet_grant_backing
    verified = verify_wallet_grant_backing(cfg, allocations)
    if verified != amounts:
        raise ValueError('Wallet GL provenance does not reconcile to purchase source totals')
    for source, amount in amounts.items():
        account = cfg[source]
        row = frappe.db.sql("SELECT COALESCE(SUM(credit-debit),0) AS amount FROM `tabGL Entry` WHERE company=%s AND account=%s AND is_cancelled=0",
                            (cfg['company'], account), as_dict=True)[0]
        if money(row.amount) < money(amount):
            raise ValueError('Insufficient ERPNext liability GL credit balance for ' + source)


def _ready(cfg, sources, gateway):
    _company_check('Company', cfg['company'], cfg['company'])
    _company_check('Customer', cfg['customer'], cfg['company'])
    _company_check('Item', cfg['item'], cfg['company'])
    if frappe.db.get_value('Item', cfg['item'], 'is_stock_item'):
        raise ValueError('Membership billing Item must be a non-stock service')
    if frappe.db.get_value('Customer', cfg['customer'], 'disabled'):
        raise ValueError('Configured Customer is disabled')
    taxes = _tax_rows(cfg['tax_template'], cfg['company'])
    if gateway > 0:
        _company_check('Account', cfg['razorpay_clearing'], cfg['company'])
    for source in sources:
        _company_check('Account', cfg[source], cfg['company'])
    return taxes


def _reservation_allocations(enrollment):
    wallet = money(enrollment.wallet_applied_amount or 0)
    if wallet == 0:
        if enrollment.wallet_reservation_key:
            raise ValueError('Unexpected wallet reservation on non-wallet checkout')
        return []
    if (not enrollment.wallet_reservation_key or enrollment.wallet_settlement_status != 'Settled'):
        raise ValueError('Wallet must be captured and settled before accounting')
    reservation = frappe.db.get_value('Club100 Wallet Reservation',
        {'reservation_key': enrollment.wallet_reservation_key},
        ['name', 'status', 'member', 'reference', 'total_amount'], as_dict=True)
    if (not reservation or reservation.status != 'Captured'
            or reservation.member != enrollment.member or reservation.reference != enrollment.name
            or money(reservation.total_amount) != wallet):
        raise ValueError('Wallet reservation identity or amount mismatch')
    items = frappe.get_all('Club100 Wallet Reservation Item',
                           filters={'parent': reservation.name},
                           fields=['grant', 'source_type', 'amount'])
    allocations = []
    for item in items:
        expected = -money(item.amount)
        ledger = frappe.db.get_value('Club100 Wallet Ledger',
            {'event_key': 'WALLET:SPEND:' + reservation.name + ':' + item.grant},
            ['entry_type', 'member', 'grant', 'amount'], as_dict=True)
        grant = frappe.db.get_value('Club100 Wallet Grant', item.grant,
                    ['source_type', 'member'], as_dict=True)
        if (not ledger or ledger.entry_type != 'Spend' or ledger.member != enrollment.member
                or ledger.grant != item.grant or money(ledger.amount) != expected
                or not grant or grant.member != enrollment.member or grant.source_type != item.source_type):
            raise ValueError('Wallet spend ledger/grant source did not match reservation')
        allocations.append({'grant': item.grant, 'source': item.source_type, 'amount': str(money(item.amount))})
    return allocations


def _enrollment(name):
    doc = frappe.get_doc('Club100 Enrollment', str(name or '').strip())
    if (doc.website_source != 'PWA Commerce' or doc.pricing_model != 'Online Plan'
            or cint(doc.is_trial) or doc.payment_status != 'Paid' or not doc.member
            or doc.status not in ('Scheduled', 'Active', 'Completed')):
        raise ValueError('Only fully paid, nontrial PWA Commerce memberships can be invoiced')
    if frappe.db.get_value('Club100 Plan', doc.plan, 'plan_code') != 'self':
        raise ValueError('Only the Self plan is enabled for this accounting pilot')
    duration = cint(doc.membership_duration_months)
    if duration not in (1, 3, 6, 12) or money(doc.monthly_equivalent) * duration != money(doc.amount_payable):
        raise ValueError('Membership is not the original server-priced Self offer')
    if doc.payment_gateway == 'Razorpay':
        if (not str(doc.payment_id or '').startswith('pay_')
                or not cint(doc.get('razorpay_capture_verified'))):
            raise ValueError('Razorpay must have verified capture evidence')
    elif doc.payment_gateway == 'Club100 Wallet':
        if not str(doc.payment_id or '').startswith('WALLET:'):
            raise ValueError('Wallet-only payment identity does not match')
    else:
        raise ValueError('Unsupported payment method for accounting pilot')
    return doc


def _financial_plan(enrollment):
    from club100_core.wallet.checkout import expected_gateway_due
    gross = money(enrollment.amount_payable)
    wallet = money(enrollment.wallet_applied_amount or 0)
    gateway = money(expected_gateway_due(enrollment))
    if enrollment.payment_gateway == 'Club100 Wallet' and gateway != 0:
        raise ValueError('Wallet-only order cannot have gateway amount')
    if enrollment.payment_gateway == 'Razorpay' and gateway <= 0:
        raise ValueError('Razorpay order must have a positive captured amount')
    allocations = _reservation_allocations(enrollment)
    financial = breakdown(gross, wallet, gateway, allocations)
    return financial, allocations


def preview_one(enrollment_id):
    """Read-only, Administrator/System Manager; never creates financial documents."""
    _admin()
    enrollment = _enrollment(enrollment_id)
    fin, allocations = _financial_plan(enrollment)
    cfg = _config()
    issues = []
    taxes = []
    try:
        from club100_core.accounting.billing_customer import require_existing_for_member
        cfg['customer'] = require_existing_for_member(enrollment.member, cfg['company'])
        taxes = _ready(cfg, fin['sources'], fin['gateway'])
        _require_wallet_liability_backing(cfg, fin['sources'], allocations)
    except Exception as exc:
        issues.append(str(exc))
    record = frappe.db.get_value('Club100 Membership Accounting', {'enrollment':enrollment.name}, 'name')
    if enrollment.sales_invoice and not record:
        issues.append('Enrollment already has a Sales Invoice outside the Stage 5E accounting mapping')
    return {'enrollment': enrollment.name, 'member': enrollment.member,
            'status': 'Already Posted' if record else 'Ready' if not issues else 'Blocked',
            'gross': str(fin['gross']), 'razorpay': str(fin['gateway']),
            'wallet': str(fin['wallet']),
            'sources': {k: str(v) for k, v in fin['sources'].items()},
            'gst_template_rows': [{'rate': x['rate'], 'account': x['account_head']} for x in taxes],
            'accounting_record': record, 'issues': issues,
            'preview_only': True, 'site': frappe.local.site}


@contextmanager
def _internal_write():
    old = getattr(frappe.flags, 'club100_accounting_internal', False)
    frappe.flags.club100_accounting_internal = True
    try:
        yield
    finally:
        frappe.flags.club100_accounting_internal = old


def _create_invoice(enrollment, cfg, taxes):
    # ERPNext can replace an explicitly supplied historical posting_date with today
    # unless set_posting_time is enabled. Keep invoice and due dates aligned with
    # the verified payment date so a delayed accounting job cannot invalidate them.
    payment_time = get_datetime(enrollment.payment_date or now_datetime())
    invoice_date = getdate(payment_time)
    doc = frappe.get_doc({
        'doctype': 'Sales Invoice', 'company': cfg['company'],
        'customer': cfg['customer'], 'set_posting_time': 1,
        'posting_date': invoice_date,
        'posting_time': payment_time.strftime('%H:%M:%S'),
        'due_date': invoice_date,
        'currency': 'INR', 'update_stock': 0,
        'taxes_and_charges': cfg['tax_template'],
        'remarks': 'Club100 Self membership ' + enrollment.name + ' (server-priced checkout)',
        'items': [{'item_code': cfg['item'], 'qty': 1, 'rate': float(money(enrollment.amount_payable)),
                   'description': ('Club100 Self membership | ' + str(enrollment.program or '')
                     + ' | ' + str(enrollment.membership_duration_months) + ' months'
                     + ' | ' + str(enrollment.start_date or '')
                     + ' to ' + str(enrollment.end_date or ''))}],
        'taxes': taxes,
    })
    doc.insert(ignore_permissions=True)
    invoice_integrity(enrollment.amount_payable, doc.net_total, doc.total_taxes_and_charges, doc.grand_total)
    if len(doc.items or []) != 1 or money(doc.items[0].net_amount) != money(doc.net_total):
        raise ValueError('Invoice generated unexpected extra lines/charges')
    doc.submit()
    if doc.docstatus != 1:
        raise ValueError('Invoice not submitted')
    return doc


def _receive_razorpay(enrollment, invoice, cfg, gateway):
    if gateway == 0:
        return None
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    payment = get_payment_entry('Sales Invoice', invoice.name, party_amount=float(gateway))
    payment.paid_to = cfg['razorpay_clearing']
    payment.paid_amount = float(gateway)
    payment.received_amount = float(gateway)
    payment.reference_no = str(enrollment.payment_id)
    payment.reference_date = getdate(enrollment.payment_date or nowdate())
    entries = [r for r in payment.references if r.reference_doctype == 'Sales Invoice'
               and r.reference_name == invoice.name]
    if len(entries) != 1:
        raise ValueError('ERPNext Payment Entry did not select the intended invoice')
    for entry in payment.references:
        entry.allocated_amount = float(gateway) if entry is entries[0] else 0
    payment.insert(ignore_permissions=True)
    payment.submit()
    if payment.docstatus != 1:
        raise ValueError('Razorpay clearing Payment Entry not submitted')
    return payment


def _redeem_wallet(enrollment, invoice, cfg, source_totals, wallet):
    if wallet == 0:
        return None
    rows = []
    for source, amount in source_totals.items():
        rows.append({'account': cfg[source], 'debit_in_account_currency': float(amount),
                     'credit_in_account_currency': 0})
    rows.append({'account': invoice.debit_to, 'party_type': 'Customer',
                 'party': invoice.customer, 'reference_type': 'Sales Invoice',
                 'reference_name': invoice.name,
                 'credit_in_account_currency': float(wallet), 'debit_in_account_currency': 0})
    entry = frappe.get_doc({'doctype': 'Journal Entry', 'company': cfg['company'],
          'voucher_type': 'Journal Entry', 'posting_date': getdate(enrollment.payment_date or nowdate()),
          'user_remark': 'Club100 wallet redemption ' + enrollment.name + ' invoice ' + invoice.name,
          'accounts': rows})
    entry.insert(ignore_permissions=True)
    entry.submit()
    if entry.docstatus != 1:
        raise ValueError('Wallet liability Journal Entry not submitted')
    return entry


def post_one(enrollment_id, commit=True):
    """Local test-mode only. One DB transaction; all accounting vouchers or none.

    Does NOT synthesize a Razorpay capture or modify wallet subledger. Its
    preconditions require the original verified payment and spent wallet ledger.
    """
    if not getattr(frappe.flags, 'club100_verified_checkout_post', False):
        _admin()  # Human-initiated reconciliation still requires finance privileges.
    if not _post_allowed():
        raise ValueError('Automatic accounting requires local Razorpay Test Mode')
    name = str(enrollment_id or '').strip()
    # Match member -> enrollment lock ordering used in original checkout.
    member = frappe.db.get_value('Club100 Enrollment', name, 'member')
    if not member:
        raise ValueError('Unknown enrollment/member')
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', member)
    frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE', name)
    doc = _enrollment(name)
    existing = frappe.db.get_value('Club100 Membership Accounting', {'enrollment':name}, 'name')
    if existing:
        record = frappe.get_doc('Club100 Membership Accounting', existing)
        if (not doc.sales_invoice or doc.sales_invoice != record.sales_invoice
                or record.status not in ('Posted', 'Refunded')):
            raise ValueError('Existing accounting mapping requires review')
        return {'status': 'Already Posted', 'enrollment':name, 'invoice':record.sales_invoice,
                'razorpay_entry':record.gateway_payment_entry or None,
                'wallet_entry':record.wallet_journal_entry or None}
    if doc.sales_invoice:
        raise ValueError('Enrollment linked to a pre-existing invoice; requires manual reconciliation')
    financial, allocs = _financial_plan(doc)
    cfg = _config()
    from club100_core.accounting.billing_customer import require_existing_for_member
    cfg['customer'] = require_existing_for_member(doc.member, cfg['company'])
    taxes = _ready(cfg, financial['sources'], financial['gateway'])
    # Serialize GL liability postings across members sharing a common source account.
    for liability_account in sorted({cfg[source] for source in financial['sources']}):
        frappe.db.sql('SELECT name FROM `tabAccount` WHERE name=%s FOR UPDATE', liability_account)
    _require_wallet_liability_backing(cfg, financial['sources'], allocs)
    # Never leave an invoice and failed tender entries half-posted.
    frappe.db.savepoint('club100_stage5e_post')
    try:
        invoice = _create_invoice(doc, cfg, taxes)
        payment = _receive_razorpay(doc, invoice, cfg, financial['gateway'])
        journal = _redeem_wallet(doc, invoice, cfg, financial['sources'], financial['wallet'])
        if money(frappe.db.get_value('Sales Invoice', invoice.name, 'outstanding_amount')) != 0:
            raise ValueError('Submitted invoice has nonzero outstanding after payment and wallet settlement')
        with _internal_write():
            record = frappe.get_doc({'doctype':'Club100 Membership Accounting',
                 'enrollment':doc.name, 'member':doc.member, 'customer':invoice.customer,
                 'sales_invoice':invoice.name,
                 'gateway_payment_entry':payment.name if payment else '',
                 'wallet_journal_entry':journal.name if journal else '',
                 'gross':financial['gross'], 'wallet_paid':financial['wallet'],
                 'gateway_paid':financial['gateway'], 'currency':'INR', 'status':'Posted',
                 'wallet_sources_json': json.dumps({k:str(v) for k,v in financial['sources'].items()}, sort_keys=True)})
            record.insert(ignore_permissions=True)
        doc.sales_invoice = invoice.name
        doc.save(ignore_permissions=True)
        out = {'status':'Posted', 'enrollment':doc.name, 'invoice':invoice.name,
               'razorpay_entry':payment.name if payment else None,
               'wallet_entry':journal.name if journal else None,
               'gross':str(financial['gross']), 'wallet':str(financial['wallet']),
               'razorpay':str(financial['gateway']), 'accounting_record':record.name}
        if commit:
            frappe.db.commit()
        return out
    except Exception:
        frappe.db.rollback(save_point='club100_stage5e_post')
        raise


def scheduled_reconcile_membership_invoices():
    """Inert by default. Batch limited and logs exceptions for Finance review."""
    if not _post_allowed():
        return {'status':'unsupported_environment'}
    names = frappe.get_all('Club100 Enrollment', filters={
        'website_source':'PWA Commerce', 'pricing_model':'Online Plan',
        'payment_status':'Paid', 'sales_invoice':['is','not set']},
        pluck='name', order_by='creation asc', limit_page_length=10)
    results=[]
    for name in names:
        try:
            results.append(post_one(name, commit=True))
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), 'Club100 Stage5E Invoice Posting')
            results.append({'enrollment':name,'status':'Manual Review: see Error Log'})
    return {'processed':len(results),'results':results}


def refund_readiness(enrollment_id):
    """Read-only credit note/PE/JV plan; deliberately NEVER auto-issues tax refunds."""
    _admin()
    row=frappe.db.get_value('Club100 Membership Accounting', {'enrollment':str(enrollment_id)},
        ['name','sales_invoice','status','gross','gateway_paid','wallet_paid'],as_dict=True)
    if not row:
        return {'enrollment':str(enrollment_id),'status':'No posted membership accounting record'}
    case=frappe.db.get_value('Club100 Wallet Refund Case', {'enrollment':str(enrollment_id)},
        ['name','status','gateway_refund_id'],as_dict=True)
    return {'enrollment':str(enrollment_id),'status':row.status,
            'invoice':row.sales_invoice, 'refund_case':case.name if case else None,
            'refund_case_status':case.status if case else None,
            'credit_note_due':str(money(row.gross)),
            'razorpay_refund_entry_due':str(money(row.gateway_paid)),
            'wallet_liability_reversal_due':str(money(row.wallet_paid)),
            'actual_refund_evidence':case.gateway_refund_id if case else None,
            'accounting_refund_posted':False,
            'caution':'Stage 5E does not create GST credit notes or refund GL vouchers. Finance must reconcile before production.'}


def healthcheck():
    _admin()
    cfg=_config()
    return {'site':frappe.local.site, 'local_test_mode':_local_pilot(),
       'posting_enabled':_post_allowed(),
       'scheduler_enabled':_post_allowed(),
       'configured':{key: bool(value) for key,value in cfg.items()},
       'accounting_records':frappe.db.count('Club100 Membership Accounting') if frappe.db.exists('DocType','Club100 Membership Accounting') else 0,
       'refund_auto_posting_enabled':False,
       'corporate_funding_gl_reconciled':False,
       'opening_wallet_liabilities_attested':bool(cint(frappe.conf.get('club100_accounting_opening_liabilities_verified',0))),
       'production_ready':False,
       'note':'Local-only invoice/tender posting; requires real customer/tax mapping, refund GL, corporate funding proof, full GL reconciliation and Finance sign-off.'}
