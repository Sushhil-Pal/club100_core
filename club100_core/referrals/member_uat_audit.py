"""Read-only end-to-end Club100 referral purchase / reward / wallet audit.

Copy to apps/club100_core/club100_core/referrals/member_uat_audit.py
Run: bench --site club100.local execute club100_core.referrals.member_uat_audit.report \
        --kwargs '{"referred_member":"C100-MEM-00242"}'

No inserts, saves, locks, commits, scheduled jobs, or Razorpay network calls.
The existing checkout_audit validates submitted accounting vouchers and GL.
"""
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe.utils import cint, nowdate

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def amt(value):
    return Decimal(str(value if value not in (None, "") else 0)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def fmt(value):
    return str(amt(value))


def iso(value):
    return str(value) if value else None


def obj(doc, names):
    return {key: (fmt(doc.get(key)) if key in _MONEY_FIELDS else iso(doc.get(key)))
            for key in names}


_MONEY_FIELDS = {
    "amount_payable", "gross", "gateway_paid", "wallet_paid", "grand_total",
    "net_total", "total_taxes_and_charges", "outstanding_amount", "first_rate",
    "renewal_rate", "rate", "basis_amount", "reward_amount",
    "verified_basis_amount", "verified_reward_amount", "amount", "gateway_due",
    "promotion_discount", "funded_wallet_tender",
}


def report(referred_member, include_checkout_details=0, max_enrollments=10):
    """Admin-only, read-only. Never matures rewards or changes payment records."""
    actor = frappe.session.user
    if actor != "Administrator" and "System Manager" not in frappe.get_roles(actor):
        frappe.throw("Administrator / System Manager required", frappe.PermissionError)
    member_id = str(referred_member or "").strip()
    if not member_id or not frappe.db.exists("Club100 Member", member_id):
        raise ValueError("Referred member not found: " + member_id)

    from club100_core.referrals.settlement_logic import (
        maturity_day, verified_amount, net_gateway_share, date_value,
    )
    from club100_core.accounting.checkout_audit import audit as checkout_audit
    from club100_core.wallet.core import wallet_balance

    cap = min(max(cint(max_enrollments), 1), 30)
    hold_days = cint(frappe.conf.get("club100_referral_hold_days") or 30)
    today = date_value(nowdate())
    checks = []
    issues = []

    def check(label, ok, actual=None, expected=None, severity="FAIL"):
        state = "PASS" if ok else severity
        entry = {"check": label, "status": state}
        if actual is not None:
            entry["actual"] = actual
        if expected is not None:
            entry["expected"] = expected
        checks.append(entry)
        if state in ("FAIL", "WARN"):
            issues.append(entry)
        return ok

    m = frappe.get_doc("Club100 Member", member_id)
    output = {
        "site": frappe.local.site,
        "read_only": True,
        "referred_member": {"id": member_id, "full_name": m.get("full_name"),
                            "status": m.get("status"), "customer": m.get("customer"),
                            "contact": m.get("contact")},
        "referrer": None,
        "attribution": None,
        "policy_snapshot": None,
        "enrollments": [],
        "rewards": [],
        "referrer_referral_wallet": None,
        "checks": checks,
        "issues": issues,
        "notes": [
            "Read-only ERPNext DB and saved gateway evidence; Razorpay API is NOT contacted.",
            "A verified Approved reward is expected to have NO wallet grant until the hold period ends.",
            "Reward percentages apply to eligible GST-exclusive gateway-paid value, not list price or wallet tender.",
            "Check checkout audit warnings independently; the service-item warehouse warning does not invalidate a successful payment.",
        ],
    }

    # Attribution: one, immutable first-touch record expected for this member.
    attributions = frappe.get_all(
        "Club100 Referral Attribution", filters={"referred_member": member_id},
        fields=["name"], order_by="creation asc", limit_page_length=101,
    )
    check("Exactly one referral attribution", len(attributions) == 1,
          len(attributions), 1)
    attr = partner = None
    if attributions:
        attr = frappe.get_doc("Club100 Referral Attribution", attributions[0].name)
        output["attribution"] = obj(attr, [
            "name", "referred_member", "partner", "referral_code", "reward_track",
            "policy", "first_rate", "renewal_rate", "currency",
            "minimum_duration_months", "campaign", "source", "captured_on", "status",
        ])
        check("Attribution captured", attr.status == "Captured", attr.status, "Captured")
        check("Referral attribution belongs to referred member",
              attr.referred_member == member_id, attr.referred_member, member_id)
        if attr.get("partner") and frappe.db.exists("Club100 Referral Partner", attr.partner):
            partner = frappe.get_doc("Club100 Referral Partner", attr.partner)
            output["referrer"] = {
                "partner": partner.name, "code": partner.partner_code,
                "type": partner.partner_type, "partner_status": partner.status,
                "member": partner.member,
            }
            check("Attribution code matches partner", attr.referral_code == partner.partner_code,
                  attr.referral_code, partner.partner_code)
            check("No self-referral", partner.member != member_id,
                  partner.member, "must differ from " + member_id)
            expected_track = ("Ambassador Cash" if partner.partner_type == "Ambassador"
                              else "Member Credit")
            check("Referral reward track matches partner type",
                  attr.reward_track == expected_track, attr.reward_track, expected_track)
            check("Referrer account remains Active", partner.status == "Active",
                  partner.status, "Active", severity="WARN")
            if expected_track == "Member Credit":
                check("Referrer is a member advocate with a Member ID",
                      partner.partner_type == "Member Advocate" and bool(partner.member),
                      partner.partner_type + " / " + str(partner.member),
                      "Member Advocate / member")
        else:
            check("Referrer partner exists", False, attr.get("partner"), "existing partner")
        if attr.get("policy") and frappe.db.exists("Club100 Referral Policy", attr.policy):
            pol = frappe.get_doc("Club100 Referral Policy", attr.policy)
            output["policy_snapshot"] = {
                "policy": pol.name, "currently_enabled": bool(cint(pol.enabled)),
                "current_first_rate": fmt(pol.first_purchase_rate),
                "current_renewal_rate": fmt(pol.renewal_rate),
                "captured_first_rate": fmt(attr.first_rate),
                "captured_renewal_rate": fmt(attr.renewal_rate),
                "minimum_duration_months": cint(attr.minimum_duration_months),
                "currency": attr.currency,
            }
            check("Captured referral currency is INR", attr.currency == "INR",
                  attr.currency, "INR")
            # Historical rate snapshots may intentionally differ after policy edits.
        else:
            check("Referral policy exists", False, attr.get("policy"), "existing policy")

    check("Referral hold configuration within approved bounds", 14 <= hold_days <= 180,
          hold_days, "14–180 days")
    output["hold_days"] = hold_days

    enrollments = frappe.get_all("Club100 Enrollment", filters={"member": member_id},
        fields=["name"], order_by="creation asc", limit_page_length=cap + 1)
    check("At least one enrollment exists", bool(enrollments), len(enrollments), ">= 1")
    if len(enrollments) > cap:
        check("Enrollment audit not truncated", False, len(enrollments),
              "increase max_enrollments", severity="WARN")
    all_rewards = frappe.get_all("Club100 Referral Reward",
        filters={"referred_member": member_id}, fields=["name"],
        order_by="creation asc", limit_page_length=200)
    reward_docs = [frappe.get_doc("Club100 Referral Reward", r.name) for r in all_rewards]
    rewards_by_enrollment = {}
    for reward in reward_docs:
        rewards_by_enrollment.setdefault(reward.enrollment, []).append(reward)

    def grant_check(reward):
        from club100_core.referrals.settlement import REFERRAL_KEY_PREFIX
        key = REFERRAL_KEY_PREFIX + reward.name
        grants = frappe.get_all("Club100 Wallet Grant", filters={"source_event_key": key},
                                fields=["name"], limit_page_length=3)
        out = {"source_event_key": key, "grant_count": len(grants), "grant": None,
               "ledger": [], "remaining": None}
        check("Reward " + reward.name + " grant uniqueness", len(grants) <= 1,
              len(grants), "0 or 1")
        if not grants:
            check("Reward " + reward.name + " cannot be credited before maturity",
                  reward.status != "Available", reward.status,
                  "On Hold / Approved / Reversed before credit")
            check("Reward " + reward.name + " has no premature settlement reference",
                  not bool(reward.get("settlement_reference")),
                  reward.get("settlement_reference") or "(empty)", "(empty)")
            return out
        g = frappe.get_doc("Club100 Wallet Grant", grants[0].name)
        entries = frappe.get_all("Club100 Wallet Ledger",
            filters={"grant": g.name}, fields=["name", "entry_type", "amount", "event_key"],
            order_by="creation asc", limit_page_length=100)
        net = sum((amt(x.amount) for x in entries), ZERO)
        credit = sum((amt(x.amount) for x in entries if x.entry_type == "Credit"), ZERO)
        out["grant"] = obj(g, ["name", "member", "source_type", "source_event_key",
             "referral_reward", "amount", "status", "validity_type", "valid_from", "expires_on"])
        out["remaining"] = fmt(net)
        out["ledger"] = [dict(name=x.name, entry_type=x.entry_type,
                              amount=fmt(x.amount), event_key=x.event_key) for x in entries]
        check("Reward " + reward.name + " grant only for Available reward",
              reward.status == "Available", reward.status, "Available")
        check("Reward " + reward.name + " grant linked to referrer",
              bool(partner) and g.member == partner.member,
              g.member, partner.member if partner else "referrer member")
        check("Reward " + reward.name + " grant identity and amount",
              g.source_type == "Referral" and g.referral_reward == reward.name
              and amt(g.amount) == amt(reward.get("verified_reward_amount")),
              {"source": g.source_type, "reward": g.referral_reward,
               "amount": fmt(g.amount)},
              {"source": "Referral", "reward": reward.name,
               "amount": fmt(reward.get("verified_reward_amount"))})
        check("Reward " + reward.name + " settlement reference matches grant",
              reward.get("settlement_reference") == g.name,
              reward.get("settlement_reference"), g.name)
        check("Reward " + reward.name + " referral credit ledger",
              credit == amt(g.amount) and ZERO <= net <= amt(g.amount),
              {"credit": fmt(credit), "remaining": fmt(net)},
              {"credit": fmt(g.amount), "remaining": "between zero and initial credit"})
        return out

    today_text = today.isoformat()
    for item in enrollments[:cap]:
        e = frappe.get_doc("Club100 Enrollment", item.name)
        base = {"enrollment": e.name, "status": e.status, "payment_status": e.payment_status,
                "plan": e.get("plan"), "duration_months": cint(e.get("membership_duration_months")),
                "price": fmt(e.get("amount_payable")), "payment_id": e.get("payment_id"),
                "capture_verified": bool(cint(e.get("razorpay_capture_verified"))),
                "sales_invoice": e.get("sales_invoice"), "start_date": iso(e.get("start_date")),
                "payment_date": iso(e.get("payment_date")), "checkout_audit": None}
        output["enrollments"].append(base)
        if e.payment_status != "Paid":
            continue
        check("Enrollment " + e.name + " has verified Razorpay payment",
              e.payment_gateway == "Razorpay" and bool(e.payment_id)
              and bool(cint(e.get("razorpay_capture_verified"))),
              {"gateway": e.payment_gateway, "payment_id": e.payment_id,
               "verified": bool(cint(e.get("razorpay_capture_verified")))},
              "Razorpay / pay_ / capture_verified")
        check("Enrollment " + e.name + " meets referral minimum duration",
              not attr or cint(e.get("membership_duration_months")) >= cint(attr.minimum_duration_months),
              cint(e.get("membership_duration_months")),
              cint(attr.minimum_duration_months) if attr else "unknown")
        if attr and attr.get("captured_on") and e.get("payment_date"):
            check("Enrollment " + e.name + " referral recorded before payment",
                  str(attr.captured_on) <= str(e.payment_date),
                  {"captured": iso(attr.captured_on), "payment": iso(e.payment_date)},
                  "captured no later than first qualifying payment")
        # Use Club100's established full payment/invoice/GL auditor, not a duplicate implementation.
        try:
            c = checkout_audit(e.name)
            base["checkout_audit"] = {
                "overall": c.get("overall"), "counts": c.get("counts"),
                "non_pass_checks": [x for x in c.get("checks", []) if x.get("status") != "PASS"],
            }
            if cint((c.get("counts") or {}).get("FAIL")):
                check("Enrollment " + e.name + " financial accounting audit", False,
                      base["checkout_audit"], "no financial audit failures")
            elif cint((c.get("counts") or {}).get("WARN")):
                check("Enrollment " + e.name + " financial accounting audit", False,
                      base["checkout_audit"], "no financial audit warnings", "WARN")
            else:
                check("Enrollment " + e.name + " financial accounting audit", True,
                      base["checkout_audit"], "PASS")
            if cint(include_checkout_details):
                base["checkout_audit_full"] = c
        except Exception as exc:
            check("Enrollment " + e.name + " financial audit executable", False,
                  str(exc), "checkout audit runs")

        matched = rewards_by_enrollment.get(e.name, [])
        # A qualifying referral earns exactly one reward per verified Razorpay payment.
        if attr and attr.status == "Captured" and e.payment_gateway == "Razorpay" and e.payment_id \
                and cint(e.get("razorpay_capture_verified")) \
                and cint(e.get("membership_duration_months")) >= cint(attr.minimum_duration_months):
            check("Enrollment " + e.name + " has one referral reward", len(matched) == 1,
                  [r.name for r in matched], "one reward")
        elif matched:
            check("Enrollment " + e.name + " reward requires qualifying referral", False,
                  [r.name for r in matched], "qualifying verified purchase")

    for reward in reward_docs:
        row = obj(reward, ["name", "status", "reward_track", "reward_event", "event_key",
             "attribution", "partner", "referred_member", "enrollment", "payment_reference",
             "basis_amount", "rate", "reward_amount", "currency", "basis_verification_status",
             "basis_evidence_type", "verified_basis_amount", "verified_reward_amount",
             "finance_proof_reference", "eligible_on", "settlement_reference", "settled_on",
             "credit_validity_type", "credit_expires_on"])
        row["wallet_grant"] = None
        output["rewards"].append(row)
        check("Reward " + reward.name + " linked to attribution and partner",
              bool(attr) and reward.attribution == attr.name
              and reward.partner == attr.partner and reward.referred_member == member_id,
              {"attribution": reward.attribution, "partner": reward.partner,
               "member": reward.referred_member},
              {"attribution": attr.name if attr else "missing", "member": member_id})
        check("Reward " + reward.name + " track consistent with captured referral",
              bool(attr) and reward.reward_track == attr.reward_track,
              reward.reward_track, attr.reward_track if attr else "missing")
        enrolled = frappe.get_doc("Club100 Enrollment", reward.enrollment) \
            if frappe.db.exists("Club100 Enrollment", reward.enrollment) else None
        if not enrolled:
            check("Reward " + reward.name + " enrollment exists", False,
                  reward.enrollment, "existing enrollment")
            continue
        check("Reward " + reward.name + " payment event is unique and consistent",
              reward.event_key == "RPAY:" + str(enrolled.payment_id or "")
              and reward.payment_reference == enrolled.payment_id,
              {"event_key": reward.event_key, "payment_ref": reward.payment_reference},
              "RPAY:" + str(enrolled.payment_id or ""))
        dupes = frappe.get_all("Club100 Referral Reward", filters={"event_key": reward.event_key},
                               fields=["name"], limit_page_length=3)
        check("Reward " + reward.name + " payment idempotency", len(dupes) == 1,
              [x.name for x in dupes], [reward.name])
        if attr:
            prior_paid = frappe.get_all("Club100 Enrollment",
                filters={"member": member_id, "payment_status": "Paid"},
                fields=["name", "creation", "payment_date"],
                order_by="creation asc", limit_page_length=1000)
            this_time = str(enrolled.payment_date or enrolled.creation)
            has_prior = any(x.name != enrolled.name and
                (str(x.payment_date or x.creation) < this_time or
                 (str(x.payment_date or x.creation) == this_time and x.name < enrolled.name))
                 for x in prior_paid)
            expected_event = "Renewal" if has_prior else "First Purchase"
            check("Reward " + reward.name + " first purchase / renewal type",
                  reward.reward_event == expected_event, reward.reward_event, expected_event)
            expected_rate = attr.renewal_rate if has_prior else attr.first_rate
            check("Reward " + reward.name + " uses captured rate snapshot",
                  amt(reward.rate) == amt(expected_rate), fmt(reward.rate), fmt(expected_rate))
        if reward.status in ("Approved", "Available"):
            check("Reward " + reward.name + " verified before settlement",
                  reward.basis_verification_status == "Verified"
                  and amt(reward.get("verified_basis_amount")) > 0,
                  {"basis_status": reward.basis_verification_status,
                   "verified_basis": fmt(reward.get("verified_basis_amount"))},
                  "Verified / positive verified basis")
            invoice = None
            if enrolled.sales_invoice and frappe.db.exists("Sales Invoice", enrolled.sales_invoice):
                invoice = frappe.get_doc("Sales Invoice", enrolled.sales_invoice)
            if invoice and invoice.docstatus == 1 and amt(invoice.grand_total) > 0:
                hybrid = frappe.db.get_value("Club100 Hybrid Checkout",
                    {"enrollment": enrolled.name},
                    ["status", "gateway_due"], as_dict=True)
                gateway = amt(hybrid.gateway_due) if hybrid else (
                    amt(enrolled.amount_payable) - amt(enrolled.get("wallet_applied_amount")))
                basis = net_gateway_share(invoice.net_total, invoice.grand_total, gateway)
                check("Reward " + reward.name + " verified tax-exclusive gateway basis",
                      amt(reward.verified_basis_amount) == amt(basis),
                      fmt(reward.verified_basis_amount), fmt(basis))
                if reward.basis_evidence_type == "Submitted Invoice":
                    check("Reward " + reward.name + " evidence points to invoice",
                          reward.finance_proof_reference == "AUTO:INVOICE:" + invoice.name,
                          reward.finance_proof_reference, "AUTO:INVOICE:" + invoice.name)
                expected_reward = verified_amount(basis, reward.rate, gateway)
                check("Reward " + reward.name + " verified reward amount",
                      amt(reward.verified_reward_amount) == amt(expected_reward),
                      fmt(reward.verified_reward_amount), fmt(expected_reward))
            else:
                check("Reward " + reward.name + " has submitted invoice evidence",
                      False, enrolled.sales_invoice, "submitted invoice")
            if 14 <= hold_days <= 180:
                expected_day = maturity_day(enrolled.payment_date or enrolled.creation,
                                            enrolled.start_date or enrolled.enrollment_date or enrolled.creation,
                                            hold_days)
                check("Reward " + reward.name + " maturity date matches hold policy",
                      bool(reward.eligible_on) and date_value(reward.eligible_on) == expected_day,
                      iso(reward.eligible_on), str(expected_day))
                if reward.status == "Approved" and date_value(reward.eligible_on) <= today:
                    check("Reward " + reward.name + " ready for maturity processing",
                          False, iso(reward.eligible_on), "> " + today_text,
                          severity="WARN")
        if reward.status not in ("Approved", "Available", "On Hold", "Reversed"):
            check("Reward " + reward.name + " status expected", False,
                  reward.status, "Approved / Available / On Hold / Reversed", "WARN")
        if reward.status == "On Hold":
            check("Reward " + reward.name + " awaiting automatic basis verification",
                  False, "On Hold", "Approved after successful verified invoice checks", "WARN")
            check("Reward " + reward.name + " not yet verified/credited",
                  reward.basis_verification_status != "Verified"
                  and not reward.settlement_reference,
                  {"verification": reward.basis_verification_status,
                   "grant": reward.settlement_reference}, "unverified / no credit")
        row["wallet_grant"] = grant_check(reward)

    if partner and partner.member and partner.partner_type == "Member Advocate":
        try:
            balance = wallet_balance(partner.member)
            output["referrer_referral_wallet"] = {
                "member": partner.member,
                "referral_spendable": str((balance.get("by_source") or {}).get("Referral", "0.00")),
                "referral_grants": [g for g in balance.get("grants", [])
                                    if g.get("source") == "Referral"],
            }
        except Exception as exc:
            check("Referrer wallet balance readable", False, str(exc), "balance available")
    output["counts"] = {
        "PASS": sum(x["status"] == "PASS" for x in checks),
        "WARN": sum(x["status"] == "WARN" for x in checks),
        "FAIL": sum(x["status"] == "FAIL" for x in checks),
    }
    output["overall"] = ("FAIL" if output["counts"]["FAIL"] else
                         "WARN" if output["counts"]["WARN"] else "PASS")
    return output
