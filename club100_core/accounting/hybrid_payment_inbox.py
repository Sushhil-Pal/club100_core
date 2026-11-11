"""Stage 5F-D2C2C: private, opt-in, TEST-ONLY payment evidence inbox.

Payment HMAC + independent Razorpay GET -> persistent evidence for future invoice
or manual review. Never captures wallet, creates/submits invoices, activates
membership, refunds or posts GL. Not whitelisted, no current callback hook.
"""
import os
from uuid import uuid4

import frappe
from frappe.utils import cint, get_datetime, now_datetime
from club100_core.api.enrollment import _get_razorpay_config, _get_razorpay_webhook_secret
from club100_core.accounting.hybrid_reservations import _get_locked, _pair, _eligible_promotion_grants
from club100_core.accounting.hybrid_gateway_auth import (
    browser_signature_matches, parse_signed_webhook, assert_webhook_matches_fetched)
from club100_core.accounting.hybrid_gateway_probe_local import _server_fetch
from club100_core.accounting.hybrid_integration_rules import (
    validate_frozen_checkout, verify_razorpay_evidence, invoice_preflight, example_projection)
from club100_core.accounting.hybrid_evidence_rules import (
    require_provider_identity, decide_review, identity_digest, replay_equal)
from club100_core.accounting.promotion_rules import rupees

DT = 'Club100 Hybrid Payment Evidence'


def _admin():
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required', frappe.PermissionError)


def _guard():
    _admin()
    if (frappe.local.site != 'club100.local'
        or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test'):
        raise ValueError('Payment-evidence tools require local Razorpay Test Mode')


def _gateway_guard():
    if (frappe.local.site != 'club100.local'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test'):
        raise ValueError('Independent Razorpay GET requires local Test Mode')


def _identity(enrollment_id):
    """Read-only first lookup to validate signature before provider GET/DB locks."""
    name = str(enrollment_id or '').strip()
    if not name or len(name)>140:
        raise ValueError('Valid enrollment required')
    e = frappe.db.get_value('Club100 Enrollment', name,
          ['name','payment_order_id','member'], as_dict=True)
    if not e or not e.member or not isinstance(e.payment_order_id,str) or not e.payment_order_id.startswith('order_'):
        raise ValueError('Stored hybrid Razorpay order identity required')
    h = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment':name},
            ['name','member'], as_dict=True)
    if not h or h.member != e.member:
        raise ValueError('Hybrid reservation belongs to different member or is missing')
    return e


def _frozen_preflight(enrollment, hybrid):
    if hybrid.member != enrollment.member or hybrid.enrollment != enrollment.name:
        raise ValueError('Hybrid/member/enrollment identity mismatch')
    if (enrollment.website_source != 'PWA Commerce' or enrollment.pricing_model != 'Online Plan'
        or cint(enrollment.is_trial) or rupees(enrollment.amount_payable) != rupees(hybrid.original_price)):
        raise ValueError('Enrollment price/provenance differs from immutable hybrid')
    pairs = _pair(hybrid, ('Reserved','Released','Expired','Captured','Refunded'))
    fields = ('enrollment','member','status','policy_version','original_price',
              'promotion_discount','funded_wallet_tender','gateway_due','gst_percent',
              'promotion_reservation_key','funded_reservation_key','plan_snapshot_json')
    snapshot = {key:hybrid.get(key) for key in fields}
    # D2C1 validates a PRICE snapshot only in Reserved/Captured states. An
    # already-released or review record is never approved for settlement, but
    # we still recompute its ORIGINAL price to retain verified paid evidence.
    # Actual current state is separately evaluated below and forces review.
    if hybrid.status in ('Released','Refunded','Review'):
        snapshot['status'] = 'Reserved'
    pre = validate_frozen_checkout(snapshot, pairs['promotion'], pairs['funded'])
    invoice_preflight(pre, example_projection(pre))
    if pre['expected_gateway_paise'] <= 0:
        raise ValueError('Wallet-only checkout must never receive Razorpay payment')
    return pre, pairs


def _persist(enrollment_id, order, payment, origin, signed=None):
    """All writes performed only after signature validation and provider GET.

    Lock order mirrors D2B. Savepoint prevents partial evidence on failures.
    Never authorizes settlement or changes enrollment/hybrid/reservation states.
    """
    point = 'd2c2c_' + uuid4().hex[:16]
    frappe.db.savepoint(point)
    old = getattr(frappe.flags, 'club100_hybrid_evidence_write', False)
    try:
        enrollment, hybrid = _get_locked(enrollment_id)
        stored = str(enrollment.payment_order_id or '').strip()
        if (not stored or stored != order.get('id') or payment.get('order_id') != stored):
            raise ValueError('Razorpay identities changed during verification')
        if origin == 'Webhook':
            assert_webhook_matches_fetched(signed,payment)
        pre, pairs = _frozen_preflight(enrollment, hybrid)
        proof = verify_razorpay_evidence(pre, stored, order, payment)
        identity = require_provider_identity(pre,hybrid.name,stored,order,payment,proof)
        # One enrollment/payment/order must map to exactly one immutable inbox row.
        names = set()
        for filters in ({'enrollment':enrollment.name},
                        {'razorpay_order_id':stored},
                        {'razorpay_payment_id':payment['id']}):
            value=frappe.db.get_value(DT,filters,'name')
            if value:names.add(value)
        if len(names)>1:
            raise ValueError('Conflicting payment evidence identities: manual investigation required')
        if names:
            existing=frappe.get_doc(DT,next(iter(names)))
            row = {key:existing.get(key) for key in ('enrollment','hybrid_checkout','member',
                'razorpay_order_id','razorpay_payment_id','amount_paise','snapshot_sha256',
                'identity_sha256','state')}
            replay_equal(row,identity)
            return _result(existing,True)
        approved=set(_eligible_promotion_grants(hybrid.member,hybrid.policy_version)) if pairs['promotion'] else set()
        currently_approved = (not rupees(hybrid.promotion_discount or 0) or
                              all(row['grant'] in approved for row in pairs['promotion']))
        holds_reserved=True
        holds_unexpired=get_datetime(hybrid.expires_at)>now_datetime()
        for key in (hybrid.promotion_reservation_key,hybrid.funded_reservation_key):
            if not key:continue
            row=frappe.db.get_value('Club100 Wallet Reservation',{'reservation_key':key},
                ['status','expires_at'],as_dict=True)
            if not row or row.status!='Reserved':holds_reserved=False
            if not row or get_datetime(row.expires_at)<=now_datetime():holds_unexpired=False
        state=decide_review(enrollment.payment_status=='Pending' and enrollment.status=='Registered'
                 and not enrollment.payment_id and not enrollment.sales_invoice,
                 hybrid.status=='Reserved', holds_reserved, holds_unexpired,
                 currently_approved)
        payload={'doctype':DT, **identity, 'identity_sha256':identity_digest(identity),
                 'origin':origin,'provider_status':'captured',
                 'state':state['state'],'review_reason':state['review_reason'],
                 'verified_on':now_datetime(),'evidence_version':'D2C2C-v1'}
        frappe.flags.club100_hybrid_evidence_write=True
        record=frappe.get_doc(payload).insert(ignore_permissions=True)
        return _result(record,False)
    except Exception:
        frappe.db.rollback(save_point=point)
        raise
    finally:
        frappe.flags.club100_hybrid_evidence_write=old


def _result(record,replayed):
    return {'outcome':'EVIDENCE_REPLAY' if replayed else 'EVIDENCE_STORED',
        'site':frappe.local.site,'record':record.name,'enrollment':record.enrollment,
        'state':record.state,'review_reason':record.review_reason or '',
        'payment_id':record.razorpay_payment_id,'amount_paise':record.amount_paise,
        'idempotent_replay':replayed,'wallet_captured':False,
        'membership_activated':False,'invoice_or_gl_posted':False,
        'posting_authorized':False,'production_ready':False}


def ingest_browser(enrollment_id, payment_id, checkout_signature):
    """INTERNAL Administrator CLI only. This is not a browser endpoint."""
    _guard()
    e=_identity(enrollment_id)
    config=_get_razorpay_config()
    if config.get('mode')!='test' or not str(config.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Razorpay Test Mode credentials required')
    browser_signature_matches(e.payment_order_id,payment_id,checkout_signature,config['key_secret'])
    _gateway_guard()
    order,payment=_server_fetch(config,e.payment_order_id,payment_id)
    return _persist(e.name,order,payment,'Browser')


def ingest_signed_webhook(enrollment_id, raw_body_utf8, webhook_signature):
    """INTERNAL CLI only; future HTTP endpoint MUST pass actual raw bytes.

    This stage does not install or change any public webhook or browser route.
    """
    _guard()
    if not isinstance(raw_body_utf8,str):
        raise ValueError('CLI payload must be UTF-8 string; do not JSON re-encode webhooks')
    signed=parse_signed_webhook(raw_body_utf8.encode('utf-8'),webhook_signature,
                                 _get_razorpay_webhook_secret())
    e=_identity(enrollment_id)
    if signed['order_id']!=e.payment_order_id:
        raise ValueError('Signed webhook not for stored order')
    _gateway_guard()
    config=_get_razorpay_config()
    if config.get('mode')!='test' or not str(config.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Razorpay Test Mode credentials required')
    order,payment=_server_fetch(config,e.payment_order_id,signed['payment_id'])
    return _persist(e.name,order,payment,'Webhook',signed=signed)


def healthcheck():
    _admin()
    return {'site':frappe.local.site,'stage':'5F-D2C2C',
            'evidence_doctype_installed':frappe.db.exists('DocType',DT) is not None,
            'local_inbox_enabled':frappe.local.site == 'club100.local' and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test',
            'provider_get_requires_signature_and_test_mode':True,
            'public_callbacks_wired':True,'wallet_capture_enabled':False,
            'membership_activation_enabled':False,'invoice_or_gl_posting_enabled':False,
            'production_ready':False}
