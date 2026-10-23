"""Phase 2 journey API. Does not alter live workout scheduling or attendance."""
import frappe
from datetime import timedelta
from frappe.utils import cint, getdate, now_datetime
from club100_core.api.member import _get_current_member
from club100_core.api.trainer import _require_trainer
from club100_core.api.memberships import _active_memberships

LEVEL_NAMES = {"L1": "Core", "L2": "Strong", "L3": "Performance"}


def _resolve_curriculum(program, level, track="Standard"):
    name = frappe.db.get_value("Club100 Program Curriculum", {
        "program": program, "track": track, "level_code": level, "active": 1,
    }, "name")
    if not name:
        frappe.throw("No active curriculum for the selected program and level", frappe.ValidationError)
    return name


def _eligible(member, enrollment_id=None):
    memberships = _active_memberships(member)
    if enrollment_id:
        memberships = [m for m in memberships if m.name == enrollment_id]
    if not memberships:
        frappe.throw("An active membership is required to start or change a training journey", frappe.PermissionError)
    return memberships[0]


def _self_membership(member, enrollment_id=None):
    matches = []
    for row in _active_memberships(member):
        if enrollment_id and row.name != enrollment_id:
            continue
        if row.pricing_model != "Online Plan" or not row.plan:
            continue
        if frappe.db.get_value("Club100 Plan", row.plan, "plan_code") == "self":
            matches.append(row)
    if not matches:
        frappe.throw("An active Self membership is required", frappe.PermissionError)
    return matches[0]


def _self_schedule_position(doc):
    """Read-only projection of the next Self workout, including prior enrollments."""
    if doc.track != "Standard":
        return None

    from club100_core.api.self_workouts import _configuration, _scheduled
    from club100_core.curriculum.self_schedule import previous_self_slot_count

    current = now_datetime()

    for enrollment in _active_memberships(doc.member):
        if enrollment.pricing_model != "Online Plan" or not enrollment.plan:
            continue
        if enrollment.program != doc.program:
            continue
        if frappe.db.get_value("Club100 Plan", enrollment.plan, "plan_code") != "self":
            continue

        plan, cycle, content_ids, slots = _configuration(enrollment)
        scheduled = _scheduled(
            enrollment,
            slots,
            getdate(current),
            getdate(current + timedelta(days=14)),
        )

        upcoming = [
            (ordinal, when)
            for ordinal, when in scheduled
            if when >= current
        ]
        if not upcoming:
            continue

        ordinal, when = upcoming[0]
        cumulative = previous_self_slot_count(enrollment) + ordinal

        return {
            "cycleNumber": cumulative // 48 + 1,
            "weekNumber": (cumulative % 48) // 4 + 1,
            "sessionNumber": cumulative % 4 + 1,
            "nextWorkoutAt": when.isoformat(),
            "positionSource": "SelfSchedule",
        }

    return None


def _summary(doc):
    result = {"member": doc.member, "program": doc.program, "track": doc.track,
              "levelCode": doc.level_code, "levelName": LEVEL_NAMES.get(doc.level_code),
              "curriculum": doc.curriculum, "cycleNumber": cint(doc.cycle_number),
              "weekNumber": cint(doc.week_number), "sessionNumber": cint(doc.session_number),
              "lastEnrollment": doc.last_enrollment, "modified": str(doc.modified)}

    position = _self_schedule_position(doc)
    if position:
        result.update(position)

    return result


def _get(member):
    if not frappe.db.exists("Club100 Member Training Journey", member):
        return None
    return frappe.get_doc("Club100 Member Training Journey", member)


def _initialize(member, membership, level="L1"):
    if level not in LEVEL_NAMES:
        frappe.throw("Invalid fitness level", frappe.ValidationError)
    curriculum = _resolve_curriculum(membership.program, level)
    # A member row lock serializes initial creation and future edits.
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member)
    existing = _get(member)
    if existing:
        return existing
    doc = frappe.get_doc({"doctype": "Club100 Member Training Journey", "member": member,
        "program": membership.program, "track": "Standard", "level_code": level,
        "curriculum": curriculum, "cycle_number": 1, "week_number": 1,
        "session_number": 1, "last_enrollment": membership.name})
    doc.insert(ignore_permissions=True)
    return doc


@frappe.whitelist(methods=["GET"])
def my_training_journey():
    member = _get_current_member()
    doc = _get(member.name)
    return {"journey": _summary(doc) if doc else None}


@frappe.whitelist(methods=["POST"])
def start_my_training_journey(enrollment_id=None):
    member = _get_current_member()
    membership = _self_membership(member.name, enrollment_id)
    doc = _initialize(member.name, membership)
    return {"journey": _summary(doc)}


@frappe.whitelist(methods=["POST"])
def trainer_start_training_journey(member_id, enrollment_id=None):
    _require_trainer()
    membership = _eligible(member_id, enrollment_id)
    doc = _initialize(member_id, membership)
    return {"journey": _summary(doc)}


def _change_level(member, level_code, source, reason=None, expected_modified=None):
    if level_code not in LEVEL_NAMES:
        frappe.throw("Invalid level", frappe.ValidationError)
    if not expected_modified:
        frappe.throw("Current journey version is required. Refresh and retry.", frappe.ValidationError)
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member)
    doc = _get(member)
    if not doc:
        frappe.throw("Training journey has not been started", frappe.ValidationError)
    if str(doc.modified) != str(expected_modified):
        frappe.throw("Training journey has changed. Refresh before trying again.", frappe.ValidationError)
    if level_code == doc.level_code:
        return {"journey": _summary(doc), "changed": False}
    curriculum = _resolve_curriculum(doc.program, level_code, doc.track)
    previous = doc.level_code
    doc.level_code = level_code
    doc.curriculum = curriculum
    # Level changes preserve curriculum position.
    # The Self schedule determines progression independently of completion.
    doc.last_changed_by = frappe.session.user
    doc.last_level_changed_on = now_datetime()
    doc.save(ignore_permissions=True)
    record = frappe.get_doc({"doctype": "Club100 Training Level Change",
        "member": member, "journey": doc.name, "previous_level": previous,
        "new_level": level_code, "previous_program": doc.program,
        "new_program": doc.program, "changed_by_user": frappe.session.user,
        "change_source": source, "reason": reason, "effective_on": now_datetime()})
    record.insert(ignore_permissions=True)
    return {"journey": _summary(doc), "changed": True}


@frappe.whitelist(methods=["POST"])
def change_my_training_level(level_code, expected_modified, reason=None, enrollment_id=None):
    member = _get_current_member()
    _self_membership(member.name, enrollment_id)
    return _change_level(member.name, level_code, "Member", reason, expected_modified)


@frappe.whitelist(methods=["POST"])
def trainer_change_training_level(member_id, level_code, expected_modified, reason=None, enrollment_id=None):
    _require_trainer()
    _eligible(member_id, enrollment_id)
    return _change_level(member_id, level_code, "Trainer", reason, expected_modified)


@frappe.whitelist(methods=["GET"])
def trainer_training_journey(member_id):
    _require_trainer()
    _eligible(member_id)
    doc = _get(member_id)
    return {"journey": _summary(doc) if doc else None}


def next_position(cycle_number, week_number, session_number, sessions_per_week=4, weeks_per_cycle=12):
    """Pure helper for later scheduler integration; no database writes."""
    cycle, week, session = cint(cycle_number), cint(week_number), cint(session_number)
    width, weeks = cint(sessions_per_week), cint(weeks_per_cycle)
    if min(cycle, week, session, width, weeks) < 1 or week > weeks or session > width:
        raise ValueError("Invalid curriculum position")
    if session < width:
        return cycle, week, session + 1
    if week < weeks:
        return cycle, week + 1, 1
    return cycle + 1, 1, 1
