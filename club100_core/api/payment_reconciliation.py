"""Recover captured Razorpay payments for authenticated PWA Commerce orders.

Dry-run by default. Automatic execution requires explicit site configuration.
No gateway capture calls and no new Enrollment creation are performed here.
"""

import requests
import frappe
from frappe.utils import add_to_date, now_datetime, cint

from club100_core.api.enrollment import (
    RAZORPAY_API_BASE,
    _get_razorpay_config,
    _rupees_to_paise,
    _finalize_paid_enrollment_doc,
)


MAX_BATCH = 20


def _candidates(limit, min_age_minutes, enrollment_id=None):
    threshold = add_to_date(now_datetime(), minutes=-min_age_minutes, as_datetime=True)
    filters = {
        "website_source": "PWA Commerce",
        "status": "Registered",
        "payment_status": "Pending",
        "payment_order_id": ["is", "set"],
        "creation": ["<=", threshold],
    }
    if enrollment_id is not None:
        # Targeted manual recovery must never fall back to a different checkout.
        filters["name"] = enrollment_id
    return frappe.get_all(
        "Club100 Enrollment", filters=filters,
        fields=["name", "payment_order_id", "amount_payable"],
        order_by="creation asc", limit_page_length=limit,
    )


def reconcile_pending_payments(dry_run=True, limit=10, min_age_minutes=15, enrollment_id=None):
    """Server-only method. No @frappe.whitelist: invoke via bench console/scheduler.

    A preview contacts Razorpay to read captured payments but never changes ERP.
    Finalization requires dry_run=False and site flag club100_reconciliation_enabled.
    For manual recovery, enrollment_id strictly limits inspection and finalization
    to the specified enrollment. A missing/ineligible record never triggers fallback.
    """
    dry_run = dry_run is not False  # Only literal False enables write intent.
    limit = cint(limit)
    min_age_minutes = cint(min_age_minutes)
    if not 1 <= limit <= MAX_BATCH:
        frappe.throw("Reconciliation limit must be 1–20.")
    if min_age_minutes < 10:
        frappe.throw("Minimum age must be at least 10 minutes.")
    if enrollment_id is not None:
        if not isinstance(enrollment_id, str) or not enrollment_id.strip():
            frappe.throw("Provide a valid enrollment ID for targeted reconciliation.")
        enrollment_id = enrollment_id.strip()
        if len(enrollment_id) > 140:
            frappe.throw("Enrollment ID is too long.")
        # A manual operation handles a single record, never a batch.
        limit = 1
    if not dry_run and not cint(frappe.conf.get("club100_reconciliation_enabled")):
        frappe.throw("Payment reconciliation is disabled in site configuration.")

    config = _get_razorpay_config()
    results = []
    for row in _candidates(limit, min_age_minutes, enrollment_id=enrollment_id):
        name = row.name
        try:
            response = requests.get(
                f"{RAZORPAY_API_BASE}/orders/{row.payment_order_id}/payments",
                auth=(config["key_id"], config["key_secret"]),
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            payments = body.get("items")
            if not isinstance(payments, list):
                raise ValueError("Unexpected Razorpay order payments response")
            captured = [p for p in payments if p.get("status") == "captured"]
            # Reject ambiguous multiple captures: finance must investigate possible double charge.
            if len(captured) != 1:
                results.append({"enrollment": name,
                                "result": "manual_review" if len(captured) > 1 else "not_captured"})
                continue
            pay = captured[0]
            pay_id = str(pay.get("id") or "")
            valid = (pay_id.startswith("pay_")
                     and str(pay.get("order_id") or "") == row.payment_order_id
                     and pay.get("currency") == "INR"
                     and cint(pay.get("amount")) == _rupees_to_paise(row.amount_payable))
            if not valid:
                results.append({"enrollment": name, "result": "mismatch_manual_review"})
                continue
            if dry_run:
                results.append({"enrollment": name, "result": "would_finalize", "payment_id": pay_id})
                continue
            # Recheck locked record against a simultaneous browser callback/webhook.
            frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE", name)
            doc = frappe.get_doc("Club100 Enrollment", name)
            if (doc.website_source != "PWA Commerce" or doc.status != "Registered"
                    or doc.payment_status != "Pending"
                    or str(doc.payment_order_id or "") != row.payment_order_id
                    or _rupees_to_paise(doc.amount_payable) != cint(pay.get("amount"))):
                frappe.db.rollback()
                results.append({"enrollment": name, "result": "changed_skipped"})
                continue
            duplicate = frappe.db.get_value("Club100 Enrollment", {"payment_id": pay_id}, "name")
            if duplicate and duplicate != name:
                frappe.db.rollback()
                results.append({"enrollment": name, "result": "duplicate_payment_manual_review"})
                continue
            doc.payment_gateway = "Razorpay"
            doc.payment_id = pay_id
            doc.payment_date = now_datetime()
            doc.payment_status = "Paid"
            _finalize_paid_enrollment_doc(doc, issue_registration_token=False)
            frappe.db.commit()
            results.append({"enrollment": name, "result": "finalized", "payment_id": pay_id})
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "Club100 Razorpay Reconciliation")
            results.append({"enrollment": name, "result": "error_check_logs"})
    return {"dry_run": dry_run, "mode": config["mode"],
            "enrollment_id": enrollment_id, "results": results}


def scheduled_reconcile_payments():
    """Hourly hook is inert until explicitly enabled by site configuration."""
    if not cint(frappe.conf.get("club100_reconciliation_enabled")):
        return {"skipped": "disabled"}
    return reconcile_pending_payments(dry_run=False, limit=10, min_age_minutes=15)
