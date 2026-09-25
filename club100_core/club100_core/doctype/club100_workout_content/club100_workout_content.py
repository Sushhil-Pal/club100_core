import frappe
from frappe.model.document import Document


class Club100WorkoutContent(Document):
    def validate(self):
        self.validate_duration()
        self.validate_video_url()

    def validate_duration(self):
        if not self.duration_minutes or self.duration_minutes <= 0:
            frappe.throw("Duration Minutes must be greater than 0.")

    def validate_video_url(self):
        if self.status == "Active" and not self.video_url:
            frappe.throw("Video URL is required for active workout content.")