import frappe


SELF_TEMPLATE_CODE = "SELF"
STANDARD_TEMPLATE_CODE = "STANDARD"

SELF_TEMPLATE_METRICS = [
    {
        "metric": "BMI",
        "sequence": 1,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Waist",
        "sequence": 2,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.40,
    },
    {
        "metric": "Resting Heart Rate",
        "sequence": 3,
        "required": 0,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Blood Pressure",
        "sequence": 4,
        "required": 0,
        "include_in_score": 1,
        "weight": 0.30,
    },
    {
        "metric": "Chair Stand",
        "sequence": 5,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Wall Sit",
        "sequence": 6,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Pushups",
        "sequence": 7,
        "required": 0,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Plank Hold",
        "sequence": 8,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Sit & Reach",
        "sequence": 9,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Shoulder Flexibility",
        "sequence": 10,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Deep Squat",
        "sequence": 11,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Single Leg Balance",
        "sequence": 12,
        "required": 1,
        "include_in_score": 1,
        "weight": 0.25,
    },
    {
        "metric": "Self-Assessment",
        "sequence": 13,
        "required": 1,
        "include_in_score": 1,
        "weight": 1.0,
    },
]


def seed_self_assessment_configuration():
    """
    Create/update the assessment-mode master data for Self Assessment V1.

    Safe to run multiple times.

    Important: the Self template is NOT marked as the global default yet.
    The current trainer flow still selects the existing default template.
    Member Self Assessment APIs will select the SELF template explicitly in
    the later implementation stage.
    """

    # Existing assessments/templates predate assessment_mode and are
    # trainer-led by definition. Backfill only blank values.
    frappe.db.sql(
        """
        UPDATE `tabClub100 Assessment`
        SET assessment_mode = 'Trainer Led'
        WHERE COALESCE(assessment_mode, '') = ''
        """
    )

    frappe.db.sql(
        """
        UPDATE `tabClub100 Assessment Template`
        SET assessment_mode = 'Trainer Led'
        WHERE COALESCE(assessment_mode, '') = ''
        """
    )

    standard_name = frappe.db.get_value(
        "Club100 Assessment Template",
        {"template_code": STANDARD_TEMPLATE_CODE},
        "name",
    )

    if not standard_name:
        frappe.throw(
            "Club100 Standard Assessment template was not found."
        )

    standard = frappe.get_doc(
        "Club100 Assessment Template",
        standard_name,
    )
    standard.assessment_mode = "Trainer Led"
    standard.save(ignore_permissions=True)

    for config in SELF_TEMPLATE_METRICS:
        if not frappe.db.exists(
            "Club100 Fitness Metric",
            config["metric"],
        ):
            frappe.throw(
                f"Fitness Metric '{config['metric']}' was not found."
            )

    self_name = frappe.db.get_value(
        "Club100 Assessment Template",
        {"template_code": SELF_TEMPLATE_CODE},
        "name",
    )

    if self_name:
        doc = frappe.get_doc(
            "Club100 Assessment Template",
            self_name,
        )
        action = "updated"
    else:
        doc = frappe.new_doc(
            "Club100 Assessment Template"
        )
        action = "created"

    doc.template_name = "Club100 Self Assessment"
    doc.template_code = SELF_TEMPLATE_CODE
    doc.version = 1
    doc.delivery_mode = "Online"
    doc.assessment_mode = "Self Assessment"
    doc.purpose = "Both"
    doc.is_default = 0
    doc.active = 1
    doc.description = (
        "Guided member self-assessment for Club100 Self Plan. "
        "Uses the canonical Club100 fitness metrics and scoring engine."
    )

    doc.set("metrics", [])

    for config in SELF_TEMPLATE_METRICS:
        doc.append(
            "metrics",
            config,
        )

    doc.set("category_weights", [])

    for row in standard.category_weights:
        doc.append(
            "category_weights",
            {
                "category": row.category,
                "weight": row.weight,
            },
        )

    doc.save(ignore_permissions=True)

    frappe.db.commit()

    return {
        "standard_template": standard.name,
        "standard_assessment_mode": standard.assessment_mode,
        "self_template": doc.name,
        "self_template_action": action,
        "self_template_is_default": int(doc.is_default or 0),
        "self_template_metric_count": len(doc.metrics),
    }
