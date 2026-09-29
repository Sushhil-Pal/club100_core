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


def _get_authorized_session(session_id, member_name):
    enrollment = _get_active_enrollment(member_name)

    session = frappe.db.get_value(
        "Club100 Session",
        session_id,
        [
            "name",
            "cohort",
            "status",
            "delivery_mode",
        ],
        as_dict=True,
    )

    if not session:
        frappe.throw("Session not found")

    if not enrollment.cohort:
        frappe.throw(
            "Your enrollment is not assigned to a cohort"
        )

    if session.cohort != enrollment.cohort:
        frappe.throw(
            "You are not enrolled in this session",
            frappe.PermissionError,
        )

    if session.status == "Cancelled":
        frappe.throw("This session has been cancelled")

    return session


@frappe.whitelist()
def join_session(session_id):
    member = _get_current_member()

    session = _get_authorized_session(
        session_id,
        member.name,
    )

    existing_attendance = frappe.db.get_value(
        "Club100 Attendance",
        {
            "member": member.name,
            "session": session.name,
        },
        "name",
    )

    if existing_attendance:
        attendance = frappe.get_doc(
            "Club100 Attendance",
            existing_attendance,
        )

        # Don't overwrite the original join time if
        # the member reconnects to the session.
        if not attendance.join_time:
            attendance.join_time = now_datetime()

        attendance.attendance_status = "Present"
        attendance.save(ignore_permissions=True)

    else:
        attendance = frappe.get_doc(
            {
                "doctype": "Club100 Attendance",
                "member": member.name,
                "session": session.name,
                "attendance_mode": "Automatic",
                "source": "Other",
                "join_time": now_datetime(),
                "attendance_status": "Present",
            }
        )

        attendance.insert(ignore_permissions=True)

    frappe.db.commit()

    return {
        "success": True,
        "attendanceId": attendance.name,
        "sessionId": session.name,
        "joinedAt": str(attendance.join_time),
    }


@frappe.whitelist()
def leave_session(session_id):
    member = _get_current_member()

    session = _get_authorized_session(
        session_id,
        member.name,
    )

    attendance_name = frappe.db.get_value(
        "Club100 Attendance",
        {
            "member": member.name,
            "session": session.name,
        },
        "name",
    )

    if not attendance_name:
        frappe.throw(
            "Attendance record not found for this session"
        )

    attendance = frappe.get_doc(
        "Club100 Attendance",
        attendance_name,
    )

    attendance.leave_time = now_datetime()

    # Club100 Attendance.validate() should calculate
    # minutes_attended from join_time and leave_time.
    attendance.save(ignore_permissions=True)

    frappe.db.commit()

    return {
        "success": True,
        "attendanceId": attendance.name,
        "sessionId": session.name,
        "joinedAt": (
            str(attendance.join_time)
            if attendance.join_time
            else None
        ),
        "leftAt": (
            str(attendance.leave_time)
            if attendance.leave_time
            else None
        ),
        "minutesAttended": attendance.minutes_attended or 0,
        "attendanceStatus": attendance.attendance_status,
    }