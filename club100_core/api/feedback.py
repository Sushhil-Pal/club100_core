import frappe
from frappe.utils import now_datetime


def _get_current_member():
    user = frappe.session.user

    if user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.PermissionError,
        )

    member = frappe.db.get_value(
        "Club100 Member",
        {"user": user},
        ["name"],
        as_dict=True,
    )

    if not member:
        frappe.throw("Club100 member profile not found")

    return member


def _get_active_enrollment(member_name):
    enrollments = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "member": member_name,
            "status": ["in", ["Registered", "Active"]],
        },
        fields=[
            "name",
            "cohort",
            "program",
        ],
        order_by="enrollment_date desc",
        limit=1,
    )

    if not enrollments:
        frappe.throw(
            "No active Club100 program enrollment found"
        )

    return enrollments[0]


def _validate_session_access(
    session_id,
    member_name,
):
    enrollment = _get_active_enrollment(member_name)

    session = frappe.db.get_value(
        "Club100 Session",
        session_id,
        [
            "name",
            "cohort",
        ],
        as_dict=True,
    )

    if not session:
        frappe.throw("Session not found")

    if session.cohort != enrollment.cohort:
        frappe.throw(
            "You are not enrolled in this session",
            frappe.PermissionError,
        )

    return enrollment


@frappe.whitelist()
def submit_feedback(
    session_id,
    overall_rating,
    difficulty_rating,
    trainer_rating,
    energy_after_session,
    would_recommend=0,
    comments=None,
):
    member = _get_current_member()

    enrollment = _validate_session_access(
        session_id,
        member.name,
    )

    existing_feedback = frappe.db.get_value(
        "Club100 Feedback",
        {
            "member": member.name,
            "session": session_id,
        },
        "name",
    )

    if existing_feedback:
        frappe.throw(
            "Feedback has already been submitted for this session"
        )

    feedback = frappe.get_doc(
        {
            "doctype": "Club100 Feedback",
            "member": member.name,
            "session": session_id,
            "enrollment": enrollment.name,
            "overall_rating": int(overall_rating),
            "difficulty_rating": difficulty_rating,
            "trainer_rating": int(trainer_rating),
            "energy_after_session": energy_after_session,
            "would_recommend": int(
                bool(would_recommend)
            ),
            "comments": comments,
            "submitted_at": now_datetime(),
            "source": "Member App",
            "status": "Submitted",
        }
    )

    feedback.insert(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "feedbackId": feedback.name,
    }