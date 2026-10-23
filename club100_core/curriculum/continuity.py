"""Explicit read-only continuity diagnostics; no writes."""
import frappe
from club100_core.curriculum.self_schedule import previous_self_slot_count


def check_self_continuity(enrollment_id):
    row = frappe.db.get_value(
        "Club100 Enrollment", enrollment_id,
        ["name", "member", "program", "start_date", "end_date", "plan"], as_dict=True)
    if not row:
        frappe.throw("Enrollment not found", frappe.DoesNotExistError)
    if not row.plan or frappe.db.get_value("Club100 Plan", row.plan, "plan_code") != "self":
        frappe.throw("Not a Self enrollment", frappe.ValidationError)
    offset = previous_self_slot_count(row)
    journey = frappe.db.get_value("Club100 Member Training Journey", row.member,
                                  ["name", "level_code", "program"], as_dict=True)
    return {"enrollment": row.name, "priorScheduledSlots": offset,
            "nextCycle": offset // 48 + 1, "nextWeek": (offset % 48) // 4 + 1,
            "nextSession": offset % 4 + 1,
            "journeyLevel": journey.level_code if journey else None,
            "journeyProgram": journey.program if journey else None}
