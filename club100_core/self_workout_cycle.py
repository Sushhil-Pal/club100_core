"""Resolve active Self workout cycles by enrollment Program + Plan."""
import frappe


def get_active_self_workout_cycle(program, plan):
    if not program or not plan:
        frappe.throw("Program and Plan are required for Self workouts", frappe.ValidationError)
    name = frappe.db.get_value(
        "Club100 Self Workout Cycle",
        {"program": program, "plan": plan, "active": 1},
        "name",
    )
    if not name:
        frappe.throw(
            "No active Self Workout Cycle is configured for this Program and Plan.",
            frappe.ValidationError,
        )
    return frappe.get_doc("Club100 Self Workout Cycle", name)
