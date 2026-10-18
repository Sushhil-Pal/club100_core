"""One free baseline Self Assessment per registered Club100 member.

These endpoints intentionally do not grant workout or enrollment access.
"""
import frappe
from club100_core.assessment import service as assessment_service
from club100_core.api.member import _get_current_member


def _free_context(member):
    if member.status != "Active":
        return {"allowed": False, "reason": "Your Club100 account is not active."}
    if (member.onboarding_status != "Completed" or not member.date_of_birth or not member.gender):
        return {"allowed": False, "reason": "Complete your profile before starting the free assessment."}
    base = {"member": member.name, "is_free_baseline": 1, "assessment_mode": "Self Assessment", "enrollment": ["is", "not set"]}
    completed = frappe.db.get_value("Club100 Assessment", {**base, "status": "Completed"}, ["name", "fitness_score", "fitness_level", "assessment_date"], order_by="creation desc", as_dict=True)
    draft = frappe.db.get_value("Club100 Assessment", {**base, "status": "Draft"}, "name", order_by="modified desc")
    return {
        "allowed": True, "assessmentMode": "Self Assessment", "isFreeBaseline": True,
        "state": "Current" if completed else ("Draft" if draft else "Not Started"),
        "canStart": not bool(completed), "assessmentType": "Baseline",
        "draftAssessmentId": draft if not completed else None,
        "latestAssessment": ({"id": completed.name, "fitnessScore": completed.fitness_score, "fitnessLevel": completed.fitness_level, "date": completed.assessment_date} if completed else None),
        "safetyAcknowledgementRequired": not bool(draft),
    }


def _ensure_free_draft(member, assessment_id):
    doc = frappe.get_doc("Club100 Assessment", assessment_id)
    if (doc.member != member.name or not doc.is_free_baseline or doc.enrollment or doc.assessment_mode != "Self Assessment" or doc.status != "Draft"):
        frappe.throw("Free assessment draft not found.", frappe.PermissionError)
    return doc


@frappe.whitelist(methods=["GET"])
def free_assessment_context():
    return _free_context(_get_current_member())


@frappe.whitelist(methods=["POST"])
def start_free_assessment(safety_acknowledged=0):
    member = _get_current_member()
    # Serialize activations for the same member so concurrent requests cannot
    # create two one-time assessments.
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member.name)
    context = _free_context(member)
    if not context.get("allowed"):
        frappe.throw(context.get("reason"), frappe.ValidationError)
    if not context["canStart"]:
        frappe.throw("You have already completed your free baseline assessment.", frappe.ValidationError)
    if not context["draftAssessmentId"] and not frappe.utils.cint(safety_acknowledged):
        frappe.throw("Safety acknowledgement is required.", frappe.ValidationError)
    return assessment_service.start_assessment(
        member_id=member.name, assessor=None, delivery_mode="Online",
        assessment_mode="Self Assessment", template_code="SELF",
        free_baseline=True,
        safety_acknowledged_on=(frappe.utils.now_datetime() if not context["draftAssessmentId"] else None),
    )


@frappe.whitelist(methods=["GET"])
def free_assessment_detail(assessment_id):
    member = _get_current_member()
    _ensure_free_draft(member, assessment_id)
    return assessment_service.assessment_detail(
        assessment_id=assessment_id, expected_assessment_mode="Self Assessment",
        expected_member_id=member.name, expected_status="Draft",
    )


@frappe.whitelist(methods=["POST"])
def save_free_assessment(assessment_id, inputs):
    member = _get_current_member()
    _ensure_free_draft(member, assessment_id)
    result = assessment_service.save_assessment(
        assessment_id=assessment_id, inputs=inputs, status="Draft",
        expected_assessment_mode="Self Assessment",
        expected_member_id=member.name, expected_current_status="Draft",
    )
    result["assessment"]["fitnessScore"] = None
    result["assessment"]["fitnessLevel"] = None
    return result


@frappe.whitelist(methods=["POST"])
def complete_free_assessment(assessment_id, inputs):
    member = _get_current_member()
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member.name)
    _ensure_free_draft(member, assessment_id)
    context = _free_context(member)
    if context["latestAssessment"]:
        frappe.throw("Free baseline assessment already completed.", frappe.ValidationError)
    result = assessment_service.save_assessment(
        assessment_id=assessment_id, inputs=inputs, status="Completed",
        expected_assessment_mode="Self Assessment",
        expected_member_id=member.name, expected_current_status="Draft",
    )
    result["assessmentContext"] = _free_context(member)
    return result
