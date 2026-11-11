"""ERPNext Desk-only corporate pool funding confirmation (local Test Mode).

This never creates a Payment Entry or claims that a bank statement was
reconciled. A submitted, GL-backed employer advance is a prerequisite.
POST is atomic via the standard Frappe request transaction, including the
Journal Entry, source event and the pool's transition to Funded.
"""
from contextlib import contextmanager
import frappe
from club100_core.accounting.funding import (
    corporate_receipt_preview, post_corporate_receipt,
)
from club100_core.accounting.settlement import _local_pilot


def _desk_actor():
    actor = frappe.session.user
    if (actor == 'Guest' or
        (actor != 'Administrator' and 'System Manager' not in frappe.get_roles(actor))):
        frappe.throw('Only a System Manager can confirm corporate funding.', frappe.PermissionError)
    if not _local_pilot():
        frappe.throw('Corporate posting is restricted to club100.local / Razorpay Test Mode.', frappe.PermissionError)
    return actor


@contextmanager
def _trusted_accounting():
    """Elevate only the trusted, pre-authorized internal posting call."""
    actor = frappe.session.user
    if actor != 'Administrator':
        frappe.set_user('Administrator')
    try:
        yield
    finally:
        if frappe.session.user != actor:
            frappe.set_user(actor)


@frappe.whitelist(methods=['POST'])
def preview(pool_name, payment_entry_name):
    """Read-only preview for Desk; no receipt, JE or event is created."""
    _desk_actor()
    with _trusted_accounting():
        result = corporate_receipt_preview(pool_name, payment_entry_name)
    return result


@frappe.whitelist(methods=['POST'])
def confirm(pool_name, payment_entry_name):
    """Confirm an existing employer receipt and book pool funding exactly once.

    Intentionally no commit(): Frappe commits a successful POST. If the
    request errors, the transaction is rolled back by the request handler.
    """
    actor = _desk_actor()
    pool_name = str(pool_name or '').strip()
    payment_entry_name = str(payment_entry_name or '').strip()
    if not pool_name or not payment_entry_name:
        frappe.throw('Select a corporate pool and a submitted Payment Entry.')
    with _trusted_accounting():
        result = post_corporate_receipt(pool_name, payment_entry_name,
                                        commit=False, verified_by=actor)
    return result
