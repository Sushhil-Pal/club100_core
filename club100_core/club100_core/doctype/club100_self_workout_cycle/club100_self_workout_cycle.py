import frappe
from frappe.model.document import Document
from frappe.utils import cint


class Club100SelfWorkoutCycle(Document):
    def validate(self):
        self.validate_plan_configuration()
        self.validate_active_cycle_uniqueness()
        self.normalize_sequence()
        self.validate_workouts()

    def validate_plan_configuration(self):
        plan = frappe.db.get_value(
            "Club100 Plan",
            self.plan,
            [
                "name",
                "plan_code",
            ],
            as_dict=True,
        )

        if not plan:
            frappe.throw(
                "Selected Club100 Plan was not found."
            )

        if plan.plan_code != "self":
            frappe.throw(
                "Self Workout Cycles can only be linked to the Self plan."
            )

    def validate_active_cycle_uniqueness(self):
        if not self.active:
            return

        existing = frappe.db.exists(
            "Club100 Self Workout Cycle",
            {
                "program": self.program,
                "plan": self.plan,
                "active": 1,
                "name": ["!=", self.name],
            },
        )

        if existing:
            frappe.throw(
                "Only one active Self Workout Cycle is allowed for the same "
                "Program and Plan. Deactivate the existing cycle first."
            )

    def normalize_sequence(self):
        for sequence_no, row in enumerate(
            self.workouts or [],
            start=1,
        ):
            row.sequence_no = sequence_no

    def validate_workouts(self):
        rows = self.workouts or []

        if self.active and not rows:
            frappe.throw(
                "An active Self Workout Cycle must contain at least one workout."
            )

        if not self.active:
            return

        for row in rows:
            workout = frappe.db.get_value(
                "Club100 Workout Content",
                row.workout_content,
                [
                    "name",
                    "status",
                    "publish_to_app",
                ],
                as_dict=True,
            )

            if not workout:
                frappe.throw(
                    f"Workout Content {row.workout_content} was not found."
                )

            if workout.status != "Active":
                frappe.throw(
                    f"Workout Content {workout.name} must be Active before "
                    "it can be used in an active Self Workout Cycle."
                )

            if not cint(
                workout.publish_to_app
            ):
                frappe.throw(
                    f"Workout Content {workout.name} must be published to the "
                    "app before it can be used in an active Self Workout Cycle."
                )
