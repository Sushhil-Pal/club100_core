'''Audited bulk funding allocation through ERPNext Desk.

Submitting the entire batch issues immutable wallet grants in a single database
transaction. No commit here; an exception rolls back batch and all ledger rows.
'''
from contextlib import contextmanager
import frappe
from frappe.model.document import Document
from frappe.utils import today
from club100_core.wallet.logic import money
from club100_core.wallet.admin_refs import batch_allocation_key
from club100_core.wallet.admin_batch_logic import prepare_lines
from club100_core.wallet.corporate_logic import require_capacity


def _require_manager():
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required for corporate allocations.', frappe.PermissionError)


def _lock_pool(pool_name):
    rows = frappe.db.sql('''SELECT name, corporate_customer, funding_reference, amount, currency, status, funding_verified
                            FROM `tabClub100 Corporate Credit Pool` WHERE name=%s FOR UPDATE''',
                         (pool_name,), as_dict=True)
    if len(rows) != 1 or rows[0].status != 'Funded' or int(rows[0].funding_verified or 0) != 1 or rows[0].currency != 'INR':
        frappe.throw('Select a verified and funded corporate pool.')
    return rows[0]


@contextmanager
def _issuing_from_batch():
    old = getattr(frappe.flags, 'club100_wallet_batch_issue', False)
    frappe.flags.club100_wallet_batch_issue = True
    try:
        yield
    finally:
        frappe.flags.club100_wallet_batch_issue = old


class Club100CorporateAllocationBatch(Document):
    def validate(self):
        _require_manager()
        if not self.corporate_pool:
            frappe.throw('Select a Corporate Credit Pool.')
        customer = frappe.db.get_value('Club100 Corporate Credit Pool', self.corporate_pool, 'corporate_customer')
        if not customer:
            frappe.throw('Corporate Credit Pool not found.')
        self.corporate_customer = customer
        if not self.valid_from:
            self.valid_from = today()
        if self.validity_type == 'Lifetime':
            self.expires_on = None
        items = [row.as_dict() for row in self.allocations]
        try:
            normalized, total = prepare_lines(items, str(self.valid_from), self.validity_type, str(self.expires_on or ''))
        except ValueError as exc:
            frappe.throw(str(exc))
        self.allocation_total = total
        # Staff may save a draft without eligible employees, but we validate
        # company membership and funded capacity again while locked on Submit.
        for line in normalized:
            if not frappe.db.exists('Club100 Corporate Employee', {
                    'corporate_customer': customer, 'member': line['member'], 'status':'Active'}):
                frappe.throw('Employee is not active in selected company: ' + line['member'])

    def on_submit(self):
        _require_manager()
        pool = _lock_pool(self.corporate_pool)
        if pool.corporate_customer != self.corporate_customer:
            frappe.throw('Company changed since batch was prepared.')
        items = [row.as_dict() for row in self.allocations]
        try:
            normalized, total = prepare_lines(items, str(self.valid_from), self.validity_type, str(self.expires_on or ''))
        except ValueError as exc:
            frappe.throw(str(exc))
        allocated = frappe.db.sql('''SELECT COALESCE(SUM(amount),0) FROM `tabClub100 Wallet Grant`
                                    WHERE corporate_pool=%s AND source_type='Corporate' ''',
                                  (pool.name,))[0][0]
        # Retry safety: pre-existing credits from THIS batch count as allocated;
        # only check capacity for missing lines.
        pending = []
        for row, item in zip(self.allocations, normalized):
            key = batch_allocation_key(self.name, row.name)
            old = frappe.db.get_value('Club100 Wallet Grant', {'source_event_key':key},
                                      ['name','member','corporate_pool','amount','valid_from','validity_type','expires_on'], as_dict=True)
            if old:
                if (old.member != item['member'] or old.corporate_pool != pool.name or
                    money(old.amount) != item['amount'] or str(old.valid_from)[:10] != item['valid_from'] or
                    old.validity_type != item['validity_type'] or str(old.expires_on or '')[:10] != str(item['expires_on'] or '')):
                    frappe.throw('Existing credit differs from batch row: ' + row.name)
            else:
                pending.append((row, item, key))
        if pending:
            try:
                require_capacity(pool.amount, allocated, sum((x[1]['amount'] for x in pending), money(0)))
            except ValueError as exc:
                frappe.throw(str(exc))
        with _issuing_from_batch():
            for row, item, key in pending:
                grant = frappe.get_doc({
                    'doctype':'Club100 Wallet Grant',
                    'source_event_key':key,
                    'source_type':'Corporate',
                    'member':item['member'],
                    'corporate_pool':pool.name,
                    'amount':item['amount'], 'currency':'INR',
                    'valid_from':item['valid_from'], 'validity_type':item['validity_type'],
                    'expires_on':item['expires_on'], 'status':'Active', 'notes':item['notes'],
                }).insert(ignore_permissions=True)
                frappe.db.set_value('Club100 Corporate Allocation Line', row.name, 'wallet_grant', grant.name, update_modified=False)
        for row in self.allocations:
            if not frappe.db.get_value('Club100 Corporate Allocation Line', row.name, 'wallet_grant'):
                key = batch_allocation_key(self.name, row.name)
                grant_name = frappe.db.get_value('Club100 Wallet Grant', {'source_event_key':key}, 'name')
                frappe.db.set_value('Club100 Corporate Allocation Line', row.name, 'wallet_grant', grant_name, update_modified=False)

    def on_cancel(self):
        frappe.throw('Cannot cancel submitted funding allocations. Credits require audited reversals.')

    def on_trash(self):
        if self.docstatus == 1:
            frappe.throw('Submitted credit allocations cannot be deleted.')
