"""Stage 5F-D2A read-only Frappe smoke. No mutation or network calls."""
import frappe
from club100_core.accounting.promotion_rules import build_hybrid_plan
from club100_core.accounting.promotion_preview import healthcheck, preview_hybrid


def run(member_id=None):
    if frappe.local.site != 'club100.local':
        raise ValueError('Local UAT site required')
    baseline = frappe.db.count('Club100 Wallet Ledger')
    # These three numbers are *illustrative* pricing, not a claimed tax invoice.
    example = build_hybrid_plan('799.00', '100.00', '300.00', '399.00')
    assert example['invoice_total_incl_gst']=='699.00'
    assert example['invoice_gst']=='33.29'
    assert example['illustrative_gst_difference']=='4.76'
    status = healthcheck()
    member_preview = None
    if member_id:
        member_preview = preview_hybrid(member_id, '799.00', '0.00', '0.00')
    assert frappe.db.count('Club100 Wallet Ledger')==baseline
    return {'outcome':'PASS', 'example':example,'guard':status,
            'member_preview':member_preview, 'wallet_records_unchanged':True,
            'read_only':True, 'no_checkout_or_invoice_posting':True}
