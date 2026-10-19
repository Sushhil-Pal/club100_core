"""Backward-compatible marketing entrypoints for the universal member assessment."""
import frappe
from club100_core.api import member as member_api


@frappe.whitelist(methods=["GET"])
def free_assessment_context():
    return member_api.assessment_context()


@frappe.whitelist(methods=["POST"])
def start_free_assessment(safety_acknowledged=0):
    return member_api.start_self_assessment(safety_acknowledged=safety_acknowledged)


@frappe.whitelist(methods=["GET"])
def free_assessment_detail(assessment_id):
    return member_api.self_assessment_detail(assessment_id)


@frappe.whitelist(methods=["POST"])
def save_free_assessment(assessment_id, inputs, expected_modified=None):
    return member_api.save_self_assessment(assessment_id, inputs, expected_modified=expected_modified)


@frappe.whitelist(methods=["POST"])
def complete_free_assessment(assessment_id, inputs, expected_modified=None):
    return member_api.complete_self_assessment(assessment_id, inputs, expected_modified=expected_modified)

