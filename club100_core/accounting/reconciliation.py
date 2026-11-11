"""Read-only Club100 membership and wallet vs ERPNext accounting diagnostics.

No posting, no changes to customer accounts, and no claim that this report
replaces financial statements or auditor confirmation.
"""
from collections import defaultdict
from decimal import Decimal
import frappe

from club100_core.accounting.rules import money
from club100_core.accounting.settlement import _admin, _config


def accounting_readiness_report(limit=200):
    _admin()
    if type(limit) is not int or limit < 1 or limit > 500:
        raise ValueError('limit must be an integer 1-500')
    fields = ['name', 'member', 'amount_payable', 'payment_id', 'payment_gateway',
              'payment_status', 'sales_invoice', 'wallet_applied_amount', 'status']
    rows = frappe.get_all('Club100 Enrollment', filters={
        'website_source':'PWA Commerce','payment_status':['in',['Paid','Refunded']]},
        fields=fields, order_by='creation desc', limit_page_length=limit+1)
    anomalies=[]
    counts=defaultdict(int)
    for r in rows[:limit]:
        counts['paid_or_refunded_enrollments']+=1
        record_name=frappe.db.get_value('Club100 Membership Accounting', {'enrollment':r.name}, 'name')
        if not record_name:
            counts['without_accounting_record']+=1
            anomalies.append({'enrollment':r.name,'issue':'No Club100 membership accounting record'})
            continue
        rec=frappe.get_doc('Club100 Membership Accounting',record_name)
        if r.sales_invoice != rec.sales_invoice:
            anomalies.append({'enrollment':r.name,'issue':'Enrollment and mapping invoice differ'})
        invoice = frappe.db.get_value('Sales Invoice',rec.sales_invoice,
                ['docstatus','grand_total','outstanding_amount','is_return'],as_dict=True)
        if (not invoice or invoice.docstatus != 1 or invoice.is_return
                or money(invoice.grand_total)!=money(rec.gross)):
            anomalies.append({'enrollment':r.name,'issue':'Invoice missing/unsubmitted or gross mismatch'})
        elif money(invoice.outstanding_amount) != 0 and r.payment_status == 'Paid':
            anomalies.append({'enrollment':r.name,'issue':'Paid invoice has nonzero outstanding'})
        if r.payment_status == 'Refunded':
            counts['refunded']+=1
            if not rec.credit_note:
                anomalies.append({'enrollment':r.name,'issue':'Refunded membership missing credit note in accounting mapping'})
        for dt, field in [('Payment Entry','gateway_payment_entry'),('Journal Entry','wallet_journal_entry')]:
            name = rec.get(field)
            paid_value = money(rec.gateway_paid) if dt=='Payment Entry' else money(rec.wallet_paid)
            if paid_value > 0 and (not name or frappe.db.get_value(dt,name,'docstatus') != 1):
                anomalies.append({'enrollment':r.name,'issue':dt+' missing or not submitted'})
        counts['accounting_records']+=1
    cfg=_config()
    balances={}
    if cfg['company']:
        # ALL remaining ledger balances are shown, including expired balances.
        # Expiry releases need their own approved accounting policy.
        results=frappe.db.sql('''SELECT g.source_type, COALESCE(SUM(l.amount),0) AS remaining
            FROM `tabClub100 Wallet Grant` g
            LEFT JOIN `tabClub100 Wallet Ledger` l ON l.`grant`=g.name
            GROUP BY g.source_type''', as_dict=True)
        remaining={r.source_type:money(r.remaining) for r in results}
        for source in ('Corporate','Referral','Promotion'):
            account=cfg.get(source,'')
            expected=remaining.get(source,Decimal('0.00'))
            gl_credit=None
            if account and frappe.db.exists('Account',account):
                r=frappe.db.sql('''SELECT COALESCE(SUM(credit-debit),0) AS net
                    FROM `tabGL Entry` WHERE company=%s AND account=%s AND is_cancelled=0''',
                    (cfg['company'],account),as_dict=True)[0]
                gl_credit=money(r.net)
            balances[source]={'subledger_remaining':str(expected),
                'liability_account': account or None,
                'gl_account_credit_balance':str(gl_credit) if gl_credit is not None else None,
                'difference_vs_remaining':str(gl_credit-expected) if gl_credit is not None else None,
                'note':('Corporate GL can also contain unallocated corporate pool funding.' if source=='Corporate'
                        else 'Expired credits require a separate approved breakage policy.')}
    return {'records_reviewed':min(len(rows),limit),'truncated':len(rows)>limit,
        'counts':dict(counts),'anomalies':anomalies[:100],
        'anomaly_count_in_sample':len(anomalies), 'wallet_subledger_vs_gl':balances,
        'corporate_funding_receipt_reconciliation':'Not implemented: pool funding proof is not an ERPNext bank receipt',
        'refund_credit_note_reconciliation':'Not implemented: credit notes require tax and gateway refund processing',
        'erpnext_gl_reconciled':False, 'production_ready':False,
        'caution':'Read-only diagnostic, not accountant certification.'}


def configuration_candidates():
    """Small read-only list of existing ERPNext accounting objects for local setup."""
    _admin()
    return {
        'companies':frappe.get_all('Company', fields=['name','default_currency'],limit_page_length=20),
        'candidate_service_items':frappe.get_all('Item',filters={'is_stock_item':0,'disabled':0},
            fields=['name','item_name'],limit_page_length=30),
        'tax_templates':frappe.get_all('Sales Taxes and Charges Template',
            fields=['name','company','disabled'],limit_page_length=30),
        'customers':frappe.get_all('Customer',fields=['name','customer_name'],limit_page_length=20),
        'candidate_clearing_accounts':frappe.get_all('Account',filters={'is_group':0,'disabled':0},
            fields=['name','company','account_type'],limit_page_length=80),
        'note':'Choose existing records deliberately; this is not authorization to create GL postings.'}
