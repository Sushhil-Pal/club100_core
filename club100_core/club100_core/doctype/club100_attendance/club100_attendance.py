import frappe
from frappe.model.document import Document
from frappe.utils import time_diff_in_seconds, now_datetime


class Club100Attendance(Document):
    def validate(self):
        self.validate_duplicate_attendance()
        self.validate_times()
        self.calculate_minutes_attended()
        self.set_recording_metadata()

    def validate_duplicate_attendance(self):
        if not self.member or not self.session:
            return

        existing = frappe.db.exists(
            "Club100 Attendance",
            {
                "member": self.member,
                "session": self.session,
                "name": ["!=", self.name],
            },
        )

        if existing:
            frappe.throw(
                "Attendance already exists for this member and session."
            )

    def validate_times(self):
        if self.join_time and self.leave_time:
            if self.leave_time <= self.join_time:
                frappe.throw(
                    "Leave Time must be after Join Time."
                )

    def calculate_minutes_attended(self):
        if self.join_time and self.leave_time:
            seconds = time_diff_in_seconds(
                self.leave_time,
                self.join_time,
            )

            self.minutes_attended = round(seconds / 60)
        elif self.attendance_status == "Absent":
            self.minutes_attended = 0

    def set_recording_metadata(self):
        if not self.recorded_by:
            self.recorded_by = frappe.session.user

        if not self.recorded_at:
            self.recorded_at = now_datetime()