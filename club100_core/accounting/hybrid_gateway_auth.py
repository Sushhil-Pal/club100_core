"""Pure authentication and provider-response checks for Club100 hybrid checkout.

NO network, Frappe, SQL, wallet or accounting operations. A valid signature
alone does not prove capture; always independently GET Razorpay order/payment.
"""
import hashlib
import hmac
import json
from collections.abc import Mapping


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(label + ' must be a non-empty trimmed string')
    return value


def _signature(value):
    value = _text(value, 'Signature')
    if len(value) != 64 or any(ch not in '0123456789abcdefABCDEF' for ch in value):
        raise ValueError('Signature must be 64 hexadecimal characters')
    return value.lower()


def _secret(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Server-side Razorpay secret missing')
    return value


def browser_signature_matches(stored_order_id, payment_id, signature, key_secret):
    """Checkout HMAC over ERP-STORED order ID and payment ID, not browser order."""
    stored_order_id = _text(stored_order_id, 'Stored order ID')
    payment_id = _text(payment_id, 'Payment ID')
    if not stored_order_id.startswith('order_') or not payment_id.startswith('pay_'):
        raise ValueError('Invalid Razorpay order or payment ID')
    expected = hmac.new(_secret(key_secret).encode('utf-8'),
                        (stored_order_id + '|' + payment_id).encode('utf-8'),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, _signature(signature)):
        raise ValueError('Checkout payment signature invalid')
    return True


def parse_signed_webhook(raw_body, signature, webhook_secret):
    """Require HMAC of EXACT raw bytes. Event fields are still untrusted.

    Always independently fetch provider order/payment and reconcile immutable
    D2B snapshot in the caller, even for a successfully authenticated webhook.
    """
    if not isinstance(raw_body, bytes) or not raw_body:
        raise ValueError('Raw webhook payload bytes required')
    expected = hmac.new(_secret(webhook_secret).encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, _signature(signature)):
        raise ValueError('Webhook signature invalid')
    try:
        value = json.loads(raw_body.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('Malformed webhook JSON') from exc
    if not isinstance(value, Mapping) or value.get('event') != 'payment.captured':
        raise ValueError('Only payment.captured webhook supported in this contract')
    payment = (value.get('payload') or {}).get('payment') if isinstance(value.get('payload'), Mapping) else None
    payment = payment.get('entity') if isinstance(payment, Mapping) else None
    if not isinstance(payment, Mapping):
        raise ValueError('Webhook payment entity missing')
    payment_id = _text(payment.get('id'), 'Webhook payment ID')
    order_id = _text(payment.get('order_id'), 'Webhook order ID')
    if not payment_id.startswith('pay_') or not order_id.startswith('order_'):
        raise ValueError('Malformed Razorpay IDs in webhook')
    if payment.get('status') != 'captured':
        raise ValueError('Webhook does not confirm captured payment')
    return {'payment_id':payment_id,'order_id':order_id,'signed_payload_only':True,
            'posting_authorized':False}


def assert_webhook_matches_fetched(signed, payment):
    if not isinstance(signed, Mapping) or not isinstance(payment, Mapping):
        raise ValueError('Signed webhook and fetched payment required')
    if signed.get('signed_payload_only') is not True or signed.get('posting_authorized') is not False:
        raise ValueError('Not an authenticated non-posting webhook preflight')
    if signed['payment_id'] != payment.get('id') or signed['order_id'] != payment.get('order_id'):
        raise ValueError('Provider evidence differs from signed webhook identity')
    return True
