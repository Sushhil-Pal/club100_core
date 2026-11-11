import re

import frappe
from frappe.model.document import Document


class Club100ReferralPartner(Document):
    def validate(self):
        code = (self.partner_code or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{6,20}", code):
            frappe.throw("Referral code must be 6–20 letters or numbers.")
        self.partner_code = code
        if self.partner_type == "Member Advocate" and not self.member:
            frappe.throw("Member Advocate must link to a Club100 Member.")
        if self.member:
            existing = frappe.db.get_value(
                "Club100 Referral Partner",
                {"member": self.member, "name": ["!=", self.name or ""]},
                "name",
            )
            if existing:
                frappe.throw("Member already has a referral partner record.")
        if not self.is_new():
            old = frappe.get_doc(self.doctype, self.name)
            if old.partner_code != self.partner_code:
                frappe.throw("Referral code cannot change after creation.")
        if self.status == "Active" and self.partner_type == "Ambassador":
            if not self.agreement_accepted_on:
                frappe.throw("Ambassador agreement acceptance must be recorded before activation.")
        if self.status == "Active" and not self.approved_on and self.partner_type == "Ambassador":
            self.approved_on = frappe.utils.now_datetime()
            self.approved_by = frappe.session.user
