"""Read-only member-owned purchase and scheduled entitlement history."""
import frappe
from club100_core.api.member import _get_current_member

@frappe.whitelist(methods=["GET"])
def my_purchases():
    member = _get_current_member()
    rows = frappe.get_all(
        "Club100 Enrollment",
        filters={"member": member.name, "website_source": "PWA Commerce", "is_trial": 0},
        fields=["name", "program", "plan", "status", "payment_status",
                "membership_duration_months", "amount_payable", "start_date",
                "end_date", "origin_trial_enrollment", "creation"],
        order_by="creation desc", limit_page_length=100,
    )
    return {"purchases": [{
        "enrollmentId": row.name,
        "program": row.program,
        "plan": row.plan,
        "status": row.status,
        "paymentStatus": row.payment_status,
        "durationMonths": int(row.membership_duration_months or 0),
        "amountPayable": float(row.amount_payable or 0),
        "startDate": str(row.start_date) if row.start_date else None,
        "endDate": str(row.end_date) if row.end_date else None,
        "trialEnrollmentId": row.origin_trial_enrollment,
    } for row in rows]}
