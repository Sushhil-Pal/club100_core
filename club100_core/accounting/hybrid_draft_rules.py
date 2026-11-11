"""D2C3B1: pure guardrails for an ERPNext DRAFT hybrid Sales Invoice.

Discount is a supplier discount on the tax-inclusive invoice amount, not a
wallet tender. Caller must use ONLY persisted D2C3A preflight results, not
browser values. These guards make no legal GST determination or posting.
"""
import hashlib
import json
from decimal import Decimal

from club100_core.accounting.hybrid_invoice_contract import money

FIELDS = ('enrollment','member','hybrid_checkout','snapshot_sha256','evidence_name',
          'evidence_identity_sha256','original_price_incl_gst',
          'supplier_discount_incl_gst','invoice_total_incl_gst',
          'taxable_value_projected','gst_projected','funded_wallet_payment',
          'razorpay_payment','corporate_source','referral_source')


def _review(review):
    if not isinstance(review,dict) or set(FIELDS) - set(review):
        raise ValueError('Complete D2C3A server review required')
    digest = hashlib.sha256(json.dumps({key:review[key] for key in FIELDS},
                          sort_keys=True, separators=(',',':')).encode('utf-8')).hexdigest()
    if review.get('invoice_review_sha256') != digest:
        raise ValueError('D2C3A invoice-review digest mismatch')
    if (review.get('posting_authorized') is not False
            or review.get('no_automatic_tax_or_invoice_authorization') is not True
            or review.get('submitted_invoice_verified') is not False):
        raise ValueError('Only a non-posting D2C3A review may enter dry-run')
    original = money(review['original_price_incl_gst'])
    discount = money(review['supplier_discount_incl_gst'])
    invoice = money(review['invoice_total_incl_gst'])
    net = money(review['taxable_value_projected'])
    tax = money(review['gst_projected'])
    wallet = money(review['funded_wallet_payment'])
    razorpay = money(review['razorpay_payment'])
    corp = money(review['corporate_source'])
    referral = money(review['referral_source'])
    if (original <= 0 or discount >= original or razorpay <= 0
            or original - discount != invoice or invoice != wallet + razorpay
            or invoice != net + tax or wallet != corp + referral):
        raise ValueError('Frozen hybrid invoice funding does not balance')
    from decimal import ROUND_HALF_UP
    if (invoice/Decimal('1.05')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != net:
        raise ValueError('Projected net does not reconcile to 5% inclusive GST')
    return {'original':original,'discount':discount,'invoice':invoice,
            'net':net,'tax':tax,'wallet':wallet,'razorpay':razorpay,
            'corporate':corp,'referral':referral}


def draft_spec(review, cfg, tax_rows, payment_date):
    """Produce strictly scoped ERPNext draft document fields. Never submit."""
    v=_review(review)
    if not isinstance(cfg,dict) or any(not isinstance(cfg.get(k),str) or not cfg[k].strip()
           for k in ('company','customer','item','tax_template')):
        raise ValueError('Configured Company/Customer/service Item/tax template required')
    if not isinstance(tax_rows,list) or len(tax_rows) not in (1,2):
        raise ValueError('One or two inclusive tax template rows required')
    total=Decimal('0')
    for row in tax_rows:
        if not isinstance(row,dict) or row.get('charge_type') != 'On Net Total' or row.get('included_in_print_rate') not in (1,True):
            raise ValueError('Only inclusive On Net Total tax template rows permitted')
        if not isinstance(row.get('account_head'),str) or not row['account_head']:
            raise ValueError('Missing GST tax account')
        try: rate=Decimal(str(row.get('rate')))
        except Exception: raise ValueError('Invalid GST rate') from None
        if not rate.is_finite() or rate <= 0:
            raise ValueError('GST rate must be positive')
        total+=rate
    if total!=Decimal('5'):
        raise ValueError('Only explicitly approved 5% GST pilot projection supported')
    if not isinstance(payment_date,str) or len(payment_date)!=10 or payment_date[4]!='-' or payment_date[7]!='-':
        raise ValueError('Valid server-owned payment date required')
    eid=review['enrollment']
    return {'doctype':'Sales Invoice','company':cfg['company'],'customer':cfg['customer'],
       'currency':'INR','posting_date':payment_date,'due_date':payment_date,
       'update_stock':0,'taxes_and_charges':cfg['tax_template'],
       'apply_discount_on':'Grand Total','discount_amount':float(v['discount']),
       'remarks':'Club100 hybrid DRAFT validation ONLY: '+eid,
       'items':[{'item_code':cfg['item'],'qty':1,'rate':float(v['original']),
                 'description':'Club100 hybrid membership '+eid}],
       'taxes':tax_rows}


def verify_calculated_draft(review, invoice, cfg):
    """Compare ERPNext's computed invoice amounts with the frozen D2C3A plan."""
    v=_review(review)
    if invoice.get('docstatus') != 0 or invoice.get('company')!=cfg['company'] or invoice.get('customer')!=cfg['customer']:
        raise ValueError('Draft identity/document status mismatch')
    if invoice.get('currency')!='INR' or invoice.get('apply_discount_on')!='Grand Total':
        raise ValueError('Invoice currency or discount mode differs')
    if money(str(invoice.get('discount_amount')))!=v['discount']:
        raise ValueError('ERPNext supplier discount differs')
    items=invoice.get('items')
    if not isinstance(items,list) or len(items)!=1:
        raise ValueError('Expected one service item')
    if items[0].get('item_code')!=cfg['item'] or money(str(items[0].get('rate')))!=v['original']:
        raise ValueError('Unexpected service item or undiscounted rate')
    for key, expected in (('grand_total',v['invoice']),('net_total',v['net']),
                          ('total_taxes_and_charges',v['tax'])):
        if money(str(invoice.get(key)))!=expected:
            raise ValueError('ERPNext '+key+' does not match frozen discounted GST projection')
    if v['wallet'] + v['razorpay']!=money(str(invoice['grand_total'])):
        raise ValueError('Invoice is not fully funded by separate wallet and gateway tenders')
    return {'invoice_total_incl_gst':str(v['invoice']),
            'taxable_value_excl_gst':str(v['net']),
            'gst':str(v['tax']), 'original':str(v['original']),
            'supplier_discount':str(v['discount']), 'wallet_tender':str(v['wallet']),
            'gateway_tender':str(v['razorpay']), 'draft_verified':True,
            'posting_authorized':False,'production_ready':False}
