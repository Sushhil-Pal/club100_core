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


def _get_active_enrollment(member_name):
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
def upcoming_sessions():
    member = _get_current_member()

    enrollment = _get_active_enrollment(
        member.name
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
            "workout_content",
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

        if session.workout_content:
            workout = frappe.db.get_value(
                "Club100 Workout Content",
                session.workout_content,
                [
                    "title",
                    "format",
                    "fitness_level",
                    "equipment_required",
                ],
                as_dict=True,
            )

            if workout:
                workout_title = workout.title
                workout_format = workout.format
                workout_level = workout.fitness_level
                equipment_required = workout.equipment_required

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
                "format": workout_format or "Power",
                "status": "Upcoming"
                if session.status == "Scheduled"
                else session.status,
                "deliveryMode": session.delivery_mode,
                "meetingProvider": session.meeting_provider,
                "equipment": (
                    [
                        item.strip()
                        for item in equipment_required.split(",")
                        if item.strip()
                    ]
                    if equipment_required
                    else []
                ),
            }
        )

    return result


@frappe.whitelist()
def session_detail(session_id):
    member = _get_current_member()
    enrollment = _get_active_enrollment(member.name)
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
            "workout_content",
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

    workout = None

    if session.workout_content:
        workout = frappe.db.get_value(
            "Club100 Workout Content",
            session.workout_content,
            [
                "name",
                "title",
                "format",
                "fitness_level",
                "duration_minutes",
                "equipment_required",
                "video_url",
                "instructions",
            ],
            as_dict=True,
        )

    return {
        "id": session.name,
        "title": (
            workout.format
            if workout and workout.format
            else "Session"
        ),
        "subtitle": (
            workout.title
            if workout and workout.title
            else "Club100 Session"
        ),
        "date": str(session.session_date),
        "startTime": str(session.start_time),
        "endTime": str(session.end_time),
        "trainer": trainer_name or "",
        "level": (
            workout.fitness_level
            if workout and workout.fitness_level
            else "Beginner"
        ),
        "format": (
            workout.format
            if workout and workout.format
            else "Power"
        ),
        "status": session.status,
        "deliveryMode": session.delivery_mode,
        "meetingProvider": session.meeting_provider,
        "meetingUrl": session.meeting_url,
        "equipment": (
            [
                item.strip()
                for item in workout.equipment_required.split(",")
                if item.strip()
            ]
            if workout and workout.equipment_required
            else []
        ),
        "workout": (
            {
                "id": workout.name,
                "videoUrl": workout.video_url,
                "durationMinutes": workout.duration_minutes,
                "instructions": workout.instructions,
            }
            if workout
            else None
        ),
    }