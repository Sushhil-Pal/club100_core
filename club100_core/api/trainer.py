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

    # ---------------------------------------------------------
    # Completed assessments
    # ---------------------------------------------------------

    completed_rows = frappe.get_all(
        "Club100 Assessment",
        filters={
            "member": member.name,
            "status": "Completed",
        },
        fields=[
            "name",
            "assessment_type",
            "assessment_date",
            "fitness_score",
            "fitness_level",
            "creation",
        ],
        order_by=(
            "assessment_date desc, "
            "creation desc"
        ),
    )

    assessment_history = []

    for index, row in enumerate(
        completed_rows
    ):
        previous_score = None
        score_change = None

        previous_index = index + 1

        if previous_index < len(
            completed_rows
        ):
            previous_score = (
                completed_rows[
                    previous_index
                ].fitness_score
            )

        if (
            row.fitness_score is not None
            and previous_score is not None
        ):
            score_change = round(
                row.fitness_score
                - previous_score
            )

        assessment_history.append({
            "id":
                row.name,

            "type":
                row.assessment_type,

            "date":
                row.assessment_date,

            "fitnessScore":
                row.fitness_score,

            "fitnessLevel":
                row.fitness_level,

            "scoreChange":
                score_change,
        })

    latest_assessment = (
        assessment_history[0]
        if assessment_history
        else None
    )

    # ---------------------------------------------------------
    # Draft assessment
    # ---------------------------------------------------------

    draft_row = frappe.db.get_value(
        "Club100 Assessment",
        {
            "member": member.name,
            "status": "Draft",
        },
        [
            "name",
            "assessment_type",
            "assessment_date",
            "modified",
        ],
        order_by="modified desc",
        as_dict=True,
    )

    draft_assessment = (
        {
            "id":
                draft_row.name,

            "type":
                draft_row.assessment_type,

            "date":
                draft_row.assessment_date,

            "modified":
                draft_row.modified,
        }
        if draft_row
        else None
    )

    # ---------------------------------------------------------
    # Response
    # ---------------------------------------------------------

    return {
        "member": {
            "id":
                member.name,

            "fullName":
                member.full_name,

            "mobile":
                member.mobile,

            "email":
                member.email,

            "gender":
                member.gender,

            "dateOfBirth":
                member.date_of_birth,

            "joiningDate":
                member.joining_date,

            "onboardingStatus":
                member.onboarding_status
                or "Not Started",
        },

        "latestAssessment":
            latest_assessment,

        "assessmentCount":
            len(
                assessment_history
            ),

        "assessmentHistory":
            assessment_history,

        "draftAssessment":
            draft_assessment,
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

    filters = {}

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


@frappe.whitelist(methods=["GET"])
def sessions(status=None):
    trainer = _require_trainer()

    status = (status or "").strip()

    filters = {
        "trainer": trainer.name,
    }

    if status in (
        "Scheduled",
        "Live",
        "Completed",
        "Cancelled",
    ):
        filters["status"] = status

    rows = frappe.get_all(
        "Club100 Session",
        filters=filters,
        fields=[
            "name",
            "cohort",
            "program",
            "session_date",
            "start_time",
            "end_time",
            "delivery_mode",
            "status",
            "meeting_provider",
            "meeting_url",
            "attendance_synced",
        ],
        order_by=(
            "session_date desc, "
            "start_time desc"
        ),
        limit_page_length=100,
    )

    cohort_ids = list({
        row.cohort
        for row in rows
        if row.cohort
    })

    program_ids = list({
        row.program
        for row in rows
        if row.program
    })

    cohort_names = {}
    program_names = {}

    if cohort_ids:
        cohorts = frappe.get_all(
            "Club100 Cohort",
            filters={
                "name": [
                    "in",
                    cohort_ids,
                ],
            },
            fields=[
                "name",
                "cohort_name",
            ],
        )

        cohort_names = {
            row.name:
                row.cohort_name
                or row.name
            for row in cohorts
        }

    if program_ids:
        programs = frappe.get_all(
            "Club100 Program",
            filters={
                "name": [
                    "in",
                    program_ids,
                ],
            },
            fields=[
                "name",
                "program_name",
            ],
        )

        program_names = {
            row.name:
                row.program_name
                or row.name
            for row in programs
        }

    return {
        "sessions": [
            {
                "id": row.name,

                "cohort": {
                    "id": row.cohort,
                    "name":
                        cohort_names.get(
                            row.cohort
                        )
                        or row.cohort,
                },

                "program": {
                    "id": row.program,
                    "name":
                        program_names.get(
                            row.program
                        )
                        or row.program,
                },

                "sessionDate":
                    row.session_date,

                "startTime":
                    row.start_time,

                "endTime":
                    row.end_time,

                "deliveryMode":
                    row.delivery_mode,

                "status":
                    row.status,

                "meetingProvider":
                    row.meeting_provider,

                "meetingUrl":
                    row.meeting_url,

                "attendanceSynced":
                    bool(
                        row.attendance_synced
                    ),
            }
            for row in rows
        ]
    }

@frappe.whitelist(methods=["GET"])
def session_detail(session_id):
    trainer = _require_trainer()

    session_id = (
        session_id or ""
    ).strip()

    if not session_id:
        frappe.throw(
            "Session ID is required",
            frappe.ValidationError,
        )

    if not frappe.db.exists(
        "Club100 Session",
        session_id,
    ):
        frappe.throw(
            "Session not found",
            frappe.DoesNotExistError,
        )

    session = frappe.get_doc(
        "Club100 Session",
        session_id,
    )

    if session.trainer != trainer.name:
        frappe.throw(
            "You cannot access this session.",
            frappe.PermissionError,
        )

    cohort = frappe.db.get_value(
        "Club100 Cohort",
        session.cohort,
        [
            "name",
            "cohort_name",
        ],
        as_dict=True,
    )

    program = frappe.db.get_value(
        "Club100 Program",
        session.program,
        [
            "name",
            "program_name",
        ],
        as_dict=True,
    )

    # ---------------------------------------------------------
    # Enrollments valid on the session date
    # ---------------------------------------------------------

    enrollment_rows = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "cohort":
                session.cohort,

            "status":
                ["!=", "Cancelled"],
        },
        fields=[
            "name",
            "member",
            "start_date",
            "end_date",
            "status",
        ],
        order_by="creation asc",
    )

    enrollments = []

    for row in enrollment_rows:
        if (
            row.start_date
            and row.start_date
            > session.session_date
        ):
            continue

        if (
            row.end_date
            and row.end_date
            < session.session_date
        ):
            continue

        enrollments.append(
            row
        )

    member_ids = [
        row.member
        for row in enrollments
        if row.member
    ]

    members_by_id = {}

    if member_ids:
        member_rows = frappe.get_all(
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
                "email",
            ],
        )

        members_by_id = {
            row.name: row
            for row in member_rows
        }

    # ---------------------------------------------------------
    # Existing attendance
    # ---------------------------------------------------------

    attendance_rows = frappe.get_all(
        "Club100 Attendance",
        filters={
            "session":
                session.name,
        },
        fields=[
            "name",
            "member",
            "attendance_mode",
            "source",
            "join_time",
            "leave_time",
            "minutes_attended",
            "attendance_status",
            "notes",
        ],
    )

    attendance_by_member = {
        row.member: row
        for row in attendance_rows
        if row.member
    }

    participants = []

    for enrollment in enrollments:
        member = members_by_id.get(
            enrollment.member
        )

        if not member:
            continue

        attendance = (
            attendance_by_member.get(
                member.name
            )
        )

        participants.append(
            {
                "member": {
                    "id":
                        member.name,

                    "fullName":
                        member.full_name,

                    "mobile":
                        member.mobile,

                    "email":
                        member.email,
                },

                "enrollmentId":
                    enrollment.name,

                "attendance": (
                    {
                        "id":
                            attendance.name,

                        "status":
                            attendance.attendance_status,

                        "mode":
                            attendance.attendance_mode,

                        "source":
                            attendance.source,

                        "minutesAttended":
                            attendance.minutes_attended,

                        "notes":
                            attendance.notes,
                    }
                    if attendance
                    else None
                ),
            }
        )

    return {
        "session": {
            "id":
                session.name,

            "cohort": {
                "id":
                    cohort.name
                    if cohort
                    else session.cohort,

                "name":
                    (
                        cohort.cohort_name
                        if cohort
                        else session.cohort
                    ),
            },

            "program": {
                "id":
                    program.name
                    if program
                    else session.program,

                "name":
                    (
                        program.program_name
                        if program
                        else session.program
                    ),
            },

            "sessionDate":
                session.session_date,

            "startTime":
                session.start_time,

            "endTime":
                session.end_time,

            "deliveryMode":
                session.delivery_mode,

            "status":
                session.status,

            "meetingProvider":
                session.meeting_provider,

            "meetingUrl":
                session.meeting_url,

            "notes":
                session.notes,

            "attendanceSynced":
                bool(
                    session.attendance_synced
                ),

            "participants":
                participants,
        }
    }


@frappe.whitelist(methods=["POST"])
def save_session_attendance(
    session_id,
    attendance,
):
    trainer = _require_trainer()

    session_id = (
        session_id or ""
    ).strip()

    if not session_id:
        frappe.throw(
            "Session ID is required",
            frappe.ValidationError,
        )

    if isinstance(
        attendance,
        str,
    ):
        attendance = (
            frappe.parse_json(
                attendance
            )
        )

    attendance = (
        attendance or []
    )

    session = frappe.get_doc(
        "Club100 Session",
        session_id,
    )

    if session.trainer != trainer.name:
        frappe.throw(
            "You cannot update this session.",
            frappe.PermissionError,
        )

    if session.status != "Live":
        frappe.throw(
            "Attendance can only be updated while the session is live.",
            frappe.ValidationError,
        )

    if session.status == "Cancelled":
        frappe.throw(
            "Attendance cannot be updated for a cancelled session.",
            frappe.ValidationError,
        )

    valid_statuses = {
        "Present",
        "Partial",
        "Absent",
    }

    # ---------------------------------------------------------
    # Members enrolled on the session date
    # ---------------------------------------------------------

    enrollment_rows = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "cohort":
                session.cohort,

            "status":
                ["!=", "Cancelled"],
        },
        fields=[
            "member",
            "start_date",
            "end_date",
        ],
    )

    valid_members = set()

    for row in enrollment_rows:
        if (
            row.start_date
            and row.start_date
            > session.session_date
        ):
            continue

        if (
            row.end_date
            and row.end_date
            < session.session_date
        ):
            continue

        if row.member:
            valid_members.add(
                row.member
            )

    saved = 0

    for item in attendance:
        member_id = (
            item.get("memberId")
            or ""
        ).strip()

        attendance_status = (
            item.get("status")
            or ""
        ).strip()

        notes = (
            item.get("notes")
            or ""
        ).strip()

        if not member_id:
            continue

        if (
            member_id
            not in valid_members
        ):
            frappe.throw(
                f"Member {member_id} "
                "was not enrolled in this "
                "session cohort on the "
                "session date.",
                frappe.ValidationError,
            )

        if (
            attendance_status
            not in valid_statuses
        ):
            frappe.throw(
                "Attendance status must be "
                "Present, Partial or Absent.",
                frappe.ValidationError,
            )

        existing = frappe.db.get_value(
            "Club100 Attendance",
            {
                "session":
                    session.name,

                "member":
                    member_id,
            },
            "name",
        )

        if existing:
            doc = frappe.get_doc(
                "Club100 Attendance",
                existing,
            )
        else:
            doc = frappe.new_doc(
                "Club100 Attendance"
            )

            doc.session = (
                session.name
            )

            doc.member = (
                member_id
            )

        doc.attendance_status = (
            attendance_status
        )

        doc.attendance_mode = (
            "Trainer Marked"
        )

        doc.source = "Manual"

        doc.recorded_by = (
            frappe.session.user
        )

        doc.recorded_at = (
            frappe.utils.now()
        )

        doc.notes = (
            notes or None
        )

        doc.save(
            ignore_permissions=True
        )

        saved += 1

    return {
        "success": True,
        "saved": saved,
    }

@frappe.whitelist(methods=["POST"])
def complete_session(session_id):
    trainer = _require_trainer()

    session_id = (
        session_id or ""
    ).strip()

    session = frappe.get_doc(
        "Club100 Session",
        session_id,
    )

    if session.trainer != trainer.name:
        frappe.throw(
            "You cannot complete this session.",
            frappe.PermissionError,
        )

    if session.status != "Live":
        frappe.throw(
            "Only a live session can be completed.",
            frappe.ValidationError,
        )

    if session.status == "Cancelled":
        frappe.throw(
            "A cancelled session cannot be completed.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Members enrolled on the session date
    # ---------------------------------------------------------

    enrollment_rows = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "cohort":
                session.cohort,

            "status":
                ["!=", "Cancelled"],
        },
        fields=[
            "member",
            "start_date",
            "end_date",
        ],
    )

    session_members = set()

    for row in enrollment_rows:
        if (
            row.start_date
            and row.start_date
            > session.session_date
        ):
            continue

        if (
            row.end_date
            and row.end_date
            < session.session_date
        ):
            continue

        if row.member:
            session_members.add(
                row.member
            )

    attendance_members = set(
        frappe.get_all(
            "Club100 Attendance",
            filters={
                "session":
                    session.name,
            },
            pluck="member",
        )
    )

    missing = (
        session_members
        - attendance_members
    )

    if missing:
        frappe.throw(
            f"Attendance must be recorded "
            f"for all participants before "
            f"completing the session. "
            f"{len(missing)} participant(s) "
            f"are still unmarked.",
            frappe.ValidationError,
        )

    session.status = "Completed"

    session.attendance_synced = 1

    session.save(
        ignore_permissions=True
    )

    return {
        "success": True,

        "session": {
            "id":
                session.name,

            "status":
                session.status,
        },
    }

@frappe.whitelist(methods=["GET"])
def today():
    trainer = _require_trainer()

    today_date = frappe.utils.today()

    # ---------------------------------------------------------
    # Today's sessions
    # ---------------------------------------------------------

    session_rows = frappe.get_all(
        "Club100 Session",
        filters={
            "trainer": trainer.name,
            "session_date": today_date,
        },
        fields=[
            "name",
            "cohort",
            "program",
            "session_date",
            "start_time",
            "end_time",
            "delivery_mode",
            "status",
            "meeting_provider",
            "meeting_url",
        ],
        order_by="start_time asc",
    )

    cohort_ids = list({
        row.cohort
        for row in session_rows
        if row.cohort
    })

    program_ids = list({
        row.program
        for row in session_rows
        if row.program
    })

    cohort_names = {}
    program_names = {}

    if cohort_ids:
        cohorts = frappe.get_all(
            "Club100 Cohort",
            filters={
                "name": ["in", cohort_ids],
            },
            fields=[
                "name",
                "cohort_name",
            ],
        )

        cohort_names = {
            row.name:
                row.cohort_name
                or row.name
            for row in cohorts
        }

    if program_ids:
        programs = frappe.get_all(
            "Club100 Program",
            filters={
                "name": ["in", program_ids],
            },
            fields=[
                "name",
                "program_name",
            ],
        )

        program_names = {
            row.name:
                row.program_name
                or row.name
            for row in programs
        }

    sessions = []

    for row in session_rows:
        sessions.append({
            "id":
                row.name,

            "cohort": {
                "id":
                    row.cohort,

                "name":
                    cohort_names.get(
                        row.cohort
                    )
                    or row.cohort,
            },

            "program": {
                "id":
                    row.program,

                "name":
                    program_names.get(
                        row.program
                    )
                    or row.program,
            },

            "sessionDate":
                row.session_date,

            "startTime":
                row.start_time,

            "endTime":
                row.end_time,

            "deliveryMode":
                row.delivery_mode,

            "status":
                row.status,

            "meetingProvider":
                row.meeting_provider,

            "meetingUrl":
                row.meeting_url,
        })

    # ---------------------------------------------------------
    # Draft assessments
    # ---------------------------------------------------------

    assessment_rows = frappe.get_all(
        "Club100 Assessment",
        filters={
            "status": "Draft",
        },
        fields=[
            "name",
            "member",
            "assessment_type",
            "assessment_date",
            "modified",
        ],
        order_by="modified desc",
        limit_page_length=5,
    )

    member_ids = list({
        row.member
        for row in assessment_rows
        if row.member
    })

    members = {}

    if member_ids:
        member_rows = frappe.get_all(
            "Club100 Member",
            filters={
                "name": ["in", member_ids],
            },
            fields=[
                "name",
                "full_name",
                "mobile",
            ],
        )

        members = {
            row.name: row
            for row in member_rows
        }

    draft_assessments = []

    for row in assessment_rows:
        member = members.get(
            row.member
        )

        draft_assessments.append({
            "id":
                row.name,

            "assessmentType":
                row.assessment_type,

            "assessmentDate":
                row.assessment_date,

            "modified":
                row.modified,

            "member": {
                "id":
                    row.member,

                "fullName":
                    (
                        member.full_name
                        if member
                        else row.member
                    ),

                "mobile":
                    (
                        member.mobile
                        if member
                        else None
                    ),
            },
        })

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------

    scheduled_count = sum(
        1
        for item in sessions
        if item["status"] == "Scheduled"
    )

    live_count = sum(
        1
        for item in sessions
        if item["status"] == "Live"
    )

    completed_count = sum(
        1
        for item in sessions
        if item["status"] == "Completed"
    )

    return {
        "date":
            today_date,

        "summary": {
            "sessions":
                len(sessions),

            "scheduled":
                scheduled_count,

            "live":
                live_count,

            "completed":
                completed_count,

            "draftAssessments":
                len(
                    draft_assessments
                ),
        },

        "sessions":
            sessions,

        "draftAssessments":
            draft_assessments,
    }

@frappe.whitelist(methods=["GET"])
def profile():
    trainer_ref = _require_trainer()

    trainer = frappe.get_doc(
        "Club100 Trainer",
        trainer_ref.name,
    )

    return {
        "trainer": {
            "id":
                trainer.name,

            "trainerName":
                trainer.trainer_name,

            "email":
                trainer.email,

            "mobile":
                trainer.mobile,

            "trainerType":
                trainer.trainer_type,

            "status":
                trainer.status,

            "photo":
                trainer.profile_photo,

            "bio":
                trainer.bio,

            "certifications":
                trainer.certifications,

            "location":
                trainer.primary_location,

            "onlineEligible":
                bool(
                    trainer.can_deliver_online
                ),

            "offlineEligible":
                bool(
                    trainer.can_deliver_offline
                ),

            "maxOnlineCohortSize":
                trainer.max_online_cohort_size,

            "employee":
                trainer.employee,

            "user":
                trainer.user,
        }
    }


@frappe.whitelist(methods=["POST"])
def update_profile(
    mobile=None,
    email=None,
    bio=None,
    certifications=None,
    location=None,
):
    trainer_ref = _require_trainer()

    trainer = frappe.get_doc(
        "Club100 Trainer",
        trainer_ref.name,
    )

    # ---------------------------------------------------------
    # Mobile
    # ---------------------------------------------------------

    if mobile is not None:
        mobile = (
            str(mobile).strip()
        )

        if not mobile:
            frappe.throw(
                "Mobile is required.",
                frappe.ValidationError,
            )

        trainer.mobile = mobile

    # ---------------------------------------------------------
    # Email
    # ---------------------------------------------------------

    if email is not None:
        email = (
            str(email).strip()
        )

        if not email:
            frappe.throw(
                "Email is required.",
                frappe.ValidationError,
            )

        if not frappe.utils.validate_email_address(
            email
        ):
            frappe.throw(
                "Please enter a valid email address.",
                frappe.ValidationError,
            )

        trainer.email = email

    # ---------------------------------------------------------
    # Bio
    # ---------------------------------------------------------

    if bio is not None:
        trainer.bio = (
            str(bio).strip()
            or None
        )

    # ---------------------------------------------------------
    # Certifications
    # ---------------------------------------------------------

    if certifications is not None:
        trainer.certifications = (
            str(
                certifications
            ).strip()
            or None
        )

    # ---------------------------------------------------------
    # Primary Location
    # ---------------------------------------------------------

    if location is not None:
        trainer.primary_location = (
            str(location).strip()
            or None
        )

    trainer.save(
        ignore_permissions=True
    )

    return {
        "success": True,

        "trainer": {
            "id":
                trainer.name,

            "trainerName":
                trainer.trainer_name,

            "email":
                trainer.email,

            "mobile":
                trainer.mobile,

            "trainerType":
                trainer.trainer_type,

            "status":
                trainer.status,

            "photo":
                trainer.profile_photo,

            "bio":
                trainer.bio,

            "certifications":
                trainer.certifications,

            "location":
                trainer.primary_location,

            "onlineEligible":
                bool(
                    trainer.can_deliver_online
                ),

            "offlineEligible":
                bool(
                    trainer.can_deliver_offline
                ),

            "maxOnlineCohortSize":
                trainer.max_online_cohort_size,

            "employee":
                trainer.employee,

            "user":
                trainer.user,
        },
    }


@frappe.whitelist(methods=["POST"])
def upload_profile_photo():
    trainer_ref = _require_trainer()

    trainer = frappe.get_doc(
        "Club100 Trainer",
        trainer_ref.name,
    )

    uploaded_file = (
        frappe.request.files.get(
            "file"
        )
    )

    if not uploaded_file:
        frappe.throw(
            "Please select an image to upload.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Validate content type
    # ---------------------------------------------------------

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp",
    }

    content_type = (
        uploaded_file.content_type
        or ""
    ).lower()

    if content_type not in allowed_types:
        frappe.throw(
            "Profile photo must be a JPEG, PNG or WebP image.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Read and validate file size
    # ---------------------------------------------------------

    content = uploaded_file.read()

    max_size = (
        5 * 1024 * 1024
    )

    if len(content) > max_size:
        frappe.throw(
            "Profile photo must be smaller than 5 MB.",
            frappe.ValidationError,
        )

    if not content:
        frappe.throw(
            "Uploaded image is empty.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Save File attached to Trainer
    # ---------------------------------------------------------

    from frappe.utils.file_manager import save_file

    file_doc = save_file(
        uploaded_file.filename,
        content,
        "Club100 Trainer",
        trainer.name,
        is_private=0,
    )

    # Actual Club100 Trainer field:
    # profile_photo

    trainer.profile_photo = (
        file_doc.file_url
    )

    trainer.save(
        ignore_permissions=True
    )

    return {
        "success": True,

        "photo":
            trainer.profile_photo,
    }

@frappe.whitelist(methods=["POST"])
def start_session(session_id):
    trainer = _require_trainer()

    session_id = (
        session_id or ""
    ).strip()

    if not session_id:
        frappe.throw(
            "Session ID is required.",
            frappe.ValidationError,
        )

    session = frappe.get_doc(
        "Club100 Session",
        session_id,
    )

    if session.trainer != trainer.name:
        frappe.throw(
            "You cannot start this session.",
            frappe.PermissionError,
        )

    if session.status != "Scheduled":
        frappe.throw(
            "Only a scheduled session can be started.",
            frappe.ValidationError,
        )

    session.status = "Live"

    session.save(
        ignore_permissions=True
    )

    return {
        "success": True,
        "session": {
            "id": session.name,
            "status": session.status,
        },
    }


@frappe.whitelist(methods=["POST"])
def cancel_session(session_id):
    trainer = _require_trainer()

    session_id = (
        session_id or ""
    ).strip()

    if not session_id:
        frappe.throw(
            "Session ID is required.",
            frappe.ValidationError,
        )

    session = frappe.get_doc(
        "Club100 Session",
        session_id,
    )

    if session.trainer != trainer.name:
        frappe.throw(
            "You cannot cancel this session.",
            frappe.PermissionError,
        )

    if session.status not in (
        "Scheduled",
        "Live",
    ):
        frappe.throw(
            "Only a scheduled or live session can be cancelled.",
            frappe.ValidationError,
        )

    session.status = "Cancelled"

    session.save(
        ignore_permissions=True
    )

    return {
        "success": True,
        "session": {
            "id": session.name,
            "status": session.status,
        },
    }