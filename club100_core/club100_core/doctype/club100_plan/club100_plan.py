import re

import frappe
from frappe.model.document import Document


class Club100Plan(Document):
    def validate(self):
        self.normalize_plan_code()
        self.validate_delivery_configuration()
        self.validate_workout_access_window()

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
