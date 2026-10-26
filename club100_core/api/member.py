import frappe

from club100_core.assessment import service as assessment_service


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

        "fitnessGoals":
        (
            frappe.parse_json(
                member.fitness_goal
            )
            if member.fitness_goal
            else []
        ),

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
    fitness_goals=None,
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

    if isinstance(fitness_goals, str):
        fitness_goals = frappe.parse_json(fitness_goals)

    fitness_goals = fitness_goals or []

    if not fitness_goals:
        frappe.throw(
            "At least one fitness goal is required",
            frappe.ValidationError,
        )

 

    if not current_fitness_level:
        frappe.throw(
            "Current fitness level is required",
            frappe.ValidationError,
        )

    # -----------------------------------------------------
    # Update onboarding fields
    # -----------------------------------------------------

    member.date_of_birth = date_of_birth
    member.gender = gender

    member.fitness_goal = frappe.as_json(
        fitness_goals
    )

    member.current_fitness_level = (
        current_fitness_level
    )

    # Legacy optional member preference: do not overwrite it when onboarding
    # omits this value. Actual delivery mode belongs to the enrollment/plan.
    if preferred_delivery_mode:
        member.preferred_delivery_mode = preferred_delivery_mode

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


SELF_ASSESSMENT_TEMPLATE_CODE = "SELF"
SELF_ASSESSMENT_PLAN_MODES = (
    "Self Assessment",
    "Both",
)


def _get_self_assessment_membership(member, enrollment_id=None):
    if member.status != "Active":
        return None

    today = frappe.utils.getdate(
        frappe.utils.today()
    )

    enrollments = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "member": member.name,
            "status": "Active",
        },
        fields=[
            "name",
            "program",
            "plan",
            "pricing_model",
            "start_date",
            "end_date",
            "membership_duration_months",
        ],
        order_by=(
            "start_date desc, "
            "enrollment_date desc, "
            "creation desc"
        ),
    )

    if enrollment_id:
        from club100_core.api.memberships import resolve_active_enrollment
        selected = resolve_active_enrollment(member.name, enrollment_id)
        enrollments = [row for row in enrollments if row.name == selected.name]

    for enrollment in enrollments:
        # Active, date-valid membership is already owner-checked above.
        # Assessment eligibility is program-based, not delivery-mode-based.
        if (
            enrollment.start_date
            and frappe.utils.getdate(enrollment.start_date) > today
        ):
            continue
        if (
            enrollment.end_date
            and frappe.utils.getdate(enrollment.end_date) < today
        ):
            continue

        plan = (
            frappe.db.get_value(
                "Club100 Plan",
                enrollment.plan,
                ["name", "plan_name", "plan_code", "delivery_type", "assessment_mode"],
                as_dict=True,
            )
            if enrollment.plan else None
        )
        # Society/corporate membership can be cohort-based without a Plan.
        if not plan:
            plan = frappe._dict({
                "name": "", "plan_name": "Membership", "plan_code": "",
                "delivery_type": "Onsite", "assessment_mode": "Both",
            })

        program = frappe.db.get_value(
            "Club100 Program",
            enrollment.program,
            [
                "name",
                "program_name",
                "program_code",
                "assessment_included",
                "reassessment_included",
                "reassessment_interval_months",
            ],
            as_dict=True,
        )

        if not program:
            continue

        if not program.assessment_included:
            continue

        return {
            "enrollment": enrollment,
            "plan": plan,
            "program": program,
        }

    return None


def _resolve_self_assessment_context(member, enrollment_id=None):
    if member.status != "Active":
        return {"allowed": False, "reason": "Your Club100 account is not active."}

    if (
        member.onboarding_status != "Completed"
        or not member.date_of_birth
        or not member.gender
    ):
        return {
            "allowed": False,
            "reason": (
                "Complete your member onboarding before starting "
                "the fitness assessment."
            ),
        }

    membership = _get_self_assessment_membership(
        member, enrollment_id=enrollment_id
    )

    enrollment = membership["enrollment"] if membership else None
    plan = membership["plan"] if membership else None
    program = membership["program"] if membership else None

    draft = frappe.db.get_value(
        "Club100 Assessment",
        {
            "member": member.name,
            "status": "Draft",
        },
        [
            "name",
            "assessment_type",
            "assessment_date",
            "assessment_mode",
            "safety_acknowledged_on",
        ],
        order_by="modified desc",
        as_dict=True,
    )

    latest = frappe.db.get_value(
        "Club100 Assessment",
        {
            "member": member.name,
            "status": "Completed",
        },
        [
            "name",
            "assessment_type",
            "assessment_mode",
            "assessment_date",
            "fitness_score",
            "fitness_level",
        ],
        order_by=(
            "assessment_date desc, "
            "creation desc"
        ),
        as_dict=True,
    )

    # Retain program cadence when present; otherwise allow a member-wide
    # reassessment following an existing completed assessment.
    interval_months = int(program.reassessment_interval_months or 0) if program else 0

    next_due_on = None
    due = False

    if latest:
        if interval_months > 0:
            next_due_on = frappe.utils.getdate(
                frappe.utils.add_months(
                    latest.assessment_date,
                    interval_months,
                )
            )

            due = (
                frappe.utils.getdate(
                    frappe.utils.today()
                )
                >= next_due_on
            )
        else:
            due = True

    if draft:
        state = "Draft"
        can_start = True
        assessment_type = (
            draft.assessment_type
        )
    elif not latest:
        state = "Not Started"
        can_start = True
        assessment_type = "Baseline"
    elif due:
        state = "Due"
        can_start = True
        assessment_type = "Reassessment"
    else:
        state = "Current"
        can_start = False
        assessment_type = "Reassessment"

    return {
        "allowed": True,
        "assessmentMode": draft.assessment_mode if draft else "Self Assessment",
        "state": state,
        "canStart": can_start,
        "assessmentType": assessment_type,
        "draftAssessmentId": (
            draft.name
            if draft
            else None
        ),
        "safetyAcknowledgementRequired": (
            not bool(draft)
        ),
        "latestAssessment": (
            {
                "id": latest.name,
                "type": latest.assessment_type,
                "mode": latest.assessment_mode,
                "date": latest.assessment_date,
                "fitnessScore": latest.fitness_score,
                "fitnessLevel": latest.fitness_level,
            }
            if latest
            else None
        ),
        "nextAssessmentDueOn": (
            str(next_due_on)
            if next_due_on
            else None
        ),
        "reassessmentIntervalMonths": (
            interval_months
        ),
        "enrollment": ({
            "id": enrollment.name, "status": "Active",
            "startDate": enrollment.start_date, "endDate": enrollment.end_date,
            "durationMonths": enrollment.membership_duration_months,
            "pricingModel": enrollment.pricing_model,
        } if enrollment else None),
        "program": ({
            "id": program.name, "code": program.program_code,
            "name": program.program_name,
            "assessmentIncluded": bool(program.assessment_included),
            "reassessmentIncluded": bool(program.reassessment_included),
        } if program else None),
        "plan": ({
            "id": plan.name, "code": plan.plan_code,
            "name": plan.plan_name, "deliveryType": plan.delivery_type,
            "assessmentMode": plan.assessment_mode,
        } if plan else None),
        "_membership": membership,
    }


def _public_self_assessment_context(context):
    if not context.get("allowed"):
        return context

    return {
        key: value
        for key, value in context.items()
        if key != "_membership"
    }


@frappe.whitelist(methods=["GET"])
def assessment_context(enrollment_id=None):
    member = _get_current_member()

    return _public_self_assessment_context(
        _resolve_self_assessment_context(
            member, enrollment_id=enrollment_id
        )
    )


@frappe.whitelist(methods=["POST"])
def start_self_assessment(
    safety_acknowledged=0,
    enrollment_id=None,
):
    member = _get_current_member()

    context = _resolve_self_assessment_context(
        member, enrollment_id=enrollment_id
    )

    if not context.get("allowed"):
        frappe.throw(
            context.get("reason")
            or "Self Assessment is not available.",
            frappe.ValidationError,
        )

    membership = context["_membership"]
    enrollment = membership["enrollment"] if membership else None
    program = membership["program"] if membership else None

    if not context.get("draftAssessmentId"):
        if not context.get("canStart"):
            if context.get("nextAssessmentDueOn"):
                frappe.throw(
                    "Your next self reassessment is due on "
                    f"{context['nextAssessmentDueOn']}.",
                    frappe.ValidationError,
                )

            frappe.throw(
                "A new self assessment is not available yet.",
                frappe.ValidationError,
            )

        if not frappe.utils.cint(
            safety_acknowledged
        ):
            frappe.throw(
                "Safety acknowledgement is required before starting the assessment.",
                frappe.ValidationError,
            )

    # Preserve the trainer's template and assessor for an enrollment-linked draft.
    if context.get("draftAssessmentId"):
        existing = assessment_service.assessment_detail(
            assessment_id=context["draftAssessmentId"],
            expected_member_id=member.name,
            expected_status="Draft",
        )["assessment"]
        return {"assessment": existing, "resumed": True}

    result = assessment_service.start_assessment(
        member_id=member.name,
        assessor=None,
        delivery_mode="Online",
        assessment_mode="Self Assessment",
        # The shared service selects the Standard template for all new
        # assessments. Mode remains Self Assessment for reporting.
        # Optional context only; a new assessment belongs to the member.
        enrollment_id=None,
        program_id=None,
        safety_acknowledged_on=(
            frappe.utils.now_datetime()
            if not context.get(
                "draftAssessmentId"
            )
            else None
        ),
    )

    return result


@frappe.whitelist(methods=["GET"])
def self_assessment_detail(assessment_id, enrollment_id=None):
    member = _get_current_member()

    context = _resolve_self_assessment_context(
        member, enrollment_id=enrollment_id
    )

    if not context.get("allowed"):
        frappe.throw(
            context.get("reason")
            or "Self Assessment is not available.",
            frappe.ValidationError,
        )

    return assessment_service.assessment_detail(
        assessment_id=assessment_id,
        expected_member_id=member.name,
        expected_status="Draft",
    )


@frappe.whitelist(methods=["POST"])
def save_self_assessment(
    assessment_id,
    inputs,
    enrollment_id=None,
    expected_modified=None,
):
    member = _get_current_member()

    context = _resolve_self_assessment_context(
        member, enrollment_id=enrollment_id
    )

    if not context.get("allowed"):
        frappe.throw(
            context.get("reason")
            or "Self Assessment is not available.",
            frappe.ValidationError,
        )

    result = assessment_service.save_assessment(
        assessment_id=assessment_id,
        inputs=inputs,
        status="Draft",
        expected_member_id=member.name,
        expected_current_status="Draft",
        expected_modified=expected_modified,
    )

    # Draft assessments may have an internally calculated partial score.
    # Do not expose it as a member-facing Fitness Score until completion.
    result["assessment"]["fitnessScore"] = None
    result["assessment"]["fitnessLevel"] = None

    return result


@frappe.whitelist(methods=["POST"])
def complete_self_assessment(
    assessment_id,
    inputs,
    enrollment_id=None,
    expected_modified=None,
):
    member = _get_current_member()

    context = _resolve_self_assessment_context(
        member, enrollment_id=enrollment_id
    )

    if not context.get("allowed"):
        frappe.throw(
            context.get("reason")
            or "Self Assessment is not available.",
            frappe.ValidationError,
        )

    result = assessment_service.save_assessment(
        assessment_id=assessment_id,
        inputs=inputs,
        status="Completed",
        expected_member_id=member.name,
        expected_current_status="Draft",
        expected_modified=expected_modified,
    )

    result["assessmentContext"] = (
        _public_self_assessment_context(
            _resolve_self_assessment_context(
                member, enrollment_id=enrollment_id
            )
        )
    )

    return result


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

    completed_filters = {
        "member": member.name,
        "status": "Completed",
    }

    if doc.enrollment:
        completed_filters["enrollment"] = (
            doc.enrollment
        )
    elif doc.program:
        completed_filters["program"] = (
            doc.program
        )

    completed = frappe.get_all(
        "Club100 Assessment",
        filters=completed_filters,
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

            "assessmentMode":
                doc.assessment_mode,

            "enrollmentId":
                doc.enrollment,

            "programId":
                doc.program,

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


# Member Schedule and Session Detail remain active PWA endpoints.
# They contain only cohort-targeted trainer-led sessions. Online Self
# workouts use api.self_workouts virtual occurrences and never a Session DocType.


@frappe.whitelist(methods=["GET"])
def schedule(enrollment_id=None):
    member = _get_current_member()
    today_date = frappe.utils.getdate(frappe.utils.today())

    enrollment_rows = frappe.get_all(
        "Club100 Enrollment",
        filters={"member": member.name, "status": ["!=", "Cancelled"]},
        fields=["name", "program", "plan", "cohort", "start_date", "end_date", "status"],
        order_by="start_date asc",
    )
    if enrollment_id:
        from club100_core.api.memberships import resolve_active_enrollment
        selected = resolve_active_enrollment(member.name, enrollment_id)
        enrollment_rows = [e for e in enrollment_rows if e.name == selected.name]

    cohort_ids = list({e.cohort for e in enrollment_rows if e.cohort})
    if not cohort_ids:
        return {"upcoming": [], "past": []}

    session_rows = frappe.get_all(
        "Club100 Session",
        filters={"cohort": ["in", cohort_ids]},
        fields=[
            "name", "cohort", "program", "trainer", "workout_format",
            "session_date", "start_time", "end_time", "delivery_mode",
            "meeting_provider", "meeting_url", "status", "notes",
        ],
        limit_page_length=500,
    )
    session_rows.sort(key=lambda r: (
        frappe.utils.getdate(r.session_date), str(r.start_time or "")
    ))
    program_ids = list({r.program for r in session_rows if r.program})
    trainer_ids = list({r.trainer for r in session_rows if r.trainer})

    program_names = {}
    if program_ids:
        program_names = {
            r.name: r.program_name or r.name
            for r in frappe.get_all(
                "Club100 Program", filters={"name": ["in", program_ids]},
                fields=["name", "program_name"],
            )
        }

    cohorts_by_id = {
        r.name: r for r in frappe.get_all(
            "Club100 Cohort", filters={"name": ["in", cohort_ids]},
            fields=["name", "cohort_name", "fitness_level"],
        )
    }
    trainer_names = {}
    if trainer_ids:
        trainer_names = {
            r.name: r.trainer_name or r.name
            for r in frappe.get_all(
                "Club100 Trainer", filters={"name": ["in", trainer_ids]},
                fields=["name", "trainer_name"],
            )
        }
    attendance_by_session = {
        r.session: r for r in frappe.get_all(
            "Club100 Attendance", filters={"member": member.name},
            fields=["session", "attendance_status", "minutes_attended", "notes"],
        )
    }

    def enrolled_for_session(row):
        date = frappe.utils.getdate(row.session_date)
        return any(
            e.cohort == row.cohort
            and (not e.start_date or frappe.utils.getdate(e.start_date) <= date)
            and (not e.end_date or frappe.utils.getdate(e.end_date) >= date)
            for e in enrollment_rows
        )

    upcoming, past = [], []
    for row in session_rows:
        if not enrolled_for_session(row):
            continue
        cohort = cohorts_by_id.get(row.cohort)
        attendance = attendance_by_session.get(row.name)
        item = {
            "id": row.name,
            "sessionType": "Trainer Led",
            "program": {
                "id": row.program,
                "name": program_names.get(row.program) or row.program,
            },
            "plan": None,
            "cohort": {
                "id": row.cohort,
                "name": cohort.cohort_name if cohort else row.cohort,
                "fitnessLevel": cohort.fitness_level if cohort else None,
            },
            "trainer": {
                "id": row.trainer,
                "name": trainer_names.get(row.trainer) or row.trainer,
            } if row.trainer else None,
            "workoutFormat": row.workout_format,
            "workout": None,
            "contentAccess": None,
            "sessionDate": row.session_date,
            "startTime": row.start_time,
            "endTime": row.end_time,
            "deliveryMode": row.delivery_mode,
            "status": row.status,
            "meetingProvider": row.meeting_provider,
            # Preserve current schedule API behavior; detail API gates joining until Live.
            "meetingUrl": row.meeting_url,
            "notes": row.notes,
            "attendance": {
                "status": attendance.attendance_status,
                "minutesAttended": attendance.minutes_attended,
                "notes": attendance.notes,
            } if attendance else None,
        }
        if (
            frappe.utils.getdate(row.session_date) >= today_date
            and row.status not in ("Completed", "Cancelled")
        ):
            upcoming.append(item)
        else:
            past.append(item)

    past.sort(
        key=lambda i: (i["sessionDate"], str(i["startTime"] or "")),
        reverse=True,
    )
    return {"upcoming": upcoming, "past": past}


@frappe.whitelist(methods=["GET"])
def session_detail(session_id, enrollment_id=None):
    member = _get_current_member()
    session_id = (session_id or "").strip()
    if not session_id:
        frappe.throw("Session ID is required.", frappe.ValidationError)
    if not frappe.db.exists("Club100 Session", session_id):
        frappe.throw("Session not found.", frappe.DoesNotExistError)

    session = frappe.get_doc("Club100 Session", session_id)
    if not session.cohort:
        # Virtual Self workouts are resolved by api.self_workouts, not here.
        frappe.throw("This session has no valid cohort target.", frappe.ValidationError)

    enrollment_filters = {
        "member": member.name,
        "status": ["!=", "Cancelled"],
        "cohort": session.cohort,
    }
    if enrollment_id:
        from club100_core.api.memberships import resolve_active_enrollment
        selected = resolve_active_enrollment(member.name, enrollment_id)
        enrollment_filters["name"] = selected.name

    enrollment_rows = frappe.get_all(
        "Club100 Enrollment", filters=enrollment_filters,
        fields=["name", "program", "plan", "cohort", "start_date", "end_date", "status"],
    )
    session_date = frappe.utils.getdate(session.session_date)
    valid_enrollment = next((
        e for e in enrollment_rows
        if (not e.start_date or frappe.utils.getdate(e.start_date) <= session_date)
        and (not e.end_date or frappe.utils.getdate(e.end_date) >= session_date)
    ), None)
    if not valid_enrollment:
        frappe.throw("You do not have access to this session.", frappe.PermissionError)

    program = frappe.db.get_value(
        "Club100 Program", session.program,
        ["name", "program_name", "description", "session_duration_minutes"],
        as_dict=True,
    )
    cohort = frappe.db.get_value(
        "Club100 Cohort", session.cohort,
        ["name", "cohort_name", "fitness_level", "delivery_mode"],
        as_dict=True,
    )
    trainer = None
    if session.trainer:
        trainer = frappe.db.get_value(
            "Club100 Trainer", session.trainer,
            ["name", "trainer_name", "profile_photo", "bio"], as_dict=True,
        )

    attendance = frappe.db.get_value(
        "Club100 Attendance", {"session": session.name, "member": member.name},
        ["attendance_status", "attendance_mode", "minutes_attended", "notes"],
        as_dict=True,
    )
    feedback_id = frappe.db.get_value(
        "Club100 Feedback",
        {"member": member.name, "session": session.name, "status": "Submitted"},
        "name",
    )
    from club100_core.api.workout_formats import resolve_session_preparation
    return {
        "session": {
            "id": session.name,
            "sessionType": "Trainer Led",
            "status": session.status,
            "sessionDate": session.session_date,
            "startTime": session.start_time,
            "endTime": session.end_time,
            "deliveryMode": session.delivery_mode,
            "meetingProvider": session.meeting_provider,
            "meetingUrl": session.meeting_url if session.status == "Live" else None,
            "notes": session.notes,
            "preparation": resolve_session_preparation(session),
            "contentAccess": None,
            "program": {
                "id": program.name if program else session.program,
                "name": program.program_name if program else session.program,
                "description": program.description if program else None,
                "sessionDurationMinutes": program.session_duration_minutes if program else None,
            },
            "plan": None,
            "cohort": {
                "id": cohort.name if cohort else session.cohort,
                "name": cohort.cohort_name if cohort else session.cohort,
                "fitnessLevel": cohort.fitness_level if cohort else None,
            },
            "trainer": {
                "id": trainer.name,
                "name": trainer.trainer_name,
                "photo": trainer.profile_photo,
                "bio": trainer.bio,
            } if trainer else None,
            "workoutContent": None,
            "attendance": {
                "status": attendance.attendance_status,
                "mode": attendance.attendance_mode,
                "minutesAttended": attendance.minutes_attended,
                "notes": attendance.notes,
            } if attendance else None,
            "feedback": {"submitted": bool(feedback_id), "id": feedback_id},
        }
    }
