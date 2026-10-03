import frappe
from frappe.utils import getdate, now_datetime


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
        frappe.throw(
            "Club100 member profile not found"
        )

    return member


def _validate_session_access(
    session_id,
    member_name,
):
    session_id = (
        session_id or ""
    ).strip()

    if not session_id:
        frappe.throw(
            "Session ID is required.",
            frappe.ValidationError,
        )

    session = frappe.db.get_value(
        "Club100 Session",
        session_id,
        [
            "name",
            "cohort",
            "program",
            "session_date",
            "status",
        ],
        as_dict=True,
    )

    if not session:
        frappe.throw(
            "Session not found.",
            frappe.DoesNotExistError,
        )

    # Feedback is only available after completion.
    if session.status != "Completed":
        frappe.throw(
            "Feedback can only be submitted after the session is completed.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Historical enrollment validation
    #
    # Do not require the enrollment to still be Active today.
    # ---------------------------------------------------------

    enrollments = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "member":
                member_name,

            "cohort":
                session.cohort,

            "status":
                ["!=", "Cancelled"],
        },
        fields=[
            "name",
            "program",
            "cohort",
            "start_date",
            "end_date",
            "status",
        ],
        order_by=(
            "start_date desc, "
            "enrollment_date desc"
        ),
    )

    session_date = getdate(
        session.session_date
    )

    valid_enrollment = None

    for enrollment in enrollments:
        if (
            enrollment.start_date
            and getdate(
                enrollment.start_date
            ) > session_date
        ):
            continue

        if (
            enrollment.end_date
            and getdate(
                enrollment.end_date
            ) < session_date
        ):
            continue

        valid_enrollment = (
            enrollment
        )

        break

    if not valid_enrollment:
        frappe.throw(
            "You are not enrolled in this session.",
            frappe.PermissionError,
        )

    return (
        session,
        valid_enrollment,
    )


def _validate_rating(
    value,
    field_label,
):
    try:
        value = int(value)
    except (
        TypeError,
        ValueError,
    ):
        frappe.throw(
            f"{field_label} must be between 1 and 5.",
            frappe.ValidationError,
        )

    if value < 1 or value > 5:
        frappe.throw(
            f"{field_label} must be between 1 and 5.",
            frappe.ValidationError,
        )

    return value


@frappe.whitelist(
    methods=["POST"]
)
def submit_feedback(
    session_id,
    overall_rating,
    difficulty_rating,
    trainer_rating,
    energy_after_session,
    would_recommend=0,
    comments=None,
):
    member = (
        _get_current_member()
    )

    session, enrollment = (
        _validate_session_access(
            session_id,
            member.name,
        )
    )

    # ---------------------------------------------------------
    # Prevent duplicate feedback
    # ---------------------------------------------------------

    existing_feedback = (
        frappe.db.get_value(
            "Club100 Feedback",
            {
                "member":
                    member.name,

                "session":
                    session.name,
            },
            "name",
        )
    )

    if existing_feedback:
        frappe.throw(
            "Feedback has already been submitted for this session.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Validate ratings
    # ---------------------------------------------------------

    overall_rating = (
        _validate_rating(
            overall_rating,
            "Overall rating",
        )
    )

    trainer_rating = (
        _validate_rating(
            trainer_rating,
            "Trainer rating",
        )
    )

    allowed_difficulty = {
        "Too Easy",
        "Just Right",
        "Challenging",
        "Too Difficult",
    }

    if (
        difficulty_rating
        not in allowed_difficulty
    ):
        frappe.throw(
            "Invalid difficulty rating.",
            frappe.ValidationError,
        )

    allowed_energy = {
        "Low",
        "Same",
        "Better",
        "Excellent",
    }

    if (
        energy_after_session
        not in allowed_energy
    ):
        frappe.throw(
            "Invalid energy rating.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Create feedback
    # ---------------------------------------------------------

    feedback = frappe.get_doc({
        "doctype":
            "Club100 Feedback",

        "member":
            member.name,

        "session":
            session.name,

        "enrollment":
            enrollment.name,

        "overall_rating":
            overall_rating,

        "difficulty_rating":
            difficulty_rating,

        "trainer_rating":
            trainer_rating,

        "energy_after_session":
            energy_after_session,

        "would_recommend":
            int(
                bool(
                    int(
                        would_recommend
                        or 0
                    )
                )
            ),

        "comments":
            (
                comments or ""
            ).strip(),

        "submitted_at":
            now_datetime(),

        "source":
            "Member App",

        "status":
            "Submitted",
    })

    feedback.insert(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "feedbackId":
            feedback.name,
    }