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
    delivery_mode="Offline",
):
    trainer = _require_trainer()

    member_id = (
        member_id or ""
    ).strip()

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

    # -----------------------------------------
    # Resume existing draft
    # -----------------------------------------

    existing_draft = frappe.db.get_value(
        "Club100 Assessment",
        {
            "member": member_id,
            "assessor": trainer.name,
            "status": "Draft",
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

    # -----------------------------------------
    # Baseline vs Reassessment
    # -----------------------------------------

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

    # -----------------------------------------
    # Default template
    # -----------------------------------------

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

    # -----------------------------------------
    # Create assessment
    # -----------------------------------------

    doc = frappe.get_doc(
        {
            "doctype":
                "Club100 Assessment",

            "member":
                member_id,

            "assessment_type":
                assessment_type,

            "assessment_date":
                frappe.utils.today(),

            "assessor":
                trainer.name,

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

@frappe.whitelist(methods=["GET"])
def assessment_result(assessment_id):
    trainer = _require_trainer()

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

    if doc.assessor != trainer.name:
        frappe.throw(
            "You cannot view this assessment.",
            frappe.PermissionError,
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

    # ---------------------------------------------------------
    # Current category scores
    # ---------------------------------------------------------

    categories = []

    for row in doc.category_scores:
        categories.append(
            {
                "category": row.category,
                "score": row.score,
                "weight": row.weight,
                "metricsScored":
                    row.metrics_scored,
            }
        )

    # ---------------------------------------------------------
    # Current metric results
    # ---------------------------------------------------------

    metrics = []

    for row in doc.metrics:
        metric_name = frappe.db.get_value(
            "Club100 Fitness Metric",
            row.metric,
            "metric_name",
        )

        metrics.append(
            {
                "metric": row.metric,
                "metricName":
                    metric_name
                    or row.metric,

                "category":
                    row.category,

                "value":
                    row.value,

                "textValue":
                    row.text_value,

                "unit":
                    row.unit,

                "score":
                    row.score,

                "rating":
                    row.rating,

                "required":
                    bool(
                        row.required
                    ),

                "includeInScore":
                    bool(
                        row.include_in_score
                    ),

                "weight":
                    row.weight,

                "notes":
                    row.notes,
            }
        )

    # ---------------------------------------------------------
    # Find immediately previous completed assessment
    # ---------------------------------------------------------

    completed_assessments = frappe.get_all(
        "Club100 Assessment",
        filters={
            "member": doc.member,
            "status": "Completed",
        },
        fields=[
            "name",
            "assessment_date",
            "creation",
            "fitness_score",
            "fitness_level",
            "assessment_type",
        ],
        order_by=(
            "assessment_date desc, "
            "creation desc"
        ),
    )

    previous = None

    for index, item in enumerate(
        completed_assessments
    ):
        if item.name != doc.name:
            continue

        previous_index = (
            index + 1
        )

        if (
            previous_index
            < len(
                completed_assessments
            )
        ):
            previous = (
                completed_assessments[
                    previous_index
                ]
            )

        break

    # ---------------------------------------------------------
    # Previous assessment
    # ---------------------------------------------------------

    previous_assessment = None

    if previous:
        previous_doc = frappe.get_doc(
            "Club100 Assessment",
            previous.name,
        )

        # Previous categories

        previous_categories = []

        for row in (
            previous_doc.category_scores
        ):
            previous_categories.append(
                {
                    "category":
                        row.category,

                    "score":
                        row.score,
                }
            )

        # Previous metrics

        previous_metrics = []

        for row in (
            previous_doc.metrics
        ):
            metric_name = (
                frappe.db.get_value(
                    "Club100 Fitness Metric",
                    row.metric,
                    "metric_name",
                )
            )

            previous_metrics.append(
                {
                    "metric":
                        row.metric,

                    "metricName":
                        metric_name
                        or row.metric,

                    "category":
                        row.category,

                    "value":
                        row.value,

                    "textValue":
                        row.text_value,

                    "unit":
                        row.unit,

                    "score":
                        row.score,

                    "rating":
                        row.rating,

                    "includeInScore":
                        bool(
                            row.include_in_score
                        ),
                }
            )

        previous_assessment = {
            "id":
                previous.name,

            "assessmentType":
                previous.assessment_type,

            "assessmentDate":
                previous.assessment_date,

            "fitnessScore":
                previous.fitness_score,

            "fitnessLevel":
                previous.fitness_level,

            "categories":
                previous_categories,

            "metrics":
                previous_metrics,
        }

    # ---------------------------------------------------------
    # Response
    # ---------------------------------------------------------

    return {
        "assessment": {
            "id":
                doc.name,

            "member": {
                "id":
                    member.name,

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

            "fitnessScore":
                doc.fitness_score,

            "fitnessLevel":
                doc.fitness_level,

            "categories":
                categories,

            "metrics":
                metrics,

            "previousAssessment":
                previous_assessment,
        }
    }

@frappe.whitelist(methods=["GET"])
def assessments(search=None, status=None):
    trainer = _require_trainer()

    search = (search or "").strip()
    status = (status or "").strip()

    filters = {
        "assessor": trainer.name,
    }

    if status in (
        "Draft",
        "Completed",
    ):
        filters["status"] = status

    rows = frappe.get_all(
        "Club100 Assessment",
        filters=filters,
        fields=[
            "name",
            "member",
            "assessment_type",
            "assessment_date",
            "delivery_mode",
            "status",
            "fitness_score",
            "fitness_level",
            "modified",
        ],
        order_by=(
            "assessment_date desc, "
            "modified desc"
        ),
        limit_page_length=50,
    )

    member_ids = list({
        row.member
        for row in rows
        if row.member
    })

    member_names = {}

    if member_ids:
        members = frappe.get_all(
            "Club100 Member",
            filters={
                "name": [
                    "in",
                    member_ids,
                ],
            },
            fields=[
                "name",
                "full_name",
                "mobile",
            ],
        )

        member_names = {
            row.name: {
                "fullName":
                    row.full_name,
                "mobile":
                    row.mobile,
            }
            for row in members
        }

    result = []

    for row in rows:
        member = member_names.get(
            row.member,
            {},
        )

        if search:
            haystack = " ".join([
                row.name or "",
                row.member or "",
                member.get(
                    "fullName",
                    "",
                ) or "",
                member.get(
                    "mobile",
                    "",
                ) or "",
            ]).lower()

            if search.lower() not in haystack:
                continue

        result.append(
            {
                "id": row.name,
                "member": {
                    "id": row.member,
                    "fullName":
                        member.get(
                            "fullName"
                        )
                        or row.member,
                    "mobile":
                        member.get(
                            "mobile"
                        ),
                },
                "assessmentType":
                    row.assessment_type,
                "assessmentDate":
                    row.assessment_date,
                "deliveryMode":
                    row.delivery_mode,
                "status":
                    row.status,
                "fitnessScore":
                    row.fitness_score,
                "fitnessLevel":
                    row.fitness_level,
            }
        )

    return {
        "assessments": result,
    }