"""Member-owned, currently valid enrollment context for concurrent memberships."""
import frappe
from frappe.utils import getdate, nowdate
from club100_core.api.member import _get_current_member


def _active_memberships(member_name):
    today = getdate(nowdate())
    rows = frappe.get_all(
        "Club100 Enrollment",
        filters={"member": member_name, "status": "Active"},
        fields=["name", "member", "program", "plan", "pricing_model", "cohort",
                "status", "start_date", "end_date", "is_trial", "trial_status",
                "completion_percentage"],
        order_by="start_date desc, creation desc",
    )
    valid = []
    for row in rows:
        if row.start_date and getdate(row.start_date) > today:
            continue
        if row.end_date and getdate(row.end_date) < today:
            continue
        valid.append(row)
    return valid


def resolve_active_enrollment(member_name, enrollment_id):
    """Deny access unless the requested enrollment belongs to this member and is valid."""
    if not enrollment_id or not isinstance(enrollment_id, str):
        frappe.throw("An enrollment ID is required.", frappe.ValidationError)
    for row in _active_memberships(member_name):
        if row.name == enrollment_id:
            return row
    frappe.throw("Membership is unavailable or does not belong to this member.", frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def get_my_enrollments():
    member = _get_current_member()
    result = []
    for row in _active_memberships(member.name):
        plan_code = (frappe.db.get_value("Club100 Plan", row.plan, "plan_code")
                     if row.plan else None)
        result.append({
            "enrollmentId": row.name,
            "program": row.program,
            "plan": row.plan,
            "planCode": plan_code,
            "pricingModel": row.pricing_model,
            "cohortId": row.cohort,
            "status": row.status,
            "isTrial": bool(row.is_trial),
            "trialStatus": row.trial_status if row.is_trial else None,
            "startDate": str(row.start_date) if row.start_date else None,
            "endDate": str(row.end_date) if row.end_date else None,
        })
    return {"memberships": result}
