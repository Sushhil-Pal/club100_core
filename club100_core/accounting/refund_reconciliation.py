"""Read-only postcondition checks on actual submitted ERPNext refund vouchers.

Fails the containing DB transaction if a refund JE, PE or original-grant ledger
entry does not exactly offset its original counterpart. Does not post vouchers.
"""

import frappe
from club100_core.accounting.rules import money
from club100_core.accounting.refund_reconciliation_rules import (
    exact_offset, check_source_legs, check_grant_restoration,
)


def _net(doctype, voucher, account, company, *, party=None):
    if not voucher:
        raise ValueError('Expected submitted voucher missing')
    if frappe.db.get_value(doctype, voucher, 'docstatus') != 1:
        raise ValueError('Voucher missing or not submitted: '+voucher)
    extra = ' AND party=%s' if party else ''
    vals=(doctype, voucher, account, company) + ((party,) if party else ())
    row=frappe.db.sql('''SELECT COALESCE(SUM(debit-credit),0) AS net
       FROM `tabGL Entry` WHERE voucher_type=%s AND voucher_no=%s
       AND account=%s AND company=%s AND is_cancelled=0'''+extra,
       vals,as_dict=True)[0]
    return money(row.net)


def _reservation_ledger(enrollment, allocations):
    key=enrollment.wallet_reservation_key
    reserve=frappe.db.get_value('Club100 Wallet Reservation',{'reservation_key':key},
                                ['name','status','member','reference'],as_dict=True)
    if (not reserve or reserve.status!='Refunded' or reserve.member!=enrollment.member
        or reserve.reference!=enrollment.name):
        raise ValueError('Original wallet reservation was not fully refunded')
    spending, returned={},{}
    for a in allocations:
        grant=a['grant']
        spending[grant]=frappe.db.sql('''SELECT amount FROM `tabClub100 Wallet Ledger`
             WHERE event_key=%s AND `grant`=%s AND member=%s AND entry_type='Spend' ''',
             ('WALLET:SPEND:'+reserve.name+':'+grant,grant,enrollment.member),as_dict=True)
        returned[grant]=frappe.db.sql('''SELECT amount FROM `tabClub100 Wallet Ledger`
             WHERE event_key=%s AND `grant`=%s AND member=%s AND entry_type='Refund' ''',
             ('WALLET:REFUND:'+reserve.name+':'+grant,grant,enrollment.member),as_dict=True)
    # Check the complete reservation child table too, rejecting additional restored grants.
    childs=frappe.get_all('Club100 Wallet Reservation Item',filters={'parent':reserve.name},
                         fields=['grant','amount','source_type'])
    if len(childs)!=len(allocations) or {r.grant for r in childs}!={a['grant'] for a in allocations}:
        raise ValueError('Reservation grant set changed during refund')
    return check_grant_restoration(allocations,
        {g:[r.amount for r in rows] for g,rows in spending.items()},
        {g:[r.amount for r in rows] for g,rows in returned.items()})


def verify_full_refund(enrollment, rec, cfg, plan, original_invoice, note, gateway, wallet, allocations):
    """Run after voucher submission and wallet refund, before marking refunded."""
    receivable=original_invoice.debit_to
    company=cfg['company']
    if money(plan['wallet']) != 0:
        if not wallet:
            raise ValueError('Wallet refund voucher missing')
        if set(plan['sources']) - {'Corporate', 'Referral'}:
            raise ValueError('Unsupported wallet source for accounted refund')
        original_nets={s:_net('Journal Entry',rec.wallet_journal_entry,cfg[s],company)
                       for s in plan['sources']}
        reverse_nets={s:_net('Journal Entry',wallet.name,cfg[s],company)
                      for s in plan['sources']}
        total=check_source_legs(plan['sources'], original_nets, reverse_nets,
                                cfg,receivable,cfg['razorpay_clearing'])
        if total != money(plan['wallet']):
            raise ValueError('Refund source vouchers do not equal wallet tender')
        # The original JE credited receivable; restoration must debit it.
        exact_offset(-_net('Journal Entry',rec.wallet_journal_entry,receivable,company,party=rec.customer),
                     -_net('Journal Entry',wallet.name,receivable,company,party=rec.customer),
                     plan['wallet'],'wallet receivable')
        if _reservation_ledger(enrollment,allocations) != money(plan['wallet']):
            raise ValueError('Refund did not restore the original wallet amount')
    elif wallet or allocations:
        raise ValueError('Non-wallet refund has unexpected wallet vouchers')
    if money(plan['gateway']):
        if not gateway:
            raise ValueError('Gateway refund voucher missing')
        exact_offset(_net('Payment Entry',rec.gateway_payment_entry,cfg['razorpay_clearing'],company),
                     _net('Payment Entry',gateway.name,cfg['razorpay_clearing'],company),
                     plan['gateway'],'Razorpay clearing')
        exact_offset(-_net('Payment Entry',rec.gateway_payment_entry,receivable,company,party=rec.customer),
                     -_net('Payment Entry',gateway.name,receivable,company,party=rec.customer),
                     plan['gateway'],'gateway receivable')
    elif gateway:
        raise ValueError('Wallet-only refund unexpectedly has a gateway refund voucher')
    if money(frappe.db.get_value('Sales Invoice',note.name,'outstanding_amount'))!=0:
        raise ValueError('Refund Sales Invoice has residual outstanding')
    return {'wallet_reconciled':f"{money(plan['wallet']):.2f}",
            'gateway_reconciled':f"{money(plan['gateway']):.2f}",
            'original_grants_verified':len(allocations)}
