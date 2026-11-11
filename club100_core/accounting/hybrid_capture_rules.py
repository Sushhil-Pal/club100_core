"""D2C3B2C2C1: PURE wallet-capture event and post-capture reconciliation rules.

NOT an instruction or authorization to post any ledger entry. Pure contract
requires externally verified identity and posted voucher snapshots, which are
only hypothetical test fixtures in the accompanying synthetic smoke test.
A future atomic settlement service must re-check all identity, GL backing,
state, and bank/tax approval *under database row locks* before calling capture.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from club100_core.accounting.hybrid_backing_rules import money, validate_funded_allocations

ZERO=Decimal('0.00')

def _id(value, label):
    if not isinstance(value,str) or not value or value.strip()!=value or len(value)>140:
        raise ValueError('Invalid '+label+' identity')
    return value

def _money_str(value):
    return str(money(value).quantize(Decimal('0.01')))

def _signed_spend(value):
    if isinstance(value,(bool,float)) or value is None:
        raise ValueError('Spend amount must be exact negative paise')
    try:
        number=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError('Invalid spend amount') from None
    if not number.is_finite() or number>=0 or number!=number.quantize(Decimal('0.01')):
        raise ValueError('Spend amount must be finite negative paise')
    return number

def planned_spends(context, allocations, backing_totals):
    """Construct expected ledger rows only, with NO side effects or authority.

    context must be a server-verified snapshot from one locked settlement, not
    client input. It is deliberately impossible to authorize posting here.
    """
    if not isinstance(context,dict) or not isinstance(backing_totals,dict):
        raise ValueError('Settlement context and backing totals are required')
    enrollment=_id(context.get('enrollment'),'enrollment')
    member=_id(context.get('member'),'member')
    wallet=_id(context.get('wallet'),'wallet')
    reservation=_id(context.get('reservation'),'reservation')
    _id(context.get('reservation_key'),'reservation key')
    _id(context.get('invoice'),'invoice')
    _id(context.get('payment_entry'),'payment entry')
    _id(context.get('journal_entry'),'journal entry')
    _id(context.get('evidence'),'payment evidence')
    _id(context.get('hybrid_checkout'),'hybrid checkout')
    if context.get('hybrid_status')!='Reserved' or context.get('reservation_status')!='Reserved':
        raise ValueError('Wallet capture requires both checkout and hold Reserved')
    if context.get('provider_status')!='captured' or context.get('evidence_state')!='Verified Pending Invoice':
        raise ValueError('Verified captured gateway evidence required')
    if context.get('hold_not_expired') is not True:
        raise ValueError('Wallet hold expired or not checked')
    if any(type(context.get(k)) is not int or context[k]!=1 for k in
           ('invoice_docstatus','payment_docstatus','journal_docstatus')):
        raise ValueError('Sales Invoice, Payment Entry and wallet JE must be submitted')
    if context.get('invoice_member')!=member or context.get('reservation_member')!=member:
        raise ValueError('Member identity changed')
    if context.get('reservation_reference')!=enrollment:
        raise ValueError('Wallet hold reference changed')
    if context.get('payment_invoice')!=context['invoice'] or context.get('journal_invoice')!=context['invoice']:
        raise ValueError('Voucher invoice references changed')
    if money(context.get('invoice_outstanding'))!=ZERO:
        raise ValueError('Invoice outstanding must be zero')
    original=money(context.get('original_price'))
    promotion=money(context.get('promotion_discount'))
    invoice=money(context.get('invoice_amount'))
    gateway=money(context.get('gateway_amount'))
    total=money(context.get('wallet_amount'))
    if original<=ZERO or promotion>original or invoice!=original-promotion or invoice!=gateway+total:
        raise ValueError('Frozen discount, invoice and tender reconciliation failed')
    if context.get('funding_preflight_verified') is not True:
        raise ValueError('Source GL and per-grant preflight is unverified')
    canonical, totals=validate_funded_allocations(allocations,_money_str(total))
    if not canonical:
        raise ValueError('Funded-wallet settlement requires a positive tender')
    if set(backing_totals)!=set(totals) or any(money(backing_totals[s])!=v for s,v in totals.items()):
        raise ValueError('Grant source backing totals differ from frozen funded allocations')
    if money(context.get('wallet_journal_amount'))!=total:
        raise ValueError('Submitted wallet JE is not equal to funded tender')
    # No promotion ledger debit here: promotion is a supplier-funded invoice discount.
    rows=[{'event_key':'WALLET:SPEND:'+reservation+':'+a['grant'],
           'wallet':wallet,'member':member,'grant':a['grant'],
           'entry_type':'Spend','amount':'-'+_money_str(a['amount']),
           'currency':'INR','reference':enrollment} for a in canonical]
    result={'reservation':reservation,'reservation_key':context['reservation_key'],
        'enrollment':enrollment,'member':member,'wallet':wallet,
        'hybrid_checkout':context['hybrid_checkout'],
        'invoice':context['invoice'],'payment_entry':context['payment_entry'],
        'journal_entry':context['journal_entry'],'evidence':context['evidence'],
        'source_totals':{k:_money_str(v) for k,v in sorted(totals.items())},
        'total':_money_str(total),'expected_ledger':rows,
        'posting_authorized':False,'wallet_capture_authorized':False,
        'bank_statement_reconciled':False,'tax_policy_approved':False,
        'production_ready':False}
    result['identity_sha256']=_plan_digest(result)
    return result

def _plan_digest(plan):
    fields=('reservation','reservation_key','enrollment','member','wallet',
      'hybrid_checkout','invoice','payment_entry','journal_entry','evidence',
      'source_totals','total','expected_ledger', 'posting_authorized',
      'wallet_capture_authorized','bank_statement_reconciled',
      'tax_policy_approved','production_ready')
    return hashlib.sha256(json.dumps({k:plan.get(k) for k in fields},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()

def verify_captured_ledger(plan, reservation_status, ledger_rows):
    """Read-only post-capture invariant. Cannot perform or approve capture."""
    if not isinstance(plan,dict) or plan.get('posting_authorized') is not False:
        raise ValueError('Only a non-authorizing plan can be checked')
    if _plan_digest(plan)!=plan.get('identity_sha256'):
        raise ValueError('Ledger capture plan digest changed')
    if plan.get('wallet_capture_authorized') is not False or plan.get('production_ready') is not False:
        raise ValueError('Unexpected capture authorization in non-posting contract')
    if reservation_status!='Captured':
        raise ValueError('Wallet reservation not Captured')
    if not isinstance(ledger_rows,list) or len(ledger_rows)!=len(plan.get('expected_ledger',())):
        raise ValueError('Capture ledger row count mismatch')
    expected={x['event_key']:x for x in plan['expected_ledger']}
    if len(expected)!=len(plan['expected_ledger']):
        raise ValueError('Expected ledger event collision')
    observed={}
    for row in ledger_rows:
        if not isinstance(row,dict):raise ValueError('Ledger row invalid')
        key=row.get('event_key')
        if key in observed or key not in expected:raise ValueError('Missing, unexpected, or duplicate spend event')
        # No unrelated spend/refund/credit entry may masquerade as capture.
        for field in ('event_key','wallet','member','grant','entry_type','currency','reference'):
            if row.get(field)!=expected[key][field]:
                raise ValueError('Capture ledger identity mismatch: '+field)
        if _signed_spend(row.get('amount'))!=_signed_spend(expected[key]['amount']):
            raise ValueError('Capture ledger amount mismatch')
        observed[key]=True
    if set(observed)!=set(expected):raise ValueError('Missing spend event')
    return {'ledger_reconciled':True,'captured_amount':plan['total'],
        'source_totals':dict(plan['source_totals']),
        'rows':len(observed),'posting_authorized':False,
        'production_ready':False}
