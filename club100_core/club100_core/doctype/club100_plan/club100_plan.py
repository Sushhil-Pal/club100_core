import re

import frappe
from frappe.model.document import Document


class Club100Plan(Document):
    def validate(self):
        self.normalize_plan_code()
        self.validate_delivery_configuration()
        self.validate_workout_access_window()
        self.validate_self_workout_configuration()

    def normalize_plan_code(self):
        self.plan_code = str(
            self.plan_code or ""
        ).strip().lower()

        if not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*",
            self.plan_code,
        ):
            frappe.throw(
                "Plan Code must contain only lowercase letters, "
                "numbers and hyphens."
            )

    def validate_delivery_configuration(self):
        capacity = int(
            self.max_session_capacity or 0
        )

        if self.delivery_type == "Recorded":
            if capacity != 0:
                frappe.throw(
                    "Recorded plans must have Max Session Capacity = 0."
                )

            if self.coach_led:
                frappe.throw(
                    "Recorded plans cannot be marked Coach Led."
                )

        elif self.delivery_type == "Live Online":
            if capacity <= 0:
                frappe.throw(
                    "Live Online plans must have a Max Session Capacity "
                    "greater than 0."
                )

            if not self.coach_led:
                frappe.throw(
                    "Live Online plans must be marked Coach Led."
                )

    def validate_workout_access_window(self):
        minutes = int(
            self.workout_access_after_minutes or 0
        )

        if minutes < 0:
            frappe.throw(
                "Workout Access After Session (Minutes) cannot be negative."
            )

    def validate_self_workout_configuration(self):
        from frappe.utils import cint

        count = cint(self.self_workouts_per_week)
        if count < 0 or count > 7:
            frappe.throw("Self Workouts Per Week must be between 0 and 7.")

        if self.plan_code != "self":
            if count or self.self_workout_cycle:
                frappe.throw("Self workout configuration is only valid for the Self plan.")
            return

        # Allow Self plans to be configured incrementally during Phase 1.
        if self.self_workout_cycle:
            if not 1 <= count <= 7:
                frappe.throw("Set Self Workouts Per Week (1–7) before selecting a cycle.")
            cycle = frappe.db.get_value(
                "Club100 Self Workout Cycle",
                self.self_workout_cycle,
                ["plan", "active"],
                as_dict=True,
            )
            if not cycle or cycle.plan != self.name:
                frappe.throw("Self Workout Cycle must belong to this Plan.")
            if not cycle.active:
                frappe.throw("Selected Self Workout Cycle must be active.")
