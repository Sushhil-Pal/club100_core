"""Phase 3B: read-only Standard curriculum prescriptions for scheduled Self slots.

The existing virtual workout identity, legacy content/video, access controls, and
activity writes remain authoritative. Skipped slots still consume their ordinal.
"""
from datetime import datetime

import frappe
from frappe.utils import cint, get_datetime, getdate

from club100_core.curriculum.preview import SESSION_FIELDS


def self_curriculum_context(enrollment):
    """Return a preloaded snapshot; never creates journeys or updates position."""
    journey_name = frappe.db.get_value(
        "Club100 Member Training Journey", {"member": enrollment.member}, "name"
    ) if getattr(enrollment, "member", None) else None
    if not journey_name:
        return None
    journey = frappe.get_doc("Club100 Member Training Journey", journey_name)
    if journey.program != enrollment.program or journey.track != "Standard":
        return None  # Other track or program requires an explicit journey transition.
    curricula = {}
    for code in ("L1", "L2", "L3"):
        name = frappe.db.get_value("Club100 Program Curriculum", {
            "program": journey.program, "level_code": code, "track": "Standard", "active": 1
        }, "name")
        if not name:
            return None
        doc = frappe.get_doc("Club100 Program Curriculum", name)
        width, weeks = cint(doc.sessions_per_week), cint(doc.weeks_per_cycle)
        if width != 4 or weeks != 12:
            return None  # The existing Self cadence currently assumes four slots/week.
        rows = {(cint(x.week_number), cint(x.session_number)): x for x in doc.sessions}
        if len(rows) != 48:
            return None
        curricula[code] = (name, rows)
    changes = frappe.get_all("Club100 Training Level Change",
        filters={"member": journey.member},
        fields=["previous_level", "new_level", "effective_on"],
        order_by="effective_on asc, creation asc")
    return {"journey": journey, "curricula": curricula, "changes": changes,
            "ordinal_offset": previous_self_slot_count(enrollment)}


def curriculum_for_slot(ctx, ordinal, scheduled_at):
    """Match a calendar slot to Week/Session independent of completion status.

    Historical slots predating journey creation are deliberately untouched.
    Level changes become effective from the first slot at/after their timestamp.
    """
    journey = ctx["journey"]
    scheduled = get_datetime(scheduled_at)
    if scheduled < get_datetime(journey.creation):
        return None
    changes = ctx["changes"]
    level = changes[0].previous_level if changes else journey.level_code
    for change in changes:
        if get_datetime(change.effective_on) <= scheduled:
            level = change.new_level
        else:
            break
    if level not in ctx["curricula"]:
        return None
    name, rows = ctx["curricula"][level]
    cumulative = ordinal + ctx.get("ordinal_offset", 0)
    position = cumulative % 48
    week_number, session_number = position // 4 + 1, position % 4 + 1
    session = rows.get((week_number, session_number))
    if not session:
        return None
    payload = {key: getattr(session, key, None) for key in SESSION_FIELDS}
    return {
        "curriculum": name, "levelCode": level,
        "cycleNumber": cumulative // 48 + 1,
        "weekNumber": week_number, "sessionNumber": session_number,
        "prescription": payload,
        "source": "ScheduledSlot",
    }


def previous_self_slot_count(enrollment):
    """Count historical scheduled slots for completed, same-program Self enrollments.

    This deliberately excludes overlapping/future enrollments and other programs;
    completion status does not affect the ordinal (Option B). It never writes data.
    """
    from club100_core.api.self_workouts import _scheduled, _revisions

    if not enrollment.start_date:
        return 0
    current_start = getdate(enrollment.start_date)
    prior = frappe.get_all(
        "Club100 Enrollment",
        filters={"member": enrollment.member, "program": enrollment.program,
                 "pricing_model": "Online Plan", "start_date": ["<", current_start]},
        fields=["name", "member", "program", "plan", "start_date", "end_date"],
        order_by="start_date asc, creation asc",
    )
    total = 0
    for old in prior:
        if old.name == enrollment.name or not old.end_date:
            continue
        if getdate(old.end_date) >= current_start:
            # Overlapping memberships must be resolved explicitly, never double-counted.
            continue
        if not old.plan or frappe.db.get_value("Club100 Plan", old.plan, "plan_code") != "self":
            continue
        slots = frappe.get_all(
            "Club100 Self Workout Slot",
            filters={"parent": old.name, "parenttype": "Club100 Enrollment",
                     "parentfield": "self_workout_slots"},
            fields=["weekday", "start_time"], order_by="idx asc")
        revisions = _revisions(old, slots)
        if not revisions or not revisions[0][1]:
            continue  # Earlier enrollment never had a workout schedule configured.
        total += len(_scheduled(old, slots, getdate(old.start_date), getdate(old.end_date)))
    return total
