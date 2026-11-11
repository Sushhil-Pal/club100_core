"""Internal local-only Stage D2C1 read-only hybrid checkout preflight.

No whitelisting, gateway HTTP calls, SQL writes, wallet capture or invoice creation.
Any later live integration MUST re-evaluate all checks under DB row locks.
"""
import json

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from club100_core.accounting.hybrid_integration_rules import (
    validate_frozen_checkout, example_projection, invoice_preflight,
)
from club100_core.accounting.hybrid_reservations import _pair
from club100_core.accounting.promotion_rules import rupees


def _admin_local():
    if frappe.local.site != 'club100.local' or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test':
        raise ValueError('D2C1 preflight is restricted to club100.local Razorpay Test Mode')
    user=frappe.session.user
    if user!='Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required',frappe.PermissionError)


def inspect_reserved(enrollment_id):
    """Inspect a D2B hold without locking it or authorizing any state change.

    A successful preview NEVER implies Razorpay verification or GST approval.
    """
    _admin_local()
    enrollment=str(enrollment_id or '').strip()
    if not enrollment:
        raise ValueError('Enrollment required')
    e=frappe.get_doc('Club100 Enrollment',enrollment)
    if e.payment_status!='Pending' or e.status!='Registered' or e.payment_order_id or e.payment_id:
        raise ValueError('Expected new uncharged hybrid enrollment without gateway order')
    if (e.website_source!='PWA Commerce' or e.pricing_model!='Online Plan'
            or cint(e.is_trial) or not e.member):
        raise ValueError('Only nontrial PWA Commerce enrollment is eligible')
    name=frappe.db.get_value('Club100 Hybrid Checkout',{'enrollment':enrollment},'name')
    if not name:
        raise ValueError('No D2B hybrid reservation for this enrollment')
    record=frappe.get_doc('Club100 Hybrid Checkout',name)
    if record.status!='Reserved' or record.member!=e.member:
        raise ValueError('Hybrid reservation state/member does not match')
    if get_datetime(record.expires_at)<=now_datetime():
        raise ValueError('Hybrid reservation expired')
    if rupees(record.original_price)!=rupees(e.amount_payable):
        raise ValueError('Server-priced enrollment changed after reservation')
    rows=_pair(record,('Reserved',))
    snapshot={key:record.get(key) for key in (
        'enrollment','member','status','policy_version','original_price',
        'promotion_discount','funded_wallet_tender','gateway_due',
        'gst_percent','promotion_reservation_key','funded_reservation_key',
        'plan_snapshot_json')}
    pre=validate_frozen_checkout(snapshot,rows['promotion'],rows['funded'])
    example=example_projection(pre)
    invoice_preflight(pre,example)
    return {
       'site':frappe.local.site,'enrollment':enrollment,'member':e.member,
       'status':'PREVIEW_ONLY', 'hybrid_record':record.name,
       'snapshot_sha256':pre['snapshot_sha256'],
       'razorpay_order_expected_paise':pre['expected_gateway_paise'],
       'invoice_example':example,
       'blockers':['No verified Razorpay Order/Payment evidence',
                   'No integration with existing checkout or webhook',
                   'Tax/legal approval and full invoice construction outstanding',
                   'No live wallet capture/refund or GL posting'],
       'read_only':True,'posting_authorized':False,
       'warning':'Snapshot can change concurrently; revalidate under DB locks in D2C2',
    }


def healthcheck():
    _admin_local()
    return {'site':frappe.local.site,'stage':'5F-D2C1',
            'order_contract_installed':True,'payment_contract_installed':True,
            'invoice_projection_contract_installed':True,
            'browser_and_webhook_hooks_changed':False,
            'razorpay_called':False,'new_invoice_posting_enabled':False,
            'hybrid_checkout_enabled':False,'production_ready':False,
            'read_only':True}
