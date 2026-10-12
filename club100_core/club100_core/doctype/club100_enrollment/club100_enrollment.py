import frappe
from frappe.model.document import Document
from frappe.utils import cint, getdate


SEAT_OCCUPYING_STATUSES = (
    "Registered",
    "Active",
    "On Hold",
)


class Club100Enrollment(Document):
    def validate(self):
        self.set_defaults_from_member()
        self.set_defaults_from_cohort()
        self.validate_pricing_model()
        self.validate_dates()
        self.validate_sponsorship()
        self.validate_cohort_compatibility()
        self.validate_cohort_capacity()
        self.validate_duplicate_active_enrollment()

    def set_defaults_from_member(self):
        if not self.member:
            return

        member = frappe.db.get_value(
            "Club100 Member",
            self.member,
            [
                "organization",
                "current_fitness_level",
            ],
            as_dict=True,
        )

        if not member:
            return

        if (
            not self.organization
            and member.organization
        ):
            self.organization = member.organization

        if (
            not self.starting_fitness_level
            and member.current_fitness_level
        ):
            self.starting_fitness_level = (
                member.current_fitness_level
            )

        if (
            not self.current_fitness_level
            and self.starting_fitness_level
        ):
            self.current_fitness_level = (
                self.starting_fitness_level
            )

    def set_defaults_from_cohort(self):
        cohort = self._get_cohort()

        if not cohort:
            return

        if (
            cohort.society
            and not self.society
        ):
            self.society = cohort.society

        if (
            cohort.society
            and not self.pricing_model
        ):
            self.pricing_model = "Society Batch"

        if (
            self.society
            and not self.organization
        ):
            organization = frappe.db.get_value(
                "Club100 Society",
                self.society,
                "organization",
            )

            if organization:
                self.organization = organization

    def validate_pricing_model(self):
        if not self.pricing_model:
            self.pricing_model = (
                "Society Batch"
                if self.society
                else "Online Plan"
            )

        if self.pricing_model == "Online Plan":
            if not self.plan:
                frappe.throw(
                    "Plan is required for Online Plan enrollments."
                )

            return

        if self.pricing_model != "Society Batch":
            frappe.throw(
                "Invalid Enrollment Pricing Model."
            )

        if not self.society:
            frappe.throw(
                "Society is required for Society Batch enrollments."
            )

        if not self.cohort:
            frappe.throw(
                "Cohort is required for Society Batch enrollments."
            )

        if self.plan:
            frappe.throw(
                "Online Plan must be blank for Society Batch enrollments."
            )

        if cint(
            self.membership_duration_months
        ) != 1:
            frappe.throw(
                "Society Batch enrollment currently supports "
                "a 1-month membership."
            )

        cohort = self._get_cohort()

        if cohort.society != self.society:
            frappe.throw(
                "Enrollment Society must match the Cohort Society."
            )

        if not cint(
            cohort.society_pricing_enabled
        ):
            frappe.throw(
                "Society Pricing is not enabled for this Cohort."
            )

        if self.society_interest:
            interest_society = frappe.db.get_value(
                "Club100 Society Interest",
                self.society_interest,
                "society",
            )

            if (
                interest_society
                and interest_society
                != self.society
            ):
                frappe.throw(
                    "Society Interest must belong to the same Society "
                    "as the Enrollment."
                )

    def validate_dates(self):
        if self.start_date and self.end_date:
            if getdate(self.end_date) < getdate(self.start_date):
                frappe.throw(
                    "End Date cannot be before Start Date."
                )

    def validate_sponsorship(self):
        if (
            self.enrollment_type == "Sponsored"
            and not self.sponsored_by
        ):
            frappe.throw(
                "Sponsored By is required for sponsored enrollments."
            )

    def _get_cohort(self):
        if not self.cohort:
            return None

        cohort = frappe.db.get_value(
            "Club100 Cohort",
            self.cohort,
            [
                "name",
                "program",
                "plan",
                "capacity",
                "status",
                "delivery_mode",
                "society",
                "society_pricing_enabled",
                "pricing_member_count",
                "pricing_tier",
                "monthly_price_per_member",
            ],
            as_dict=True,
        )

        if not cohort:
            frappe.throw(
                "Selected Club100 Cohort was not found."
            )

        return cohort

    def validate_cohort_compatibility(self):
        cohort = self._get_cohort()

        if not cohort:
            return

        if cohort.status in ("Completed", "Cancelled"):
            frappe.throw(
                "Enrollments cannot be assigned to a completed "
                "or cancelled cohort."
            )

        if (
            self.program
            and cohort.program
            and self.program != cohort.program
        ):
            frappe.throw(
                "Enrollment Program must match the Cohort Program."
            )

        # Plan-based online cohorts must contain only members who
        # purchased the same Plan.
        if cohort.plan:
            if not self.plan:
                frappe.throw(
                    "Enrollment Plan is required for this Cohort."
                )

            if self.plan != cohort.plan:
                frappe.throw(
                    f"This enrollment uses the {self.plan} plan, "
                    f"but the selected cohort uses the "
                    f"{cohort.plan} plan."
                )

        # A plan-less cohort is intentionally allowed. This is
        # required for the future onsite Society model, where the
        # commercial pricing tier is not an online membership Plan.

    def validate_cohort_capacity(self):
        cohort = self._get_cohort()

        if not cohort:
            return

        if self.status not in SEAT_OCCUPYING_STATUSES:
            return

        capacity = cint(cohort.capacity)

        if capacity <= 0:
            frappe.throw(
                "Selected Cohort does not have a valid capacity."
            )

        seat_limit = capacity

        if (
            self.pricing_model == "Society Batch"
            or cint(
                cohort.society_pricing_enabled
            )
        ):
            pricing_member_count = cint(
                cohort.pricing_member_count
            )

            if pricing_member_count <= 0:
                frappe.throw(
                    "Selected Society Cohort does not have a valid "
                    "Pricing Member Count."
                )

            seat_limit = min(
                capacity,
                pricing_member_count,
            )

        # Serialize seat-allocation checks on this Cohort so two
        # simultaneous assignments cannot both take the last seat.
        frappe.db.sql(
            """
            SELECT name
            FROM `tabClub100 Cohort`
            WHERE name = %s
            FOR UPDATE
            """,
            self.cohort,
        )

        occupied = frappe.db.count(
            "Club100 Enrollment",
            {
                "cohort": self.cohort,
                "status": [
                    "in",
                    list(SEAT_OCCUPYING_STATUSES),
                ],
                "name": ["!=", self.name],
            },
        )

        if occupied >= seat_limit:
            frappe.throw(
                f"The selected Cohort is full "
                f"({seat_limit} members maximum for the current "
                f"pricing round)."
            )

    def validate_duplicate_active_enrollment(self):
        if not self.member or not self.program:
            return

        existing = frappe.db.exists(
            "Club100 Enrollment",
            {
                "member": self.member,
                "program": self.program,
                "status": [
                    "in",
                    list(SEAT_OCCUPYING_STATUSES),
                ],
                "name": ["!=", self.name],
            },
        )

        if existing:
            frappe.throw(
                "This member already has an active enrollment "
                "for this program."
            )
