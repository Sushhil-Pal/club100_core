import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime


class Club100Session(Document):
    def validate(self):
        self.set_defaults_from_cohort()
        self.validate_target()
        self.validate_times()
        self.validate_online_details()

    def validate_target(self):
        has_cohort = bool(self.cohort)
        has_plan = bool(self.plan)

        if has_cohort == has_plan:
            frappe.throw(
                "Select exactly one session target: Cohort or Plan."
            )

        if has_cohort:
            if not self.trainer:
                frappe.throw(
                    "Trainer is required for cohort sessions."
                )
            return

        plan = frappe.db.get_value(
            "Club100 Plan",
            self.plan,
            [
                "plan_name",
                "coach_led",
            ],
            as_dict=True,
        )

        if not plan:
            frappe.throw("Plan not found.")

        if plan.coach_led:
            frappe.throw(
                "Plan-targeted sessions are only supported for self-guided plans."
            )

        if self.trainer:
            frappe.throw(
                "Trainer must be blank for self-guided plan sessions."
            )

        if not self.workout_content:
            frappe.throw(
                "Workout Content is required for self-guided plan sessions."
            )

    def set_defaults_from_cohort(self):
        if not self.cohort:
            return

        cohort = frappe.db.get_value(
            "Club100 Cohort",
            self.cohort,
            [
                "program",
                "primary_trainer",
                "delivery_mode",
                "meeting_provider",
                "default_meeting_url",
            ],
            as_dict=True,
        )

        if not cohort:
            return

        if not self.program and cohort.program:
            self.program = cohort.program

        if not self.trainer and cohort.primary_trainer:
            self.trainer = cohort.primary_trainer

        if not self.delivery_mode and cohort.delivery_mode:
            self.delivery_mode = cohort.delivery_mode

        if not self.meeting_provider and cohort.meeting_provider:
            self.meeting_provider = cohort.meeting_provider

        if not self.meeting_url and cohort.default_meeting_url:
            self.meeting_url = cohort.default_meeting_url

    def validate_times(self):
        if not self.session_date or not self.start_time or not self.end_time:
            return

        start = get_datetime(f"{self.session_date} {self.start_time}")
        end = get_datetime(f"{self.session_date} {self.end_time}")

        if end <= start:
            frappe.throw("End Time must be after Start Time.")

    def validate_online_details(self):
        # Self-guided plan sessions do not use a live meeting URL.
        if self.plan and not self.cohort:
            return

        if self.delivery_mode in ("Online", "Hybrid"):
            if self.status in ("Scheduled", "Live") and not self.meeting_url:
                frappe.throw(
                    "Meeting URL is required for scheduled online or hybrid sessions."
                )
