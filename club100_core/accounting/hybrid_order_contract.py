"""Pure order-creation contract for a verified, immutable hybrid snapshot.

Razorpay order creation must charge the discounted gateway_due ONLY.
This module never calls Frappe, HTTP, wallet, invoice, or GL services.
"""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


def _text(v, label):
    if not isinstance(v, str) or not v or v.strip() != v:
        raise ValueError(label + ' must be nonempty, trimmed text')
    return v


def _paise(v, label):
    if type(v) is not int or v <= 0:
        raise ValueError(label + ' must be a strictly positive integer paise amount')
    return v


def build_order_payload(preflight, hybrid_name, program='', plan='', months=''):
    if not isinstance(preflight, Mapping) or preflight.get('posting_authorized') is not False:
        raise ValueError('Non-posting, server-validated hybrid snapshot required')
    receipt = _text(preflight.get('enrollment'), 'Enrollment')
    sha = _text(preflight.get('snapshot_sha256'), 'Snapshot hash')
    if len(sha) != 64 or any(x not in '0123456789abcdef' for x in sha):
        raise ValueError('Malformed immutable snapshot hash')
    hybrid = _text(hybrid_name, 'Hybrid checkout')
    amount = _paise(preflight.get('expected_gateway_paise'), 'Discounted gateway due')
    # Reconcile with the ORIGINAL immutable plan: no undercharge or overcharge.
    try:
        plan_due=Decimal(str(preflight['plan']['razorpay_tender']))*100
        invoice=Decimal(str(preflight['plan']['invoice_total_incl_gst']))*100
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError('Frozen plan has invalid Razorpay or invoice amount') from exc
    if plan_due!=amount or invoice < amount:
        raise ValueError('Gateway due does not exactly match frozen hybrid plan')
    return {'amount': amount, 'currency': 'INR', 'receipt': receipt,
            'notes': {'club100_enrollment': receipt, 'club100_hybrid': hybrid,
                      'club100_snapshot': sha, 'program': str(program or '')[:180],
                      'plan': str(plan or '')[:180],
                      'duration_months': str(months or '')[:32]}}


def validate_order_response(payload, order, reuse=False):
    if not isinstance(payload, Mapping) or not isinstance(order, Mapping):
        raise ValueError('Expected payload and provider response mappings')
    identifier = order.get('id')
    if not isinstance(identifier, str) or not identifier.startswith('order_') or len(identifier)>128:
        raise ValueError('Invalid returned order ID')
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for c in identifier):
        raise ValueError('Invalid returned order characters')
    if order.get('amount') != payload.get('amount') or type(order.get('amount')) is not int:
        raise ValueError('Razorpay order amount mismatch')
    if order.get('currency') != 'INR' or order.get('receipt') != payload.get('receipt'):
        raise ValueError('Razorpay order currency or receipt mismatch')
    notes=order.get('notes')
    if not isinstance(notes, Mapping):
        raise ValueError('Razorpay order notes missing')
    for key in ('club100_enrollment','club100_hybrid','club100_snapshot'):
        if notes.get(key)!=payload['notes'][key]:
            raise ValueError('Razorpay order notes mismatch: '+key)
    if type(order.get('amount_paid')) is not int or order['amount_paid']!=0:
        raise ValueError('Already-paid order cannot be used for new checkout')
    if type(order.get('amount_due')) is not int or order['amount_due']!=payload['amount']:
        raise ValueError('Outstanding amount differs from frozen checkout')
    if order.get('status') not in (('created','attempted') if reuse else ('created',)):
        raise ValueError('Order is not in the expected unpaid status')
    return {'order_id':identifier,'amount':payload['amount'],'currency':'INR',
            'validated_for_order_storage':True, 'posting_authorized':False}
