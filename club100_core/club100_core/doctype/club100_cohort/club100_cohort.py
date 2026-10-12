import frappe
from frappe.model.document import Document
from frappe.utils import cint, getdate, now_datetime

from club100_core.society.pricing import get_society_pricing


class Club100Cohort(Document):
    def validate(self):
        self.set_defaults_from_program()
        self.set_defaults_from_plan()
        self.set_defaults_from_society()
        self.validate_plan()
        self.validate_society_pricing()
        self.validate_dates()
        self.validate_capacity()
        self.validate_private_cohort()
        self.validate_meeting_url()

    def set_defaults_from_program(self):
        if not self.program:
            return

        program = frappe.db.get_value(
            "Club100 Program",
            self.program,
            ["delivery_mode", "default_cohort_capacity"],
            as_dict=True,
        )

        if not program:
            return

        if not self.delivery_mode and program.delivery_mode:
            self.delivery_mode = program.delivery_mode

        if (
            not self.plan
            and not self.capacity
            and program.default_cohort_capacity
        ):
            self.capacity = program.default_cohort_capacity

    def set_defaults_from_plan(self):
        if not self.plan:
            return

        plan = frappe.db.get_value(
            "Club100 Plan",
            self.plan,
            ["delivery_type", "max_session_capacity", "status"],
            as_dict=True,
        )

        if not plan:
            return

        if (
            not self.capacity
            and cint(plan.max_session_capacity) > 0
        ):
            self.capacity = cint(plan.max_session_capacity)

    def set_defaults_from_society(self):
        if not self.society:
            return

        society = frappe.db.get_value(
            "Club100 Society",
            self.society,
            ["organization", "status"],
            as_dict=True,
        )

        if not society:
            frappe.throw("Selected Club100 Society was not found.")

        self.society_pricing_enabled = 1

        if not self.organization and society.organization:
            self.organization = society.organization

    def validate_plan(self):
        if (
            self.delivery_mode in ("Online", "Hybrid")
            and not self.plan
        ):
            frappe.throw(
                "Plan is required for online or hybrid cohorts."
            )

        if not self.plan:
            return

        plan = frappe.db.get_value(
            "Club100 Plan",
            self.plan,
            [
                "plan_name",
                "delivery_type",
                "max_session_capacity",
                "status",
            ],
            as_dict=True,
        )

        if not plan:
            frappe.throw("Selected Club100 Plan was not found.")

        if plan.status != "Active":
            frappe.throw("Selected Club100 Plan is not active.")

        if plan.delivery_type == "Recorded":
            frappe.throw(
                "Recorded plans such as Self cannot be assigned "
                "to a live cohort."
            )

        if plan.delivery_type != "Live Online":
            frappe.throw(
                "Selected Club100 Plan is not configured for "
                "live online cohorts."
            )

        if self.delivery_mode == "Onsite":
            frappe.throw(
                "Online membership Plans cannot be assigned to "
                "onsite cohorts."
            )

    def validate_society_pricing(self):
        if not cint(self.society_pricing_enabled):
            self.pricing_member_count = 0
            self.pricing_tier = ""
            self.monthly_price_per_member = 0
            self.pricing_last_calculated_on = None
            return

        if not self.society:
            frappe.throw(
                "Society is required when Society Pricing is enabled."
            )

        if self.delivery_mode != "Onsite":
            frappe.throw(
                "Society Pricing is currently available only for onsite cohorts."
            )

        if self.plan:
            frappe.throw(
                "Online membership Plan must be blank for Society onsite cohorts."
            )

        program_code = frappe.db.get_value(
            "Club100 Program",
            self.program,
            "program_code",
        )

        if program_code != "general-fitness":
            frappe.throw(
                "Society Pricing is currently available only for the "
                "General Fitness program."
            )

        member_count = cint(self.pricing_member_count)

        if member_count < 0:
            frappe.throw("Pricing Member Count cannot be negative.")

        pricing = get_society_pricing(member_count)

        self.pricing_tier = pricing["tier"]
        self.monthly_price_per_member = pricing["monthly_price_per_member"]
        self.pricing_last_calculated_on = now_datetime()

        if self.status in ("Open", "Active") and member_count < 5:
            frappe.throw(
                "A Society cohort must have at least 5 pricing members "
                "before it can be Open or Active."
            )

    def validate_dates(self):
        if self.start_date and self.end_date:
            if getdate(self.end_date) < getdate(self.start_date):
                frappe.throw(
                    "End Date cannot be before Start Date."
                )

    def validate_capacity(self):
        capacity = cint(self.capacity)

        if capacity <= 0:
            frappe.throw("Capacity must be greater than 0.")

        if cint(self.society_pricing_enabled):
            pricing_member_count = cint(self.pricing_member_count)

            if pricing_member_count > capacity:
                frappe.throw(
                    "Pricing Member Count cannot exceed Cohort Capacity."
                )

        if not self.plan:
            return

        max_capacity = cint(
            frappe.db.get_value(
                "Club100 Plan",
                self.plan,
                "max_session_capacity",
            )
            or 0
        )

        if max_capacity <= 0:
            frappe.throw(
                "Selected Plan does not have a valid live-session "
                "capacity."
            )

        if capacity > max_capacity:
            frappe.throw(
                f"Capacity cannot exceed {max_capacity} for "
                f"the {self.plan} plan."
            )

    def validate_private_cohort(self):
        if (
            self.cohort_type == "Private"
            and not self.organization
            and not self.society
        ):
            frappe.throw(
                "Organization or Society is required for private cohorts."
            )

    def validate_meeting_url(self):
        if (
            self.delivery_mode in ("Online", "Hybrid")
            and self.status in ("Open", "Active")
            and not self.default_meeting_url
        ):
            frappe.msgprint(
                "Meeting URL has not been configured for this "
                "online cohort."
            )
