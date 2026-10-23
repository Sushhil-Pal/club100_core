"""Guarded Society curriculum DRAFT importer (not an activation workflow).

Dry run is default. A deliberate dry_run=False creates an inactive Society MIXED
curriculum. Updating an existing inactive draft requires allow_update=True.
No cohorts, sessions, memberships, attendance or Standard curricula are modified.
"""
import csv
import hashlib
from pathlib import Path

import frappe
from frappe.utils import cint

from club100_core.curriculum.society import FORMATS, validate_society_curriculum

SOURCE = Path(__file__).with_name('society_curriculum_draft.csv')
CODE = 'SOC-EVERYDAY-MIXED'
SOURCE_REFERENCE = 'Club100 Society curriculum 48-session authoring draft v1'
FIELDS = (
    'week_number', 'session_number', 'workout_format', 'session_title',
    'objective', 'warm_up', 'main_block_a', 'main_block_b', 'finisher_skill',
    'cooldown', 'intensity', 'equipment', 'duration', 'core_variation',
    'strong_variation', 'performance_variation', 'trainer_cues',
)


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes')
    return bool(value)


def _rows():
    with SOURCE.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            frappe.throw('Society CSV header does not match the 17-field template', frappe.ValidationError)
        rows = list(reader)
    if len(rows) != 48:
        frappe.throw('Society draft must contain 48 sessions', frappe.ValidationError)
    expected = {(w, s) for w in range(1, 13) for s in range(1, 5)}
    found = set()
    for row in rows:
        for field in FIELDS:
            if not str(row.get(field) or '').strip():
                frappe.throw(f'Missing {field} in Society draft', frappe.ValidationError)
        try:
            week, session = int(row['week_number']), int(row['session_number'])
        except (ValueError, TypeError):
            frappe.throw('Invalid week or session number', frappe.ValidationError)
        if (week, session) not in expected or (week, session) in found:
            frappe.throw(f'Unexpected/duplicate Society session: {week}/{session}', frappe.ValidationError)
        found.add((week, session))
        if row['workout_format'] != FORMATS[session - 1]:
            frappe.throw(f'Invalid format rotation in Week {week}, Session {session}', frappe.ValidationError)
        row['week_number'] = week
        row['session_number'] = session
    if found != expected:
        frappe.throw('Incomplete 12-week Society session grid', frappe.ValidationError)
    return sorted(rows, key=lambda x: (x['week_number'], x['session_number']))


def _program(program):
    if not program:
        frappe.throw('Explicit ERP program ID is required (program=...)', frappe.ValidationError)
    if not frappe.db.exists('Club100 Program', program):
        frappe.throw(f'ERP program does not exist: {program}', frappe.DoesNotExistError)
    return program


def import_society_draft(program=None, dry_run=True, allow_update=False):
    """Run dry-run first; to write pass dry_run=False and explicit ERP program ID.

    Never creates an active curriculum. Never overwrites an active curriculum.
    Existing inactive drafts require allow_update=True for replacement.
    """
    dry_run, allow_update = _bool(dry_run), _bool(allow_update)
    program = _program(program)
    rows = _rows()
    checksum = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    exists = frappe.db.exists('Club100 Program Curriculum', CODE)
    if exists:
        doc = frappe.get_doc('Club100 Program Curriculum', CODE)
        if doc.track != 'Society' or doc.level_code != 'MIXED' or doc.program != program:
            frappe.throw('Existing curriculum identity does not match requested Society draft', frappe.ValidationError)
        if cint(doc.active):
            frappe.throw('Refusing to overwrite an ACTIVE Society curriculum', frappe.ValidationError)
        if doc.source_reference != SOURCE_REFERENCE:
            frappe.throw('Existing draft belongs to a different source', frappe.ValidationError)
        if not dry_run and not allow_update:
            frappe.throw('Existing draft update requires allow_update=True', frappe.ValidationError)
    result = {'dry_run': dry_run, 'action': 'update' if exists else 'create',
              'curriculum': CODE, 'program': program, 'track': 'Society',
              'level': 'MIXED', 'active': 0, 'sessions': len(rows), 'sha256': checksum}
    if dry_run:
        return result
    doc = frappe.get_doc('Club100 Program Curriculum', CODE) if exists else frappe.new_doc('Club100 Program Curriculum')
    doc.update({'curriculum_code': CODE, 'program': program, 'track': 'Society',
                'level_code': 'MIXED', 'level_name': 'Mixed Levels',
                'weeks_per_cycle': 12, 'sessions_per_week': 4,
                'repeat_cycle': 1, 'active': 0,
                'source_reference': SOURCE_REFERENCE,
                'notes': 'DRAFT — not trainer-approved or active. CSV SHA256: ' + checksum})
    doc.set('sessions', [])
    for row in rows:
        doc.append('sessions', row)
    validate_society_curriculum(doc)
    if exists:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)
    result['saved'] = True
    return result


def verify_society_draft():
    """Read-only validation; always returns inactive approval status as recorded."""
    exists = frappe.db.exists('Club100 Program Curriculum', CODE)
    if not exists:
        return {'curriculum': CODE, 'status': 'MISSING'}
    doc = frappe.get_doc('Club100 Program Curriculum', CODE)
    validate_society_curriculum(doc)
    return {'curriculum': CODE, 'status': 'VALID_DRAFT' if not cint(doc.active) else 'ACTIVE',
            'active': cint(doc.active), 'program': doc.program,
            'track': doc.track, 'level': doc.level_code,
            'sessions': len(doc.sessions),
            'formats': [doc.sessions[i].workout_format for i in range(4)]}
