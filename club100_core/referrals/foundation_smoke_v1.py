"""Club100 referral foundation local-only, rollback-only integration smoke suite.

Install this file under club100_core/referrals/; invoke with bench execute.

No permanent data writes are intended. The suite uses transaction savepoints
and never calls commit. It refuses to run outside club100.local.
"""
from __future__ import annotations

import os
import secrets
from decimal import Decimal

import frappe
from frappe.utils import now_datetime

from club100_core.referrals import service

DOCTYPES = (
    "Club100 Referral Partner",
    "Club100 Referral Policy",
    "Club100 Referral Campaign",
    "Club100 Referral Attribution",
    "Club100 Referral Reward",
)


def _local_only():
    if frappe.local.site != "club100.local":
        raise RuntimeError("Refusing to run referral smoke tests outside club100.local")


def _test_tag():
    return "C1T" + secrets.token_hex(4).upper()


def _candidate_members():
    """Use existing local member records; don't create Users or change profiles."""
    return frappe.db.sql(
        """SELECT m.name, m.`user` AS user_id
           FROM `tabClub100 Member` m
           INNER JOIN `tabUser` u ON u.name = m.`user`
           WHERE COALESCE(m.`user`, '') NOT IN ('', 'Guest', 'Administrator')
             AND u.enabled = 1
             AND NOT EXISTS (
                 SELECT 1 FROM `tabClub100 Referral Partner` p WHERE p.member = m.name
             )
             AND NOT EXISTS (
                 SELECT 1 FROM `tabClub100 Referral Attribution` a WHERE a.referred_member = m.name
             )
             AND NOT EXISTS (
                 SELECT 1 FROM `tabClub100 Enrollment` e
                 WHERE e.member = m.name AND e.payment_status = 'Paid'
             )
           ORDER BY m.creation DESC LIMIT 6""",
        as_dict=True,
    )


def inspect():
    """Read-only prerequisite inspection; does not expose member identities."""
    _local_only()
    missing = [dt for dt in DOCTYPES if not frappe.db.exists("DocType", dt)]
    if missing:
        return {"ready": False, "reason": "Missing DocTypes", "missing": missing}
    try:
        members = _candidate_members()
    except Exception as exc:
        return {
            "ready": False,
            "reason": "Unable to inspect candidate member records",
            "exception_type": type(exc).__name__,
        }
    return {
        "site": frappe.local.site,
        "ready": True,
        "existing_eligible_member_count_capped_at_6": len(members),
        "member_api_scenarios_possible": len(members) >= 2,
        "enabled_member_policies": frappe.db.count(
            "Club100 Referral Policy", {"reward_track": "Member Credit", "enabled": 1}
        ),
        "enabled_ambassador_policies": frappe.db.count(
            "Club100 Referral Policy", {"reward_track": "Ambassador Cash", "enabled": 1}
        ),
        "note": "Read-only; no database changes.",
    }


def _assert_rejected(fn, label, results):
    try:
        fn()
    except Exception as exc:
        results.append({"case": label, "result": "PASS", "rejected_as": type(exc).__name__})
        if hasattr(frappe, "clear_messages"):
            frappe.clear_messages()
        return
    raise AssertionError(f"{label}: expected rejection, but operation succeeded")


def _pass(results, label, condition):
    if not condition:
        raise AssertionError(f"{label}: assertion failed")
    results.append({"case": label, "result": "PASS"})


def _policy(track, tag, results):
    """Use an existing enabled policy if present, otherwise make a rollback-only one."""
    name = frappe.db.get_value(
        "Club100 Referral Policy", {"reward_track": track, "enabled": 1}, "name"
    )
    if name:
        return frappe.get_doc("Club100 Referral Policy", name)
    doc = frappe.get_doc({
        "doctype": "Club100 Referral Policy",
        "policy_code": f"{tag}-{'MC' if track == 'Member Credit' else 'AC'}",
        "reward_track": track,
        "enabled": 1,
        "first_purchase_rate": 10 if track == "Member Credit" else 12,
        "renewal_rate": 2,
        "minimum_duration_months": 3,
        "currency": "INR",
    })
    doc.insert(ignore_permissions=True)
    results.append({"case": "Temporary test policy for " + track, "result": "PASS"})
    return doc


def _partner(code, kind, member=None, status="Active", accepted=False):
    record = {
        "doctype": "Club100 Referral Partner",
        "partner_code": code,
        "partner_type": kind,
        "member": member,
        "display_name": "Automated Referral Smoke Test",
        "status": status,
    }
    if accepted:
        record["agreement_accepted_on"] = now_datetime()
    doc = frappe.get_doc(record)
    doc.insert(ignore_permissions=True)
    return doc


def _member_user(member_record):
    frappe.set_user(member_record["user_id"])


def _test_suite(results, eligible_members):
    # The test has no payment hooks; these calculations are pure.
    _pass(results, "Currency-safe percentage rounding",
          service.quote_reward("10788", "12") == Decimal("1294.56")
          and service.quote_reward("799", "2") == Decimal("15.98"))
    _assert_rejected(lambda: service.quote_reward("100", "-1"), "Reject negative commission", results)
    _assert_rejected(lambda: service.quote_reward("0", "2"), "Reject zero-amount reward basis", results)
    _pass(results, "Malformed codes rejected", service.validate_referral_code("bad-code!") == {"valid": False})

    tag = _test_tag()
    member_policy = _policy("Member Credit", tag, results)
    cash_policy = _policy("Ambassador Cash", tag, results)
    _assert_rejected(
        lambda: frappe.get_doc({
            "doctype": "Club100 Referral Policy", "policy_code": tag + "-DUP",
            "reward_track": "Member Credit", "enabled": 1,
            "first_purchase_rate": 5, "renewal_rate": 1,
            "minimum_duration_months": 1, "currency": "INR",
        }).insert(ignore_permissions=True),
        "Prevent second active member policy", results,
    )

    ambassador_code = tag + "A"
    _assert_rejected(
        lambda: _partner(ambassador_code, "Ambassador", accepted=False),
        "Ambassador agreement required before activation", results,
    )
    ambassador = _partner(ambassador_code, "Ambassador", accepted=True)
    _pass(results, "Approved external ambassador can be active",
          ambassador.status == "Active" and not ambassador.member
          and bool(ambassador.approved_on))
    _pass(results, "Active ambassador code validates publicly",
          service.validate_referral_code(ambassador_code) == {"valid": True})
    ambassador.status = "Suspended"
    ambassador.save(ignore_permissions=True)
    _pass(results, "Suspended ambassador code invalid",
          service.validate_referral_code(ambassador_code) == {"valid": False})
    ambassador.status = "Active"
    ambassador.save(ignore_permissions=True)
    _pass(results, "Reactivated ambassador code valid",
          service.validate_referral_code(ambassador_code) == {"valid": True})

    _assert_rejected(
        lambda: frappe.get_doc({
            "doctype": "Club100 Referral Campaign", "campaign_code": tag + "-BAD",
            "title": "Invalid date order", "campaign_type": "Influencer",
            "status": "Active", "starts_on": "2026-10-30", "ends_on": "2026-10-29",
        }).insert(ignore_permissions=True),
        "Campaign rejects end before start", results,
    )
    campaign = frappe.get_doc({
        "doctype": "Club100 Referral Campaign", "campaign_code": tag + "-CAM",
        "title": "Referral smoke test", "campaign_type": "Influencer",
        "partner": ambassador.name, "status": "Active",
    })
    campaign.insert(ignore_permissions=True)
    _pass(results, "Campaign linked to ambassador", campaign.partner == ambassador.name)

    original_user = frappe.session.user
    try:
        frappe.set_user("Guest")
        _assert_rejected(service.ensure_my_referral_code, "Guest cannot generate referral code", results)
        _assert_rejected(lambda: service.claim_my_referral(ambassador_code),
                         "Guest cannot claim a referral", results)
    finally:
        frappe.set_user(original_user)

    if len(eligible_members) < 2:
        results.append({
            "case": "Member code generation and full attribution scenarios",
            "result": "SKIP", "reason": "Requires two existing eligible local members; no real profiles modified",
        })
        return

    a, b = eligible_members[0], eligible_members[1]
    try:
        _member_user(a)
        issue1 = service.ensure_my_referral_code()
        issue2 = service.ensure_my_referral_code()
        code = issue1["code"]
        _pass(results, "Member referral code is stable and advocate-only",
              code == issue2["code"] and issue1["partner_type"] == "Member Advocate")
        _pass(results, "Member code validates", service.validate_referral_code(code) == {"valid": True})
        _assert_rejected(lambda: service.claim_my_referral(code), "Self-referral denied", results)

        _member_user(b)
        first = service.claim_my_referral(code)
        again = service.claim_my_referral(code)
        _pass(results, "Direct referral captured and repeated claim idempotent",
              first == {"captured": True, "existing": False}
              and again == {"captured": True, "existing": True})
        _assert_rejected(lambda: service.claim_my_referral(ambassador_code),
                         "Cannot overwrite another referrer", results)
        attribution_name = frappe.db.get_value("Club100 Referral Attribution", {"referred_member": b["name"]}, "name")
        attribution = frappe.get_doc("Club100 Referral Attribution", attribution_name)
        snapshot_matches = (
            attribution.reward_track == "Member Credit"
            and attribution.partner == code
            and attribution.referral_code == code
            and attribution.policy == member_policy.name
            and Decimal(str(attribution.first_rate)) == Decimal(str(member_policy.first_purchase_rate))
            and Decimal(str(attribution.renewal_rate)) == Decimal(str(member_policy.renewal_rate))
        )
        if not snapshot_matches:
            raise AssertionError(
                "Member credit snapshot mismatch: "
                f"track={attribution.reward_track!r}; "
                f"partner_matches={attribution.partner == code}; "
                f"code_matches={attribution.referral_code == code}; "
                f"policy_matches={attribution.policy == member_policy.name}; "
                f"first_rate={attribution.first_rate!r} expected={member_policy.first_purchase_rate!r}; "
                f"renewal_rate={attribution.renewal_rate!r} expected={member_policy.renewal_rate!r}"
            )
        _pass(results, "Member credit and rates snapshotted on attribution", True)
        enrollment_name = frappe.db.get_value(
            "Club100 Enrollment", {"member": b["name"]}, "name"
        )
        if enrollment_name:
            reward_event_key = tag + "-EVENT"
            reward = frappe.get_doc({
                "doctype": "Club100 Referral Reward",
                "event_key": reward_event_key,
                "attribution": attribution.name,
                "partner": attribution.partner,
                "referred_member": b["name"],
                "enrollment": enrollment_name,
                "payment_reference": "LOCAL-SMOKE-NO-PAYMENT",
                "reward_track": "Member Credit",
                "reward_event": "First Purchase",
                "basis_amount": 100,
                "rate": 10,
                "reward_amount": 10,
                "currency": "INR",
                "status": "Pending",
            })
            reward.insert(ignore_permissions=True)
            _pass(results, "Reward ledger accepts pending test record", reward.name == reward_event_key)
            invalid_reward = frappe.get_doc("Club100 Referral Reward", reward.name)
            invalid_reward.reward_amount = 99
            _assert_rejected(lambda: invalid_reward.save(ignore_permissions=True),
                             "Reward financial fields immutable", results)
            settled = frappe.get_doc("Club100 Referral Reward", reward.name)
            settled.status = "Redeemed"
            settled.save(ignore_permissions=True)
            reopened = frappe.get_doc("Club100 Referral Reward", reward.name)
            reopened.status = "Pending"
            _assert_rejected(lambda: reopened.save(ignore_permissions=True),
                             "Settled cash/credit rewards cannot reopen", results)
            _assert_rejected(lambda: frappe.get_doc({
                "doctype": "Club100 Referral Reward",
                "event_key": reward_event_key,
                "attribution": attribution.name,
                "partner": attribution.partner,
                "referred_member": b["name"],
                "enrollment": enrollment_name,
                "payment_reference": "LOCAL-SMOKE-NO-PAYMENT",
                "reward_track": "Member Credit",
                "reward_event": "First Purchase",
                "basis_amount": 100, "rate": 10, "reward_amount": 10,
                "currency": "INR", "status": "Pending",
            }).insert(ignore_permissions=True), "Duplicate reward event key denied", results)
        else:
            results.append({"case": "Reward ledger lifecycle tests", "result": "SKIP",
                            "reason": "No existing enrollment for test member"})

        paid_member = frappe.db.sql(
            """SELECT m.name FROM `tabClub100 Member` m
               WHERE EXISTS (SELECT 1 FROM `tabClub100 Enrollment` e
                             WHERE e.member=m.name AND e.payment_status='Paid')
                 AND NOT EXISTS (SELECT 1 FROM `tabClub100 Referral Attribution` a
                                 WHERE a.referred_member=m.name)
               LIMIT 1""", as_dict=True,
        )
        if paid_member:
            _assert_rejected(
                lambda: service.capture_referral_from_trusted_flow(
                    paid_member[0]["name"], code, source="Admin"),
                "Reject referral captured after paid enrollment", results,
            )
        else:
            results.append({"case": "Paid-enrollment late referral rejection", "result": "SKIP",
                            "reason": "No paid member without existing referral attribution"})

        bad = frappe.get_doc("Club100 Referral Attribution", attribution.name)
        bad.first_rate = float(bad.first_rate) + 1
        _assert_rejected(lambda: bad.save(ignore_permissions=True),
                         "Captured financial rates are immutable", results)

        # Use a third eligible existing member for the ambassador cash track.
        if len(eligible_members) >= 3:
            c = eligible_members[2]
            _member_user(c)
            captured = service.claim_my_referral(ambassador_code)
            cash_attr_name = frappe.db.get_value("Club100 Referral Attribution", {"referred_member": c["name"]}, "name")
            cash_attr = frappe.get_doc("Club100 Referral Attribution", cash_attr_name)
            _pass(results, "Ambassador attribution uses cash track",
                  captured["captured"] and cash_attr.reward_track == "Ambassador Cash"
                  and Decimal(str(cash_attr.first_rate)) == Decimal(str(cash_policy.first_purchase_rate)))
        else:
            results.append({"case": "Cash attribution through member API", "result": "SKIP",
                            "reason": "Requires third eligible local member"})
    finally:
        frappe.set_user(original_user)


def run():
    """Run integration checks and rollback all created/changed records.

    Requires CLUB100_ALLOW_REFERRAL_TEST=1, and site club100.local.
    Intended for bench execute; do not call from a web request.
    """
    _local_only()
    if os.environ.get("CLUB100_ALLOW_REFERRAL_TEST") != "1":
        raise RuntimeError("Refusing to run: set CLUB100_ALLOW_REFERRAL_TEST=1")
    info = inspect()
    if not info.get("ready"):
        return {"outcome": "NOT_RUN", "preflight": info}

    old_user = frappe.session.user
    savepoint = "club100_referral_smoke_test"
    before = {dt: frappe.db.count(dt) for dt in DOCTYPES}
    results = []
    err = None
    try:
        frappe.db.savepoint(savepoint)
        try:
            _test_suite(results, _candidate_members())
        except Exception as exc:
            err = {"type": type(exc).__name__, "message": str(exc)[:250]}
        finally:
            frappe.set_user(old_user)
            frappe.db.rollback(save_point=savepoint)
    finally:
        frappe.set_user(old_user)
    after = {dt: frappe.db.count(dt) for dt in DOCTYPES}
    unchanged = before == after
    skipped = sum(r["result"] == "SKIP" for r in results)
    return {
        "site": frappe.local.site,
        "outcome": "FAIL" if err is not None or not unchanged else ("PARTIAL" if skipped else "PASS"),
        "passed": sum(r["result"] == "PASS" for r in results),
        "skipped": skipped,
        "cases": results,
        "error": err,
        "rollback_verified_by_record_counts": unchanged,
        "note": "No payment, membership credit, or payout APIs invoked.",
    }
