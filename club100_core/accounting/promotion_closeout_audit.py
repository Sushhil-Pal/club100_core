"""Read-only Club100 promotion and tax closeout inspection.

Run inside the ERPNext site:
bench --site club100.local execute club100_core.accounting.promotion_closeout_audit.report \
  --kwargs '{"enrollment_id":"C100-ENR-00239"}'

No writes, payment API requests, or model save calls.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import frappe


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _field(obj, field):
    try:
        return obj.get(field)
    except Exception:
        return getattr(obj, field, None)


def report(enrollment_id='C100-ENR-00239'):
    if frappe.session.user != 'Administrator' and not any(
        role in ('System Manager', 'Accounts Manager') for role in frappe.get_roles(frappe.session.user)
    ):
        frappe.throw('Administrator / finance manager required for the read-only invoice audit', frappe.PermissionError)
    if frappe.local.site != 'club100.local':
        frappe.throw('This diagnostic is restricted to club100.local')
    if not frappe.db.exists('Club100 Enrollment', enrollment_id):
        frappe.throw('Unknown Enrollment')
    enrollment = frappe.get_doc('Club100 Enrollment', enrollment_id)
    inv_name = _field(enrollment, 'sales_invoice')
    if not inv_name or not frappe.db.exists('Sales Invoice', inv_name):
        return {'enrollment': enrollment_id, 'invoice': inv_name, 'status': 'FAIL', 'reason': 'No linked Sales Invoice', 'read_only': True}
    inv = frappe.get_doc('Sales Invoice', inv_name)
    company = inv.company
    item_codes = sorted({i.item_code for i in inv.items if i.item_code})

    issues = []
    totals = {
        'original_line_total': str(sum((_money(i.amount) for i in inv.items), Decimal('0.00'))),
        'discount_amount': str(_money(_field(inv, 'discount_amount'))),
        'apply_discount_on': _field(inv, 'apply_discount_on'),
        'net_total_after_discount': str(_money(inv.net_total)),
        'total_tax_after_discount': str(_money(inv.total_taxes_and_charges)),
        'grand_total': str(_money(inv.grand_total)),
        'outstanding': str(_money(inv.outstanding_amount)),
    }
    if _money(inv.net_total) + _money(inv.total_taxes_and_charges) != _money(inv.grand_total):
        issues.append({'severity':'FAIL','issue':'Net total plus final tax differs from grand total'})
    if inv.docstatus != 1:
        issues.append({'severity':'FAIL','issue':'Sales Invoice is not submitted'})

    gl = frappe.get_all(
        'GL Entry',
        filters={'voucher_type':'Sales Invoice','voucher_no':inv.name,'is_cancelled':0},
        fields=['name','account','debit','credit'],
        limit_page_length=200,
    )
    tax_rows = []
    for row in inv.taxes:
        account = _field(row, 'account_head')
        before = _money(_field(row, 'tax_amount'))
        after_raw = _field(row, 'tax_amount_after_discount_amount')
        after = _money(after_raw) if after_raw is not None else None
        gl_credit = sum((_money(g.credit) - _money(g.debit) for g in gl if g.account == account), Decimal('0.00'))
        tax_rows.append({
            'account':account,
            'rate': _field(row,'rate'),
            'tax_amount_before_discount':str(before),
            'tax_amount_after_discount':str(after) if after is not None else 'FIELD UNAVAILABLE',
            'gst_liability_net_credit_in_invoice_gl':str(gl_credit),
            'matches_after_discount':(gl_credit == after) if after is not None else None,
        })
        if after is not None and gl_credit != after:
            issues.append({'severity':'FAIL','issue':'GST GL liability differs from after-discount tax','account':account})
        elif after is None:
            issues.append({'severity':'WARN','issue':'Tax after-discount field not available','account':account})
    if gl and sum((_money(g.credit) - _money(g.debit) for g in gl), Decimal('0.00')) != Decimal('0.00'):
        issues.append({'severity':'FAIL','issue':'Invoice GL entries do not balance'})
    if not gl:
        issues.append({'severity':'FAIL','issue':'No submitted invoice GL entries found'})

    item_details = []
    for code in item_codes:
        item = frappe.get_doc('Item', code)
        defaults = []
        for d in (item.get('item_defaults') or []):
            defaults.append({'company':_field(d,'company'), 'default_warehouse':_field(d,'default_warehouse')})
        invoice_warehouse = [
            {'warehouse':_field(i,'warehouse'), 'item_code':i.item_code}
            for i in inv.items if i.item_code == code
        ]
        item_details.append({
            'item_code':code,
            'is_stock_item':_field(item,'is_stock_item'),
            'invoice_row_warehouse':invoice_warehouse,
            'item_company_defaults':defaults,
        })
        if not _field(item,'is_stock_item') and any(x['warehouse'] for x in invoice_warehouse):
            issues.append({'severity':'WARN','issue':'Non-stock service item has an invoice warehouse','item':code})

    member = _field(enrollment,'member')
    grants = frappe.get_all(
        'Club100 Wallet Grant',
        filters={'member':member,'source_type':'Promotion'},
        fields=['name','status','amount','notes','owner','creation','source_event_key'],
        order_by='creation desc',limit_page_length=50
    )
    grant_rows = [
        {'grant':g.name,'amount':str(_money(g.amount)),'status':g.status,
         'issued_by':g.owner,'issued_on':str(g.creation),
         'reason_in_internal_notes':g.notes,
         'issuance_reference':g.source_event_key}
        for g in grants
    ]
    for g in grant_rows:
        if not (g['reason_in_internal_notes'] or '').strip():
            issues.append({'severity':'WARN','issue':'Promotion grant missing Internal Notes business reason','grant':g['grant']})

    return {
        'enrollment':enrollment_id,
        'member':member,
        'invoice':inv.name,
        'customer':inv.customer,
        'submitted':inv.docstatus == 1,
        'read_only':True,
        'invoice_totals':totals,
        'tax_rows_and_gl':tax_rows,
        'items_and_warehouse_defaults':item_details,
        'promotion_grants':grant_rows,
        'issues':issues,
        'overall':'FAIL' if any(x['severity']=='FAIL' for x in issues) else ('WARN' if issues else 'PASS'),
        'notes':[
            'ERPNext Grand Total discount can retain tax_amount before discount; tax_amount_after_discount_amount is the relevant post-discount comparison.',
            'Do not edit submitted invoices or their GL Entries to clear warnings.',
            'This checks recorded ERPNext facts, not the legal GST rate or bank-side Razorpay settlement.',
        ]
    }
