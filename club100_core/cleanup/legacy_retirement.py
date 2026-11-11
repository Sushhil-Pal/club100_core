"""Read-only preflight and guarded local deletion of retired workout DocTypes.

Use through `bench --site club100.local execute ...` from the Frappe bench root.
This module is intentionally not whitelisted and cannot be invoked by an HTTP client.
"""
import os
import frappe

RETIRED = (
    "Club100 Self Workout Cycle",
    "Club100 Self Workout Cycle Item",
    "Club100 Workout Content",
)


def _existing_column(doctype, fieldname):
    if not frappe.db.table_exists(doctype):
        return False
    return fieldname in frappe.db.get_table_columns(doctype)


def _sample(sql, values=None):
    return frappe.db.sql(sql, values or (), as_dict=True)


def _old_link_fields():
    # Once the new Session/Activity models have been migrated, only the retired
    # DocTypes may contain links to each other. Extra links must block deletion.
    result = []
    for table in ('DocField', 'Custom Field'):
        fields = frappe.get_all(table,
            filters={'fieldtype': ['in', ('Link', 'Table', 'Table MultiSelect')],
                     'options': ['in', RETIRED]},
            fields=['parent', 'fieldname', 'options'] if table == 'DocField'
                   else ['dt', 'fieldname', 'options'],
            limit_page_length=1000,
        )
        for f in fields:
            owner = f.get('parent') if table == 'DocField' else f.get('dt')
            if owner not in RETIRED:
                result.append({'owner': owner, 'field': f.fieldname, 'target': f.options})
    return result


def preflight():
    """Report counts and reasons why permanent removal must not proceed."""
    counts = {}
    for dt in RETIRED:
        counts[dt] = frappe.db.count(dt) if frappe.db.exists('DocType', dt) else 0

    plan_sessions = []
    if _existing_column('Club100 Session', 'plan'):
        plan_sessions = _sample(
            "SELECT name, plan, status FROM `tabClub100 Session` "
            "WHERE COALESCE(plan, '') != '' LIMIT 30"
        )

    missing_snapshots = []
    if _existing_column('Club100 Self Workout Activity', 'curriculum_snapshot'):
        missing_snapshots = _sample(
            "SELECT name, workout_id, status FROM `tabClub100 Self Workout Activity` "
            "WHERE curriculum_snapshot IS NULL OR curriculum_snapshot = '' LIMIT 30"
        )

    old_content_sessions = []
    if _existing_column('Club100 Session', 'workout_content'):
        old_content_sessions = _sample(
            "SELECT name, workout_content FROM `tabClub100 Session` "
            "WHERE COALESCE(workout_content, '') != '' LIMIT 30"
        )

    external_links = _old_link_fields()
    problems = []
    if plan_sessions:
        problems.append('Plan-targeted persistent Sessions remain (Self should be virtual).')
    if missing_snapshots:
        problems.append('Started/completed workout activities without curriculum snapshots remain.')
    if old_content_sessions:
        problems.append('Session rows still reference old Workout Content records.')
    if external_links:
        problems.append('Non-retired DocTypes still link to obsolete DocTypes.')
    # Do not delete if our canonical curriculum architecture is not installed.
    for required in ('Club100 Program Curriculum', 'Club100 Curriculum Session',
                     'Club100 Member Training Journey', 'Club100 Self Workout Activity'):
        if not frappe.db.exists('DocType', required):
            problems.append('Required DocType missing: ' + required)

    return {
        'site': frappe.local.site,
        'obsolete_record_counts': counts,
        'plan_targeted_sessions_sample': plan_sessions,
        'sessions_with_old_content_sample': old_content_sessions,
        'activities_without_snapshot_sample': missing_snapshots,
        'external_legacy_links': external_links,
        'blocking_issues': problems,
        'safe_to_retire': not problems,
    }


def retire_local():
    """Explicit destructive operation, strictly local site only.

    Before running this, the operator must create a verified bench DB backup.
    Set CLUB100_CONFIRM_LEGACY_DELETE=DELETE_LOCAL_LEGACY_WORKOUT_DATA in docker
    exec environment. Does not change files in repository.
    """
    if frappe.local.site != 'club100.local':
        frappe.throw('Deletion is supported only on local club100.local.', frappe.PermissionError)
    if os.environ.get('CLUB100_CONFIRM_LEGACY_DELETE') != 'DELETE_LOCAL_LEGACY_WORKOUT_DATA':
        frappe.throw('Explicit local deletion confirmation is required.', frappe.PermissionError)
    if not frappe.conf.get('developer_mode'):
        frappe.throw('Developer mode is required for this local cleanup.', frappe.PermissionError)
    report = preflight()
    if not report['safe_to_retire']:
        frappe.throw('Cleanup blocked: ' + str(report['blocking_issues']), frappe.ValidationError)

    # Avoid Frappe controller-file cleanup mid-operation. The apply script
    # archives and removes source files only AFTER this DB step succeeds.
    old_in_patch = getattr(frappe.flags, 'in_patch', False)
    frappe.flags.in_patch = True
    try:
        for dt in RETIRED:
            if frappe.db.exists('DocType', dt):
                frappe.delete_doc('DocType', dt, force=True, ignore_permissions=True)
    finally:
        frappe.flags.in_patch = old_in_patch
    return {'retired': list(RETIRED), 'previous_counts': report['obsolete_record_counts']}
