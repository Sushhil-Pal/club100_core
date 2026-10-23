"""Read-only curriculum preview for Member Self and Trainer PWA.

Does NOT assign workouts, advance journeys, change enrollments or grant video access.
"""
import frappe
from frappe.utils import cint
from club100_core.curriculum import journey as journey_service
from club100_core.api.member import _get_current_member
from club100_core.api.trainer import _require_trainer

SESSION_FIELDS = (
    "week_number", "session_number", "training_block", "workout_format",
    "format_session_number", "session_title", "objective", "warm_up",
    "main_block_a", "main_block_b", "finisher_skill", "cooldown",
    "intensity", "equipment", "duration",
)


def _preview(member_id):
    journey = journey_service._get(member_id)
    if not journey:
        return {"journey": None, "session": None}
    curriculum = frappe.get_doc("Club100 Program Curriculum", journey.curriculum)
    if not curriculum.active:
        frappe.throw("The assigned curriculum is inactive", frappe.ValidationError)
    if curriculum.program != journey.program or curriculum.level_code != journey.level_code or curriculum.track != journey.track:
        frappe.throw("Training journey and curriculum do not match", frappe.ValidationError)
    week, session_no = cint(journey.week_number), cint(journey.session_number)
    rows = [row for row in curriculum.sessions
            if cint(row.week_number) == week and cint(row.session_number) == session_no]
    if len(rows) != 1:
        frappe.throw("Curriculum position must match exactly one workout", frappe.ValidationError)
    session = {key: getattr(rows[0], key, None) for key in SESSION_FIELDS}
    return {
        "journey": journey_service._summary(journey),
        "session": session,
        "curriculumCycleLength": cint(curriculum.weeks_per_cycle),
        "curriculumSessionsPerWeek": cint(curriculum.sessions_per_week),
        "previewOnly": True,
    }


@frappe.whitelist(methods=["GET"])
def my_next_curriculum_workout(enrollment_id=None):
    """Preview the current curriculum slot for the authenticated Self member."""
    member = _get_current_member()
    journey_service._self_membership(member.name, enrollment_id)
    return _preview(member.name)


@frappe.whitelist(methods=["GET"])
def trainer_next_curriculum_workout(member_id, enrollment_id=None):
    """Preview the current curriculum slot for a trainer and eligible member."""
    _require_trainer()
    journey_service._eligible(member_id, enrollment_id)
    return _preview(member_id)


def verify_curriculum_positions():
    """Read-only import integrity check for every active Standard curriculum."""
    names = frappe.get_all("Club100 Program Curriculum", filters={"active": 1, "track": "Standard"}, pluck="name")
    results = []
    for name in names:
        curriculum = frappe.get_doc("Club100 Program Curriculum", name)
        width = cint(curriculum.sessions_per_week)
        weeks = cint(curriculum.weeks_per_cycle)
        expected = {(w, s) for w in range(1, weeks + 1) for s in range(1, width + 1)}
        found = [(cint(r.week_number), cint(r.session_number)) for r in curriculum.sessions]
        actual = set(found)
        results.append({"curriculum": name, "expected": len(expected), "found": len(found),
                        "missing": sorted(expected - actual), "duplicates": len(found) - len(actual),
                        "extra": sorted(actual - expected),
                        "ok": len(found) == len(expected) and actual == expected})
    return results
