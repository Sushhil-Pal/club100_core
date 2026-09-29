import frappe


def _get_current_member():
    user = frappe.session.user

    if user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.PermissionError,
        )

    member = frappe.db.get_value(
        "Club100 Member",
        {"user": user},
        ["name"],
        as_dict=True,
    )

    if not member:
        frappe.throw("Club100 member profile not found")

    return member


@frappe.whitelist()
def progress_summary():
    member = _get_current_member()

    assessments = frappe.get_all(
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
        ],
        order_by="assessment_date asc, creation asc",
    )

    if not assessments:
        return {
            "fitnessScore": {
                "baseline": 0,
                "current": 0,
                "change": 0,
            },
            "hasReassessment": False,
            "categoryScores": [],
            "assessments": [],
        }

    # ---------------------------------------------------------
    # Find baseline assessment
    # ---------------------------------------------------------

    baseline_assessment = next(
        (
            assessment
            for assessment in assessments
            if assessment.assessment_type == "Baseline"
        ),
        assessments[0],
    )

    # ---------------------------------------------------------
    # Check whether at least one reassessment exists
    # ---------------------------------------------------------

    reassessments = [
        assessment
        for assessment in assessments
        if assessment.assessment_type == "Reassessment"
    ]

    has_reassessment = len(reassessments) > 0

    # If reassessment exists, use the latest reassessment.
    # Otherwise baseline itself is the current assessment.
    if has_reassessment:
        current_assessment = reassessments[-1]
    else:
        current_assessment = baseline_assessment

    # ---------------------------------------------------------
    # Fitness score
    # ---------------------------------------------------------

    baseline_score = baseline_assessment.fitness_score or 0
    current_score = current_assessment.fitness_score or 0

    # ---------------------------------------------------------
    # Category scores
    # ---------------------------------------------------------

    baseline_categories = _get_category_scores(
        baseline_assessment.name
    )

    current_categories = _get_category_scores(
        current_assessment.name
    )

    all_categories = sorted(
        set(baseline_categories.keys())
        | set(current_categories.keys())
    )

    category_scores = []

    for category in all_categories:
        category_scores.append(
            {
                "label": category,
                "baseline": baseline_categories.get(
                    category,
                    0,
                ),
                "current": current_categories.get(
                    category,
                    baseline_categories.get(category, 0),
                ),
            }
        )

    # ---------------------------------------------------------
    # Assessment history
    # Latest assessment first
    # ---------------------------------------------------------

    assessment_history = []

    for assessment in reversed(assessments):
        assessment_history.append(
            {
                "id": assessment.name,
                "date": (
                    str(assessment.assessment_date)
                    if assessment.assessment_date
                    else None
                ),
                "type": assessment.assessment_type,
                "score": assessment.fitness_score or 0,
                "fitnessLevel": assessment.fitness_level,
            }
        )

    return {
        "fitnessScore": {
            "baseline": baseline_score,
            "current": current_score,
            "change": current_score - baseline_score,
        },
        "hasReassessment": has_reassessment,
        "categoryScores": category_scores,
        "assessments": assessment_history,
    }


def _get_category_scores(assessment_name):
    assessment = frappe.get_doc(
        "Club100 Assessment",
        assessment_name,
    )

    category_values = {}

    for metric in assessment.metrics:
        if not metric.category:
            continue

        if metric.score is None:
            continue

        category_values.setdefault(
            metric.category,
            [],
        )

        category_values[metric.category].append(
            float(metric.score)
        )

    category_scores = {}

    for category, scores in category_values.items():
        if not scores:
            continue

        category_scores[category] = round(
            sum(scores) / len(scores)
        )

    return category_scores