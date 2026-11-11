"""Read-only local promotion UAT readiness diagnostic. Not a payment endpoint."""
import frappe
from club100_core.wallet.core import wallet_balance
from club100_core.accounting.hybrid_reservations import _eligible_promotion_grants


def report(member_id):
    if frappe.local.site != 'club100.local' or str(frappe.conf.get('razorpay_mode') or '').lower() != 'test':
        raise ValueError('club100.local and Razorpay Test Mode required')
    user=frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('System Manager required',frappe.PermissionError)
    member=frappe.db.get_value('Club100 Member',str(member_id or '').strip(),['name','full_name'],as_dict=True)
    if not member: raise ValueError('Club100 Member not found')
    policy=str(frappe.conf.get('club100_hybrid_promotion_policy_version') or 'member-funded-v1').strip()
    eligible=set(_eligible_promotion_grants(member.name,policy))
    balance=wallet_balance(member.name)
    grants=[]; total=0.0
    for g in balance['grants']:
        if g['source']!='Promotion': continue
        amount=round(float(g['spendable_remaining']),2) if g['spendable'] else 0.0
        selected=g['grant'] in eligible
        if selected: total+=amount
        grants.append({'grant':g['grant'],'status':'Eligible' if selected else 'Not Eligible',
                       'remaining':g['remaining'],'spendable_remaining':g['spendable_remaining'],
                       'active':g['spendable'],'eligible_discount_amount':str(amount if selected else 0)})
    return {'member':member.name,'member_name':member.full_name,'policy_version':policy,
            'grants':grants,'approved_spendable_discount':str(round(total,2)),
            'spendable_promotion_discount':str(round(total,2)),
            'read_only':True,'production_tax_approval':False,
            'note':'Authorized Promotion Wallet Grants are directly eligible; no additional approval document is required. Promotion is a price discount, never wallet cash tender.'}
