import frappe
from frappe.utils import getdate, nowdate


@frappe.whitelist()
def current_program():
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

    # ---------------------------------------------------------
    # Current enrollment
    # ---------------------------------------------------------

    enrollments = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "member":
                member.name,

            "status": [
                "in",
                [
                    "Registered",
                    "Active",
                ],
            ],
        },
        fields=[
            "name",
            "program",
            "cohort",
            "start_date",
            "end_date",
            "completion_percentage",
            "status",
        ],
        order_by=(
            "start_date desc, "
            "enrollment_date desc"
        ),
        limit=1,
    )

    if not enrollments:
        return None

    enrollment = enrollments[0]

    # ---------------------------------------------------------
    # Program
    # ---------------------------------------------------------

    program = frappe.db.get_value(
        "Club100 Program",
        enrollment.program,
        [
            "name",
            "program_name",
            "duration_weeks",
            "session_duration_minutes",
        ],
        as_dict=True,
    )

    if not program:
        frappe.throw(
            "Club100 program not found"
        )

    total_weeks = (
        program.duration_weeks
        or 1
    )

    current_week = 1

    today = getdate(
        nowdate()
    )

    if enrollment.start_date:
        start_date = getdate(
            enrollment.start_date
        )

        if today >= start_date:
            days_elapsed = (
                today -
                start_date
            ).days

            current_week = (
                days_elapsed // 7
            ) + 1

            current_week = min(
                current_week,
                total_weeks,
            )

    # ---------------------------------------------------------
    # Eligible completed cohort sessions
    #
    # Count only sessions that fall within this member's
    # enrollment period.
    # ---------------------------------------------------------

    eligible_session_names = []

    if enrollment.cohort:
        completed_sessions = (
            frappe.get_all(
                "Club100 Session",
                filters={
                    "cohort":
                        enrollment.cohort,

                    "status":
                        "Completed",
                },
                fields=[
                    "name",
                    "session_date",
                ],
            )
        )

        for session in completed_sessions:
            session_date = getdate(
                session.session_date
            )

            if (
                enrollment.start_date
                and session_date
                < getdate(
                    enrollment.start_date
                )
            ):
                continue

            if (
                enrollment.end_date
                and session_date
                > getdate(
                    enrollment.end_date
                )
            ):
                continue

            eligible_session_names.append(
                session.name
            )

    # ---------------------------------------------------------
    # Attendance
    # ---------------------------------------------------------

    sessions_attended = 0
    attendance_percentage = 0

    if eligible_session_names:
        sessions_attended = (
            frappe.db.count(
                "Club100 Attendance",
                {
                    "member":
                        member.name,

                    "session": [
                        "in",
                        eligible_session_names,
                    ],

                    "attendance_status": [
                        "in",
                        [
                            "Present",
                            "Partial",
                        ],
                    ],
                },
            )
        )

        attendance_percentage = round(
            (
                sessions_attended
                / len(
                    eligible_session_names
                )
            )
            * 100
        )

    # ---------------------------------------------------------
    # Program completion
    # ---------------------------------------------------------

    completion_percentage = (
        enrollment.completion_percentage
        if enrollment.completion_percentage
        is not None
        else 0
    )

    # Until completion_percentage is maintained explicitly,
    # use elapsed program week as the fallback.
    if not completion_percentage:
        completion_percentage = round(
            (
                current_week
                / total_weeks
            )
            * 100
        )

        completion_percentage = min(
            completion_percentage,
            100,
        )

    # ---------------------------------------------------------
    # Response
    # ---------------------------------------------------------

    return {
        "id":
            program.name,

        "name":
            program.program_name,

        "startDate": (
            str(
                enrollment.start_date
            )
            if enrollment.start_date
            else None
        ),

        "endDate": (
            str(
                enrollment.end_date
            )
            if enrollment.end_date
            else None
        ),

        "currentWeek":
            current_week,

        "totalWeeks":
            total_weeks,

        "completionPercentage":
            completion_percentage,

        "sessionsCompleted":
            sessions_attended,

        "attendancePercentage":
            attendance_percentage,

        "sessionDurationMinutes": (
            program.session_duration_minutes
            or 60
        ),

        "enrollmentId":
            enrollment.name,

        "cohortId":
            enrollment.cohort,
    }