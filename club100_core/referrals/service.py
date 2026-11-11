"""Club100 first-level referral foundation.

Foundation stage only: no payment hooks, wallet credit posting, or cash transfers.
All accounting actions require a later verified-payment + refund integration.
"""

import re
import secrets
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe.utils import now_datetime

TRACK_MEMBER = "Member Credit"
TRACK_AMBASSADOR = "Ambassador Cash"


def _member_for_request():
    user = frappe.session.user
    if user == "Guest":
        frappe.throw("Login required", frappe.PermissionError)
    member = frappe.db.get_value("Club100 Member", {"user": user}, "name")
    if not member:
        frappe.throw("Club100 member profile not found", frappe.PermissionError)
    return member


def _new_code():
    # Avoid predictable member IDs and sequential referral codes.
    for _ in range(8):
        candidate = "C1" + secrets.token_hex(5).upper()
        if not frappe.db.exists("Club100 Referral Partner", candidate):
            return candidate
    frappe.throw("Unable to allocate referral code. Please retry.")


def _active_policy(track):
    policy_name = frappe.db.get_value(
        "Club100 Referral Policy", {"reward_track": track, "enabled": 1}, "name"
    )
    if not policy_name:
        frappe.throw("Referral program is not accepting new referrals for this track.")
    return frappe.get_doc("Club100 Referral Policy", policy_name)


def _find_my_partner(member):
    name = frappe.db.get_value("Club100 Referral Partner", {"member": member}, "name")
    return frappe.get_doc("Club100 Referral Partner", name) if name else None


@frappe.whitelist(methods=["POST"])
def ensure_my_referral_code():
    """Issue a direct-referral code to an authenticated Club100 member.

    Ambassadors are approved through ERP admin, not this endpoint.
    """
    member = _member_for_request()
    partner = _find_my_partner(member)
    if partner:
        if partner.status != "Active":
            frappe.throw("Referral account is not active.")
        return {"code": partner.partner_code, "partner_type": partner.partner_type}

    # Having a member account does not confer ambassador status.
    member_doc = frappe.get_doc("Club100 Member", member)
    display_name = (member_doc.get("full_name") or member_doc.name).strip()
    partner = frappe.get_doc({
        "doctype": "Club100 Referral Partner",
        "partner_code": _new_code(),
        "partner_type": "Member Advocate",
        "member": member,
        "display_name": display_name,
        "status": "Active",
    })
    partner.insert(ignore_permissions=True)
    return {"code": partner.partner_code, "partner_type": partner.partner_type}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def validate_referral_code(code=None):
    """Public validation; never reveals identities or earnings."""
    code = str(code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,20}", code):
        return {"valid": False}
    name = frappe.db.get_value("Club100 Referral Partner", {"partner_code": code, "status": "Active"}, "name")
    if not name:
        return {"valid": False}
    partner_type = frappe.db.get_value("Club100 Referral Partner", name, "partner_type")
    track = TRACK_AMBASSADOR if partner_type == "Ambassador" else TRACK_MEMBER
    return {"valid": bool(frappe.db.exists(
        "Club100 Referral Policy", {"reward_track": track, "enabled": 1}
    ))}


@frappe.whitelist(methods=["POST"])
def claim_my_referral(code=None):
    """Capture a direct first-touch referral after authentication.

    Checkout/signup integration can call capture_referral_from_trusted_flow()
    after member creation. The guest cannot supply someone else's member ID.
    """
    member = _member_for_request()
    return capture_referral_from_trusted_flow(member, code, source="Member PWA")


def capture_referral_from_trusted_flow(member, code, source="Website", campaign=None):
    """Server-trusted capture of one referrer before first qualifying payment."""
    code = str(code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,20}", code):
        frappe.throw("Invalid referral code.")
    if not frappe.db.exists("Club100 Member", member):
        frappe.throw("Member does not exist.")
    existing = frappe.db.get_value("Club100 Referral Attribution", {"referred_member": member}, "name")
    if existing:
        existing_code = frappe.db.get_value("Club100 Referral Attribution", existing, "referral_code")
        if existing_code == code:
            return {"captured": True, "existing": True}
        frappe.throw("An existing referral cannot be overwritten.")
    if frappe.db.exists("Club100 Enrollment", {"member": member, "payment_status": "Paid"}):
        frappe.throw("Referral code must be submitted before the first paid enrollment.")
    # Do not accept a new referral after Razorpay checkout has started, even
    # if its payment callback has not yet marked the Enrollment as Paid.
    if frappe.db.sql(
        """SELECT name FROM `tabClub100 Enrollment`
           WHERE member=%s AND payment_status='Pending'
             AND COALESCE(payment_order_id,'')!='' LIMIT 1""", member
    ):
        frappe.throw("Referral must be claimed before starting payment.")
    partner_id = frappe.db.get_value("Club100 Referral Partner", {"partner_code": code, "status": "Active"}, "name")
    if not partner_id:
        frappe.throw("Referral code is not active.")
    partner = frappe.get_doc("Club100 Referral Partner", partner_id)
    if partner.member and partner.member == member:
        frappe.throw("Self-referrals are not permitted.")
    track = TRACK_AMBASSADOR if partner.partner_type == "Ambassador" else TRACK_MEMBER
    policy = _active_policy(track)
    if campaign:
        campaign_doc = frappe.get_doc("Club100 Referral Campaign", campaign)
        if campaign_doc.status != "Active":
            frappe.throw("Referral campaign is not active.")
        if campaign_doc.partner and campaign_doc.partner != partner.name:
            frappe.throw("Referral code and campaign do not match.")
    attribution = frappe.get_doc({
        "doctype": "Club100 Referral Attribution",
        "referred_member": member,
        "partner": partner.name,
        "referral_code": code,
        "reward_track": track,
        "policy": policy.name,
        "first_rate": policy.first_purchase_rate,
        "renewal_rate": policy.renewal_rate,
        "currency": policy.currency,
        "minimum_duration_months": policy.minimum_duration_months,
        "campaign": campaign,
        "source": source if source in ("Member PWA", "Website", "Ambassador Link", "Admin") else "Website",
        "captured_on": now_datetime(),
        "status": "Captured",
    })
    attribution.insert(ignore_permissions=True)
    return {"captured": True, "existing": False}


@frappe.whitelist(methods=["GET"])
def my_referral_dashboard():
    """Only aggregate referral and earnings information for the caller."""
    member = _member_for_request()
    partner = _find_my_partner(member)
    track = (TRACK_AMBASSADOR if partner and partner.partner_type == "Ambassador"
             else TRACK_MEMBER)
    program_enabled = bool(frappe.db.exists(
        "Club100 Referral Policy", {"reward_track": track, "enabled": 1}
    ))
    if not partner:
        return {"registered": False, "can_register": True,
                "program_enabled": program_enabled}
    referral_count = frappe.db.count(
        "Club100 Referral Attribution", {"partner": partner.name, "status": "Captured"}
    )
    summary = frappe.db.sql(
        """SELECT reward_track, status, COUNT(*) AS count,
                  COALESCE(SUM(reward_amount),0) AS amount
           FROM `tabClub100 Referral Reward`
           WHERE partner=%s GROUP BY reward_track, status""",
        (partner.name,), as_dict=True
    )
    return {
        "registered": True,
        "program_enabled": program_enabled,
        "code": partner.partner_code,
        "partner_type": partner.partner_type,
        "status": partner.status,
        "referral_count": referral_count,
        "reward_summary": summary,
    }


def foundation_healthcheck():
    """Read-only post-migration check, intended for bench execute."""
    names = (
        "Club100 Referral Partner",
        "Club100 Referral Policy",
        "Club100 Referral Campaign",
        "Club100 Referral Attribution",
        "Club100 Referral Reward",
    )
    return {
        "site": frappe.local.site,
        "doctypes": {name: bool(frappe.db.exists("DocType", name)) for name in names},
        "enabled_policies": frappe.db.count("Club100 Referral Policy", {"enabled": 1}),
        "note": "No credits, payouts, or payment hooks are installed in Foundation V1.",
    }


def quote_reward(net_amount, rate):
    """Pure calculation; DOES NOT write monetary liabilities or credits."""
    base = Decimal(str(net_amount))
    pct = Decimal(str(rate))
    if base <= 0 or pct < 0 or pct > 100:
        raise ValueError("Invalid eligible amount or percentage")
    return (base * pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
