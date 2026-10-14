"""Thirty-minute reminders for virtual Self workouts (no Session records)."""

from datetime import timedelta
from hashlib import sha256

import frappe
from frappe.utils import cint, getdate, now_datetime

from club100_core.api.push_notifications import send_push_to_member
from club100_core.api.self_workouts import (
    _build,
    _configuration,
    _scheduled,
    _workout_data,
)

REMINDER_MINUTES = 30


def _eligible_enrollments(now, window_end):
    """Only active Online Self enrollments, with an active push device."""
    plans = frappe.get_all(
        "Club100 Plan", filters={"plan_code": "self"}, pluck="name"
    )
    if not plans:
        return []
    rows = frappe.get_all(
        "Club100 Enrollment",
        filters={"plan": ["in", plans], "status": "Active", "pricing_model": "Online Plan"},
        fields=["name", "member", "program", "plan", "start_date", "end_date", "status"],
    )
    eligible = []
    for row in rows:
        if not row.member or not row.start_date:
            continue
        if getdate(row.start_date) > getdate(window_end):
            continue
        if row.end_date and getdate(row.end_date) < getdate(now):
            continue
        if not frappe.db.exists("Club100 Push Subscription", {"member": row.member, "active": 1}):
            continue
        eligible.append(row)
    return eligible


def _reminder_key(workout):
    """Include scheduled time so a genuine reschedule gets its own reminder."""
    source = f"{workout['id']}|{workout['availableFrom']}|30"
    return sha256(source.encode("utf-8")).hexdigest()


def _send_once(enrollment, workout):
    """Persist a unique delivery marker only after at least one successful push.

    A database uniqueness constraint prevents concurrent duplicate claims. As with
    any external push service, a process crash between push delivery and commit
    can cause a retry; true exactly-once network delivery cannot be guaranteed.
    """
    key = _reminder_key(workout)
    if frappe.db.exists("Club100 Self Workout Reminder", {"reminder_key": key}):
        return {"sent": 0, "failed": 0, "skipped": "already_sent"}
    if frappe.db.exists("Club100 Self Workout Activity", {
        "member": enrollment.member, "enrollment": enrollment.name,
        "workout_id": workout["id"], "status": "Completed",
    }):
        return {"sent": 0, "failed": 0, "skipped": "completed"}

    marker = frappe.get_doc({
        "doctype": "Club100 Self Workout Reminder",
        "reminder_key": key,
        "enrollment": enrollment.name,
        "member": enrollment.member,
        "workout_id": workout["id"],
        "scheduled_at": workout["availableFrom"].replace("T", " "),
    })
    try:
        marker.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        return {"sent": 0, "failed": 0, "skipped": "already_claimed"}

    result = send_push_to_member(
        member=enrollment.member,
        title="Your Club100 workout starts in 30 minutes",
        body=workout["title"],
        url=f"/self-workout/{workout['id']}",
    )
    if result["sent"] > 0:
        marker.sent_at = now_datetime()
        marker.sent_count = result["sent"]
        marker.save(ignore_permissions=True)
    else:
        # No successful delivery: permit the next scheduler run to retry.
        frappe.delete_doc("Club100 Self Workout Reminder", marker.name, ignore_permissions=True)
    frappe.db.commit()
    return {**result, "skipped": False}


def process_self_workout_reminders():
    """Run alongside Session reminders every five minutes."""
    now = now_datetime()
    window_end = now + timedelta(minutes=REMINDER_MINUTES)
    candidates = processed = sent = failed = 0
    skipped_incomplete = 0
    for enrollment in _eligible_enrollments(now, window_end):
        try:
            plan, cycle, ids, slots = _configuration(enrollment)
            times = _scheduled(enrollment, slots, getdate(now), getdate(window_end))
            content = _workout_data(ids)
        except (frappe.ValidationError, frappe.DoesNotExistError):
            # A newly activated Self enrollment may not have its weekly slots yet.
            skipped_incomplete += 1
            continue
        for ordinal, scheduled_at in times:
            if not now < scheduled_at <= window_end:
                continue
            workout = _build(enrollment, plan, cycle, ids, content, ordinal, scheduled_at, now)
            if not workout:
                continue
            candidates += 1
            outcome = _send_once(enrollment, workout)
            if outcome.get("skipped"):
                continue
            processed += 1
            sent += cint(outcome["sent"])
            failed += cint(outcome["failed"])
    return {
        "now": str(now), "window_end": str(window_end),
        "candidates": candidates, "processed": processed,
        "sent": sent, "failed": failed,
        "skipped_incomplete": skipped_incomplete,
    }
