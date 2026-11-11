"""Club100 checkout accounting UAT audit v2 -- READ ONLY.

Install as: apps/club100_core/club100_core/accounting/checkout_audit.py
Run using bench execute on the local ERPNext site.

Never inserts, updates, submits, commits, locks, or calls Razorpay.
It validates existing ERPNext documents and their GL associations.
Hybrid checkout uses its own funded reservation key and tender snapshots.
"""

import json
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import frappe
from frappe.utils import getdate, nowdate


ZERO = Decimal('0.00')
CENT = Decimal('0.01')


def _money(value):
    try:
        return Decimal(str(value if value is not None else 0)).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _display(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return value
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _val(doc, field, default=None):
    if doc is None:
        return default
    try:
        result = doc.get(field)
        return default if result is None else result
    except (AttributeError, KeyError):
        return default


def _doc(doctype, name):
    if not name or not frappe.db.exists(doctype, name):
        return None
    return frappe.get_doc(doctype, name)


def _check(output, label, valid, *, actual=None, expected=None, severity='FAIL', detail=None):
    status = 'PASS' if valid else severity
    item = {'check': label, 'status': status}
    if actual is not None:
        item['actual'] = _display(actual)
    if expected is not None:
        item['expected'] = _display(expected)
    if detail:
        item['detail'] = detail
    output['checks'].append(item)
    return bool(valid)


def _gl_summary(doctype, voucher, company, output):
    if not voucher:
        return None
    # A voided/cancelled voucher must not be counted as posted GL evidence.
    rows = frappe.db.sql('''
        SELECT account, party_type, party, debit, credit
          FROM `tabGL Entry`
         WHERE voucher_type=%s AND voucher_no=%s
           AND company=%s AND is_cancelled=0
    ''', (doctype, voucher, company), as_dict=True)
    debit = sum((_money(r.debit) for r in rows), ZERO)
    credit = sum((_money(r.credit) for r in rows), ZERO)
    _check(output, doctype + ' has uncancelled GL Entries', len(rows) > 0,
           actual=len(rows), expected='at least 1')
    _check(output, doctype + ' GL debits equal credits',
           bool(rows) and debit == credit,
           actual={'debit': str(debit), 'credit': str(credit)},
           expected='balanced')
    return {'voucher': voucher, 'doctype': doctype, 'rows': len(rows),
            'debit': str(debit), 'credit': str(credit),
            'accounts': sorted(set(str(r.account) for r in rows))}


def audit(enrollment_id=None):
    """Report-only audit of an existing Club100 Enrollment and its financial docs.

    Example: bench --site club100.local execute \\
      club100_core.accounting.checkout_audit.audit \\
      --kwargs '{"enrollment_id":"C100-ENR-00222"}'

    No Gateway GET is issued. "Captured" is corroborated from saved verified
    evidence, but independent Razorpay confirmation belongs to a separate check.
    """
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('Administrator or System Manager required to audit financial records',
                     frappe.PermissionError)
    name = str(enrollment_id or '').strip()
    if not name:
        raise ValueError('enrollment_id is required')
    output = {'enrollment': name, 'site': frappe.local.site,
              'read_only': True, 'checks': [], 'documents': {}, 'gl': [],
              'notes': ['No records are created/changed; no Razorpay API request is made.']}
    enrollment = _doc('Club100 Enrollment', name)
    _check(output, 'Enrollment exists', bool(enrollment), actual=name)
    if not enrollment:
        output['overall'] = 'FAIL'
        output['counts'] = {'PASS': 0, 'WARN': 0, 'FAIL': 1}
        return output

    enr = enrollment
    member_id = _val(enr, 'member')
    member = _doc('Club100 Member', member_id)
    payment_id = str(_val(enr, 'payment_id') or '')
    gateway = _money(_val(enr, 'gateway_due_amount'))
    wallet = _money(_val(enr, 'wallet_applied_amount'))
    original = _money(_val(enr, 'amount_payable'))
    # A hybrid checkout holds wallet tender and reservation keys in a separate
    # immutable snapshot, not in the enrollment's ordinary checkout fields.
    hybrid_rows = frappe.get_all('Club100 Hybrid Checkout',
        filters={'enrollment': name}, fields=['name'], limit_page_length=10)
    _check(output, 'At most one Hybrid Checkout for this enrollment',
           len(hybrid_rows) <= 1, actual=len(hybrid_rows), expected='0 or 1')
    hybrid = (_doc('Club100 Hybrid Checkout', hybrid_rows[0].name)
              if len(hybrid_rows) == 1 else None)
    if hybrid:
        output['documents']['hybrid_checkout'] = {
            'name': hybrid.name, 'status': _val(hybrid, 'status'),
            'member': _val(hybrid, 'member'), 'enrollment': _val(hybrid, 'enrollment'),
            'original_price': str(_money(_val(hybrid, 'original_price'))),
            'promotion_discount': str(_money(_val(hybrid, 'promotion_discount'))),
            'wallet_tender': str(_money(_val(hybrid, 'funded_wallet_tender'))),
            'gateway_due': str(_money(_val(hybrid, 'gateway_due'))),
            'funded_reservation_key': _val(hybrid, 'funded_reservation_key'),
        }
        _check(output, 'Hybrid Checkout is Captured',
               _val(hybrid, 'status') == 'Captured',
               actual=_val(hybrid, 'status'), expected='Captured')
        _check(output, 'Hybrid Checkout belongs to enrollment and Member',
               _val(hybrid, 'member') == member_id and _val(hybrid, 'enrollment') == name,
               actual={'member': _val(hybrid, 'member'),
                       'enrollment': _val(hybrid, 'enrollment')})
    output['documents']['enrollment'] = {
        'name': enr.name, 'member': member_id, 'program': _val(enr, 'program'),
        'plan': _val(enr, 'plan'), 'months': _val(enr, 'membership_duration_months'),
        'start_date': _display(_val(enr, 'start_date')),
        'end_date': _display(_val(enr, 'end_date')),
        'status': _val(enr, 'status'), 'payment_status': _val(enr, 'payment_status'),
        'payment_gateway': _val(enr, 'payment_gateway'),
        'order_id': _val(enr, 'payment_order_id'), 'payment_id': payment_id,
        'amount_payable': str(original), 'wallet_applied': str(wallet),
        'gateway_due': str(gateway), 'invoice': _val(enr, 'sales_invoice'),
    }
    _check(output, 'Enrollment is Paid', _val(enr, 'payment_status') == 'Paid',
           actual=_val(enr, 'payment_status'), expected='Paid')
    start = _val(enr, 'start_date')
    expected_status = 'Scheduled' if start and getdate(start) > getdate(nowdate()) else 'Active'
    _check(output, 'Enrollment status matches start date',
           _val(enr, 'status') in ((expected_status, 'Completed') if expected_status == 'Active'
                                         else ('Scheduled',)),
           actual=_val(enr, 'status'), expected=expected_status)
    _check(output, 'Member exists', bool(member), actual=member_id)
    if member:
        _check(output, 'Member account is Active', _val(member, 'status') == 'Active',
               actual=_val(member, 'status'), expected='Active')
    _check(output, 'Membership start and end dates are populated',
           bool(start and _val(enr, 'end_date')))

    if _val(enr, 'payment_gateway') == 'Razorpay':
        _check(output, 'Razorpay evidence on enrollment',
               payment_id.startswith('pay_')
               and str(_val(enr, 'payment_order_id') or '').startswith('order_')
               and bool(_val(enr, 'razorpay_capture_verified')),
               actual={'payment_id': payment_id,
                       'capture_verified': bool(_val(enr, 'razorpay_capture_verified'))},
               expected='pay_ ID, order_ ID and verified capture flag')
    elif _val(enr, 'payment_gateway') == 'Club100 Wallet':
        _check(output, 'Wallet-only payment evidence', payment_id.startswith('WALLET:'),
               actual=payment_id, expected='WALLET:...')
    else:
        _check(output, 'Recognized checkout tender', False,
               actual=_val(enr, 'payment_gateway'))

    customer_link = str(_val(member, 'customer') or '')
    contact_link = str(_val(member, 'contact') or '')
    customer = _doc('Customer', customer_link)
    contact = _doc('Contact', contact_link)
    output['documents']['member'] = {
        'name': member_id, 'full_name': _val(member, 'full_name'),
        'customer': customer_link or None, 'contact': contact_link or None}
    _check(output, 'Member has individual Customer', bool(customer), actual=customer_link)
    _check(output, 'Member has Contact', bool(contact), actual=contact_link,
           severity='WARN')
    if customer:
        _check(output, 'Customer is enabled and Individual',
               not bool(_val(customer, 'disabled'))
               and _val(customer, 'customer_type') == 'Individual',
               actual={'type': _val(customer, 'customer_type'),
                       'disabled': bool(_val(customer, 'disabled'))})
        shared = frappe.db.get_value('Club100 Member',
            {'customer': customer_link, 'name': ['!=', member_id]}, 'name')
        _check(output, 'Customer is not shared with another Member', not shared,
               actual=shared or 'unique')
        configured_uat = str(frappe.conf.get('club100_accounting_test_customer') or '')
        _check(output, 'Customer is not the shared UAT Customer',
               customer_link != configured_uat, actual=customer_link)
    if contact:
        matches = any(_val(row, 'link_doctype') == 'Customer'
                      and _val(row, 'link_name') == customer_link
                      for row in (contact.get('links') or []))
        _check(output, 'Contact links to Member Customer', matches,
               actual=contact_link, severity='WARN')

    mappings = frappe.get_all('Club100 Membership Accounting',
                              filters={'enrollment': name},
                              fields=['name'], limit_page_length=10)
    _check(output, 'Exactly one Membership Accounting record', len(mappings) == 1,
           actual=len(mappings), expected=1)
    mapping = _doc('Club100 Membership Accounting', mappings[0].name) if len(mappings) == 1 else None
    invoice_id = str(_val(enr, 'sales_invoice') or '')
    invoice = _doc('Sales Invoice', invoice_id)
    _check(output, 'Sales Invoice linked from Enrollment exists', bool(invoice),
           actual=invoice_id)

    if mapping:
        output['documents']['membership_accounting'] = {
            'name': mapping.name, 'status': _val(mapping, 'status'),
            'invoice': _val(mapping, 'sales_invoice'),
            'customer': _val(mapping, 'customer'),
            'payment_entry': _val(mapping, 'gateway_payment_entry'),
            'wallet_journal_entry': _val(mapping, 'wallet_journal_entry'),
            'gross': str(_money(_val(mapping, 'gross'))),
            'gateway_paid': str(_money(_val(mapping, 'gateway_paid'))),
            'wallet_paid': str(_money(_val(mapping, 'wallet_paid'))),
            'sources': _val(mapping, 'wallet_sources_json'),
        }
        _check(output, 'Membership Accounting is Posted', _val(mapping, 'status') == 'Posted',
               actual=_val(mapping, 'status'), expected='Posted')
        _check(output, 'Membership Accounting maps correct Member',
               _val(mapping, 'member') == member_id,
               actual=_val(mapping, 'member'), expected=member_id)
        _check(output, 'Membership Accounting invoice matches Enrollment',
               _val(mapping, 'sales_invoice') == invoice_id,
               actual=_val(mapping, 'sales_invoice'), expected=invoice_id)
        _check(output, 'Membership Accounting Customer matches Member',
               _val(mapping, 'customer') == customer_link,
               actual=_val(mapping, 'customer'), expected=customer_link)

    if invoice:
        grand = _money(_val(invoice, 'grand_total'))
        taxes = _money(_val(invoice, 'total_taxes_and_charges'))
        net = _money(_val(invoice, 'net_total'))
        outstanding = _money(_val(invoice, 'outstanding_amount'))
        company = _val(invoice, 'company')
        output['documents']['sales_invoice'] = {
            'name': invoice_id, 'docstatus': _val(invoice, 'docstatus'),
            'customer': _val(invoice, 'customer'), 'company': company,
            'net_total': str(net), 'taxes': str(taxes),
            'grand_total': str(grand), 'outstanding': str(outstanding),
            'items': [{'item_code': _val(i, 'item_code'),
                       'description': _val(i, 'description'),
                       'warehouse': _val(i, 'warehouse'),
                       'amount': str(_money(_val(i, 'amount')))}
                      for i in (invoice.get('items') or [])],
            'tax_rows': [{'account': _val(t, 'account_head'),
                          'rate': _val(t, 'rate'),
                          'inclusive': bool(_val(t, 'included_in_print_rate')),
                          'amount': str(_money(_val(t, 'tax_amount')))}
                         for t in (invoice.get('taxes') or [])],
        }
        _check(output, 'Sales Invoice submitted', _val(invoice, 'docstatus') == 1,
               actual=_val(invoice, 'docstatus'), expected=1)
        _check(output, 'Sales Invoice billed to Member Customer',
               _val(invoice, 'customer') == customer_link,
               actual=_val(invoice, 'customer'), expected=customer_link)
        _check(output, 'Sales Invoice outstanding is zero', outstanding == ZERO,
               actual=str(outstanding), expected='0.00')
        config_company = str(frappe.conf.get('club100_accounting_company') or '').strip()
        if config_company:
            _check(output, 'Sales Invoice uses configured Club100 company',
                   company == config_company, actual=company, expected=config_company)
        config_tax = str(frappe.conf.get('club100_accounting_inclusive_tax_template') or '').strip()
        if config_tax:
            _check(output, 'Sales Invoice uses configured GST template',
                   _val(invoice, 'taxes_and_charges') == config_tax,
                   actual=_val(invoice, 'taxes_and_charges'), expected=config_tax)
        _check(output, 'Sales Invoice net + tax = grand total', net + taxes == grand,
               actual={'net': str(net), 'tax': str(taxes), 'grand': str(grand)})
        if mapping:
            _check(output, 'Membership Accounting gross equals invoice',
                   _money(_val(mapping, 'gross')) == grand,
                   actual=str(_money(_val(mapping, 'gross'))), expected=str(grand))
        else:
            _check(output, 'Invoice matches enrollment original price', grand == original,
                   actual=str(grand), expected=str(original), severity='WARN',
                   detail='Approved promotions may reduce the invoiced total.')
        tax_lines = list(invoice.get('taxes') or [])
        _check(output, 'GST rows are inclusive', bool(tax_lines) and all(
               bool(_val(t, 'included_in_print_rate')) for t in tax_lines),
               actual=len(tax_lines), severity='WARN')
        _check(output, 'Exactly one membership service item',
               len(invoice.get('items') or []) == 1,
               actual=len(invoice.get('items') or []), expected=1)
        if invoice.get('items'):
            item = invoice.get('items')[0]
            nonstock = bool(frappe.db.exists('Item', _val(item, 'item_code'))) and not bool(
                frappe.db.get_value('Item', _val(item, 'item_code'), 'is_stock_item'))
            _check(output, 'Billing item is non-stock service', nonstock,
                   actual=_val(item, 'item_code'))
            _check(output, 'No warehouse specified for service item',
                   not bool(_val(item, 'warehouse')),
                   actual=_val(item, 'warehouse') or '(blank)', severity='WARN')
            description = str(_val(item, 'description') or '')
            descriptive = all(str(s) in description for s in (
                _val(enr, 'program'), _val(enr, 'membership_duration_months'),
                _val(enr, 'start_date'), _val(enr, 'end_date')) if s)
            _check(output, 'Invoice line describes program/duration/service dates',
                   descriptive, actual=description, severity='WARN')
        gl = _gl_summary('Sales Invoice', invoice_id, company, output)
        if gl:
            output['gl'].append(gl)
    else:
        grand = ZERO
        company = None

    gateway_expected = _money(_val(mapping, 'gateway_paid')) if mapping else gateway
    wallet_expected = _money(_val(mapping, 'wallet_paid')) if mapping else wallet
    if mapping and invoice:
        _check(output, 'Accounting tender split equals invoiced total',
               gateway_expected + wallet_expected == grand,
               actual={'razorpay': str(gateway_expected), 'wallet': str(wallet_expected)},
               expected=str(grand))
        if hybrid:
            _check(output, 'Hybrid wallet tender matches Membership Accounting',
                   _money(_val(hybrid, 'funded_wallet_tender')) == wallet_expected,
                   actual=str(_money(_val(hybrid, 'funded_wallet_tender'))),
                   expected=str(wallet_expected))
            _check(output, 'Hybrid Razorpay tender matches Membership Accounting',
                   _money(_val(hybrid, 'gateway_due')) == gateway_expected,
                   actual=str(_money(_val(hybrid, 'gateway_due'))),
                   expected=str(gateway_expected))
            _check(output, 'Hybrid original less promotion matches invoice',
                   _money(_val(hybrid, 'original_price'))
                   - _money(_val(hybrid, 'promotion_discount')) == grand,
                   actual={'original': str(_money(_val(hybrid, 'original_price'))),
                           'promotion': str(_money(_val(hybrid, 'promotion_discount')))},
                   expected=str(grand))
            if wallet != wallet_expected:
                # Do not mislabel the posted GL as failed when the legacy
                # Enrollment summary is not mirrored from Hybrid Checkout.
                _check(output, 'Enrollment wallet summary mirrors hybrid tender', False,
                       actual=str(wallet), expected=str(wallet_expected),
                       severity='WARN' if wallet == ZERO else 'FAIL',
                       detail='Hybrid Checkout and Membership Accounting are authoritative; '
                              'enrollment.wallet_applied_amount was not populated by hybrid finalizer.')
            else:
                _check(output, 'Enrollment wallet summary mirrors hybrid tender', True,
                       actual=str(wallet), expected=str(wallet_expected))
        else:
            _check(output, 'Enrollment wallet amount matches accounting',
                   wallet == wallet_expected,
                   actual=str(wallet), expected=str(wallet_expected))
    if hybrid and gateway_expected > ZERO:
        evidence_rows = frappe.get_all('Club100 Hybrid Payment Evidence',
            filters={'enrollment': name}, fields=['name'], limit_page_length=10)
        _check(output, 'Exactly one Hybrid Payment Evidence record',
               len(evidence_rows) == 1, actual=len(evidence_rows), expected=1)
        evidence = (_doc('Club100 Hybrid Payment Evidence', evidence_rows[0].name)
                    if len(evidence_rows) == 1 else None)
        if evidence:
            output['documents']['hybrid_evidence'] = {
                'name': evidence.name, 'state': _val(evidence, 'state'),
                'provider_status': _val(evidence, 'provider_status'),
                'payment_id': _val(evidence, 'razorpay_payment_id'),
                'amount_paise': _val(evidence, 'amount_paise')}
            _check(output, 'Hybrid evidence Settled and captured',
                   _val(evidence, 'state') == 'Settled'
                   and _val(evidence, 'provider_status') == 'captured',
                   actual={'state': _val(evidence, 'state'),
                           'provider_status': _val(evidence, 'provider_status')},
                   expected={'state': 'Settled', 'provider_status': 'captured'})
            _check(output, 'Hybrid payment evidence matches Razorpay amount and IDs',
                   _val(evidence, 'hybrid_checkout') == hybrid.name
                   and _val(evidence, 'razorpay_payment_id') == payment_id
                   and _val(evidence, 'razorpay_order_id') == _val(enr, 'payment_order_id')
                   and _money(Decimal(str(_val(evidence, 'amount_paise') or 0)) / 100) == gateway_expected,
                   actual={'payment_id': _val(evidence, 'razorpay_payment_id'),
                           'amount_paise': _val(evidence, 'amount_paise')},
                   expected={'payment_id': payment_id,
                             'amount_paise': int(gateway_expected * 100)})
    payment_name = str(_val(mapping, 'gateway_payment_entry') or '')
    payment = _doc('Payment Entry', payment_name)
    if gateway_expected > 0:
        _check(output, 'Gateway Payment Entry linked', bool(payment),
               actual=payment_name)
        if payment:
            output['documents']['payment_entry'] = {
                'name': payment.name, 'docstatus': _val(payment, 'docstatus'),
                'party': _val(payment, 'party'),
                'paid_amount': str(_money(_val(payment, 'paid_amount'))),
                'received_amount': str(_money(_val(payment, 'received_amount'))),
                'paid_to': _val(payment, 'paid_to'),
                'reference_no': _val(payment, 'reference_no'),
                'allocations': [
                    {'invoice': _val(r, 'reference_name'),
                     'amount': str(_money(_val(r, 'allocated_amount')))}
                    for r in (payment.get('references') or [])],
            }
            _check(output, 'Payment Entry submitted', _val(payment, 'docstatus') == 1,
                   actual=_val(payment, 'docstatus'), expected=1)
            _check(output, 'Payment Entry party is Member Customer',
                   _val(payment, 'party_type') == 'Customer'
                   and _val(payment, 'party') == customer_link,
                   actual=_val(payment, 'party'), expected=customer_link)
            _check(output, 'Payment Entry references Razorpay payment ID',
                   str(_val(payment, 'reference_no') or '') == payment_id,
                   actual=_val(payment, 'reference_no'), expected=payment_id)
            _check(output, 'Payment Entry amount equals gateway tender',
                   _money(_val(payment, 'paid_amount')) == gateway_expected
                   and _money(_val(payment, 'received_amount')) == gateway_expected,
                   actual={'paid': str(_money(_val(payment, 'paid_amount'))),
                           'received': str(_money(_val(payment, 'received_amount')))},
                   expected=str(gateway_expected))
            refs = [r for r in (payment.get('references') or [])
                    if _val(r, 'reference_doctype') == 'Sales Invoice'
                    and _val(r, 'reference_name') == invoice_id]
            _check(output, 'Payment Entry fully allocated to this invoice',
                   len(refs) == 1 and sum((_money(_val(r, 'allocated_amount'))
                                          for r in refs), ZERO) == gateway_expected,
                   actual=[str(_money(_val(r, 'allocated_amount'))) for r in refs],
                   expected=str(gateway_expected))
            cfg_account = str(frappe.conf.get('club100_accounting_razorpay_clearing_account') or '')
            if cfg_account:
                _check(output, 'Payment Entry uses configured Razorpay clearing account',
                       _val(payment, 'paid_to') == cfg_account,
                       actual=_val(payment, 'paid_to'), expected=cfg_account)
            if payment_id:
                other_entries = frappe.get_all('Payment Entry',
                    filters={'reference_no': payment_id, 'docstatus': 1},
                    fields=['name'], limit_page_length=10)
                _check(output, 'Razorpay payment ID has exactly one submitted Payment Entry',
                       len(other_entries) == 1 and other_entries[0].name == payment.name,
                       actual=[p.name for p in other_entries], expected=[payment.name])
            gl = _gl_summary('Payment Entry', payment.name, company, output)
            if gl:
                output['gl'].append(gl)
    else:
        _check(output, 'No gateway Payment Entry for wallet-only checkout',
               not payment_name, actual=payment_name or '(none)')

    journal_name = str(_val(mapping, 'wallet_journal_entry') or '')
    journal = _doc('Journal Entry', journal_name)
    if wallet_expected > 0:
        _check(output, 'Wallet Journal Entry linked', bool(journal),
               actual=journal_name)
        if journal:
            output['documents']['wallet_journal_entry'] = {
                'name': journal.name, 'docstatus': _val(journal, 'docstatus'),
                'total_debit': str(_money(_val(journal, 'total_debit'))),
                'total_credit': str(_money(_val(journal, 'total_credit')))}
            _check(output, 'Wallet Journal Entry submitted',
                   _val(journal, 'docstatus') == 1, actual=_val(journal, 'docstatus'))
            _check(output, 'Wallet Journal Entry totals balance',
                   _money(_val(journal, 'total_debit')) == _money(_val(journal, 'total_credit')),
                   actual={'debit': str(_money(_val(journal, 'total_debit'))),
                           'credit': str(_money(_val(journal, 'total_credit')))})
            _check(output, 'Wallet Journal Entry clears Member receivable',
                   any(_val(a, 'reference_type') == 'Sales Invoice'
                       and _val(a, 'reference_name') == invoice_id
                       and _val(a, 'party') == customer_link
                       and _money(_val(a, 'credit_in_account_currency')) == wallet_expected
                       for a in (journal.get('accounts') or [])),
                   actual=journal_name)
            gl = _gl_summary('Journal Entry', journal.name, company, output)
            if gl:
                output['gl'].append(gl)
        reservation_key = str(_val(hybrid, 'funded_reservation_key') or '') if hybrid else str(
            _val(enr, 'wallet_reservation_key') or '')
        _check(output, 'Wallet reservation key is present', bool(reservation_key),
               actual=reservation_key or '(blank)',
               expected='Hybrid funded_reservation_key' if hybrid else 'Enrollment wallet_reservation_key')
        reservation_row = frappe.db.get_value('Club100 Wallet Reservation',
                     {'reservation_key': reservation_key},
                     ['name', 'status', 'member', 'reference', 'total_amount'], as_dict=True) if reservation_key else None
        _check(output, 'Captured wallet reservation matches this enrollment',
               bool(reservation_row and reservation_row.status == 'Captured'
                    and reservation_row.member == member_id
                    and reservation_row.reference == name
                    and _money(reservation_row.total_amount) == wallet_expected),
               actual=reservation_row.name if reservation_row else '(none)')
        if reservation_row:
            output['documents']['wallet_reservation'] = {
                'name': reservation_row.name, 'status': reservation_row.status,
                'member': reservation_row.member, 'reference': reservation_row.reference,
                'amount': str(_money(reservation_row.total_amount)),
                'source_key': 'hybrid' if hybrid else 'normal'}
        if reservation_row:
            items = frappe.get_all('Club100 Wallet Reservation Item',
                  filters={'parent': reservation_row.name},
                  fields=['grant', 'source_type', 'amount'])
            _check(output, 'Wallet grant allocations equal recorded spend',
                   sum((_money(x.amount) for x in items), ZERO) == wallet_expected,
                   actual=str(sum((_money(x.amount) for x in items), ZERO)),
                   expected=str(wallet_expected))
            for item in items:
                ledger_rows = frappe.get_all('Club100 Wallet Ledger',
                    filters={'event_key': 'WALLET:SPEND:' + reservation_row.name + ':' + item.grant},
                    fields=['name', 'entry_type', 'member', 'grant', 'amount', 'reference'],
                    limit_page_length=10)
                _check(output, 'Exactly one wallet Spend ledger for grant ' + item.grant,
                       len(ledger_rows) == 1,
                       actual=len(ledger_rows), expected=1)
                ledger = ledger_rows[0] if len(ledger_rows) == 1 else None
                _check(output, 'Wallet Spend ledger exists for grant ' + item.grant,
                       bool(ledger and ledger.entry_type == 'Spend'
                            and ledger.member == member_id and ledger.grant == item.grant
                            and _money(ledger.amount) == -_money(item.amount)),
                       actual=str(_money(ledger.amount)) if ledger else '(none)',
                       expected=str(-_money(item.amount)))
    else:
        _check(output, 'No wallet Journal Entry for gateway-only checkout',
               not journal_name, actual=journal_name or '(none)')
        _check(output, 'No wallet reservation for gateway-only checkout',
               not _val(enr, 'wallet_reservation_key'),
               actual=_val(enr, 'wallet_reservation_key') or '(none)')
        # A promotion-only checkout legitimately captures a Promotion reservation,
        # but has NO funded wallet tender or corresponding liability Journal Entry.
        if hybrid and _money(_val(hybrid, 'promotion_discount')) > ZERO:
            _check(output, 'No funded reservation in promotion-only checkout',
                   not _val(hybrid, 'funded_reservation_key'),
                   actual=_val(hybrid, 'funded_reservation_key') or '(none)')
        else:
            wallet_res = frappe.db.get_value('Club100 Wallet Reservation',
                          {'reference': name, 'status': 'Captured'}, 'name')
            _check(output, 'No captured wallet reservation for gateway-only checkout',
                   not wallet_res, actual=wallet_res or '(none)')

    # Promotion is a supplier discount grant, not money tender. Its reservation
    # is separate from the backed wallet tender reservation above.
    promo_amount = _money(_val(hybrid, 'promotion_discount')) if hybrid else ZERO
    if promo_amount > ZERO:
        _check(output, 'Promotion discount shown on Sales Invoice',
               bool(invoice and _money(_val(invoice, 'discount_amount')) == promo_amount
                    and _val(invoice, 'apply_discount_on') == 'Grand Total'),
               actual={'discount': str(_money(_val(invoice, 'discount_amount'))) if invoice else None,
                       'apply_on': _val(invoice, 'apply_discount_on') if invoice else None},
               expected={'discount': str(promo_amount), 'apply_on': 'Grand Total'})
        promo_key = str(_val(hybrid, 'promotion_reservation_key') or '')
        _check(output, 'Promotion reservation key exists', bool(promo_key),
               actual=promo_key or '(none)')
        p = frappe.db.get_value('Club100 Wallet Reservation',
              {'reservation_key': promo_key},
              ['name','status','member','reference','total_amount'], as_dict=True) if promo_key else None
        _check(output, 'Captured Promotion reservation matches enrollment',
               bool(p and p.status == 'Captured' and p.member == member_id
                    and p.reference == name and _money(p.total_amount) == promo_amount),
               actual=p.name if p else '(none)',expected=str(promo_amount))
        if p:
            output['documents']['promotion_reservation']={
                'name':p.name,'status':p.status,'member':p.member,
                'amount':str(_money(p.total_amount))}
            items = frappe.get_all('Club100 Wallet Reservation Item',
                 filters={'parent':p.name},fields=['grant','source_type','amount'])
            total = sum((_money(i.amount) for i in items), ZERO)
            _check(output, 'Promotion allocations match invoice discount',
                   total == promo_amount and bool(items)
                   and all(i.source_type == 'Promotion' for i in items),
                   actual=str(total),expected=str(promo_amount))
            for item in items:
                grant = frappe.db.get_value('Club100 Wallet Grant', item.grant,
                     ['member','source_type','source_event_key','funding_reference','owner'],as_dict=True)
                _check(output, 'Promotion grant belongs to member ' + item.grant,
                       bool(grant and grant.member == member_id
                            and grant.source_type == 'Promotion'),actual=item.grant)
                # Issuance identity is stored on the immutable Wallet Grant,
                # not on a separate per-grant approval document.
                key = str(grant.source_event_key or '') if grant else ''
                issued = bool(grant and grant.owner
                              and key.startswith('PROMOTION:ADMIN:')
                              and str(grant.funding_reference or '') == 'PROMO:ADMIN:' + key)
                _check(output, 'Promotion grant has administrator issuance provenance ' + item.grant,
                       issued,
                       actual={'source_event_key':key,'issuer':grant.owner if grant else None},
                       expected='system-generated PROMOTION:ADMIN: issuance reference')
                ledger = frappe.get_all('Club100 Wallet Ledger',
                    filters={'event_key':'WALLET:SPEND:'+p.name+':'+item.grant},
                    fields=['name','entry_type','member','grant','amount'],limit_page_length=10)
                _check(output, 'Exactly one captured Promotion Spend for ' + item.grant,
                       len(ledger) == 1 and ledger[0].entry_type == 'Spend'
                       and ledger[0].member == member_id and ledger[0].grant == item.grant
                       and _money(ledger[0].amount) == -_money(item.amount),
                       actual=[{'name':x.name,'amount':str(_money(x.amount))} for x in ledger],
                       expected=str(-_money(item.amount)))
        # This discount must not create a wallet-liability Journal Entry itself.
        if wallet_expected == ZERO:
            _check(output, 'Promotion-only checkout has no wallet liability Journal Entry',
                   not journal_name,actual=journal_name or '(none)')

    # A submitted and zero-outstanding SI is only correct with matched tender GL.
    # Even when the caller sees a green Paid badge, any missing PE/JE is a FAIL.
    counts = {key: sum(1 for c in output['checks'] if c['status'] == key)
              for key in ('PASS', 'WARN', 'FAIL')}
    output['counts'] = counts
    output['overall'] = 'FAIL' if counts['FAIL'] else 'WARN' if counts['WARN'] else 'PASS'
    output['notes'].append('GST checks are arithmetic/inclusive only; not a legal tax-rate or place-of-supply opinion.')
    output['notes'].append('External Razorpay capture status was not fetched; saved capture evidence and ERPNext vouchers were inspected.')
    output['notes'].append('Membership Accounting Posted is a custom status, not ERPNext docstatus; vouchers must also be submitted.')
    if hybrid:
        output['notes'].append('Hybrid wallet tender and reservation key live on Club100 Hybrid Checkout, not ordinary enrollment wallet fields.')
    return output
