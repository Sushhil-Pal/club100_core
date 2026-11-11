"""Authenticated PWA facade for the local, guarded hybrid integration pilot.

No financial numbers or identities supplied by a member can authorize posting.
All actual settlement runs ONLY after HMAC + independent provider GET in the
existing payment callback bridge.
"""
import frappe
from frappe.utils import cint
from club100_core.accounting import hybrid_runtime


def _member_auth():
    if frappe.session.user == 'Guest':
        frappe.throw('Login required', frappe.PermissionError)
    from club100_core.api.member import _get_current_member
    return _get_current_member()


@frappe.whitelist(methods=['GET'])
def checkout_quote(program=None, duration_months=None):
    _member_auth()
    return hybrid_runtime.quote(program, cint(duration_months))


@frappe.whitelist(methods=['POST'])
def create_hybrid_checkout(program=None, duration_months=None,
                           promotion_discount='0', wallet_amount='0'):
    _member_auth()
    return hybrid_runtime.create(program, cint(duration_months),
                                 promotion_discount, wallet_amount)


@frappe.whitelist(methods=['POST'])
def resume_hybrid_checkout(enrollment_id=None):
    _member_auth()
    return hybrid_runtime.resume(enrollment_id)


@frappe.whitelist(methods=['GET'])
def hybrid_checkout_state(program=None):
    _member_auth()
    return hybrid_runtime.state(program)
