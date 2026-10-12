import frappe

from club100_core.society.enrollment import (
    create_pending_society_enrollment,
    get_invite_context,
    issue_invite as issue_society_invite,
)


@frappe.whitelist(
    allow_guest=True,
    methods=["GET", "POST"],
)
def get_invite(
    invite_token=None,
):
    return get_invite_context(
        invite_token
    )


@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def create_pending_enrollment(
    invite_token=None,
    cohort=None,
    email=None,
):
    return create_pending_society_enrollment(
        invite_token=invite_token,
        cohort_name=cohort,
        email=email,
    )


@frappe.whitelist(
    methods=["POST"],
)
def issue_invite(
    interest_id=None,
    cohort=None,
    validity_days=30,
):
    return issue_society_invite(
        interest_id=interest_id,
        cohort_name=cohort,
        validity_days=validity_days,
    )
