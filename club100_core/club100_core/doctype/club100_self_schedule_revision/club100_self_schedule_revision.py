import frappe
from frappe.model.document import Document

class Club100SelfScheduleRevision(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("Schedule revisions are immutable", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("Schedule revisions cannot be deleted", frappe.PermissionError)
