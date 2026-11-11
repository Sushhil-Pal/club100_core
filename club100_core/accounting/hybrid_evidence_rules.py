"""Pure evidence/replay contract. No database, gateway, wallet, or GL operations."""
import hashlib
import json
from collections.abc import Mapping

STATE_PENDING = 'Verified Pending Invoice'
STATE_REVIEW = 'Manual Review'


def require_provider_identity(preflight, hybrid_id, stored_order_id, order, payment, proof):
    """Require authenticated server-fetched proof plus full D2C2B provenance."""
    if not isinstance(preflight, Mapping) or preflight.get('posting_authorized') is not False:
        raise ValueError('Server-validated non-posting preflight required')
    if not isinstance(order, Mapping) or not isinstance(payment, Mapping):
        raise ValueError('Server-fetched order and payment required')
    if not isinstance(proof, Mapping) or proof.get('validated_fields_only') is not True or proof.get('posting_authorized') is not False:
        raise ValueError('D2C1 provider field validation required')
    if proof.get('mode') != 'razorpay' or proof.get('amount_paise') != preflight.get('expected_gateway_paise'):
        raise ValueError('Gateway amount or proof mode mismatch')
    if proof.get('order_id') != stored_order_id or proof.get('payment_id') != payment.get('id'):
        raise ValueError('Verified provider order/payment does not match stored identity')
    notes = order.get('notes')
    if not isinstance(notes, Mapping):
        raise ValueError('Razorpay order notes missing')
    expected = {'club100_enrollment':preflight['enrollment'],
                'club100_hybrid':hybrid_id,
                'club100_snapshot':preflight['snapshot_sha256']}
    for key, value in expected.items():
        if notes.get(key) != value:
            raise ValueError('Provider provenance mismatch: ' + key)
    return {'enrollment':preflight['enrollment'], 'hybrid_checkout':hybrid_id,
            'member':preflight['member'], 'razorpay_order_id':stored_order_id,
            'razorpay_payment_id':proof['payment_id'],
            'amount_paise':proof['amount_paise'],
            'snapshot_sha256':preflight['snapshot_sha256']}


def decide_review(enrollment_pending, hybrid_reserved, reservations_reserved,
                  holds_unexpired, tax_approval_current):
    reasons = []
    if not enrollment_pending: reasons.append('Enrollment no longer pending and unfinalized')
    if not hybrid_reserved: reasons.append('Hybrid checkout is not Reserved')
    if not reservations_reserved: reasons.append('Wallet hold has changed state')
    if not holds_unexpired: reasons.append('Reservation expired after gateway capture')
    if not tax_approval_current: reasons.append('Promotional GST approval not currently valid')
    return {'state':STATE_REVIEW if reasons else STATE_PENDING,
            'review_reason':'; '.join(reasons)}


def identity_digest(fields):
    allow=('enrollment','hybrid_checkout','member','razorpay_order_id',
           'razorpay_payment_id','amount_paise','snapshot_sha256')
    if any(x not in fields for x in allow):
        raise ValueError('Incomplete canonical evidence identity')
    return hashlib.sha256(json.dumps({k:fields[k] for k in allow},
                        sort_keys=True,separators=(',',':')).encode()).hexdigest()


def replay_equal(existing, identity):
    for key in ('enrollment','hybrid_checkout','member','razorpay_order_id',
                'razorpay_payment_id','snapshot_sha256'):
        if existing.get(key) != identity.get(key):
            raise ValueError('Conflicting payment evidence replay: ' + key)
    if type(existing.get('amount_paise')) is not int or existing['amount_paise'] != identity.get('amount_paise'):
        raise ValueError('Conflicting payment evidence amount')
    if existing.get('identity_sha256') != identity_digest(identity):
        raise ValueError('Immutable payment evidence digest mismatch')
    if existing.get('state') not in (STATE_PENDING, STATE_REVIEW, 'Settled'):
        raise ValueError('Unrecognized payment evidence state')
    return True
