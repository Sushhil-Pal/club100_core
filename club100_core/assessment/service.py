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

    return doc


def start_assessment(
    member_id,
    assessor=None,
    delivery_mode="Offline",
    assessment_mode="Trainer Led",
    template_code=None,
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

    # Resume only a draft belonging to the same assessment mode.
    # This prevents a future Self Assessment draft from being
    # resumed by the trainer workflow, and vice versa.
    existing_draft = frappe.db.get_value(
        "Club100 Assessment",
        {
            "member": member_id,
            "status": "Draft",
            "assessment_mode": assessment_mode,
        },
        [
            "name",
            "assessment_type",
            "assessment_date",
            "template",
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
                "assessmentDate":
                    existing_draft.assessment_date,
                "template":
                    existing_draft.template,
            },
            "resumed": True,
        }

    # Preserve the current Baseline/Reassessment behavior during
    # this refactor. Enrollment/program-aware classification will
    # be introduced separately after the trainer flow is verified.
    completed_count = frappe.db.count(
        "Club100 Assessment",
        {
            "member": member_id,
            "status": "Completed",
        },
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
            "No active default assessment template is configured.",
            frappe.ValidationError,
        )

    doc = frappe.get_doc(
        {
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
    )

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
            "assessmentDate":
                doc.assessment_date,
            "template":
                doc.template,
        },
        "resumed": False,
    }


def assessment_detail(
    assessment_id,
    expected_assessment_mode=None,
):
    doc = _get_assessment(
        assessment_id,
        expected_assessment_mode,
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
            ],
            as_dict=True,
        )

        inputs.append(
            {
                "rowId": row.name,
                "input": row.input,
                "inputName": row.input_name,
                "inputCode":
                    master.input_code
                    if master
                    else None,
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

                "options":
                    master.options
                    if master
                    else None,

                "description":
                    master.description
                    if master
                    else None,

                "instructions":
                    master.instructions
                    if master
                    else None,
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
            "assessmentDate":
                doc.assessment_date,
            "deliveryMode":
                doc.delivery_mode,
            "status":
                doc.status,
            "template":
                doc.template,
            "inputs":
                inputs,
        }
    }


def save_assessment(
    assessment_id,
    inputs,
    status="Draft",
    expected_assessment_mode=None,
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
        assessment_id,
        expected_assessment_mode,
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

    return {
        "success": True,
        "assessment": {
            "id": doc.name,
            "status": doc.status,
            "fitnessScore":
                doc.fitness_score,
            "fitnessLevel":
                doc.fitness_level,
        },
    }
