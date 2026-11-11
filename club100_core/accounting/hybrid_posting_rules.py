"""D2C3B2B: pure settlement posting contracts for a future ATOMIC ERPNext UAT.

NO database, gateway, callback, invoice, wallet or GL side effects.
Checking a contract never authorizes tax treatment, payment posting or activation.
"""
from decimal import Decimal
from collections.abc import Mapping
import hashlib
import json
from club100_core.accounting.hybrid_draft_rules import _review, verify_calculated_draft
from club100_core.accounting.hybrid_invoice_contract import money


def _dict(value, label):
    if not isinstance(value, Mapping):
        raise ValueError(label + ' must be a mapping')
    return value


def _amount(value):
    # Convert ERPNext's computed floating-point fields ONLY at this boundary.
    # Never convert untrusted source amounts through Python floats.
    return money(str(value))


def build_posting_plan(review, cfg, evidence):
    """Derive exact tender and liability ledger requirements from signed review.

    Requires a D2C3A review from server-owned records, never HTTP inputs.
    """
    v = _review(_dict(review,'Invoice review'))
    cfg = _dict(cfg,'Accounting config')
    evidence = _dict(evidence,'Payment evidence')
    required = ('company','customer','razorpay_clearing')
    if any(not isinstance(cfg.get(k),str) or not cfg[k].strip() for k in required):
        raise ValueError('Company, customer and gateway clearing account required')
    if (v['corporate'] > 0 and not str(cfg.get('Corporate') or '').strip()) or (
        v['referral'] > 0 and not str(cfg.get('Referral') or '').strip()):
        raise ValueError('Funding source liability account missing')
    used = [cfg['razorpay_clearing']] + [cfg[source] for source, amount in (
        ('Corporate',v['corporate']), ('Referral',v['referral'])) if amount > 0]
    if len(used) != len(set(used)):
        raise ValueError('Clearing and liability accounts must be distinct')
    if evidence.get('state') != 'Verified Pending Invoice' or evidence.get('provider_status') != 'captured':
        raise ValueError('Only captured, pending financial evidence can be settled')
    if (evidence.get('name') != review['evidence_name'] or
        evidence.get('identity_sha256') != review['evidence_identity_sha256'] or
        evidence.get('enrollment') != review['enrollment'] or
        evidence.get('member') != review['member'] or
        evidence.get('hybrid_checkout') != review['hybrid_checkout']):
        raise ValueError('Payment evidence identity differs from frozen review')
    payment_id = evidence.get('razorpay_payment_id')
    order_id = evidence.get('razorpay_order_id')
    if (not isinstance(payment_id,str) or not payment_id.startswith('pay_') or
        not isinstance(order_id,str) or not order_id.startswith('order_')):
        raise ValueError('Captured Razorpay provider IDs required')
    amount_paise = evidence.get('amount_paise')
    if type(amount_paise) is not int or amount_paise != int(v['razorpay'] * 100):
        raise ValueError('Captured Razorpay amount does not equal frozen tender')
    identity_keys = ('enrollment','hybrid_checkout','member','razorpay_order_id',
                     'razorpay_payment_id','amount_paise','snapshot_sha256')
    if evidence.get('snapshot_sha256') != review['snapshot_sha256']:
        raise ValueError('Payment evidence snapshot differs from frozen review')
    identity = {key:evidence.get(key) for key in identity_keys}
    digest = hashlib.sha256(json.dumps(identity,sort_keys=True,
                     separators=(',',':')).encode('utf-8')).hexdigest()
    if evidence.get('identity_sha256') != digest:
        raise ValueError('Payment evidence identity SHA256 does not verify')
    if v['razorpay'] <= 0:
        raise ValueError('This trial supports captured gateway payments only')
    sources = {source: str(amount) for source, amount in (
        ('Corporate',v['corporate']), ('Referral',v['referral'])) if amount > 0}
    return {'enrollment':review['enrollment'], 'member':review['member'],
        'evidence':evidence['name'], 'order_id':order_id, 'payment_id':payment_id,
        'invoice_review_sha256':review['invoice_review_sha256'],
        'company':cfg['company'], 'customer':cfg['customer'],
        'invoice_amount':str(v['invoice']), 'original_price':str(v['original']),
        'supplier_discount':str(v['discount']), 'net':str(v['net']),
        'gst':str(v['tax']), 'gateway_amount':str(v['razorpay']),
        'funded_amount':str(v['wallet']), 'source_amounts':sources,
        'clearing_account':cfg['razorpay_clearing'],
        'liability_accounts':{k:cfg[k] for k in sources},
        'promotion_is_not_tender':True,
        'must_rollback_in_b2b':True, 'posting_authorized':False}


def validate_submitted_invoice(review, cfg, doc):
    """Verify ERPNext's posted invoice amounts, including before payment posting."""
    _dict(doc,'Invoice')
    if doc.get('docstatus') != 1 or not isinstance(doc.get('name'),str) or not doc['name']:
        raise ValueError('Submitted invoice identity and docstatus required')
    clone = dict(doc)
    clone['docstatus'] = 0
    checked = verify_calculated_draft(review,clone,cfg)
    if _amount(doc.get('outstanding_amount')) != money(review['invoice_total_incl_gst']):
        raise ValueError('Invoice must be fully outstanding before separate tenders post')
    return checked


def validate_gateway_entry(plan, invoice_name, doc):
    _dict(plan,'Posting plan');_dict(doc,'Payment Entry')
    if (doc.get('docstatus') != 1 or doc.get('payment_type') != 'Receive'
        or doc.get('party_type') != 'Customer' or doc.get('party') != plan['customer']
        or doc.get('paid_to') != plan['clearing_account']
        or doc.get('reference_no') != plan['payment_id']):
        raise ValueError('Razorpay clearing Payment Entry provenance mismatch')
    due = money(plan['gateway_amount'])
    if _amount(doc.get('paid_amount')) != due or _amount(doc.get('received_amount')) != due:
        raise ValueError('Razorpay Payment Entry amount mismatch')
    refs = doc.get('references')
    if not isinstance(refs,list) or len(refs) != 1:
        raise ValueError('Exactly one invoice reference required')
    r=refs[0]
    if (not isinstance(r,Mapping) or r.get('reference_doctype') != 'Sales Invoice'
        or r.get('reference_name') != invoice_name
        or _amount(r.get('allocated_amount')) != due):
        raise ValueError('Payment Entry invoice allocation mismatch')
    return True


def validate_wallet_entry(plan, invoice_name, receivable_account, doc):
    _dict(plan,'Posting plan')
    wallet = money(plan['funded_amount'])
    if wallet == 0:
        if doc is not None:
            raise ValueError('Zero-funded checkout cannot create a wallet Journal Entry')
        return True
    _dict(doc,'Journal Entry')
    if doc.get('docstatus') != 1 or doc.get('company') != plan['company']:
        raise ValueError('Submitted wallet liability Journal Entry required')
    lines = doc.get('accounts')
    if not isinstance(lines,list):
        raise ValueError('Journal Entry account lines missing')
    expected = {plan['liability_accounts'][s]:money(amount)
                for s,amount in plan['source_amounts'].items()}
    if len(lines) != len(expected)+1:
        raise ValueError('Journal Entry must have exact source-debit and receivable-credit legs')
    total_debit=Decimal('0.00');total_credit=Decimal('0.00')
    receivable=0
    for line in lines:
        if not isinstance(line,Mapping):raise ValueError('Invalid Journal Entry row')
        account=line.get('account')
        debit=_amount(line.get('debit_in_account_currency',0))
        credit=_amount(line.get('credit_in_account_currency',0))
        total_debit+=debit;total_credit+=credit
        if account in expected:
            if debit != expected.pop(account) or credit != 0:
                raise ValueError('Funding liability debit mismatch')
            if line.get('reference_type') or line.get('reference_name'):
                raise ValueError('Liability leg cannot reference receivable invoice')
        elif account == receivable_account:
            receivable+=1
            if (debit != 0 or credit != wallet or line.get('party_type') != 'Customer'
                or line.get('party') != plan['customer']
                or line.get('reference_type') != 'Sales Invoice'
                or line.get('reference_name') != invoice_name):
                raise ValueError('Invoice receivable credit mismatch')
        else:
            raise ValueError('Unexpected Journal Entry GL account')
    if expected or receivable != 1 or total_debit != wallet or total_credit != wallet:
        raise ValueError('Wallet Journal Entry unbalanced or incomplete')
    return True


def validate_final_balance(plan, outstanding, captured_promotion, captured_funded):
    if _amount(outstanding) != Decimal('0.00'):
        raise ValueError('Invoice must have zero outstanding before activation')
    if _amount(captured_promotion) != money(plan['supplier_discount']):
        raise ValueError('Promotional allowance was not captured exactly once')
    if _amount(captured_funded) != money(plan['funded_amount']):
        raise ValueError('Funded wallet amount not captured exactly once')
    if money(plan['gateway_amount']) + money(plan['funded_amount']) != money(plan['invoice_amount']):
        raise ValueError('Paid sources do not reconcile to invoice')
    return {'reconciled':True,'posting_authorized':False,'membership_activation_enabled':False}
