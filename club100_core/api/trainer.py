import frappe


def _require_trainer():
    user = frappe.session.user

    if not user or user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.AuthenticationError,
        )

    trainer = frappe.db.get_value(
        "Club100 Trainer",
        {
            "user": user,
            "status": "Active",
        },
        [
            "name",
            "trainer_name",
        ],
        as_dict=True,
    )

    if not trainer:
        frappe.throw(
            "Trainer access required",
            frappe.PermissionError,
        )

    return trainer


@frappe.whitelist(methods=["GET"])
def members(search=None):
    _require_trainer()

    search = (search or "").strip()

    # Do not browse/load the member database by default.
    if len(search) < 2:
        return {
            "members": [],
        }

    like = f"%{search}%"

    rows = frappe.get_all(
        "Club100 Member",
        filters={
            "status": "Active",
        },
        or_filters={
            "name": ["like", like],
            "full_name": ["like", like],
            "mobile": ["like", like],
            "email": ["like", like],
        },
        fields=[
            "name",
            "full_name",
            "mobile",
            "email",
            "gender",
            "date_of_birth",
            "onboarding_status",
        ],
        order_by="full_name asc",
        limit_page_length=20,
    )

    return {
        "members": [
            {
                "id": row.name,
                "fullName": row.full_name,
                "mobile": row.mobile,
                "email": row.email,
                "gender": row.gender,
                "dateOfBirth": row.date_of_birth,
                "onboardingStatus":
                    row.onboarding_status
                    or "Not Started",
            }
            for row in rows
        ]
    }

@frappe.whitelist(methods=["GET"])
def member_detail(member_id):
    _require_trainer()

    member_id = (member_id or "").strip()

    if not member_id:
        frappe.throw(
            "Member ID is required",
            frappe.ValidationError,
        )

    member = frappe.db.get_value(
        "Club100 Member",
        {
            "name": member_id,
            "status": "Active",
        },
        [
            "name",
            "full_name",
            "mobile",
            "email",
            "gender",
            "date_of_birth",
            "joining_date",
            "onboarding_status",
        ],
        as_dict=True,
    )

    if not member:
        frappe.throw(
            "Member not found",
            frappe.DoesNotExistError,
        )

    latest_assessment = frappe.db.get_value(
        "Club100 Assessment",
        {
            "member": member.name,
            "status": "Completed",
        },
        [
            "name",
            "assessment_type",
            "assessment_date",
            "fitness_score",
            "fitness_level",
        ],
        order_by="assessment_date desc, creation desc",
        as_dict=True,
    )

    assessment_count = frappe.db.count(
        "Club100 Assessment",
        {
            "member": member.name,
            "status": "Completed",
        },
    )

    return {
        "member": {
            "id": member.name,
            "fullName": member.full_name,
            "mobile": member.mobile,
            "email": member.email,
            "gender": member.gender,
            "dateOfBirth": member.date_of_birth,
            "joiningDate": member.joining_date,
            "onboardingStatus":
                member.onboarding_status
                or "Not Started",
        },

        "latestAssessment": (
            {
                "id": latest_assessment.name,
                "type":
                    latest_assessment.assessment_type,
                "date":
                    latest_assessment.assessment_date,
                "fitnessScore":
                    latest_assessment.fitness_score,
                "fitnessLevel":
                    latest_assessment.fitness_level,
            }
            if latest_assessment
            else None
        ),

        "assessmentCount":
            assessment_count,
    }

@frappe.whitelist(methods=["POST"])
def start_assessment(
    member_id,
    assessment_type="Baseline",
    delivery_mode="Offline",
):
    trainer = _require_trainer()

    member_id = (member_id or "").strip()

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

    template = frappe.db.get_value(
        "Club100 Assessment Template",
        {
            "is_default": 1,
            "active": 1,
        },
        "name",
    )

    if not template:
        frappe.throw(
            "No active default assessment template is configured.",
            frappe.ValidationError,
        )

    doc = frappe.get_doc(
        {
            "doctype": "Club100 Assessment",
            "member": member_id,
            "assessment_type": assessment_type,
            "assessment_date": frappe.utils.today(),
            "assessor": trainer.name,
            "delivery_mode": delivery_mode,
            "template": template,
            "status": "Draft",
        }
    )

    doc.insert(ignore_permissions=True)

    return {
        "assessment": {
            "id": doc.name,
            "member": doc.member,
            "status": doc.status,
            "assessmentType": doc.assessment_type,
            "assessmentDate": doc.assessment_date,
            "template": doc.template,
        }
    }

@frappe.whitelist(methods=["GET"])
def assessment_detail(assessment_id):
    trainer = _require_trainer()

    assessment_id = (assessment_id or "").strip()

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
                "hasValue": bool(row.is_entered),

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

@frappe.whitelist(methods=["POST"])
def save_assessment(
    assessment_id,
    inputs,
    status="Draft",
):
    trainer = _require_trainer()

    assessment_id = (assessment_id or "").strip()
    status = (status or "Draft").strip()

    if status not in ("Draft", "Completed"):
        frappe.throw(
            "Invalid assessment status",
            frappe.ValidationError,
        )

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

    if isinstance(inputs, str):
        inputs = frappe.parse_json(inputs)

    inputs = inputs or []

    doc = frappe.get_doc(
        "Club100 Assessment",
        assessment_id,
    )

    # Trainer may only update assessments they are
    # conducting.
    if doc.assessor != trainer.name:
        frappe.throw(
            "You cannot update this assessment.",
            frappe.PermissionError,
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