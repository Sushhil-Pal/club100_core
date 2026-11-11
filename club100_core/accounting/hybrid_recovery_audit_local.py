"""Read-only investigation of a captured hybrid payment under Manual Review.

Never posts/reopens/extends wallet holds, changes the checkout state, charges Razorpay
or invokes settle_verified_from_bridge. Bench Administrator/Test Mode only.
"""

import frappe
from frappe.utils import cint, get_datetime, now_datetime


def _guard():
    from club100_core.accounting.hybrid_runtime import require_financial_enabled
    if frappe.session.user != 'Administrator':
        raise PermissionError('Administrator required for captured hybrid review')
    require_financial_enabled()


def report(enrollment_id=None, verify_provider=1):
    """Read-only; provider check performs authenticated GET requests only.

    Returns no write-mode decision: escrow restoration requires separate finance
    review if the original wallet reservation has expired or been released.
    """
    _guard()
    name = str(enrollment_id or '').strip()
    if not name or len(name) > 140:
        raise ValueError('Valid enrollment ID required')
    if not frappe.db.exists('Club100 Enrollment', name):
        raise ValueError('Enrollment not found')
    e = frappe.get_doc('Club100 Enrollment', name)
    h_name = frappe.db.get_value('Club100 Hybrid Checkout', {'enrollment': name}, 'name')
    if not h_name:
        raise ValueError('Not a hybrid checkout')
    h = frappe.get_doc('Club100 Hybrid Checkout', h_name)
    ev_name = frappe.db.get_value('Club100 Hybrid Payment Evidence', {'enrollment': name}, 'name')
    if not ev_name:
        raise ValueError('No persisted captured-payment evidence')
    ev = frappe.get_doc('Club100 Hybrid Payment Evidence', ev_name)
    if h.member != e.member or ev.member != e.member or ev.hybrid_checkout != h.name:
        raise ValueError('Member / evidence identity mismatch')
    holds = []
    for kind, key in [('funded', h.funded_reservation_key), ('promotion', h.promotion_reservation_key)]:
        if not key:
            continue
        hold = frappe.db.get_value('Club100 Wallet Reservation', {'reservation_key': key},
            ['name','member','reference','total_amount','status','expires_at'], as_dict=True)
        if not hold or hold.member != e.member or hold.reference != name:
            raise ValueError('Original wallet reservation missing or belongs to another enrollment')
        holds.append({'kind':kind, 'name':hold.name, 'key':key,
            'amount':str(hold.total_amount), 'status':hold.status,
            'expires_at':str(hold.expires_at),
            'is_expired': get_datetime(hold.expires_at) <= now_datetime(),
            'items':[dict(x) for x in frappe.get_all('Club100 Wallet Reservation Item',
                filters={'parent':hold.name},fields=['grant','source_type','amount'],order_by='idx asc')]})
    mappings = frappe.get_all('Club100 Membership Accounting', filters={'enrollment': name},
        fields=['name','status','sales_invoice','gateway_payment_entry','wallet_journal_entry'])
    payment_entries = (frappe.get_all('Payment Entry',
        filters={'reference_no':ev.razorpay_payment_id,'docstatus':1},
        fields=['name','party','paid_amount','received_amount'])
        if ev.razorpay_payment_id else [])
    captured_ledgers = []
    for hold in holds:
        for row in hold['items']:
            ledger = frappe.db.get_value('Club100 Wallet Ledger',
                {'event_key':'WALLET:SPEND:'+hold['name']+':'+row['grant']},'name')
            if ledger:
                captured_ledgers.append(ledger)
    provider = {'checked':False}
    if cint(verify_provider):
        from club100_core.api.enrollment import _get_razorpay_config
        from club100_core.accounting.hybrid_gateway_probe_local import _server_fetch
        cfg = _get_razorpay_config()
        if cfg.get('mode') != 'test' or not str(cfg.get('key_id') or '').startswith('rzp_test_'):
            raise ValueError('Razorpay Test Mode credentials required')
        order, payment = _server_fetch(cfg,e.payment_order_id,ev.razorpay_payment_id)
        valid = (order.get('id') == e.payment_order_id and payment.get('id') == ev.razorpay_payment_id
            and payment.get('order_id') == e.payment_order_id
            and payment.get('status') == 'captured' and payment.get('currency') == 'INR'
            and type(payment.get('amount')) is int and int(payment['amount']) == int(ev.amount_paise))
        provider = {'checked':True,'captured_and_identity_match':valid,
                    'status':payment.get('status'),'amount_paise':payment.get('amount')}
    conflicts = bool(mappings or payment_entries or captured_ledgers or e.sales_invoice)
    holds_good = bool(holds) and all(x['status']=='Reserved' and not x['is_expired'] for x in holds)
    review = (ev.state=='Manual Review' and h.status=='Review' and e.payment_status=='Pending')
    outcome = ('MANUAL_ACCOUNTING_REVIEW' if conflicts
        else 'RESERVATION_REVIEW_REQUIRED' if not holds_good
        else 'PAYMENT_EVIDENCE_REVIEW_REQUIRED' if not review or not provider['checked'] or not provider['captured_and_identity_match']
        else 'CANDIDATE_FOR_CONTROLLED_RECOVERY')
    return {'enrollment':name,'member':e.member,'hybrid_checkout':h.name,
        'hybrid_status':h.status,'enrollment_payment_status':e.payment_status,
        'evidence':ev.name,'evidence_state':ev.state, 'review_reason':ev.review_reason,
        'payment_id':ev.razorpay_payment_id,'order_id':e.payment_order_id,
        'gateway_paise_recorded':ev.amount_paise,'wallet_tender':str(h.funded_wallet_tender),
        'provider':provider,'holds':holds,'accounting_mappings':[dict(x) for x in mappings],
        'submitted_gateway_payment_entries':[dict(x) for x in payment_entries],
        'already_captured_wallet_ledger_names':captured_ledgers,
        'enrollment_invoice':e.sales_invoice or None,'outcome':outcome,
        'read_only':True,'second_charge':False,
        'note':'No payment, invoice or wallet write performed. Never retry gateway; expired/released holds require separate reconciliation.'}
