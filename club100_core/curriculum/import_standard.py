"""Idempotent, explicitly authorized import of the Club100 Standard curricula.

Dry-run defaults to true; real changes require dry_run=False and program mapping.
Never touches assessments, enrollments, cohorts, sessions, or Self Workout schedules.
"""
import json
from pathlib import Path
import frappe

SOURCE = Path(__file__).with_name("standard_curricula.json")
PROGRAMS = ("Everyday Fitness", "Weight Management", "Active 50+")


def _lookup_program(label, program_map):
    name = (program_map or {}).get(label)
    if name:
        if not frappe.db.exists("Club100 Program", name):
            frappe.throw(f"Mapped program does not exist: {label} -> {name}")
        return name
    possible = frappe.get_all("Club100 Program", filters={"program_name": label}, pluck="name")
    if len(possible) != 1:
        frappe.throw(f"Provide explicit program_map for '{label}'. Matches: {possible}")
    return possible[0]


def import_standard_curricula(dry_run=True, program_map=None):
    """Return a report; when dry_run=False create/update only matching curriculum docs.

    Example: bench --site club100.local execute
      club100_core.curriculum.import_standard.import_standard_curricula
      --kwargs '{"dry_run": true}'
    """
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() not in ("0", "false", "no")
    if isinstance(program_map, str):
        program_map = json.loads(program_map)
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    if len(data) != 9:
        frappe.throw("Expected 9 Standard curricula")
    mapping = {p: _lookup_program(p, program_map) for p in PROGRAMS}
    result = []
    for item in data:
        rows = item["sessions"]
        if len(rows) != 48 or {(r["week_number"], r["session_number"]) for r in rows} != {(w, s) for w in range(1, 13) for s in range(1, 5)}:
            frappe.throw("Invalid 12-week session matrix: " + item["curriculum_code"])
        code = item["curriculum_code"]
        exists = frappe.db.exists("Club100 Program Curriculum", code)
        result.append({"curriculum": code, "program": mapping[item["program_key"]], "sessions": len(rows), "action": "update" if exists else "create"})
        if dry_run:
            continue
        doc = frappe.get_doc("Club100 Program Curriculum", code) if exists else frappe.new_doc("Club100 Program Curriculum")
        # Refuse to overwrite a curriculum already used by any future integration.
        if exists and doc.get("source_reference") != item["source_reference"]:
            frappe.throw("Existing curriculum has a different source: " + code)
        for field in ("curriculum_code", "track", "level_code", "level_name", "weeks_per_cycle", "sessions_per_week", "active", "repeat_cycle", "source_reference"):
            doc.set(field, item[field])
        doc.program = mapping[item["program_key"]]
        doc.set("sessions", [])
        for row in rows:
            doc.append("sessions", row)
        doc.save(ignore_permissions=True) if exists else doc.insert(ignore_permissions=True)
    return {"dry_run": bool(dry_run), "curricula": len(result), "sessions": sum(x["sessions"] for x in result), "results": result}


def verify_standard_curricula():
    """Read-only verification of imported Standard track contents."""
    reports = []
    for x in json.loads(SOURCE.read_text(encoding="utf-8")):
        code = x["curriculum_code"]
        if not frappe.db.exists("Club100 Program Curriculum", code):
            reports.append({"curriculum": code, "status": "MISSING"})
            continue
        d = frappe.get_doc("Club100 Program Curriculum", code)
        seen = {(r.week_number, r.session_number) for r in d.sessions}
        expected = {(w,s) for w in range(1,13) for s in range(1,5)}
        reports.append({"curriculum": code, "status": "OK" if len(d.sessions)==48 and seen==expected and d.repeat_cycle else "CHECK", "sessions": len(d.sessions), "formats": sorted(set(r.workout_format for r in d.sessions))})
    return reports
