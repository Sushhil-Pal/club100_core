"""Pure full-refund eligibility and Razorpay evidence rules; no network or Frappe."""
from decimal import Decimal


def money(value):
    d = Decimal(str(value or 0))
    if not d.is_finite() or d.quantize(Decimal('0.01')) != d:
        raise ValueError('Money must have at most 2 decimal places')
    return d.quantize(Decimal('0.01'))


def refund_kind(payment_status, settlement, gateway_due, captured_verified):
    gateway = money(gateway_due)
    if payment_status == 'Paid' and settlement == 'Settled':
        if gateway > 0 and not captured_verified:
            raise ValueError('Razorpay payment capture has not been verified')
        return 'Paid'
    if (payment_status == 'Pending' and settlement == 'Review'
            and gateway > 0 and captured_verified):
        return 'Captured Review'
    raise ValueError('Enrollment is not a supported full-refund candidate')


def check_refund(refund, payment_id, amount_paise, expected_id=None):
    """Reject ambiguous, partial, failed and unrelated gateway refunds."""
    if not isinstance(refund, dict):
        raise ValueError('Invalid Razorpay refund response')
    rid = str(refund.get('id') or '')
    if not rid.startswith('rfnd_') or (expected_id and rid != expected_id):
        raise ValueError('Razorpay refund ID differs')
    if str(refund.get('payment_id') or '') != payment_id:
        raise ValueError('Refund belongs to a different Razorpay payment')
    if refund.get('currency') != 'INR' or type(refund.get('amount')) is not int or refund['amount'] != amount_paise:
        raise ValueError('Refund amount/currency differs from full gateway amount')
    status = str(refund.get('status') or '').lower()
    if status not in ('pending', 'processed', 'failed'):
        raise ValueError('Unknown gateway refund status')
    return status
