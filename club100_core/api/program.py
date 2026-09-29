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
        frappe.throw("Club100 member profile not found")   

    enrollments = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "member": member.name,
            "status": ["in", ["Registered", "Active"]],
        },
        fields=[
            "name",
            "program",
            "cohort",
            "start_date",
            "end_date",
            "completion_percentage",
        ],
        order_by="start_date desc, enrollment_date desc",
        limit=1,
    )

    if not enrollments:
        return None
    
    enrollment = enrollments[0]

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
        frappe.throw("Club100 program not found")

    total_weeks = program.duration_weeks or 1
    current_week = 1

    if enrollment.start_date:
        start_date = getdate(enrollment.start_date)
        today = getdate(nowdate())

        if today >= start_date:
            days_elapsed = (today - start_date).days
            current_week = (days_elapsed // 7) + 1

            current_week = min(
                current_week,
                total_weeks,
            )

    # ---------------------------------------
    # Completed sessions
    # ---------------------------------------

    completed_sessions = 0
    attendance_percentage = 0

    if enrollment.cohort:
        session_names = frappe.get_all(
            "Club100 Session",
            filters={
                "cohort": enrollment.cohort,
                "status": "Completed",
            },
            pluck="name",
        )

        total_completed_sessions = len(session_names)

        if session_names:
            completed_sessions = frappe.db.count(
                "Club100 Attendance",
                {
                    "member": member.name,
                    "session": ["in", session_names],
                    "attendance_status": [
                        "in",
                        ["Present", "Partial"],
                    ],
                },
            )

            attendance_percentage = round(
                (
                    completed_sessions
                    / total_completed_sessions
                )
                * 100
            )

    # ---------------------------------------
    # Program completion
    # ---------------------------------------

    completion_percentage = (
        enrollment.completion_percentage
        if enrollment.completion_percentage is not None
        else 0
    )

    # Until we start maintaining completion_percentage
    # automatically, calculate it from program progress.
    if not completion_percentage:
        completion_percentage = round(
            (current_week / total_weeks) * 100
        )

        completion_percentage = min(
            completion_percentage,
            100,
        )

    return {
        "id": program.name,
        "name": program.program_name,
        "startDate": (
            str(enrollment.start_date)
            if enrollment.start_date
            else None
        ),
        "endDate": (
            str(enrollment.end_date)
            if enrollment.end_date
            else None
        ),
        "currentWeek": current_week,
        "totalWeeks": total_weeks,
        "completionPercentage": completion_percentage,
        "sessionsCompleted": completed_sessions,
        "attendancePercentage": attendance_percentage,
        "sessionDurationMinutes": (
            program.session_duration_minutes or 60
        ),
        "enrollmentId": enrollment.name,
        "cohortId": enrollment.cohort,
    }