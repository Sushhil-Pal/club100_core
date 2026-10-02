import frappe


def _get_current_member():
    user = frappe.session.user

    if user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.PermissionError,
        )

    member_name = frappe.db.get_value(
        "Club100 Member",
        {"user": user},
        "name",
    )

    if not member_name:
        frappe.throw(
            "Club100 member profile not found"
        )

    return frappe.get_doc(
        "Club100 Member",
        member_name,
    )


@frappe.whitelist()
def me():
    member = _get_current_member()

    return {
        "id": member.name,
        "fullName": member.full_name,
        "email": member.email,
        "mobile": member.mobile,
        "memberType": member.member_type,
        "organization": member.organization,
        "status": member.status,

        "dateOfBirth": member.date_of_birth,
        "gender": member.gender,

        "fitnessLevel":
            member.current_fitness_level,

        "fitnessGoal":
            member.fitness_goal,

        "preferredDeliveryMode":
            member.preferred_delivery_mode,

        "medicalNotes":
            member.medical_notes,

        "onboardingStatus":
            member.onboarding_status
            or "Not Started",

        "onboardingCompletedOn":
            member.onboarding_completed_on,
    }


@frappe.whitelist(
    methods=["POST"],
)
def update_profile(
    full_name=None,
    email=None,
    mobile=None,
):
    member = _get_current_member()

    if full_name is not None:
        full_name = full_name.strip()

        if not full_name:
            frappe.throw(
                "Full name is required"
            )

        name_parts = full_name.split(
            maxsplit=1
        )

        member.first_name = name_parts[0]

        member.last_name = (
            name_parts[1]
            if len(name_parts) > 1
            else ""
        )

    if email is not None:
        member.email = email.strip()

    if mobile is not None:
        member.mobile = mobile.strip()

    member.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "member": {
            "id": member.name,
            "fullName": member.full_name,
            "email": member.email,
            "mobile": member.mobile,
            "memberType": member.member_type,
            "fitnessLevel": member.current_fitness_level,
            "organization": member.organization,
            "status": member.status,
        },
    }


@frappe.whitelist(methods=["POST"])
def complete_onboarding(
    date_of_birth=None,
    gender=None,
    fitness_goal=None,
    current_fitness_level=None,
    preferred_delivery_mode=None,
    medical_notes=None,
):
    member = _get_current_member()

    # -----------------------------------------------------
    # Basic validation
    # -----------------------------------------------------

    if not date_of_birth:
        frappe.throw(
            "Date of birth is required",
            frappe.ValidationError,
        )

    if not gender:
        frappe.throw(
            "Gender is required",
            frappe.ValidationError,
        )

    if not fitness_goal:
        frappe.throw(
            "Fitness goal is required",
            frappe.ValidationError,
        )

    if not current_fitness_level:
        frappe.throw(
            "Current fitness level is required",
            frappe.ValidationError,
        )

    if not preferred_delivery_mode:
        frappe.throw(
            "Preferred delivery mode is required",
            frappe.ValidationError,
        )

    # -----------------------------------------------------
    # Update onboarding fields
    # -----------------------------------------------------

    member.date_of_birth = date_of_birth
    member.gender = gender

    member.fitness_goal = fitness_goal

    member.current_fitness_level = (
        current_fitness_level
    )

    member.preferred_delivery_mode = (
        preferred_delivery_mode
    )

    member.medical_notes = (
        medical_notes or ""
    )

    member.onboarding_status = "Completed"

    member.onboarding_completed_on = (
        frappe.utils.now_datetime()
    )

    member.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,

        "member": {
            "id": member.name,
            "fullName": member.full_name,

            "dateOfBirth":
                member.date_of_birth,

            "gender":
                member.gender,

            "fitnessLevel":
                member.current_fitness_level,

            "fitnessGoal":
                member.fitness_goal,

            "preferredDeliveryMode":
                member.preferred_delivery_mode,

            "medicalNotes":
                member.medical_notes,

            "onboardingStatus":
                member.onboarding_status,

            "onboardingCompletedOn":
                member.onboarding_completed_on,
        },
    }

@frappe.whitelist(methods=["GET"])
def progress():
    member = _get_current_member()

    completed = frappe.get_all(
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

    # ---------------------------------------------------------
    # No completed assessments
    # ---------------------------------------------------------

    if not completed:
        return {
            "hasAssessment": False,
            "hasPreviousAssessment": False,
            "currentAssessment": None,
            "previousAssessment": None,
            "fitnessScore": {
                "current": None,
                "previous": None,
                "change": None,
            },
            "categoryScores": [],
            "assessments": [],
        }

    current_row = completed[0]

    previous_row = (
        completed[1]
        if len(completed) > 1
        else None
    )

    current_doc = frappe.get_doc(
        "Club100 Assessment",
        current_row.name,
    )

    previous_doc = (
        frappe.get_doc(
            "Club100 Assessment",
            previous_row.name,
        )
        if previous_row
        else None
    )

    # ---------------------------------------------------------
    # Overall score
    # ---------------------------------------------------------

    score_change = None

    if (
        current_row.fitness_score is not None
        and previous_row
        and previous_row.fitness_score is not None
    ):
        score_change = round(
            current_row.fitness_score
            - previous_row.fitness_score
        )

    # ---------------------------------------------------------
    # Category comparison
    # ---------------------------------------------------------

    previous_categories = {}

    if previous_doc:
        previous_categories = {
            row.category: row.score
            for row in previous_doc.category_scores
        }

    category_scores = []

    for row in current_doc.category_scores:
        previous_score = (
            previous_categories.get(
                row.category
            )
        )

        change = None

        if (
            row.score is not None
            and previous_score is not None
        ):
            change = round(
                row.score
                - previous_score
            )

        category_scores.append({
            "category":
                row.category,

            "current":
                row.score,

            "previous":
                previous_score,

            "change":
                change,
        })

    # ---------------------------------------------------------
    # Assessment history
    # ---------------------------------------------------------

    assessments = []

    for index, row in enumerate(
        completed
    ):
        history_previous = (
            completed[index + 1]
            if index + 1 < len(completed)
            else None
        )

        history_change = None

        if (
            row.fitness_score is not None
            and history_previous
            and history_previous.fitness_score
            is not None
        ):
            history_change = round(
                row.fitness_score
                - history_previous.fitness_score
            )

        assessments.append({
            "id":
                row.name,

            "type":
                row.assessment_type,

            "date":
                row.assessment_date,

            "score":
                row.fitness_score,

            "fitnessLevel":
                row.fitness_level,

            "change":
                history_change,
        })

    # ---------------------------------------------------------
    # Response
    # ---------------------------------------------------------

    return {
        "hasAssessment": True,

        "hasPreviousAssessment":
            previous_row is not None,

        "currentAssessment": {
            "id":
                current_row.name,

            "type":
                current_row.assessment_type,

            "date":
                current_row.assessment_date,

            "fitnessLevel":
                current_row.fitness_level,
        },

        "previousAssessment": (
            {
                "id":
                    previous_row.name,

                "type":
                    previous_row.assessment_type,

                "date":
                    previous_row.assessment_date,

                "fitnessLevel":
                    previous_row.fitness_level,
            }
            if previous_row
            else None
        ),

        "fitnessScore": {
            "current":
                current_row.fitness_score,

            "previous":
                (
                    previous_row.fitness_score
                    if previous_row
                    else None
                ),

            "change":
                score_change,
        },

        "categoryScores":
            category_scores,

        "assessments":
            assessments,
    }


@frappe.whitelist(methods=["GET"])
def assessment_result(assessment_id):
    member = _get_current_member()

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

    # Members may only view their own assessments.
    if doc.member != member.name:
        frappe.throw(
            "You cannot view this assessment.",
            frappe.PermissionError,
        )

    # Member-facing results are only available
    # after completion.
    if doc.status != "Completed":
        frappe.throw(
            "Assessment result is not available yet.",
            frappe.ValidationError,
        )

    # ---------------------------------------------------------
    # Current categories
    # ---------------------------------------------------------

    categories = []

    for row in doc.category_scores:
        categories.append({
            "category":
                row.category,

            "score":
                row.score,

            "weight":
                row.weight,

            "metricsScored":
                row.metrics_scored,
        })

    # ---------------------------------------------------------
    # Current metrics
    # ---------------------------------------------------------

    metrics = []

    for row in doc.metrics:
        metric_name = frappe.db.get_value(
            "Club100 Fitness Metric",
            row.metric,
            "metric_name",
        )

        metrics.append({
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
        })

    # ---------------------------------------------------------
    # Immediately previous completed assessment
    # ---------------------------------------------------------

    completed = frappe.get_all(
        "Club100 Assessment",
        filters={
            "member":
                member.name,

            "status":
                "Completed",
        },
        fields=[
            "name",
            "assessment_type",
            "assessment_date",
            "creation",
            "fitness_score",
            "fitness_level",
        ],
        order_by=(
            "assessment_date desc, "
            "creation desc"
        ),
    )

    previous = None

    for index, item in enumerate(
        completed
    ):
        if item.name != doc.name:
            continue

        previous_index = (
            index + 1
        )

        if previous_index < len(
            completed
        ):
            previous = completed[
                previous_index
            ]

        break

    previous_assessment = None

    if previous:
        previous_doc = frappe.get_doc(
            "Club100 Assessment",
            previous.name,
        )

        previous_categories = []

        for row in (
            previous_doc.category_scores
        ):
            previous_categories.append({
                "category":
                    row.category,

                "score":
                    row.score,
            })

        previous_metrics = []

        for row in (
            previous_doc.metrics
        ):
            metric_name = frappe.db.get_value(
                "Club100 Fitness Metric",
                row.metric,
                "metric_name",
            )

            previous_metrics.append({
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
            })

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

    return {
        "assessment": {
            "id":
                doc.name,

            "assessmentType":
                doc.assessment_type,

            "assessmentDate":
                doc.assessment_date,

            "deliveryMode":
                doc.delivery_mode,

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