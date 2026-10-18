"""One 14-day Self trial per member, independent of payment and membership."""
import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate
from club100_core.api.member import _get_current_member
from club100_core.self_workout_cycle import get_active_self_workout_cycle


def _trial_context(member):
    used = frappe.db.get_value(
        "Club100 Enrollment", {"member": member.name, "is_trial": 1},
        ["name", "program", "status", "start_date", "end_date", "trial_status"],
        order_by="creation desc", as_dict=True,
    )
    if used:
        return {"eligible": False, "reason": "Your free Self trial has already been used.",
                "trialEnrollmentId": used.name, "trialEndDate": used.end_date,
                "trialStatus": ("Converted" if used.trial_status == "Converted" else
                                "Expired" if used.end_date and getdate(used.end_date) < getdate(nowdate())
                                else used.trial_status or used.status)}
    if member.status != "Active":
        return {"eligible": False, "reason": "Your Club100 member account is not active."}
    if member.onboarding_status != "Completed":
        return {"eligible": False, "reason": "Complete your member profile first."}
    return {"eligible": True, "durationDays": 14, "planCode": "self"}


@frappe.whitelist(methods=["GET"])
def self_trial_context():
    return _trial_context(_get_current_member())


@frappe.whitelist(methods=["POST"])
def activate_self_trial(program):
    member = _get_current_member()
    # Lock the member row to serialize simultaneous requests by this member.
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member.name)
    state = _trial_context(member)
    if not state["eligible"]:
        frappe.throw(state["reason"], frappe.ValidationError)
    program = str(program or "").strip()
    if not frappe.db.exists("Club100 Program", program):
        frappe.throw("Select a valid Club100 fitness program.", frappe.ValidationError)
    plan = frappe.db.get_value("Club100 Plan", {"plan_code": "self", "status": "Active"}, "name")
    if not plan:
        frappe.throw("The Self plan is not available.", frappe.ValidationError)
    get_active_self_workout_cycle(program, plan)
    today = getdate(nowdate())
    # Inclusive day range: activation day + 13 more calendar days.
    end = add_days(today, 13)
    doc = frappe.get_doc({
        "doctype": "Club100 Enrollment", "member": member.name,
        "program": program, "plan": plan, "pricing_model": "Online Plan",
        "enrollment_type": "Individual", "status": "Active",
        "payment_status": "Not Required", "enrollment_date": today,
        "start_date": today, "end_date": end,
        # Required legacy numeric field; trial validity is ALWAYS date-based.
        "membership_duration_months": 1,
        "is_trial": 1, "trial_status": "Active",
        "trial_activated_on": now_datetime(),
        "website_source": "Free Self Trial",
    })
    doc.insert(ignore_permissions=True)
    return {"enrollmentId": doc.name, "program": program, "plan": plan,
            "trial": True, "startDate": str(today), "endDate": str(end),
            "durationDays": 14, "scheduleRequired": True}


@frappe.whitelist(methods=["GET"])
def self_trial_programs():
    """Public-to-logged-in-member read of configured Self trial programs."""
    _get_current_member()
    plan = frappe.db.get_value("Club100 Plan", {"plan_code": "self", "status": "Active"}, "name")
    if not plan:
        return {"programs": []}
    cycles = frappe.get_all(
        "Club100 Self Workout Cycle",
        filters={"plan": plan, "active": 1},
        fields=["program"],
    )
    ids = sorted({row.program for row in cycles if row.program})
    if not ids:
        return {"programs": []}
    rows = frappe.get_all(
        "Club100 Program",
        filters={"name": ["in", ids]},
        fields=["name", "program_name"],
        order_by="program_name asc",
    )
    return {"programs": [{"id": row.name, "name": row.program_name or row.name} for row in rows]}
