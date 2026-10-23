"""Member-specific virtual Self workouts. No future Session records are created.

Phase 2: schedule is derived from current enrollment slots, starting at enrollment
start_date; immutable schedule revision history is a subsequent requirement.
"""
from datetime import datetime, timedelta
import json

import frappe
from frappe.utils import cint, getdate, now_datetime, get_datetime

from club100_core.api.member import _get_current_member
from club100_core.curriculum.self_schedule import self_curriculum_context, curriculum_for_slot

WEEKDAYS = {name: index for index, name in enumerate((
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
))}


def _enrollment(member, enrollment_id):
    if not enrollment_id:
        frappe.throw("Enrollment ID is required", frappe.ValidationError)
    row = frappe.db.get_value(
        "Club100 Enrollment",
        {"name": enrollment_id, "member": member.name, "status": "Active", "pricing_model": "Online Plan"},
        ["name", "member", "program", "plan", "start_date", "end_date", "status"],
        as_dict=True,
    )
    if not row or not row.plan:
        frappe.throw("Active Self enrollment not found", frappe.PermissionError)
    today = getdate(now_datetime())
    if row.start_date and today < getdate(row.start_date):
        frappe.throw("Self enrollment has not started yet", frappe.PermissionError)
    if row.end_date and today > getdate(row.end_date):
        frappe.throw("Self enrollment has expired", frappe.PermissionError)
    return row


def _configuration(enrollment):
    plan = frappe.db.get_value(
        "Club100 Plan", enrollment.plan,
        ["plan_code", "self_workouts_per_week", "workout_access_after_minutes"],
        as_dict=True,
    )
    if not plan or plan.plan_code != "self":
        frappe.throw("Enrollment is not a Self plan", frappe.ValidationError)
    if not 1 <= cint(plan.self_workouts_per_week) <= 7:
        frappe.throw("Self plan is not fully configured", frappe.ValidationError)
    # Curriculum is authoritative; the retired legacy cycle is never consulted.
    _require_standard_curriculum(enrollment.program)
    slots = frappe.get_all(
        "Club100 Self Workout Slot", filters={"parent": enrollment.name, "parenttype": "Club100 Enrollment", "parentfield": "self_workout_slots"},
        fields=["weekday", "start_time"], order_by="idx asc",
    )
    if len(slots) != cint(plan.self_workouts_per_week) or len({s.weekday for s in slots}) != len(slots):
        frappe.throw("Self enrollment schedule is incomplete", frappe.ValidationError)
    for slot in slots:
        if slot.weekday not in WEEKDAYS or slot.start_time is None:
            frappe.throw("Invalid Self workout slot", frappe.ValidationError)
    return plan, None, [], slots


def _revisions(enrollment, baseline_slots):
    """Return dated schedule snapshots, or legacy enrollment slots before first edit."""
    rows = frappe.get_all(
        "Club100 Self Schedule Revision",
        filters={"enrollment": enrollment.name},
        fields=["name", "effective_from"], order_by="effective_from asc, creation asc",
    )
    if not rows:
        return [(getdate(enrollment.start_date), baseline_slots)]
    snapshots = []
    for row in rows:
        slots = frappe.get_all("Club100 Self Workout Slot",
            filters={"parent": row.name, "parenttype": "Club100 Self Schedule Revision",
                     "parentfield": "self_workout_slots"},
            fields=["weekday", "start_time"], order_by="idx asc")
        snapshots.append((getdate(row.effective_from), slots))
    return snapshots


def _scheduled(enrollment, slots, start, finish):
    anchor = getdate(enrollment.start_date)
    if not anchor:
        frappe.throw("Enrollment start date is required", frappe.ValidationError)
    cutoff = getdate(enrollment.end_date) if enrollment.end_date else None
    first = max(start, anchor)
    last = min(finish, cutoff) if cutoff else finish
    if first > last:
        return []
    revisions = _revisions(enrollment, slots)
    # Count all eligible historical calendar slots, not just requested dates.
    # This preserves sequence numbers when future slots change.
    revision_idx = 0
    ordinal = 0
    result = []
    for day_offset in range((last - anchor).days + 1):
        day = anchor + timedelta(days=day_offset)
        while (revision_idx + 1 < len(revisions)
               and revisions[revision_idx + 1][0] <= day):
            revision_idx += 1
        slot_map = {WEEKDAYS[s.weekday]: s for s in revisions[revision_idx][1]}
        slot = slot_map.get(day.weekday())
        if slot is None:
            continue
        if day >= first:
            raw_time = str(slot.start_time).split(".")[0]
            if len(raw_time.split(":")) == 2:
                raw_time += ":00"
            raw_time = raw_time.zfill(8)
            scheduled_at = datetime.fromisoformat(f"{day.isoformat()}T{raw_time}")
            result.append((ordinal, scheduled_at))
        ordinal += 1
    return result


def _parse_slots(slots, expected):
    if isinstance(slots, str):
        try:
            slots = json.loads(slots)
        except ValueError:
            frappe.throw("Invalid JSON schedule", frappe.ValidationError)
    if not isinstance(slots, list) or len(slots) != expected:
        frappe.throw(f"Select exactly {expected} workout slots", frappe.ValidationError)
    seen = set()
    parsed = []
    for slot in slots:
        if not isinstance(slot, dict):
            frappe.throw("Invalid workout slot", frappe.ValidationError)
        weekday = slot.get("weekday")
        raw_time = str(slot.get("start_time") or "")
        if weekday not in WEEKDAYS or weekday in seen:
            frappe.throw("Workouts must have distinct valid weekdays", frappe.ValidationError)
        seen.add(weekday)
        try:
            time = datetime.strptime(raw_time, "%H:%M:%S").time() if len(raw_time)==8 else datetime.strptime(raw_time, "%H:%M").time()
        except ValueError:
            frappe.throw("Invalid workout start time", frappe.ValidationError)
        parsed.append({"weekday": weekday, "start_time": time.strftime("%H:%M:%S")})
    return parsed


@frappe.whitelist(methods=["POST"])
def revise_self_workout_schedule(enrollment_id, effective_from, slots):
    """Append an immutable future schedule snapshot. Past workouts remain unchanged."""
    enrollment = _enrollment(_get_current_member(), enrollment_id)
    plan, cycle, ids, baseline = _configuration(enrollment)
    effective = getdate(effective_from)
    today = getdate(now_datetime())
    if effective <= today:
        frappe.throw("Schedule changes must start on a future date", frappe.ValidationError)
    if enrollment.end_date and effective > getdate(enrollment.end_date):
        frappe.throw("Schedule change is after enrollment end date", frappe.ValidationError)
    parsed = _parse_slots(slots, cint(plan.self_workouts_per_week))
    # Serialize updates for the same enrollment in concurrent requests.
    frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE", enrollment.name)
    revisions = frappe.get_all("Club100 Self Schedule Revision",
        filters={"enrollment": enrollment.name},
        fields=["name", "effective_from"], order_by="effective_from asc")
    if revisions and effective <= getdate(revisions[-1].effective_from):
        frappe.throw("New schedule must start after the latest revision", frappe.ValidationError)
    if not revisions:
        baseline_doc = frappe.get_doc({
            "doctype": "Club100 Self Schedule Revision", "enrollment": enrollment.name,
            "effective_from": enrollment.start_date, "self_workout_slots": [
                {"weekday": s.weekday, "start_time": str(s.start_time)} for s in baseline],
            "notes": "Original enrollment schedule snapshot",
        })
        baseline_doc.insert(ignore_permissions=True)
    new_doc = frappe.get_doc({
        "doctype": "Club100 Self Schedule Revision", "enrollment": enrollment.name,
        "effective_from": effective, "self_workout_slots": parsed,
    })
    new_doc.insert(ignore_permissions=True)
    return {"revision": new_doc.name, "effectiveFrom": str(effective), "slots": parsed}


def _require_standard_curriculum(program):
    # One shared curriculum engine, independent of the deprecated Self Cycle.
    if not frappe.db.exists("Club100 Program Curriculum", {
        "program": program, "track": "Standard", "level_code": "L1", "active": 1,
    }):
        frappe.throw("An active Standard curriculum is required", frappe.ValidationError)


def _legacy_activity(workout_id):
    # Old started/completed workouts retain their original content references.
    return frappe.db.get_value("Club100 Self Workout Activity", {"workout_id": workout_id},
                               ["name", "workout_content", "curriculum_snapshot"], as_dict=True)


def _duration_minutes(text):
    # Workbooks express duration as e.g. '50–55 min'. Never use it for permission
    # without fallback, since access duration must remain explicit and predictable.
    numbers = __import__('re').findall(r"\d+", str(text or ""))
    return int(numbers[0]) if numbers else 60


def _build(enrollment, plan, cycle, content_ids, content, ordinal, scheduled_at, now, include_video=False, curriculum_context=None):
    workout_id = f"{enrollment.name}:{scheduled_at.date().isoformat()}:{ordinal + 1}"
    activity = _legacy_activity(workout_id)
    historical_unassigned = False
    prescribed = curriculum_for_slot(curriculum_context, ordinal, scheduled_at) if curriculum_context else None
    old_content = None
    if activity and activity.workout_content:
        old_content = frappe.db.get_value("Club100 Workout Content", activity.workout_content,
            ["name", "title", "format", "fitness_level", "duration_minutes", "equipment_required", "thumbnail", "instructions", "video_url"], as_dict=True)
    snapshot = None
    if activity and activity.curriculum_snapshot:
        try:
            snapshot = json.loads(activity.curriculum_snapshot)
        except (ValueError, TypeError):
            frappe.throw("Stored curriculum snapshot is invalid", frappe.ValidationError)
    # Historical legacy workouts use the original recorded Workout Content.
    if old_content and not snapshot:
        title = old_content.title
        fmt = old_content.format
        level = old_content.fitness_level
        duration = cint(old_content.duration_minutes)
        equipment = old_content.equipment_required
        thumb = old_content.thumbnail
        content_name = old_content.name
        active_prescription = None
    else:
        active_prescription = snapshot or prescribed
        if not active_prescription:
            # Before the journey was introduced, unstarted calendar slots had no
            # durable workout-content assignment. Do not invent a retroactive
            # prescription or fail the entire month-long schedule response.
            journey = curriculum_context.get("journey") if curriculum_context else None
            if not journey or scheduled_at >= get_datetime(journey.creation):
                frappe.throw("No assigned curriculum for this workout slot", frappe.ValidationError)
            historical_unassigned = True
            title = "Past workout (not assigned)"
            fmt = None
            level = None
            duration = 60
            equipment = None
            thumb = None
            content_name = None
        else:
            detail = active_prescription["prescription"]
            title = detail["session_title"]
            fmt = detail["workout_format"]
            level = {"L1":"Core","L2":"Strong","L3":"Performance"}.get(active_prescription["levelCode"])
            duration = _duration_minutes(detail.get("duration"))
            equipment = detail.get("equipment")
            thumb = None
            content_name = None
    grace = max(0, cint(plan.workout_access_after_minutes))
    end_at = scheduled_at + timedelta(minutes=duration + grace)
    state = "Expired" if historical_unassigned else (
        "Locked" if now < scheduled_at else ("Available" if now <= end_at else "Expired")
    )
    result = {
        "id": workout_id, "enrollment": enrollment.name, "sequenceNo": ordinal + 1,
        "cycle": (active_prescription["curriculum"] if active_prescription else "Historical"),
        "workoutContent": content_name, "title": title, "format": fmt,
        "fitnessLevel": level, "durationMinutes": duration,
        "equipmentRequired": equipment, "thumbnail": thumb,
        "scheduledDate": scheduled_at.date().isoformat(),
        "startTime": scheduled_at.time().isoformat(),
        "availableFrom": scheduled_at.isoformat(), "availableUntil": end_at.isoformat(),
        "accessState": state,
        "curriculumWorkout": active_prescription,
    }
    if include_video:
        detail = (active_prescription or {}).get("prescription") or {}
        result["instructions"] = old_content.instructions if old_content and not snapshot else None
        result["videoUrl"] = (old_content.video_url if old_content and not snapshot and state == "Available" else None)
        result["prescription"] = detail
    return result


@frappe.whitelist(methods=["GET"])
def get_self_workout_schedule_settings(enrollment_id):
    """Return the member-owned weekly schedule without requiring it to exist."""
    enrollment = _enrollment(_get_current_member(), enrollment_id)
    plan = frappe.db.get_value("Club100 Plan", enrollment.plan,
        ["plan_code", "self_workouts_per_week"], as_dict=True)
    if not plan or plan.plan_code != "self":
        frappe.throw("Enrollment is not a Self plan", frappe.ValidationError)
    count = cint(plan.self_workouts_per_week)
    if not 1 <= count <= 7:
        frappe.throw("Self plan is not fully configured", frappe.ValidationError)
    _require_standard_curriculum(enrollment.program)
    baseline = frappe.get_all("Club100 Self Workout Slot",
        filters={"parent": enrollment.name, "parenttype": "Club100 Enrollment",
                 "parentfield": "self_workout_slots"},
        fields=["weekday", "start_time"], order_by="idx asc")
    revisions = frappe.get_all("Club100 Self Schedule Revision",
        filters={"enrollment": enrollment.name},
        fields=["name", "effective_from"], order_by="effective_from asc, creation asc")
    today = getdate(now_datetime())
    current = baseline
    pending = []
    for rev in revisions:
        rows = frappe.get_all("Club100 Self Workout Slot",
            filters={"parent": rev.name, "parenttype": "Club100 Self Schedule Revision",
                     "parentfield": "self_workout_slots"},
            fields=["weekday", "start_time"], order_by="idx asc")
        if getdate(rev.effective_from) <= today:
            current = rows
        else:
            pending.append({"effectiveFrom": str(rev.effective_from),
                            "slots": [{"weekday": r.weekday, "start_time": str(r.start_time)} for r in rows]})
    return {"enrollment": enrollment.name, "workoutsPerWeek": count,
            "configured": len(baseline) == count,
            "slots": [{"weekday": r.weekday, "start_time": str(r.start_time)} for r in current],
            "pendingRevisions": pending,
            "earliestChangeDate": str(today + timedelta(days=1))}


@frappe.whitelist(methods=["POST"])
def set_initial_self_workout_schedule(enrollment_id, slots):
    """Only the owning member may populate an empty enrollment schedule once."""
    enrollment = _enrollment(_get_current_member(), enrollment_id)
    plan = frappe.db.get_value("Club100 Plan", enrollment.plan,
        ["plan_code", "self_workouts_per_week"], as_dict=True)
    if not plan or plan.plan_code != "self":
        frappe.throw("Self plan is not fully configured", frappe.ValidationError)
    _require_standard_curriculum(enrollment.program)
    count = cint(plan.self_workouts_per_week)
    if not 1 <= count <= 7:
        frappe.throw("Self plan is not fully configured", frappe.ValidationError)
    parsed = _parse_slots(slots, count)
    frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE", enrollment.name)
    if frappe.db.exists("Club100 Self Workout Slot",
            {"parent": enrollment.name, "parentfield": "self_workout_slots"}):
        frappe.throw("Schedule already exists; use schedule revisions", frappe.ValidationError)
    if frappe.db.exists("Club100 Self Schedule Revision", {"enrollment": enrollment.name}):
        frappe.throw("Schedule revisions already exist", frappe.ValidationError)
    doc = frappe.get_doc("Club100 Enrollment", enrollment.name)
    doc.set("self_workout_slots", parsed)
    doc.save(ignore_permissions=True)  # Ownership checked above, normal DocType validations still run.
    return {"enrollment": enrollment.name, "configured": True, "slots": parsed}

@frappe.whitelist(methods=["GET"])
def list_virtual_workouts(enrollment_id, from_date=None, to_date=None):
    """List metadata for at most 32 consecutive dates (video URL never included)."""
    enrollment = _enrollment(_get_current_member(), enrollment_id)
    now = now_datetime()
    start = getdate(from_date) if from_date else getdate(now)
    finish = getdate(to_date) if to_date else start + timedelta(days=13)
    if finish < start or (finish - start).days > 31:
        frappe.throw("Select a date window of 1 to 32 days", frappe.ValidationError)
    if start < getdate(now) - timedelta(days=90) or finish > getdate(now) + timedelta(days=90):
        frappe.throw("Date window must be within 90 days of today", frappe.ValidationError)
    plan, cycle, ids, slots = _configuration(enrollment)
    curriculum_context = self_curriculum_context(enrollment)
    records = [_build(enrollment, plan, cycle, ids, None, ordinal, when, now, curriculum_context=curriculum_context)
               for ordinal, when in _scheduled(enrollment, slots, start, finish)]
    return {"enrollment": enrollment.name, "workouts": [r for r in records if r]}


@frappe.whitelist(methods=["GET"])
def virtual_workout_detail(workout_id):
    """Recalculate member access server-side; return video only while Available."""
    parts = str(workout_id or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        frappe.throw("Invalid virtual workout ID", frappe.ValidationError)
    enrollment = _enrollment(_get_current_member(), parts[0])
    day = getdate(parts[1])
    now = now_datetime()
    if day < getdate(now) - timedelta(days=90) or day > getdate(now) + timedelta(days=90):
        frappe.throw("Workout outside supported date window", frappe.ValidationError)
    plan, cycle, ids, slots = _configuration(enrollment)
    dates = _scheduled(enrollment, slots, day, day)
    if not dates or dates[0][0] + 1 != int(parts[2]):
        frappe.throw("Virtual workout not found", frappe.DoesNotExistError)
    workout = _build(enrollment, plan, cycle, ids, None, dates[0][0], dates[0][1], now, True,
                     curriculum_context=self_curriculum_context(enrollment))
    if not workout:
        frappe.throw("Virtual workout not found", frappe.DoesNotExistError)
    return workout
