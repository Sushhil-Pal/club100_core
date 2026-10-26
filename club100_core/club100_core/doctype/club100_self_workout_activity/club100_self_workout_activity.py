import frappe
from frappe.model.document import Document


class Club100SelfWorkoutActivity(Document):
    def validate(self):
        if self.is_new():
            if self.status != "Started" or not self.started_at or self.completed_at:
                frappe.throw("New activity must begin as Started", frappe.ValidationError)
            if self.member != frappe.db.get_value("Club100 Enrollment", self.enrollment, "member"):
                frappe.throw("Member and enrollment must match", frappe.ValidationError)
        else:
            old = self.get_doc_before_save()
            if old:
                immutable = ("member", "enrollment", "workout_id", "scheduled_at", "started_at")
                if any(str(self.get(f)) != str(old.get(f)) for f in immutable):
                    frappe.throw("Workout activity identity cannot be edited", frappe.ValidationError)
                if old.status == "Completed" and (self.status != "Completed" or str(self.completed_at) != str(old.completed_at)):
                    frappe.throw("Completed workout cannot be changed", frappe.ValidationError)
                if old.status == "Started" and self.status not in ("Started", "Completed"):
                    frappe.throw("Invalid workout activity status change", frappe.ValidationError)
                if self.status == "Completed" and not self.completed_at:
                    frappe.throw("Completion time is required", frappe.ValidationError)
