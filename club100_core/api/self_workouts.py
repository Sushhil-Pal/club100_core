"""Member-specific virtual Self workouts. No future Session records are created.

Phase 2: schedule is derived from current enrollment slots, starting at enrollment
start_date; immutable schedule revision history is a subsequent requirement.
"""
from datetime import datetime, timedelta

import frappe
from frappe.utils import cint, getdate, now_datetime

from club100_core.api.member import _get_current_member

WEEKDAYS = {name: index for index, name in enumerate((
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
))}


def _enrollment(member, enrollment_id):
    if not enrollment_id:
        frappe.throw("Enrollment ID is required", frappe.ValidationError)
    row = frappe.db.get_value(
        "Club100 Enrollment",
        {"name": enrollment_id, "member": member.name, "status": "Active", "pricing_model": "Online Plan"},
        ["name", "program", "plan", "start_date", "end_date", "status"],
        as_dict=True,
    )
    if not row or not row.plan:
        frappe.throw("Active Self enrollment not found", frappe.PermissionError)
    return row


def _configuration(enrollment):
    plan = frappe.db.get_value(
        "Club100 Plan", enrollment.plan,
        ["plan_code", "self_workouts_per_week", "self_workout_cycle", "workout_access_after_minutes"],
        as_dict=True,
    )
    if not plan or plan.plan_code != "self":
        frappe.throw("Enrollment is not a Self plan", frappe.ValidationError)
    if not 1 <= cint(plan.self_workouts_per_week) <= 7 or not plan.self_workout_cycle:
        frappe.throw("Self plan is not fully configured", frappe.ValidationError)
    cycle = frappe.get_doc("Club100 Self Workout Cycle", plan.self_workout_cycle)
    if not cycle.active or cycle.plan != enrollment.plan or cycle.program != enrollment.program:
        frappe.throw("Self workout cycle configuration is invalid", frappe.ValidationError)
    content_ids = [row.workout_content for row in sorted(cycle.workouts, key=lambda x: cint(x.sequence_no))]
    if not content_ids:
        frappe.throw("Self workout cycle has no workouts", frappe.ValidationError)
    slots = frappe.get_all(
        "Club100 Self Workout Slot", filters={"parent": enrollment.name, "parenttype": "Club100 Enrollment", "parentfield": "self_workout_slots"},
        fields=["weekday", "start_time"], order_by="idx asc",
    )
    if len(slots) != cint(plan.self_workouts_per_week) or len({s.weekday for s in slots}) != len(slots):
        frappe.throw("Self enrollment schedule is incomplete", frappe.ValidationError)
    for slot in slots:
        if slot.weekday not in WEEKDAYS or slot.start_time is None:
            frappe.throw("Invalid Self workout slot", frappe.ValidationError)
    return plan, cycle, content_ids, slots


def _scheduled(enrollment, slots, start, finish):
    anchor = getdate(enrollment.start_date)
    if not anchor:
        frappe.throw("Enrollment start date is required", frappe.ValidationError)
    cutoff = getdate(enrollment.end_date) if enrollment.end_date else None
    first = max(start, anchor)
    last = min(finish, cutoff) if cutoff else finish
    if first > last:
        return []
    slot_by_day = {WEEKDAYS[s.weekday]: s for s in slots}
    # Stable ordinal derives only from the slots since enrollment's first day.
    ordinal = sum(1 for day_offset in range((first - anchor).days)
                  if (anchor + timedelta(days=day_offset)).weekday() in slot_by_day)
    result = []
    for day_offset in range((last - first).days + 1):
        day = first + timedelta(days=day_offset)
        slot = slot_by_day.get(day.weekday())
        if slot is None:
            continue
        raw_time = str(slot.start_time).split(".")[0]
        if len(raw_time.split(":")) == 2:
            raw_time += ":00"
        scheduled_at = datetime.fromisoformat(f"{day.isoformat()}T{raw_time}")
        result.append((ordinal, scheduled_at))
        ordinal += 1
    return result


def _workout_data(content_ids):
    records = frappe.get_all("Club100 Workout Content", filters={"name": ["in", content_ids]},
        fields=["name", "title", "format", "fitness_level", "duration_minutes", "equipment_required", "thumbnail", "instructions", "status", "publish_to_app", "video_url"])
    return {r.name: r for r in records}


def _build(enrollment, plan, cycle, content_ids, content, ordinal, scheduled_at, now, include_video=False):
    index = ordinal % len(content_ids)
    if ordinal >= len(content_ids) and not cint(cycle.repeat_cycle):
        return None
    workout = content.get(content_ids[index])
    if not workout or workout.status != "Active" or not cint(workout.publish_to_app):
        return None
    duration = max(0, cint(workout.duration_minutes))
    grace = max(0, cint(plan.workout_access_after_minutes))
    end_at = scheduled_at + timedelta(minutes=duration + grace)
    state = "Locked" if now < scheduled_at else ("Available" if now <= end_at else "Expired")
    result = {
        "id": f"{enrollment.name}:{scheduled_at.date().isoformat()}:{ordinal + 1}",
        "enrollment": enrollment.name, "sequenceNo": ordinal + 1,
        "cycle": cycle.name, "workoutContent": workout.name,
        "title": workout.title, "format": workout.format, "fitnessLevel": workout.fitness_level,
        "durationMinutes": duration, "equipmentRequired": workout.equipment_required,
        "thumbnail": workout.thumbnail,
        "scheduledDate": scheduled_at.date().isoformat(), "startTime": scheduled_at.time().isoformat(),
        "availableFrom": scheduled_at.isoformat(), "availableUntil": end_at.isoformat(), "accessState": state,
    }
    if include_video:
        result["instructions"] = workout.instructions
        result["videoUrl"] = workout.video_url if state == "Available" else None
    return result


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
    content = _workout_data(ids)
    records = [_build(enrollment, plan, cycle, ids, content, ordinal, when, now)
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
    workout = _build(enrollment, plan, cycle, ids, _workout_data(ids), dates[0][0], dates[0][1], now, True)
    if not workout:
        frappe.throw("Virtual workout not found", frappe.DoesNotExistError)
    return workout
