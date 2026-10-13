import frappe


ASSESSMENT_MODES = (
    "Trainer Led",
    "Self Assessment",
)


def _validate_assessment_mode(assessment_mode):
    assessment_mode = (
        assessment_mode or "Trainer Led"
    ).strip()

    if assessment_mode not in ASSESSMENT_MODES:
        frappe.throw(
            "Invalid assessment mode",
            frappe.ValidationError,
        )

    return assessment_mode


def _get_assessment(
    assessment_id,
    expected_assessment_mode=None,
    expected_member_id=None,
    expected_enrollment_id=None,
    expected_status=None,
):
    assessment_id = (
        assessment_id or ""
    ).strip()

    if not assessment_id:
        frappe.throw(
            "Assessment ID is required",
            frappe.ValidationError,
        )

    if not frappe.db.exists(
        "Club100 Assessment",
        assessment_id,
    ):
        frappe.throw(
            "Assessment not found",
            frappe.DoesNotExistError,
        )

    doc = frappe.get_doc(
        "Club100 Assessment",
        assessment_id,
    )

    if expected_assessment_mode:
        expected_assessment_mode = (
            _validate_assessment_mode(
                expected_assessment_mode
            )
        )

        if (
            doc.assessment_mode
            != expected_assessment_mode
        ):
            frappe.throw(
                "Assessment not found",
                frappe.DoesNotExistError,
            )

    if (
        expected_member_id
        and doc.member != expected_member_id
    ):
        frappe.throw(
            "Assessment not found",
            frappe.DoesNotExistError,
        )

    if (
        expected_enrollment_id
        and doc.enrollment
        != expected_enrollment_id
    ):
        frappe.throw(
            "Assessment not found",
            frappe.DoesNotExistError,
        )

    if (
        expected_status
        and doc.status != expected_status
    ):
        frappe.throw(
            "Assessment not found",
            frappe.DoesNotExistError,
        )

    return doc


def _validate_enrollment_context(
    member_id,
    enrollment_id,
    program_id=None,
):
    if not enrollment_id:
        return program_id

    enrollment = frappe.db.get_value(
        "Club100 Enrollment",
        enrollment_id,
        [
            "name",
            "member",
            "program",
        ],
        as_dict=True,
    )

    if not enrollment:
        frappe.throw(
            "Enrollment not found",
            frappe.DoesNotExistError,
        )

    if enrollment.member != member_id:
        frappe.throw(
            "Enrollment does not belong to this member.",
            frappe.PermissionError,
        )

    if (
        program_id
        and enrollment.program != program_id
    ):
        frappe.throw(
            "Enrollment and program do not match.",
            frappe.ValidationError,
        )

    return enrollment.program


def _update_enrollment_assessment_links(doc):
    if (
        doc.status != "Completed"
        or not doc.enrollment
    ):
        return

    values = {
        "latest_assessment": doc.name,
    }

    baseline_assessment = frappe.db.get_value(
        "Club100 Enrollment",
        doc.enrollment,
        "baseline_assessment",
    )

    if (
        doc.assessment_type == "Baseline"
        and not baseline_assessment
    ):
        values["baseline_assessment"] = (
            doc.name
        )

    frappe.db.set_value(
        "Club100 Enrollment",
        doc.enrollment,
        values,
        update_modified=False,
    )


def start_assessment(
    member_id,
    assessor=None,
    delivery_mode="Offline",
    assessment_mode="Trainer Led",
    template_code=None,
    enrollment_id=None,
    program_id=None,
    safety_acknowledged_on=None,
):
    member_id = (
        member_id or ""
    ).strip()

    assessment_mode = _validate_assessment_mode(
        assessment_mode
    )

    if not member_id:
        frappe.throw(
            "Member ID is required",
            frappe.ValidationError,
        )

    if not frappe.db.exists(
        "Club100 Member",
        {
            "name": member_id,
            "status": "Active",
        },
    ):
        frappe.throw(
            "Member not found",
            frappe.DoesNotExistError,
        )

    program_id = _validate_enrollment_context(
        member_id=member_id,
        enrollment_id=enrollment_id,
        program_id=program_id,
    )

    # Resume only a draft belonging to the same assessment mode.
    # If an enrollment is supplied, also scope the draft to that
    # membership so a draft from a previous membership is not reused.
    draft_filters = {
        "member": member_id,
        "status": "Draft",
        "assessment_mode": assessment_mode,
    }

    if enrollment_id:
        draft_filters["enrollment"] = (
            enrollment_id
        )

    existing_draft = frappe.db.get_value(
        "Club100 Assessment",
        draft_filters,
        [
            "name",
            "assessment_type",
            "assessment_date",
            "template",
            "enrollment",
            "program",
        ],
        order_by="modified desc",
        as_dict=True,
    )

    if existing_draft:
        return {
            "assessment": {
                "id":
                    existing_draft.name,
                "member":
                    member_id,
                "status":
                    "Draft",
                "assessmentType":
                    existing_draft.assessment_type,
                "assessmentMode":
                    assessment_mode,
                "assessmentDate":
                    existing_draft.assessment_date,
                "template":
                    existing_draft.template,
                "enrollmentId":
                    existing_draft.enrollment,
                "programId":
                    existing_draft.program,
            },
            "resumed": True,
        }

    # New enrollment-aware flows classify Baseline/Reassessment within
    # the membership. Legacy trainer calls that do not provide an
    # enrollment keep the existing member-wide behavior.
    completed_filters = {
        "member": member_id,
        "status": "Completed",
    }

    if enrollment_id:
        completed_filters["enrollment"] = (
            enrollment_id
        )
    elif program_id:
        completed_filters["program"] = (
            program_id
        )

    completed_count = frappe.db.count(
        "Club100 Assessment",
        completed_filters,
    )

    assessment_type = (
        "Reassessment"
        if completed_count > 0
        else "Baseline"
    )

    template_filters = {
        "active": 1,
        "assessment_mode": assessment_mode,
    }

    if template_code:
        template_filters["template_code"] = (
            template_code
        )
    else:
        template_filters["is_default"] = 1

    template = frappe.db.get_value(
        "Club100 Assessment Template",
        template_filters,
        "name",
    )

    if not template:
        frappe.throw(
            "No active assessment template is configured for this assessment mode.",
            frappe.ValidationError,
        )

    payload = {
        "doctype":
            "Club100 Assessment",
        "member":
            member_id,
        "assessment_type":
            assessment_type,
        "assessment_mode":
            assessment_mode,
        "assessment_date":
            frappe.utils.today(),
        "assessor":
            assessor,
        "delivery_mode":
            delivery_mode,
        "template":
            template,
        "status":
            "Draft",
    }

    if enrollment_id:
        payload["enrollment"] = (
            enrollment_id
        )

    if program_id:
        payload["program"] = program_id

    if safety_acknowledged_on:
        payload["safety_acknowledged_on"] = (
            safety_acknowledged_on
        )

    doc = frappe.get_doc(payload)

    doc.insert(
        ignore_permissions=True
    )

    return {
        "assessment": {
            "id": doc.name,
            "member": doc.member,
            "status": doc.status,
            "assessmentType":
                doc.assessment_type,
            "assessmentMode":
                doc.assessment_mode,
            "assessmentDate":
                doc.assessment_date,
            "template":
                doc.template,
            "enrollmentId":
                doc.enrollment,
            "programId":
                doc.program,
        },
        "resumed": False,
    }


def assessment_detail(
    assessment_id,
    expected_assessment_mode=None,
    expected_member_id=None,
    expected_enrollment_id=None,
    expected_status=None,
):
    doc = _get_assessment(
        assessment_id=assessment_id,
        expected_assessment_mode=(
            expected_assessment_mode
        ),
        expected_member_id=(
            expected_member_id
        ),
        expected_enrollment_id=(
            expected_enrollment_id
        ),
        expected_status=expected_status,
    )

    member = frappe.db.get_value(
        "Club100 Member",
        doc.member,
        [
            "name",
            "full_name",
            "gender",
            "date_of_birth",
        ],
        as_dict=True,
    )

    inputs = []

    for row in doc.inputs:
        master = frappe.db.get_value(
            "Club100 Assessment Input",
            row.input,
            [
                "input_code",
                "options",
                "description",
                "instructions",
                "equipment_required",
            ],
            as_dict=True,
        )

        inputs.append(
            {
                "rowId": row.name,
                "input": row.input,
                "inputName": row.input_name,
                "inputCode": (
                    master.input_code
                    if master
                    else None
                ),
                "category": row.category,
                "resultType":
                    row.result_type,
                "unit": row.unit,
                "required":
                    bool(row.required),
                "value": row.value,
                "hasValue":
                    bool(row.is_entered),
                "textValue":
                    row.text_value,
                "notes": row.notes,
                "options": (
                    master.options
                    if master
                    else None
                ),
                "description": (
                    master.description
                    if master
                    else None
                ),
                "instructions": (
                    master.instructions
                    if master
                    else None
                ),
                "equipmentRequired": (
                    master.equipment_required
                    if master
                    else None
                ),
            }
        )

    return {
        "assessment": {
            "id": doc.name,
            "member": {
                "id": member.name,
                "fullName":
                    member.full_name,
                "gender":
                    member.gender,
                "dateOfBirth":
                    member.date_of_birth,
            },
            "assessmentType":
                doc.assessment_type,
            "assessmentMode":
                doc.assessment_mode,
            "assessmentDate":
                doc.assessment_date,
            "deliveryMode":
                doc.delivery_mode,
            "status":
                doc.status,
            "template":
                doc.template,
            "enrollmentId":
                doc.enrollment,
            "programId":
                doc.program,
            "safetyAcknowledgedOn":
                doc.safety_acknowledged_on,
            "inputs":
                inputs,
        }
    }


def save_assessment(
    assessment_id,
    inputs,
    status="Draft",
    expected_assessment_mode=None,
    expected_member_id=None,
    expected_enrollment_id=None,
    expected_current_status=None,
):
    status = (
        status or "Draft"
    ).strip()

    if status not in (
        "Draft",
        "Completed",
    ):
        frappe.throw(
            "Invalid assessment status",
            frappe.ValidationError,
        )

    if isinstance(inputs, str):
        inputs = frappe.parse_json(inputs)

    inputs = inputs or []

    doc = _get_assessment(
        assessment_id=assessment_id,
        expected_assessment_mode=(
            expected_assessment_mode
        ),
        expected_member_id=(
            expected_member_id
        ),
        expected_enrollment_id=(
            expected_enrollment_id
        ),
        expected_status=(
            expected_current_status
        ),
    )

    rows_by_name = {
        row.name: row
        for row in doc.inputs
    }

    for item in inputs:
        row_id = item.get("rowId")

        if not row_id:
            continue

        row = rows_by_name.get(row_id)

        if not row:
            continue

        result_type = row.result_type

        if result_type in (
            "Select",
            "Text",
            "Yes/No",
        ):
            text_value = item.get(
                "textValue"
            )

            row.text_value = (
                str(text_value).strip()
                if text_value not in (
                    None,
                    "",
                )
                else None
            )

            row.is_entered = (
                1
                if text_value not in (
                    None,
                    "",
                )
                else 0
            )

        else:
            value = item.get("value")

            row.value = (
                value
                if value not in (
                    None,
                    "",
                )
                else 0
            )

            row.is_entered = (
                1
                if value not in (
                    None,
                    "",
                )
                else 0
            )

    doc.status = status

    doc.save(
        ignore_permissions=True
    )

    _update_enrollment_assessment_links(
        doc
    )

    return {
        "success": True,
        "assessment": {
            "id": doc.name,
            "status": doc.status,
            "assessmentType":
                doc.assessment_type,
            "assessmentMode":
                doc.assessment_mode,
            "enrollmentId":
                doc.enrollment,
            "programId":
                doc.program,
            "fitnessScore":
                doc.fitness_score,
            "fitnessLevel":
                doc.fitness_level,
        },
    }
