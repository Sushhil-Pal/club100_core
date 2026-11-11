import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today, now_datetime

from club100_core.wallet.core import money, get_or_create_wallet, post_grant_credit


class Club100WalletGrant(Document):
    def validate(self):
        if not self.is_new():
            # Only promotion revocation is mutable. Issuance, amount, dates and
            # funding identity are immutable. The original ledger is not edited.
            old = self.get_doc_before_save()
            if not old or old.source_type != 'Promotion' or old.status != 'Active' or self.status != 'Revoked':
                frappe.throw('Wallet grants are immutable except one-way Promotion revocation.')
            user = frappe.session.user
            if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
                frappe.throw('System Manager required to revoke Promotion credits', frappe.PermissionError)
            immutable = ('source_event_key', 'member', 'wallet', 'source_type',
                         'corporate_pool', 'corporate_customer', 'funding_reference',
                         'funding_verified', 'referral_reward', 'amount', 'currency',
                         'valid_from', 'validity_type', 'expires_on', 'notes')
            for field in immutable:
                if str(self.get(field) or '') != str(old.get(field) or ''):
                    frappe.throw('Cannot change issued wallet grant field: ' + field)
            if not str(self.revocation_reason or '').strip():
                frappe.throw('Revocation reason is required')
            if self.revoked_by or self.revoked_on:
                frappe.throw('Revocation audit fields are system-managed')
            self.revoked_by = user
            self.revoked_on = now_datetime()
            return
        if self.currency != "INR":
            frappe.throw("Only INR grants are supported.")
        if money(self.amount) <= 0:
            frappe.throw("Grant amount must be positive.")
        if self.validity_type not in ("Lifetime", "Expires On"):
            frappe.throw("Select Lifetime or Expires On.")
        if not self.valid_from:
            self.valid_from = today()
        if self.validity_type == "Lifetime":
            if self.expires_on:
                frappe.throw("Lifetime credits cannot have an expiry date.")
        elif not self.expires_on or getdate(self.expires_on) < getdate(self.valid_from):
            frappe.throw("Expiry Date must be on or after Valid From.")
        if self.status != "Active":
            frappe.throw("Grants must be created Active.")
        if self.revocation_reason or self.revoked_by or self.revoked_on:
            frappe.throw('New wallet grants cannot include revocation metadata')
        if not (self.source_event_key or "").strip():
            frappe.throw("Unique Allocation Reference is required.")
        if self.source_type == "Corporate":
            if not self.corporate_pool:
                frappe.throw("Corporate grants require a verified Corporate Credit Pool.")
            if not self.corporate_customer or not self.funding_reference or not self.funding_verified:
                frappe.throw("Corporate grant funding metadata is incomplete.")
            if self.referral_reward:
                frappe.throw("Corporate grant cannot reference a referral reward.")
        elif self.source_type == "Promotion":
            if self.corporate_pool or not self.funding_reference or self.corporate_customer or self.referral_reward:
                frappe.throw("Promotion grant needs an administrative issuance reference and no corporate/referral link.")
            if not str(self.notes or '').strip():
                frappe.throw('Business reason is required for issuing Promotion credit.')
        elif self.source_type == "Referral":
            if not getattr(frappe.flags, "club100_referral_issue_grant", False):
                frappe.throw("Referral grants can only be issued by the trusted maturity service.")
            if self.corporate_pool or self.corporate_customer or not self.referral_reward:
                frappe.throw("Referral grant must have a reward, not a corporate funding link.")
            reward = frappe.get_doc("Club100 Referral Reward", self.referral_reward)
            partner_member = frappe.db.get_value("Club100 Referral Partner", reward.partner, "member")
            from club100_core.wallet.admin_refs import allocation_event_key
            if (reward.status != "Approved" or reward.basis_verification_status != "Verified"
                    or reward.reward_track != "Member Credit" or not partner_member
                    or self.member != partner_member
                    or self.source_event_key != allocation_event_key("Referral", reward.name)
                    or money(self.amount) != money(reward.verified_reward_amount)
                    or self.validity_type != reward.credit_validity_type
                    or str(self.expires_on or "") != str(reward.credit_expires_on or "")):
                frappe.throw("Referral grant does not match finance-approved reward.")
        else:
            frappe.throw("Unsupported funding source.")

    def before_insert(self):
        if (not getattr(frappe.flags, "club100_referral_issue_grant", False)
                and "System Manager" not in frappe.get_roles(frappe.session.user)
                and frappe.session.user != "Administrator"):
            frappe.throw("System Manager required to allocate credits.", frappe.PermissionError)
        self.amount = money(self.amount)
        # Internal allocation IDs are generated automatically for Desk-created grants.
        # Batch grants carry a deterministic key that makes retries idempotent.
        from club100_core.wallet.admin_refs import allocation_event_key
        if not self.source_event_key:
            self.source_event_key = allocation_event_key(self.source_type, self.referral_reward)
        if (self.source_event_key.startswith("CORP:BATCH:")
                and not getattr(frappe.flags, "club100_wallet_batch_issue", False)):
            frappe.throw("Batch allocation keys can only be created by submitting a batch.")
        if self.source_type == "Corporate":
            # The selected funded pool is authoritative. Never require staff
            # to manually copy its company, receipt, or verification flag.
            self.corporate_customer = None
            self.funding_reference = None
            self.funding_verified = 0
        elif self.source_type == "Referral":
            self.corporate_pool = None
            self.corporate_customer = None
            self.funding_verified = 0
            if not self.referral_reward or not getattr(frappe.flags, "club100_referral_issue_grant", False):
                frappe.throw("Only approved internal referral issues are allowed.")
            self.funding_reference = "REFERRAL:FINANCE:" + self.referral_reward
        elif self.source_type == "Promotion":
            # An explicit legacy approval reference remains supported; for
            # new Desk grants the approving System Manager is the audit actor.
            self.corporate_pool = None
            self.corporate_customer = None
            self.referral_reward = None
            # Never accept a supplied approval or funding reference for a new
            # promotion. Tie provenance to this system-generated issuance key.
            self.funding_reference = "PROMO:ADMIN:" + self.source_event_key
        if self.source_type == "Corporate":
            # Locks funded pool row and checks company membership and capacity.
            # The lock is held until the same DB transaction commits/rolls back.
            from club100_core.wallet.corporate import validate_and_lock_allocation
            validate_and_lock_allocation(self)
        self.wallet = get_or_create_wallet(self.member)
        # Lock in same Member -> Wallet order as reservations. Corporate pool
        # was validated above; its lock remains held until transaction end.
        frappe.db.sql("SELECT name FROM `tabClub100 Wallet` WHERE name=%s FOR UPDATE", self.wallet)

    def after_insert(self):
        # Runs in the same Frappe DB transaction as grant insertion.
        # If ledger insertion fails, the entire grant insert rolls back.
        post_grant_credit(self)

    def on_trash(self):
        frappe.throw("Posted credit grants cannot be deleted; use an audited reversal workflow.")
