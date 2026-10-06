import frappe

from frappe.utils import (
    add_to_date,
    get_datetime,
    getdate,
    now_datetime,
)

from club100_core.api.push_notifications import (
    send_push_to_member,
)


REMINDER_MINUTES = 30


def _session_datetime(
    session_date,
    start_time,
):
    if not session_date or not start_time:
        return None

    return get_datetime(
        f"{session_date} {start_time}"
    )


def _get_session_members(session):
    enrollment_rows = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "cohort":
                session.cohort,

            "status":
                ["!=", "Cancelled"],
        },
        fields=[
            "member",
            "start_date",
            "end_date",
        ],
    )

    members = []

    session_date = getdate(
        session.session_date
    )

    for enrollment in enrollment_rows:
        if not enrollment.member:
            continue

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

        members.append(
            enrollment.member
        )

    return list(set(members))


def _get_session_title(session):
    if not session.program:
        return "Club100 Session"

    program_name = frappe.db.get_value(
        "Club100 Program",
        session.program,
        "program_name",
    )

    return (
        program_name
        or "Club100 Session"
    )


def send_session_reminder(
    session_name,
):
    session = frappe.get_doc(
        "Club100 Session",
        session_name,
    )

    # Only scheduled sessions should receive reminders.
    if session.status != "Scheduled":
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "reason": "Session is not Scheduled",
        }

    # Prevent duplicate 30-minute reminders.
    if session.get(
        "30_min_reminder_sent"
    ):
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "reason":
                "30-minute reminder already sent",
        }

    members = _get_session_members(
        session
    )

    if not members:
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "reason":
                "No eligible members",
        }

    session_title = (
        _get_session_title(
            session
        )
    )

    total_sent = 0
    total_failed = 0

    for member in members:
        result = send_push_to_member(
            member=member,

            title=(
                "Your Club100 session "
                "starts in 30 minutes"
            ),

            body=session_title,

            url=(
                f"/session/"
                f"{session.name}"
            ),
        )

        total_sent += (
            result["sent"]
        )

        total_failed += (
            result["failed"]
        )

    # Mark the session only if at least one
    # notification was successfully delivered.
    if total_sent > 0:
        frappe.db.set_value(
            "Club100 Session",
            session.name,
            "30_min_reminder_sent",
            1,
        )

        frappe.db.commit()

    return {
        "sent":
            total_sent,

        "failed":
            total_failed,

        "skipped":
            False,
    }

def process_session_reminders():
    now = now_datetime()

    window_start = add_to_date(
        now,
        minutes=24,
    )

    window_end = add_to_date(
        now,
        minutes=30,
    )

    logger = frappe.logger("club100")

    logger.info(
        f"Session reminder scheduler running. "
        f"Now={now}, "
        f"Window={window_start} to {window_end}"
    )

    sessions = frappe.get_all(
        "Club100 Session",
        filters={
            "status":
                "Scheduled",

            "30_min_reminder_sent":
                0,

            "session_date":
                [
                    "between",
                    [
                        getdate(
                            window_start
                        ),
                        getdate(
                            window_end
                        ),
                    ],
                ],
        },
        fields=[
            "name",
            "session_date",
            "start_time",
        ],
    )

    logger.info(
        f"Candidate sessions: "
        f"{len(sessions)}"
    )

    processed = 0

    for row in sessions:
        session_dt = (
            _session_datetime(
                row.session_date,
                row.start_time,
            )
        )

        logger.info(
            f"Checking {row.name}: "
            f"session_dt={session_dt}"
        )

        if not session_dt:
            continue

        if not (
            window_start
            <= session_dt
            <= window_end
        ):
            logger.info(
                f"Skipping {row.name}: "
                f"outside reminder window"
            )
            continue

        result = (
            send_session_reminder(
                row.name
            )
        )

        processed += 1

        logger.info(
            f"Reminder result "
            f"{row.name}: {result}"
        )

    return {
        "now":
            str(now),

        "window_start":
            str(window_start),

        "window_end":
            str(window_end),

        "candidates":
            len(sessions),

        "processed":
            processed,
    }