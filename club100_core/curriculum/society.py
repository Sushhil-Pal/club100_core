"""Read-only Society cohort curriculum lookup. No attendance or enrollment mutations.

Only existing cohort sessions determine progression; a new member joins that position.
An authored, active MIXED Society curriculum is required. Cancelled sessions do not
consume sequence positions; historical rescheduling should be reviewed before launch.
"""
import frappe
from frappe.utils import cint

FORMATS = ("Power", "Flow", "Pulse", "Play")


def position_from_ordinal(ordinal):
    """Zero-based cohort session ordinal to 12-week, 4-session repeating position."""
    index = int(ordinal)
    if index < 0:
        raise ValueError("Ordinal must not be negative")
    return {"cycleNumber": index // 48 + 1,
            "weekNumber": (index % 48) // 4 + 1,
            "sessionNumber": index % 4 + 1,
            "format": FORMATS[index % 4]}


def validate_society_curriculum(curriculum):
    if curriculum.track != "Society" or curriculum.level_code != "MIXED":
        frappe.throw("Expected Society MIXED curriculum", frappe.ValidationError)
    if cint(curriculum.weeks_per_cycle) != 12 or cint(curriculum.sessions_per_week) != 4 or not cint(curriculum.repeat_cycle):
        frappe.throw("Society curriculum must repeat every 12 weeks with four sessions/week", frappe.ValidationError)
    rows = {(cint(s.week_number), cint(s.session_number)): s for s in curriculum.sessions}
    if len(rows) != 48 or len(curriculum.sessions) != 48:
        frappe.throw("Society curriculum must contain 48 unique sessions", frappe.ValidationError)
    for week in range(1, 13):
        for session in range(1, 5):
            row = rows.get((week, session))
            if not row or row.workout_format != FORMATS[session - 1]:
                frappe.throw("Society session rotation must be Power/Flow/Pulse/Play", frappe.ValidationError)
            for level in ("core_variation", "strong_variation", "performance_variation"):
                if not (getattr(row, level, None) or "").strip():
                    frappe.throw("Missing %s for Week %s Session %s" % (level, week, session), frappe.ValidationError)
    return rows


def preview_society_cohort_session(cohort_id, session_id):
    """Administrative read-only preview; not exposed as unauthenticated web API."""
    if frappe.session.user == "Guest" or not frappe.has_permission("Club100 Cohort", "read", cohort_id):
        frappe.throw("Not permitted to preview cohort curriculum", frappe.PermissionError)
    cohort = frappe.get_doc("Club100 Cohort", cohort_id)
    if not cohort.society or cohort.delivery_mode != "Onsite":
        frappe.throw("Only onsite Society cohorts are supported", frappe.ValidationError)
    matches = frappe.get_all("Club100 Program Curriculum", filters={
        "program": cohort.program, "track": "Society", "level_code": "MIXED", "active": 1}, pluck="name", limit_page_length=3)
    if len(matches) != 1:
        frappe.throw("Expected exactly one active Society MIXED curriculum for cohort program", frappe.ValidationError)
    curriculum = frappe.get_doc("Club100 Program Curriculum", matches[0])
    rows = validate_society_curriculum(curriculum)
    # Stable sequence among non-cancelled planned/delivered sessions. No attendance reads.
    sessions = frappe.get_all("Club100 Session", filters={
        "cohort": cohort.name, "status": ["in", ["Scheduled", "Live", "Completed"]]},
        fields=["name", "session_date", "start_time", "workout_format"],
        order_by="session_date asc, start_time asc, creation asc, name asc", limit_page_length=0)
    names = [s.name for s in sessions]
    if session_id not in names:
        frappe.throw("Session is not an eligible session in this cohort", frappe.DoesNotExistError)
    ordinal = names.index(session_id)
    position = position_from_ordinal(ordinal)
    s = sessions[ordinal]
    row = rows[(position["weekNumber"], position["sessionNumber"])]
    # Existing format may use a linked Workout Format name rather than these labels.
    # Do not guess conversion; surface both for explicit integration verification.
    return {"cohort": cohort.name, "sessionId": s.name, "scheduledDate": str(s.session_date),
            "scheduledFormatLink": s.workout_format, "curriculum": curriculum.name,
            **position, "title": row.session_title, "objective": row.objective,
            "warmUp": row.warm_up, "mainBlockA": row.main_block_a,
            "mainBlockB": row.main_block_b, "finisher": row.finisher_skill,
            "cooldown": row.cooldown, "duration": row.duration,
            "core": row.core_variation, "strong": row.strong_variation,
            "performance": row.performance_variation, "trainerCues": row.trainer_cues}
