import frappe
from frappe.utils import getdate, nowdate


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


def _get_active_enrollment(member_name, enrollment_id=None):
    if enrollment_id:
        from club100_core.api.memberships import resolve_active_enrollment
        return resolve_active_enrollment(member_name, enrollment_id)
    enrollments = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "member": member_name,
            "status": ["in", ["Registered", "Active"]],
        },
        fields=[
            "name",
            "program",
            "cohort",
        ],
        order_by="enrollment_date desc",
        limit=1,
    )

    if not enrollments:
        return None

    return enrollments[0]

@frappe.whitelist()
def upcoming_sessions(enrollment_id=None):
    member = _get_current_member()

    enrollment = _get_active_enrollment(
        member.name, enrollment_id
    )

    if not enrollment:
        return []

    if not enrollment.cohort:
        return []


    sessions = frappe.get_all(
        "Club100 Session",
        filters={
            "cohort": enrollment.cohort,
            "status": ["in", ["Scheduled", "Live"]],
            "session_date": [">=", nowdate()],
        },
        fields=[
            "name",
            "program",
            "trainer",
            "workout_format",
            "session_date",
            "start_time",
            "end_time",
            "delivery_mode",
            "meeting_provider",
            "meeting_url",
            "status",
        ],
        order_by="session_date asc, start_time asc",
    )

    result = []

    for session in sessions:
        trainer_name = None
        workout_title = None
        workout_format = None
        workout_level = None
        equipment_required = None

        if session.trainer:
            trainer_name = frappe.db.get_value(
                "Club100 Trainer",
                session.trainer,
                "trainer_name",
            )

        workout_format = session.workout_format
        if session.program:
            workout_title = frappe.db.get_value(
                "Club100 Program", session.program, "program_name"
            )
        if workout_format:
            equipment_required = frappe.db.get_value(
                "Club100 Workout Format", workout_format, "default_items_to_bring"
            )

        result.append(
            {
                "id": session.name,
                "title": workout_format or "Session",
                "subtitle": workout_title or "Club100 Session",
                "date": str(session.session_date),
                "startTime": str(session.start_time),
                "endTime": str(session.end_time),
                "trainer": trainer_name or "",
                "level": workout_level or "Beginner",
                "format": workout_format,
                "status": "Upcoming"
                if session.status == "Scheduled"
                else session.status,
                "deliveryMode": session.delivery_mode,
                "meetingProvider": session.meeting_provider,
                "equipment": (
                    [
                        item.strip()
                        for item in equipment_required.splitlines()
                        if item.strip()
                    ]
                    if equipment_required
                    else []
                ),
            }
        )

    return result


@frappe.whitelist()
def session_detail(session_id, enrollment_id=None):
    member = _get_current_member()
    enrollment = _get_active_enrollment(member.name, enrollment_id)
    if not enrollment:
        frappe.throw(
            "You are not enrolled in this session",
            frappe.PermissionError,
        )

    session = frappe.db.get_value(
        "Club100 Session",
        session_id,
        [
            "name",
            "cohort",
            "program",
            "trainer",
            "workout_format",
            "session_date",
            "start_time",
            "end_time",
            "delivery_mode",
            "meeting_provider",
            "meeting_url",
            "status",
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

    trainer_name = None

    if session.trainer:
        trainer_name = frappe.db.get_value(
            "Club100 Trainer",
            session.trainer,
            "trainer_name",
        )

    workout_format = session.workout_format
    program_title = (frappe.db.get_value("Club100 Program", session.program, "program_name")
                     if session.program else None)
    equipment_required = (frappe.db.get_value(
        "Club100 Workout Format", workout_format, "default_items_to_bring")
        if workout_format else None)

    return {
        "id": session.name,
        "title": workout_format or "Session",
        "subtitle": program_title or "Club100 Session",
        "date": str(session.session_date),
        "startTime": str(session.start_time),
        "endTime": str(session.end_time),
        "trainer": trainer_name or "",
        "level": "Beginner",  # Legacy display default; trainer-led level remains trainer-owned.
        "format": workout_format,
        "status": session.status,
        "deliveryMode": session.delivery_mode,
        "meetingProvider": session.meeting_provider,
        "meetingUrl": session.meeting_url,
        "equipment": (
            [line.strip() for line in equipment_required.splitlines() if line.strip()]
            if equipment_required else []
        ),
        "workout": None,  # Self workout prescriptions are served by virtual_workout_detail.
    }
