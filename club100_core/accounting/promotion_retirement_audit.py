"""Read-only retirement readiness and archive check for obsolete promotion approvals."""
import frappe

DT = 'Club100 Promotion Discount Approval'

def report():
    if frappe.local.site != 'club100.local':
        raise ValueError('This audit is limited to club100.local')
    exists = bool(frappe.db.exists('DocType', DT))
    count = frappe.db.count(DT) if exists else 0
    fields = frappe.get_all('DocField',filters={'options':DT},fields=['parent','fieldname'])
    custom = frappe.get_all('Custom Field',filters={'options':DT},fields=['dt','fieldname'])
    fields = [dict(x) for x in fields if x.parent != DT]
    return {'site':frappe.local.site,'doctype_exists':exists,'approval_records':count,
            'link_fields':fields,'custom_link_fields':[dict(x) for x in custom],
            'safe_to_retire':count == 0 and not fields and not custom,
            'read_only':True,
            'note':'No member grants, GL, invoices or ledger records are changed.'}
