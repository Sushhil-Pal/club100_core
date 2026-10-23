"""Trainer-facing read-only Society prescription lookup."""
import frappe
from club100_core.curriculum.society import position_from_ordinal, validate_society_curriculum


@frappe.whitelist(methods=['GET'])
def trainer_society_session_prescription(session_id):
    from club100_core.api.trainer import _require_trainer
    trainer = _require_trainer()
    session = frappe.db.get_value('Club100 Session', session_id,
        ['name','cohort','trainer','status','workout_format','society_curriculum_code','society_curriculum_position','society_override_reason'],as_dict=True)
    if not session or not session.cohort or not session.society_curriculum_position:
        frappe.throw('No curriculum-driven Society session found.', frappe.DoesNotExistError)
    cohort = frappe.db.get_value('Club100 Cohort', session.cohort,
        ['society','delivery_mode','primary_trainer'], as_dict=True)
    if not cohort or not cohort.society or cohort.delivery_mode != 'Onsite':
        frappe.throw('Not an onsite Society cohort.', frappe.ValidationError)
    if trainer.name not in (session.trainer, cohort.primary_trainer) and not frappe.has_role('System Manager'):
        frappe.throw('Trainer is not assigned to this session or cohort.', frappe.PermissionError)
    curriculum = frappe.get_doc('Club100 Program Curriculum',session.society_curriculum_code)
    rows = validate_society_curriculum(curriculum)
    pos = position_from_ordinal(int(session.society_curriculum_position)-1)
    row = rows[(pos['weekNumber'],pos['sessionNumber'])]
    overridden = session.workout_format != pos['format']
    response = {
        'sessionId': session.name,
        'cohort': session.cohort,
        'curriculum': curriculum.name,
        'curriculumStatus': 'Active for local testing — pending trainer approval',
        **pos,
        'curriculumFormat': pos['format'],
        'actualFormat': session.workout_format,
        'formatOverridden': overridden,
        'overrideReason': (session.society_override_reason or '') if overridden else '',
        'prescriptionAvailable': not overridden,
    }
    if overridden:
        # No misleading standard exercises or training cues for a substituted class.
        return response
    response.update({
        'title': row.session_title,
        'objective': row.objective,
        'warmUp': row.warm_up,
        'mainBlockA': row.main_block_a,
        'mainBlockB': row.main_block_b,
        'finisher': row.finisher_skill,
        'cooldown': row.cooldown,
        'duration': row.duration,
        'core': row.core_variation,
        'strong': row.strong_variation,
        'performance': row.performance_variation,
        'trainerCues': row.trainer_cues,
    })
    return response
