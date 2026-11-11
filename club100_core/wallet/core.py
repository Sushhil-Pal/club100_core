"""Club100 Wallet Stage 2B foundation.

No checkout spending, personal top-ups, cash transfers, referral
reward releases, or third-party purchases. Internal spending services support
reserve/capture/release/full-refund, but checkout integration is disabled.
Only System Managers can create
funded corporate/promotional grants via ERPNext Desk. No commits in here.
"""
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal
from datetime import date

import frappe
from frappe.utils import getdate, now_datetime

from club100_core.wallet.logic import money, is_active, spend_order_key


def _require_member(member):
    if not frappe.db.exists("Club100 Member", member):
        frappe.throw("Club100 member does not exist.")


def get_or_create_wallet(member):
    """Internal only: serialize concurrent wallet creation on Member row."""
    _require_member(member)
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member)
    existing = frappe.db.get_value("Club100 Wallet", {"member": member}, "name")
    if existing:
        wallet = frappe.get_doc("Club100 Wallet", existing)
        if wallet.status != "Active" or wallet.currency != "INR":
            frappe.throw("Member wallet is not active or INR.")
        return existing
    new_wallet = frappe.get_doc({"doctype": "Club100 Wallet", "member": member, "currency": "INR", "status": "Active"})
    new_wallet.insert(ignore_permissions=True)
    return new_wallet.name


@contextmanager
def _internal_ledger_post():
    previous = getattr(frappe.flags, "club100_wallet_internal_post", False)
    frappe.flags.club100_wallet_internal_post = True
    try:
        yield
    finally:
        frappe.flags.club100_wallet_internal_post = previous


def post_grant_credit(grant):
    """Called exactly once by grant.after_insert in the same DB transaction."""
    if grant.wallet != frappe.db.get_value("Club100 Wallet", {"member": grant.member}, "name"):
        frappe.throw("Grant wallet does not match member.")
    event = "WALLET:GRANT:" + grant.name
    with _internal_ledger_post():
        entry = frappe.get_doc({
            "doctype": "Club100 Wallet Ledger",
            "event_key": event,
            "wallet": grant.wallet,
            "member": grant.member,
            "grant": grant.name,
            "entry_type": "Credit",
            "amount": money(grant.amount),
            "currency": "INR",
            "reference": grant.source_event_key,
        })
        entry.insert(ignore_permissions=True)
    return entry.name


def _grant_rows(member):
    # Derived from immutable ledger postings; no client-provided balance.
    return frappe.db.sql("""
        SELECT g.name, g.member, g.wallet, g.source_type, g.validity_type,
               g.valid_from, g.expires_on, g.amount, g.status,
               COALESCE(SUM(l.amount),0) AS net_amount
        FROM `tabClub100 Wallet Grant` g
        LEFT JOIN `tabClub100 Wallet Ledger` l ON l.`grant` = g.name
        WHERE g.member=%s
        GROUP BY g.name, g.member, g.wallet, g.source_type, g.validity_type,
                 g.valid_from, g.expires_on, g.amount, g.status
        ORDER BY g.creation ASC
    """, (member,), as_dict=True)


def wallet_balance(member, as_of=None):
    """Read-only balance; expired grants are unavailable immediately.

    Lifetime has no expiry; Expires On is valid through end of that date.
    Amounts are INR and returned as strings to preserve paise precision.
    """
    _require_member(member)
    on_date = getdate(as_of) if as_of else getdate()
    wallet = frappe.db.get_value("Club100 Wallet", {"member": member}, ["name", "status"], as_dict=True)
    totals = defaultdict(lambda: Decimal("0.00"))
    expired = Decimal("0.00")
    future = Decimal("0.00")
    grants = []
    # Active reservation holds reduce spendable balance, not ledger grant balance.
    # Ignore expired holds without needing a background expiry job.
    now = now_datetime()
    held_rows = frappe.db.sql("""SELECT i.`grant`, COALESCE(SUM(i.amount),0) AS amount
        FROM `tabClub100 Wallet Reservation Item` i
        JOIN `tabClub100 Wallet Reservation` r ON r.name=i.parent
        WHERE r.member=%s AND r.status='Reserved' AND r.expires_at > %s
        GROUP BY i.`grant`""", (member, now), as_dict=True)
    held = {r.grant: money(r.amount) for r in held_rows}
    reserved_total = Decimal("0.00")
    for g in _grant_rows(member):
        net = money(g.net_amount)
        if net < 0 or net > money(g.amount):
            frappe.throw("Wallet ledger integrity check failed for grant " + g.name)
        if g.status != "Active":
            continue
        active = is_active(g.valid_from, g.validity_type, g.expires_on, on_date)
        hold = held.get(g.name, Decimal("0.00"))
        if hold < 0 or hold > net:
            frappe.throw("Wallet reservation integrity check failed for grant " + g.name)
        if active:
            totals[g.source_type] += net - hold
            reserved_total += hold
        elif getdate(g.valid_from) > on_date:
            future += net
        else:
            expired += net
        grants.append({
            "grant": g.name, "source": g.source_type,
            "remaining": str(net), "reserved": str(hold if active else Decimal("0.00")),
            "spendable_remaining": str(net - hold if active else Decimal("0.00")),
            "validity": g.validity_type,
            "valid_from": str(g.valid_from), "expires_on": str(g.expires_on) if g.expires_on else None,
            "spendable": bool(active and wallet and wallet.status == "Active" and net > hold),
        })
    available = sum(totals.values(), Decimal("0.00"))
    if not wallet or wallet.status != "Active":
        spendable_total = Decimal("0.00")
    else:
        spendable_total = available
    return {
        "currency": "INR", "wallet": wallet.name if wallet else None,
        "wallet_status": wallet.status if wallet else "Not Created",
        "available": str(spendable_total), "reserved": str(reserved_total),
        "by_source": {key: str(totals[key] if wallet and wallet.status == "Active" else Decimal("0.00")) for key in ("Corporate", "Referral", "Promotion")},
        "expired_unspent": str(expired), "future_unvested": str(future),
        "grants": sorted(grants, key=spend_order_key),
        "note": "Ledger-derived balance minus active reservations; checkout remains disabled.",
    }


@frappe.whitelist(methods=["GET"])
def my_wallet():
    """Authenticated member sees only their own balance and grant expiries."""
    if frappe.session.user == "Guest":
        frappe.throw("Login required", frappe.PermissionError)
    member = frappe.db.get_value("Club100 Member", {"user": frappe.session.user}, "name")
    if not member:
        frappe.throw("Member profile not found", frappe.PermissionError)
    balance = wallet_balance(member)
    # Show actually funded tender separately from allocations. Promotions are
    # displayed as promotions, not corporate/referral payment balances.
    from club100_core.accounting.wallet_quote import verified_wallet_quote
    verified = verified_wallet_quote(balance)
    balance['allocated_by_source'] = dict(balance['by_source'])
    balance['unbacked_by_source'] = verified['unbacked_by_source']
    balance['by_source'].update(verified['by_source'])
    balance['available'] = verified['total']
    balance['note'] = 'Spendable corporate/referral credits require source GL backing. Promotions are discounts, not cash tender.'
    return balance


def wallet_healthcheck():
    """Read-only bench healthcheck, not a financial integration test."""
    types = ("Club100 Wallet", "Club100 Wallet Grant", "Club100 Wallet Ledger")
    return {
        "site": frappe.local.site,
        "doctypes": {name: bool(frappe.db.exists("DocType", name)) for name in types},
        "wallets": frappe.db.count("Club100 Wallet") if frappe.db.exists("DocType", "Club100 Wallet") else None,
        "grants": frappe.db.count("Club100 Wallet Grant") if frappe.db.exists("DocType", "Club100 Wallet Grant") else None,
        "ledger_entries": frappe.db.count("Club100 Wallet Ledger") if frappe.db.exists("DocType", "Club100 Wallet Ledger") else None,
        "wallet_spending_enabled": False,
        "wallet_reservation_engine_available": bool(frappe.db.exists("DocType", "Club100 Wallet Reservation")),
        "referral_credit_posting_enabled": False,
        "cash_topups_enabled": False,
    }
