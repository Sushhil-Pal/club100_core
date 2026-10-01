import frappe
from frappe.model.document import Document
from frappe.utils import flt


class Club100MetricScoringConfig(Document):

    def validate(self):
        self.validate_rules()

    def validate_rules(self):
        for row in self.rules:

            if not row.method:
                frappe.throw(
                    f"Row {row.idx}: Method is required."
                )

            self.validate_age_range(row)

            if row.method in (
                "Linear-Higher",
                "Linear-Lower",
            ):
                self.validate_linear(row)

            elif row.method == "Range":
                self.validate_range(row)

            elif row.method == "Category":
                self.validate_category(row)

            elif row.method == "Direct":
                self.validate_direct(row)

    def validate_age_range(self, row):
        age_from = flt(row.age_from or 0)
        age_to = flt(row.age_to or 0)

        if age_from and age_to and age_from > age_to:
            frappe.throw(
                f"Row {row.idx}: "
                "Age From cannot exceed Age To."
            )

    def validate_linear(self, row):
        if (
            row.excellent_value is None
            or row.risk_value is None
            or row.poor_value is None
        ):
            frappe.throw(
                f"Row {row.idx}: Excellent Value, "
                "Risk Value and Poor Value are required "
                f"for {row.method}."
            )

        excellent = flt(row.excellent_value)
        risk = flt(row.risk_value)
        poor = flt(row.poor_value)

        if row.method == "Linear-Higher":
            if not (excellent > risk > poor):
                frappe.throw(
                    f"Row {row.idx}: Linear-Higher requires "
                    "Excellent > Risk > Poor."
                )

        elif row.method == "Linear-Lower":
            if not (excellent < risk < poor):
                frappe.throw(
                    f"Row {row.idx}: Linear-Lower requires "
                    "Excellent < Risk < Poor."
                )

    def validate_range(self, row):
        if row.lower_limit is None or row.upper_limit is None:
            frappe.throw(
                f"Row {row.idx}: Lower Limit and Upper Limit "
                "are required for Range."
            )

        if flt(row.lower_limit) >= flt(row.upper_limit):
            frappe.throw(
                f"Row {row.idx}: Lower Limit must be "
                "less than Upper Limit."
            )

    def validate_category(self, row):
        if (
            row.lower_limit is None
            or row.upper_limit is None
            or row.score is None
        ):
            frappe.throw(
                f"Row {row.idx}: Lower Limit, Upper Limit "
                "and Score are required for Category."
            )

        if flt(row.lower_limit) > flt(row.upper_limit):
            frappe.throw(
                f"Row {row.idx}: Lower Limit cannot exceed "
                "Upper Limit."
            )

        if not 0 <= flt(row.score) <= 100:
            frappe.throw(
                f"Row {row.idx}: Score must be between "
                "0 and 100."
            )

    def validate_direct(self, row):
        if row.lower_limit is None or row.upper_limit is None:
            frappe.throw(
                f"Row {row.idx}: Lower Limit and Upper Limit "
                "are required for Direct."
            )

        if flt(row.lower_limit) >= flt(row.upper_limit):
            frappe.throw(
                f"Row {row.idx}: Lower Limit must be "
                "less than Upper Limit."
            )