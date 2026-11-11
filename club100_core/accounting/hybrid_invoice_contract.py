"""D2C3A: PURE, non-posting hybrid invoice/tender validation.

This is a financial preflight, NOT a GST ruling, a submitted ERPNext invoice,
proof of bank settlement, or authorization to mark an enrollment Paid.
Do not accept this function's inputs directly from a browser or webhook.
"""
import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_PAISE = Decimal('0.01')
_HASH = re.compile(r'^[0-9a-f]{64}$')


def money(value):
    if type(value) is bool or isinstance(value, float):
        raise ValueError('Money must be a decimal string, int or Decimal')
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Invalid money value') from None
    if not d.is_finite() or d < 0 or d.quantize(_PAISE) != d:
        raise ValueError('Amount must be non-negative, finite and in whole paise')
    return d.quantize(_PAISE)


def cents(value):
    return int(money(value) * 100)


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise ValueError(name + ' must be a mapping')
    return value


def _hash(value, name):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(name + ' must be a lowercase SHA256 digest')
    return value


def _allocations(promotion, funded):
    seen = set()
    result = {'promotion': Decimal('0.00'), 'funded': Decimal('0.00')}
    sources = {'Corporate': Decimal('0.00'), 'Referral': Decimal('0.00')}
    for kind, items, permitted in (
        ('promotion', promotion, {'Promotion'}),
        ('funded', funded, {'Corporate', 'Referral'}),
    ):
        if not isinstance(items, list):
            raise ValueError(kind + ' allocations must be a list')
        for item in items:
            _mapping(item, kind + ' allocation')
            if set(item) != {'grant', 'source', 'amount'}:
                raise ValueError('Unexpected allocation fields')
            grant, source = item['grant'], item['source']
            if not isinstance(grant, str) or not grant.strip() or source not in permitted:
                raise ValueError('Invalid grant or cross-source wallet value')
            if grant in seen:
                raise ValueError('A grant cannot be allocated twice across discount/tender')
            seen.add(grant)
            amount = money(item['amount'])
            if amount <= 0:
                raise ValueError('Zero allocation line is invalid')
            result[kind] += amount
            if kind == 'funded':
                sources[source] += amount
    return result, sources


def _evidence_identity(evidence):
    identity_keys = ('enrollment', 'hybrid_checkout', 'member',
                     'razorpay_order_id', 'razorpay_payment_id',
                     'amount_paise', 'snapshot_sha256')
    body = {key: evidence.get(key) for key in identity_keys}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
               separators=(',', ':')).encode('utf-8')).hexdigest()


def build_invoice_review(preflight, evidence, hybrid_id, promotion_lines, funded_lines):
    """Review independently recorded Razorpay evidence + immutable D2B quote.

    Caller MUST independently validate DB-held reservations and supply only
    the immutable evidence record written by D2C2C. No payment lookup here.
    """
    pre = _mapping(preflight, 'D2C1 checkout preflight')
    ev = _mapping(evidence, 'D2C2C verified payment evidence')
    if pre.get('posting_authorized') is not False:
        raise ValueError('D2C1 non-posting checkout preflight required')
    if not isinstance(hybrid_id, str) or not hybrid_id.strip():
        raise ValueError('Stored hybrid checkout ID required')
    snapshot = _hash(pre.get('snapshot_sha256'), 'Checkout snapshot')
    if ev.get('state') != 'Verified Pending Invoice' or ev.get('provider_status') != 'captured':
        raise ValueError('Payment is not eligible for automatic invoice review')
    for k, val in (('enrollment', pre.get('enrollment')),
                   ('member', pre.get('member')),
                   ('hybrid_checkout', hybrid_id),
                   ('snapshot_sha256', snapshot)):
        if ev.get(k) != val or not val:
            raise ValueError('Verified payment identity mismatch: ' + k)
    if not str(ev.get('razorpay_order_id') or '').startswith('order_') or not str(ev.get('razorpay_payment_id') or '').startswith('pay_'):
        raise ValueError('Stored Razorpay IDs required')
    if type(ev.get('amount_paise')) is not int or ev['amount_paise'] <= 0:
        raise ValueError('Verified Razorpay paise amount must be positive integer')
    if ev.get('identity_sha256') != _evidence_identity(ev):
        raise ValueError('Stored payment evidence digest is inconsistent')
    plan = _mapping(pre.get('plan'), 'Frozen D2B price plan')
    gross = money(plan.get('original_price_incl_gst'))
    discount = money(plan.get('promotion_discount'))
    invoice = money(plan.get('invoice_total_incl_gst'))
    wallet = money(plan.get('funded_wallet_tender'))
    gateway = money(plan.get('razorpay_tender'))
    net = money(plan.get('invoice_net_excl_gst'))
    tax = money(plan.get('invoice_gst'))
    if gross - discount != invoice or invoice != wallet + gateway or invoice != net + tax:
        raise ValueError('Frozen invoice and tender totals do not reconcile')
    if discount >= gross or gateway <= 0 or ev['amount_paise'] != cents(gateway):
        raise ValueError('Invalid discounted payable or independently verified payment amount')
    if pre.get('expected_gateway_paise') != cents(gateway):
        raise ValueError('D2C1 expected gateway due differs from immutable price plan')
    for key, amount in (('invoice_total', invoice), ('original_price', gross),
                        ('discount', discount), ('funded_wallet', wallet)):
        if money(pre.get(key)) != amount:
            raise ValueError('D2C1 pricing mismatch: ' + key)
    totals, sources = _allocations(promotion_lines, funded_lines)
    if totals['promotion'] != discount or totals['funded'] != wallet:
        raise ValueError('Reservation allocation amounts do not match the frozen price')
    from club100_core.accounting.hybrid_gst import normalized_rate
    frozen_rate = normalized_rate(plan.get('tax_rate_percent'))
    # Verify the original frozen GST rate; do not recalculate a historical
    # checkout using a newly configured rate.
    calculated_net = (invoice / (Decimal('1') + frozen_rate/100)).quantize(
        _PAISE, rounding=ROUND_HALF_UP)
    if calculated_net != net or invoice - calculated_net != tax:
        raise ValueError('Frozen inclusive GST arithmetic is inconsistent')
    body = {'enrollment': pre['enrollment'], 'member': pre['member'],
            'hybrid_checkout': hybrid_id, 'snapshot_sha256': snapshot,
            'evidence_name': ev.get('name') or '',
            'evidence_identity_sha256': ev['identity_sha256'],
            'original_price_incl_gst': str(gross),
            'supplier_discount_incl_gst': str(discount),
            'invoice_total_incl_gst': str(invoice),
            'taxable_value_projected': str(net), 'gst_projected': str(tax),
            'funded_wallet_payment': str(wallet), 'razorpay_payment': str(gateway),
            'corporate_source': str(sources['Corporate']),
            'referral_source': str(sources['Referral'])}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True,
                     separators=(',', ':')).encode()).hexdigest()
    return {**body, 'invoice_review_sha256': digest,
            'supplier_discount_is_not_wallet_payment': True,
            'no_automatic_tax_or_invoice_authorization': True,
            'submitted_invoice_verified': False,
            'razorpay_currently_refetched': False,
            'posting_authorized': False}


def validate_tax_template_rows(rows, company_tax_rate='5'):
    """Conservative template-format check. Does NOT determine GST legality."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError('Expected one or two GST component rows')
    total = Decimal('0')
    for r in rows:
        _mapping(r, 'Tax row')
        if (r.get('charge_type') != 'On Net Total'
                or r.get('included_in_print_rate') not in (1, True)
                or not isinstance(r.get('account_head'), str)
                or not r['account_head'].strip()):
            raise ValueError('Expected inclusive On Net Total GST tax account row')
        try:
            rate = Decimal(str(r.get('rate')))
        except (InvalidOperation, ValueError):
            raise ValueError('Invalid tax rate') from None
        if not rate.is_finite() or rate <= 0:
            raise ValueError('Positive tax rate required')
        total += rate
    from club100_core.accounting.hybrid_gst import normalized_rate
    if total != normalized_rate(company_tax_rate):
        raise ValueError('Tax template total differs from configured GST rate')
    return {'rows': len(rows), 'rate_percent': str(total),
            'tax_approval_separately_required': True,
            'posting_authorized': False}
