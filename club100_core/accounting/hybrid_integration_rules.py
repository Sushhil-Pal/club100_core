"""Club100 Stage 5F-D2C1: pure, NON-POSTING payment and invoice contracts.

These functions validate trusted SERVER-FETCHED Razorpay order/payment evidence
and a frozen D2B hybrid checkout snapshot. They DO NOT fetch data from Razorpay,
verify HTTP/HMAC signatures, capture wallets, issue invoices, or authorize any
checkout transition. Never supply raw browser payment payloads as evidence.

The statutory tax treatment and final ERPNext invoice construction require a
separate approved integration. This contract is a fail-closed prerequisite only.
"""
import hashlib
import json
from decimal import Decimal
from collections.abc import Mapping

from club100_core.accounting.promotion_rules import (
    build_hybrid_plan, rupees, verify_source_split,
)

PLAN_FIELDS = (
    'original_price_incl_gst', 'promotion_discount', 'invoice_total_incl_gst',
    'funded_wallet_tender', 'razorpay_tender', 'invoice_net_excl_gst',
    'invoice_gst', 'tax_rate_percent',
)


def _mapping(value, label):
    if not isinstance(value, Mapping):
        raise ValueError(label + ' must be a mapping')
    return value


def _identity(value, prefix, label):
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) <= len(prefix):
        raise ValueError(label + ' is missing or malformed')
    if any(ch.isspace() for ch in value):
        raise ValueError(label + ' contains whitespace')
    return value


def _str(value):
    return str(value or '').strip()


def _paise(value):
    return int(rupees(value) * 100)


def _exact_paise(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(label + ' must be a nonnegative integer paise amount')
    return value


def _allocations(items, kind):
    if not isinstance(items, list):
        raise ValueError(kind + ' allocation snapshot must be a list')
    result=[]
    for row in items:
        _mapping(row, kind + ' grant')
        if set(row) != {'grant', 'source', 'amount'}:
            raise ValueError('Unexpected keys in ' + kind + ' allocation snapshot')
        grant=_str(row['grant']); source=_str(row['source'])
        if not grant or not source:
            raise ValueError('Missing grant identity or source')
        amount=str(rupees(row['amount']))
        result.append({'grant':grant,'source':source,'amount':amount})
    return result


def validate_frozen_checkout(snapshot, promotion_lines, funded_lines):
    """Recompute totals from D2B DB snapshot and ORIGINAL source allocations.

    In D2C2 callers must hold member/enrollment/hybrid/reservation locks, and
    verify the reservation rows' identities before allowing a transaction.
    """
    _mapping(snapshot,'Hybrid checkout snapshot')
    enrollment=_str(snapshot.get('enrollment'))
    member=_str(snapshot.get('member'))
    policy=_str(snapshot.get('policy_version'))
    if not enrollment or not member or not policy:
        raise ValueError('Immutable enrollment/member/policy identity required')
    if _str(snapshot.get('status')) not in ('Reserved','Captured'):
        raise ValueError('Hybrid checkout status must be Reserved or Captured')
    p=_allocations(promotion_lines,'promotion')
    f=_allocations(funded_lines,'funded')
    discount=rupees(snapshot.get('promotion_discount','0'))
    funded=rupees(snapshot.get('funded_wallet_tender','0'))
    gross=rupees(snapshot.get('original_price'))
    gateway=rupees(snapshot.get('gateway_due','0'))
    verify_source_split({'promotion':p,'funded':f},discount,funded)
    if bool(p) != bool(discount) or bool(f) != bool(funded):
        raise ValueError('Reservation legs disagree with their amounts')
    if (bool(_str(snapshot.get('promotion_reservation_key'))) != bool(discount)
            or bool(_str(snapshot.get('funded_reservation_key'))) != bool(funded)):
        raise ValueError('Hybrid reservation key/amount mismatch')
    if discount == 0 and funded == 0:
        raise ValueError('No wallet or discount leg: use legacy checkout')
    calculated=build_hybrid_plan(gross,discount,funded,gateway,
                                  _str(snapshot.get('gst_percent')))
    stored=snapshot.get('plan_snapshot_json')
    if isinstance(stored,str):
        try:stored=json.loads(stored)
        except (ValueError,TypeError) as exc:
            raise ValueError('Invalid frozen plan snapshot JSON') from exc
    _mapping(stored,'Frozen plan')
    # Compare complete canonical plan, not a selected subset that could be stale.
    if dict(stored)!=calculated:
        raise ValueError('Frozen pricing snapshot differs from server recomputation')
    immutable={
        'enrollment':enrollment,'member':member,'policy_version':policy,
        'original_price':str(gross),'promotion_discount':str(discount),
        'funded_wallet_tender':str(funded),'gateway_due':str(gateway),
        'gst_percent':_str(snapshot['gst_percent']),
        'promotion_reservation_key':_str(snapshot.get('promotion_reservation_key')),
        'funded_reservation_key':_str(snapshot.get('funded_reservation_key')),
        'promotion_allocations':p,'funded_allocations':f,
        'plan':calculated,
    }
    canonical=json.dumps(immutable,sort_keys=True,separators=(',',':'))
    return {
       'snapshot_sha256':hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
       'enrollment':enrollment,'member':member,
       'expected_gateway_paise':_paise(gateway),
       'invoice_total':calculated['invoice_total_incl_gst'],
       'invoice_net_projection':calculated['invoice_net_excl_gst'],
       'invoice_gst_projection':calculated['invoice_gst'],
       'original_price':str(gross),
       'discount':str(discount),'funded_wallet':str(funded),
       'plan':calculated,'posting_authorized':False,
    }


def verify_razorpay_evidence(preflight, stored_order_id, order, payment):
    """Validate order and payment data AFTER trusted server fetch/authentication.

    Neither this function nor a caller-supplied dictionary is proof of payment.
    A later integration must validate callback signature, fetch Razorpay-owned
    records using server credentials, lock DB rows, and compare this contract.
    """
    _mapping(preflight,'Validated preflight')
    if preflight.get('posting_authorized') is not False:
        raise ValueError('Not a non-posting preflight')
    due=_exact_paise(preflight.get('expected_gateway_paise'), 'Expected gateway due')
    if due == 0:
        if order is not None or payment is not None or stored_order_id:
            raise ValueError('Wallet-only hybrid checkout must not have Razorpay evidence')
        return {'mode':'wallet_only','amount_paise':0,'payment_id':None,'order_id':None,
                'validated_fields_only':True,'posting_authorized':False}
    order_id=_identity(stored_order_id,'order_','Stored Razorpay order ID')
    o=_mapping(order,'Razorpay order'); p=_mapping(payment,'Razorpay payment')
    if _str(o.get('id')) != order_id or _str(p.get('order_id')) != order_id:
        raise ValueError('Order ID mismatch')
    payment_id=_identity(p.get('id'),'pay_','Razorpay payment ID')
    if _str(p.get('status')).lower() != 'captured':
        raise ValueError('Razorpay payment is not captured')
    if _str(o.get('currency')).upper() != 'INR' or _str(p.get('currency')).upper() != 'INR':
        raise ValueError('Razorpay currency must be INR')
    if _exact_paise(o.get('amount'),'Razorpay order amount') != due or _exact_paise(p.get('amount'),'Razorpay payment amount') != due:
        raise ValueError('Razorpay amount differs from frozen hybrid gateway amount')
    if _str(o.get('status')).lower() != 'paid':
        raise ValueError('Razorpay order is not fully paid')
    if _exact_paise(o.get('amount_paid'),'Razorpay order paid amount') != due:
        raise ValueError('Razorpay order paid amount is not the frozen gateway amount')
    if _exact_paise(o.get('amount_due'),'Razorpay order outstanding amount') != 0:
        raise ValueError('Razorpay order still has an outstanding balance')
    if _str(o.get('receipt')) != preflight.get('enrollment'):
        raise ValueError('Razorpay receipt is not this enrollment')
    notes=o.get('notes'); _mapping(notes,'Razorpay order notes')
    if _str(notes.get('club100_enrollment')) != preflight.get('enrollment'):
        raise ValueError('Razorpay order enrollment note does not match')
    if _exact_paise(p.get('amount_refunded',0),'Already refunded amount') != 0:
        raise ValueError('Razorpay payment has already been refunded, request review')
    return {'mode':'razorpay','order_id':order_id,'payment_id':payment_id,
            'amount_paise':due,'validated_fields_only':True,'posting_authorized':False}


def invoice_preflight(preflight, invoice_projection):
    """Validate proposed line/tax/tender PROJECTION, not a submitted invoice.

    Projection must be derived server-side. This does not certify GST scheme,
    place-of-supply, taxpayer identity or ERPNext tax template/roundoff behavior.
    """
    _mapping(preflight,'Validated preflight')
    _mapping(invoice_projection,'Invoice projection')
    if invoice_projection.get('enrollment')!=preflight.get('enrollment'):
        raise ValueError('Invoice enrollment mismatch')
    if invoice_projection.get('currency')!='INR':
        raise ValueError('Invoice currency mismatch')
    plan=preflight['plan']
    for field, expected in (
       ('original_price_incl_gst',plan['original_price_incl_gst']),
       ('supplier_discount_incl_gst',plan['promotion_discount']),
       ('invoice_total_incl_gst',plan['invoice_total_incl_gst']),
       ('funded_wallet_tender',plan['funded_wallet_tender']),
       ('razorpay_tender',plan['razorpay_tender']),
       ('projected_net_excl_gst',plan['invoice_net_excl_gst']),
       ('projected_gst',plan['invoice_gst']),
    ):
       if rupees(invoice_projection.get(field))!=rupees(expected):
           raise ValueError('Invoice projection mismatch for '+field)
    if invoice_projection.get('promotion_accounted_as')!='supplier_discount_before_tax':
        raise ValueError('Promotion must never be added as a payment tender')
    if invoice_projection.get('funded_accounted_as')!='invoice_payment':
        raise ValueError('Corporate/Referral must be invoice payment sources')
    return {'preflight_only':True,'gross':plan['invoice_total_incl_gst'],
            'gst_projection':plan['invoice_gst'], 'posting_authorized':False,
            'tax_scheme_approval_required':True}


def example_projection(preflight):
    """Return a deliberately non-posting example from immutable plan fields."""
    plan=preflight['plan']
    return {'enrollment':preflight['enrollment'],'currency':'INR',
      'original_price_incl_gst':plan['original_price_incl_gst'],
      'supplier_discount_incl_gst':plan['promotion_discount'],
      'invoice_total_incl_gst':plan['invoice_total_incl_gst'],
      'funded_wallet_tender':plan['funded_wallet_tender'],
      'razorpay_tender':plan['razorpay_tender'],
      'projected_net_excl_gst':plan['invoice_net_excl_gst'],
      'projected_gst':plan['invoice_gst'],
      'promotion_accounted_as':'supplier_discount_before_tax',
      'funded_accounted_as':'invoice_payment'}
