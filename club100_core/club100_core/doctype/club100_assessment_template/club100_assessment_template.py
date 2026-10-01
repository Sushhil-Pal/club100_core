import frappe
from frappe.model.document import Document


class Club100AssessmentTemplate(Document):
    def validate(self):
        self.validate_metrics()
        self.populate_metric_details()

    def validate_metrics(self):
        seen = set()

        for row in self.metrics:
            if not row.metric:
                frappe.throw(
                    f"Row {row.idx}: Metric is required."
                )

            if row.metric in seen:
                frappe.throw(
                    f"Metric '{row.metric}' is duplicated in the template."
                )

            seen.add(row.metric)

            if row.weight is not None and row.weight < 0:
                frappe.throw(
                    f"Row {row.idx}: Weight cannot be negative."
                )

    def populate_metric_details(self):
        for row in self.metrics:
            if not row.metric:
                continue

            row.category = frappe.db.get_value(
                "Club100 Fitness Metric",
                row.metric,
                "category",
            )