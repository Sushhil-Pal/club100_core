"""Corporate wallet funding controls (Stage 2C). No wallet spends or payouts.

Pool rows are locked before inserting grants, and allocation totals are derived
from immutable grants inside the transaction. This assumes all corporate grants
pass through the standard DocType insert controller, not raw SQL.
"""
import frappe
from frappe.utils import cint

from club100_core.wallet.logic import money
from club100_core.wallet.corporate_logic import remaining_capacity, require_capacity


def validate_and_lock_allocation(grant):
    if not grant.corporate_pool:
        frappe.throw("Corporate pool is mandatory for employer-funded credits.")

    # Serialize allocations against this tranche for correct concurrency limits.
    rows = frappe.db.sql("""
        SELECT name, corporate_customer, funding_reference, amount,
               currency, funding_verified, status
          FROM `tabClub100 Corporate Credit Pool`
         WHERE name=%s FOR UPDATE
    """, (grant.corporate_pool,), as_dict=True)
    if len(rows) != 1:
        frappe.throw("Corporate credit pool does not exist.")
    pool = rows[0]
    if pool.status != "Funded" or cint(pool.funding_verified) != 1 or pool.currency != "INR":
        frappe.throw("Corporate credit pool is not verified and funded.")

    # Funding Verified on the Pool is not financial provenance. Reject new
    # allocations unless its own immutable event and submitted liability JE
    # are both present and match the posted source GL.
    from club100_core.accounting.funding import CORPORATE_EVENT
    from club100_core.accounting.settlement import _config
    from club100_core.accounting.wallet_backing import _event, _corporate_source, _voucher_credit
    cfg = _config()
    event = _event(CORPORATE_EVENT + pool.name,
                   event_type='Corporate Advance Reclassified',
                   source_type='Corporate', company=cfg['company'], pool=pool.name)
    _corporate_source(cfg, frappe._dict(corporate_pool=pool.name), event)
    if _voucher_credit(event.journal_entry, cfg['Corporate'], cfg['company']) != money(pool.amount):
        frappe.throw('Corporate funding liability GL does not match the pool. Confirm funding before allocation.')

    eligible = frappe.db.exists("Club100 Corporate Employee", {
        "corporate_customer": pool.corporate_customer,
        "member": grant.member,
        "status": "Active",
    })
    if not eligible:
        frappe.throw("Member must be an active employee of the pool's corporate customer.")

    # An explicit mismatch is suspicious: never silently reassign another company.
    if grant.corporate_customer and grant.corporate_customer != pool.corporate_customer:
        frappe.throw("Grant corporate customer differs from selected pool.")
    if grant.funding_reference and grant.funding_reference != pool.funding_reference:
        frappe.throw("Grant funding reference differs from selected pool.")
    grant.corporate_customer = pool.corporate_customer
    grant.funding_reference = pool.funding_reference
    grant.funding_verified = 1

    allocated = frappe.db.sql("""
        SELECT COALESCE(SUM(amount), 0)
          FROM `tabClub100 Wallet Grant`
         WHERE corporate_pool=%s AND source_type='Corporate'
    """, (pool.name,))[0][0]
    try:
        require_capacity(pool.amount, allocated, grant.amount)
    except ValueError as exc:
        frappe.throw(str(exc))
    # No separate balance to update. The grant insert + immutable credit posting
    # occur in the same transaction, protected by the above row lock.


def pool_summary(pool_name):
    """Read-only admin summary; no new records and no state change."""
    if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("System Manager required.", frappe.PermissionError)
    p = frappe.db.get_value(
        "Club100 Corporate Credit Pool", pool_name,
        ["name", "corporate_customer", "funding_reference", "amount", "status"], as_dict=True)
    if not p:
        frappe.throw("Corporate credit pool not found.")
    allocated = frappe.db.sql("""
        SELECT COALESCE(SUM(amount),0)
          FROM `tabClub100 Wallet Grant`
         WHERE corporate_pool=%s AND source_type='Corporate'
    """, (pool_name,))[0][0]
    return {
        "pool": p.name, "customer": p.corporate_customer,
        "funding_reference": p.funding_reference,
        "funded": str(money(p.amount)), "allocated": str(money(allocated)),
        "unallocated": str(remaining_capacity(p.amount, allocated)),
        "status": p.status, "currency": "INR",
    }


def corporate_healthcheck():
    """Read-only local inspection. This does not confirm that funding was truly received."""
    dts = ("Club100 Corporate Credit Pool", "Club100 Corporate Employee")
    installed = {dt: bool(frappe.db.exists("DocType", dt)) for dt in dts}
    return {
        "site": frappe.local.site,
        "doctypes": installed,
        "pools": frappe.db.count(dts[0]) if installed[dts[0]] else None,
        "employee_links": frappe.db.count(dts[1]) if installed[dts[1]] else None,
        "corporate_allocations": frappe.db.count("Club100 Wallet Grant", {"source_type":"Corporate"}),
        "wallet_spending_enabled": False,
        "corporate_portal_enabled": False,
        "funding_confirmation": "Manual System Manager verification; reconcile bank/invoice outside wallet",
    }
