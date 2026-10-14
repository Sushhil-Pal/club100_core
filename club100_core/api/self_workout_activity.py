"""Activity persistence for member-specific virtual Self workouts.

Scheduled workouts remain virtual. Only actual starts create documents.
"""
import frappe
from frappe.utils import now_datetime

from club100_core.api.member import _get_current_member
from club100_core.api.self_workouts import _enrollment, virtual_workout_detail


def _owned_workout(workout_id):
    parts = str(workout_id or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        frappe.throw("Invalid virtual workout ID", frappe.ValidationError)
    member = _get_current_member()
    enrollment = _enrollment(member, parts[0])
    return member, enrollment, virtual_workout_detail(workout_id)


def _row(member, workout_id):
    name = frappe.db.get_value("Club100 Self Workout Activity", {
        "member": member.name, "workout_id": workout_id,
    }, "name")
    return frappe.get_doc("Club100 Self Workout Activity", name) if name else None


def _payload(doc):
    return {
        "name": doc.name, "workoutId": doc.workout_id,
        "status": doc.status,
        "startedAt": str(doc.started_at) if doc.started_at else None,
        "completedAt": str(doc.completed_at) if doc.completed_at else None,
    }


@frappe.whitelist(methods=["POST"])
def start_self_workout(workout_id):
    """Start only during access window; repeated starts are idempotent."""
    member, enrollment, workout = _owned_workout(workout_id)
    existing = _row(member, workout_id)
    if existing:
        return _payload(existing)
    if workout["accessState"] != "Available":
        frappe.throw("Workout is not currently available", frappe.PermissionError)
    doc = frappe.get_doc({
        "doctype": "Club100 Self Workout Activity",
        "member": member.name,
        "enrollment": enrollment.name,
        "workout_id": workout["id"],
        "workout_content": workout["workoutContent"],
        "scheduled_at": workout["availableFrom"].replace("T", " "),
        "started_at": now_datetime(),
        "status": "Started",
    })
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # A concurrent request inserted the unique workout_id first.
        doc = _row(member, workout_id)
        if not doc:
            raise
    return _payload(doc)


@frappe.whitelist(methods=["POST"])
def complete_self_workout(workout_id):
    """Complete a previously started workout; completion is idempotent.

    The workout may be completed after its video access window ends.
    """
    member, enrollment, workout = _owned_workout(workout_id)
    # Serialize concurrent completions without creating phantom records.
    name = frappe.db.get_value("Club100 Self Workout Activity", {
        "member": member.name, "enrollment": enrollment.name,
        "workout_id": workout_id,
    }, "name")
    if not name:
        frappe.throw("Start the workout before marking it completed", frappe.ValidationError)
    frappe.db.sql("SELECT name FROM `tabClub100 Self Workout Activity` WHERE name=%s FOR UPDATE", name)
    doc = frappe.get_doc("Club100 Self Workout Activity", name)
    if doc.status == "Started":
        doc.status = "Completed"
        doc.completed_at = now_datetime()
        doc.save(ignore_permissions=True)
    return _payload(doc)


@frappe.whitelist(methods=["GET"])
def get_self_workout_activity(enrollment_id):
    """Activity for the signed-in member's active Self enrollment."""
    member = _get_current_member()
    enrollment = _enrollment(member, enrollment_id)
    rows = frappe.get_all("Club100 Self Workout Activity",
        filters={"member": member.name, "enrollment": enrollment.name},
        fields=["name", "workout_id", "status", "started_at", "completed_at"],
        order_by="scheduled_at desc", limit_page_length=500)
    activities = [{
        "name": r.name, "workoutId": r.workout_id,
        "status": r.status,
        "startedAt": str(r.started_at) if r.started_at else None,
        "completedAt": str(r.completed_at) if r.completed_at else None,
    } for r in rows]
    return {"enrollment": enrollment.name, "activities": activities,
            "started": sum(r["status"] == "Started" for r in activities),
            "completed": sum(r["status"] == "Completed" for r in activities)}
