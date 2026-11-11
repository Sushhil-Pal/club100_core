"""Club100 referral payment bridge (Stage 2A, local pilot).

All generated rewards stay On Hold: amount_payable is a gross payment amount,
not a verified tax-exclusive reward basis. No wallet posting or cash payout.
Only call record_verified_payment from the common server-verified enrollment
finalizer. Never expose it as a whitelisted API.
"""

import json
import re
from decimal import Decimal

import frappe
from frappe.utils import cint, now_datetime

from club100_core.referrals.service import (
    TRACK_AMBASSADOR,
    TRACK_MEMBER,
    _active_policy,
    quote_reward,
)


def _decimal(value):
    return Decimal(str(value or 0))


def reserve_website_referral(code):
    """Validate referral offer before anonymous website payment.

    Store the returned JSON in the server-created Pending Enrollment. It must
    never be accepted later from the browser as arbitrary JSON.
    """
    code = str(code or "").strip().upper()
    if not code:
        return ""
    if not re.fullmatch(r"[A-Z0-9]{6,20}", code):
        frappe.throw("Invalid referral code.")
    partner_name = frappe.db.get_value(
        "Club100 Referral Partner", {"partner_code": code, "status": "Active"}, "name"
    )
    if not partner_name:
        frappe.throw("Referral code is not active.")
    partner = frappe.get_doc("Club100 Referral Partner", partner_name)
    track = TRACK_AMBASSADOR if partner.partner_type == "Ambassador" else TRACK_MEMBER
    policy = _active_policy(track)
    # Rate snapshot is tied to this server-generated enrollment, and is not
    # read back from query parameters at finalization.
    payload = {
        "version": 1,
        "code": code,
        "partner": partner.name,
        "track": track,
        "policy": policy.name,
        "first_rate": str(policy.first_purchase_rate),
        "renewal_rate": str(policy.renewal_rate),
        "currency": policy.currency,
        "minimum_duration_months": cint(policy.minimum_duration_months),
        "reserved_on": str(now_datetime()),
    }
    return json.dumps(payload, sort_keys=True)


def _capture_pending_website_referral(enrollment):
    raw = enrollment.get("referral_snapshot_json")
    if not raw or enrollment.website_source != "Website":
        return None
    if not enrollment.member:
        return None
    data = json.loads(raw)
    if data.get("version") != 1:
        frappe.throw("Unsupported referral snapshot version")
    member = enrollment.member
    existing = frappe.db.get_value(
        "Club100 Referral Attribution", {"referred_member": member},
        ["name", "referral_code"], as_dict=True,
    )
    if existing:
        if existing.referral_code != data["code"]:
            frappe.throw("Website enrollment conflicts with existing referral attribution")
        return existing.name

    # Reject a referral introduced through a later enrollment. On the initial
    # payment the current enrollment is already Paid in the DB.
    prior = frappe.db.sql(
        """SELECT name FROM `tabClub100 Enrollment`
           WHERE member=%s AND payment_status='Paid' AND name!=%s
           LIMIT 1""", (member, enrollment.name), as_dict=True,
    )
    if prior:
        return None

    partner = frappe.get_doc("Club100 Referral Partner", data["partner"])
    if partner.partner_code != data["code"] or partner.status != "Active":
        frappe.throw("Reserved referral partner is no longer active")
    if partner.member and partner.member == member:
        frappe.throw("Self-referrals are not permitted")
    if data["track"] not in (TRACK_MEMBER, TRACK_AMBASSADOR):
        frappe.throw("Invalid reserved referral track")
    if (TRACK_AMBASSADOR if partner.partner_type == "Ambassador" else TRACK_MEMBER) != data["track"]:
        frappe.throw("Reserved referral track no longer matches partner")

    doc = frappe.get_doc({
        "doctype": "Club100 Referral Attribution",
        "referred_member": member,
        "partner": partner.name,
        "referral_code": data["code"],
        "reward_track": data["track"],
        "policy": data["policy"],
        "first_rate": _decimal(data["first_rate"]),
        "renewal_rate": _decimal(data["renewal_rate"]),
        "currency": data["currency"],
        "minimum_duration_months": cint(data["minimum_duration_months"]),
        "source": "Website",
        "status": "Captured",
        "captured_on": data["reserved_on"],
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def record_verified_payment(enrollment):
    """Idempotently create a *non-payable* provisional reward after capture.

    The caller is the verified-payment finalizer while the Enrollment row is
    locked FOR UPDATE. Existing rewards use the Razorpay payment ID as a
    unique event key, so browser verify/webhook retries cannot duplicate them.
    """
    if enrollment.payment_status != "Paid" or not enrollment.member:
        return {"status": "skipped", "reason": "not_paid_or_missing_member"}
    if (enrollment.payment_gateway != "Razorpay" or not enrollment.payment_id
            or not cint(enrollment.get("razorpay_capture_verified"))):
        return {"status": "skipped", "reason": "not_verified_razorpay"}

    # Serialize reward classification across concurrent enrollments for
    # the SAME member; enrollment-level locks alone are insufficient.
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", enrollment.member)

    # For guest checkout the referrer was frozen on the Pending enrollment.
    _capture_pending_website_referral(enrollment)

    attribution_name = frappe.db.get_value(
        "Club100 Referral Attribution", {"referred_member": enrollment.member, "status": "Captured"}, "name"
    )
    if not attribution_name:
        return {"status": "skipped", "reason": "no_attribution"}
    attr = frappe.get_doc("Club100 Referral Attribution", attribution_name)
    if cint(enrollment.membership_duration_months) < cint(attr.minimum_duration_months):
        return {"status": "skipped", "reason": "below_minimum_duration"}
    if attr.currency != "INR":
        return {"status": "skipped", "reason": "currency_not_supported"}

    event_key = "RPAY:" + str(enrollment.payment_id).strip()
    existing = frappe.db.get_value("Club100 Referral Reward", {"event_key": event_key},
                                    ["name", "enrollment", "attribution"], as_dict=True)
    if existing:
        if existing.enrollment != enrollment.name or existing.attribution != attr.name:
            frappe.throw("Payment ID already credited to a different enrollment/attribution")
        return {"status": "existing", "reward": existing.name}

    # First vs renewal includes prior verified paid memberships, not free trials.
    previous = frappe.db.sql(
        """SELECT name FROM `tabClub100 Enrollment`
           WHERE member=%s AND payment_status='Paid' AND name!=%s
             AND (COALESCE(payment_date, creation) < %s
               OR (COALESCE(payment_date, creation) = %s AND name < %s))
           LIMIT 1""",
        (enrollment.member, enrollment.name, enrollment.payment_date or enrollment.creation,
         enrollment.payment_date or enrollment.creation, enrollment.name), as_dict=True,
    )
    event = "Renewal" if previous else "First Purchase"
    rate = _decimal(attr.renewal_rate if previous else attr.first_rate)
    if rate <= 0:
        return {"status": "skipped", "reason": "zero_rate"}
    amount = _decimal(enrollment.amount_payable)
    # A supplier discount changes the taxable invoice total. Referral rewards
    # must never use the PRE-discount price as the captured payment basis.
    hybrid = frappe.db.get_value('Club100 Hybrid Checkout',
        {'enrollment': enrollment.name}, ['status','original_price',
         'promotion_discount','funded_wallet_tender','gateway_due'], as_dict=True)
    if hybrid:
        if hybrid.status != 'Captured' or not enrollment.sales_invoice:
            return {"status": "skipped", "reason": "hybrid_not_reconciled"}
        invoice = frappe.db.get_value('Sales Invoice', enrollment.sales_invoice,
            ['docstatus','grand_total'], as_dict=True)
        if not invoice or invoice.docstatus != 1:
            return {"status": "skipped", "reason": "hybrid_invoice_not_submitted"}
        amount = _decimal(invoice.grand_total)
        if (amount != _decimal(hybrid.original_price)-_decimal(hybrid.promotion_discount)
                or amount != _decimal(hybrid.funded_wallet_tender)+_decimal(hybrid.gateway_due)):
            return {"status": "skipped", "reason": "hybrid_invoice_tender_mismatch"}
    if amount <= 0:
        return {"status": "skipped", "reason": "no_payment_amount"}

    # SAFETY: This provisional calculation uses the amount charged by
    # Razorpay (gross). Without a tax-exclusive verified invoice, it is NOT
    # an earned/available reward. Release must be implemented in Stage 2B.
    # Freeze tax configuration only for *new* PWA Self reward events,
    # never retroactively for existing On Hold rewards. A configured tax rate
    # is evidence only in the explicitly local-only automatic pilot.
    from club100_core.referrals.auto_basis import checkout_tax_snapshot
    gst_snapshot = checkout_tax_snapshot(enrollment)
    reward = frappe.get_doc({
        "auto_tax_rate_percent": str(gst_snapshot) if gst_snapshot is not None else None,
        "doctype": "Club100 Referral Reward",
        "event_key": event_key,
        "attribution": attr.name,
        "partner": attr.partner,
        "referred_member": enrollment.member,
        "enrollment": enrollment.name,
        "payment_reference": enrollment.payment_id,
        "reward_track": attr.reward_track,
        "reward_event": event,
        "basis_amount": amount,
        "rate": rate,
        "reward_amount": quote_reward(amount, rate),
        "currency": "INR",
        "status": "On Hold",
        "eligible_on": None,
        "notes": "PROVISIONAL GROSS ESTIMATE. Tax-exclusive basis, refunds and eligibility not verified. Do not release, redeem or pay.",
    })
    reward.insert(ignore_permissions=True)
    return {"status": "created_on_hold", "reward": reward.name}


def safely_record_verified_payment(enrollment):
    """Never break a verified membership activation over referral processing.

    Any referral error is rolled back to a savepoint and should be repaired via
    reconciliation after inspecting Error Log. This does NOT create payouts.
    """
    try:
        frappe.db.savepoint("club100_referral_record")
        return record_verified_payment(enrollment)
    except Exception:
        try:
            frappe.db.rollback(save_point="club100_referral_record")
        except Exception:
            pass
        try:
            frappe.log_error(frappe.get_traceback(), "Club100 Referral Reward Recording")
        except Exception:
            pass
        return {"status": "deferred_error"}


def reconcile_one_paid_enrollment(enrollment_id):
    """Bench-only recovery: acquires the same row lock as payment finalizer."""
    if frappe.session.user not in ("Administrator",) and "System Manager" not in frappe.get_roles():
        frappe.throw("System Manager required", frappe.PermissionError)
    frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE", enrollment_id)
    enrollment = frappe.get_doc("Club100 Enrollment", enrollment_id)
    result = safely_record_verified_payment(enrollment)
    # bench execute handles commit per normal transaction semantics; explicitly
    # commit for the admin recovery command.
    frappe.db.commit()
    return result


def stage2_healthcheck():
    """Read-only, no enrollment or reward mutation."""
    return {
        "site": frappe.local.site,
        "enrollment_referral_snapshot_field": bool(frappe.get_meta("Club100 Enrollment").has_field("referral_snapshot_json")),
        "referral_rewards": frappe.db.count("Club100 Referral Reward"),
        "on_hold_rewards": frappe.db.count("Club100 Referral Reward", {"status": "On Hold"}),
    }
