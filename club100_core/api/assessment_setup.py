import frappe


SELF_TEMPLATE_CODE = "SELF"
STANDARD_TEMPLATE_CODE = "STANDARD"



SELF_ASSESSMENT_INPUT_GUIDANCE = {
    "HEIGHT": (
        "Remove footwear. Stand upright against a flat wall with heels together "
        "and look straight ahead. Measure from the floor to the top of the head "
        "and enter the result in centimetres."
    ),
    "WEIGHT": (
        "Use a stable weighing scale on a hard, level surface. Remove footwear "
        "and heavy outer clothing. Stand still in the centre of the scale and "
        "enter the reading in kilograms."
    ),
    "WAIST": (
        "Stand relaxed with feet about hip-width apart. Place the tape horizontally "
        "around the waist midway between the lowest rib and the top of the hip bone. "
        "Measure after a normal exhale without pulling the tape tight into the skin."
    ),
    "RESTING_HR": (
        "Optional. Sit quietly for at least 5 minutes. Measure your heart rate with "
        "a wearable/monitor, or count your pulse for a full 60 seconds. Enter beats "
        "per minute."
    ),
    "BP_SYS": (
        "Optional. Rest seated for at least 5 minutes with back supported, feet flat "
        "and the cuffed arm supported near heart level. Enter the upper (systolic) "
        "number from the blood-pressure reading."
    ),
    "BP_DIA": (
        "Optional. Use the same seated blood-pressure reading and enter the lower "
        "(diastolic) number."
    ),
    "CHAIR_STAND": (
        "Use a stable chair without wheels and place it against a wall. Sit near the "
        "middle of the seat with feet flat and arms crossed over the chest. On start, "
        "stand fully upright and sit back down with control. Count each full stand "
        "completed in 30 seconds."
    ),
    "WALL_SIT": (
        "Place your back flat against a wall with feet forward. Slide down until your "
        "knees are comfortably near a right angle while keeping your back supported. "
        "Start the timer once stable. Stop when you stand up, lose the position, or "
        "need to stop because of discomfort. Record the hold time in seconds."
    ),
    "PUSHUP_TYPE": (
        "Optional. Select Full if you performed the test from the toes, or Knee if "
        "your knees stayed supported on the floor."
    ),
    "PUSHUP_REPS": (
        "Optional. Perform controlled consecutive push-ups using the selected variation. "
        "Lower with control and press back to the start position. Count only repetitions "
        "completed with consistent form; stop when form breaks or you need to rest."
    ),
    "PLANK_HOLD": (
        "Use a forearm plank with elbows below the shoulders and body in a straight "
        "line from shoulders to heels. Start the timer once stable. Stop when your hips "
        "drop/rise noticeably, you lose the position, or you need to stop because of "
        "discomfort. Record the time in seconds."
    ),
    "SIT_REACH": (
        "Sit with legs straight and feet against a firm box or wall. Place a ruler or "
        "tape so 0 cm is at the toe line. With both hands together, reach forward slowly "
        "without bouncing. Record centimetres beyond the toes as positive; if you do not "
        "reach the toes, record the shortfall as a negative value."
    ),
    "SHOULDER_LEFT": (
        "Place the left hand over the left shoulder and reach down the back. Reach the "
        "right hand up the back from below. Measure the fingertip distance: enter a "
        "positive value if the fingers overlap, 0 if they just touch, or a negative value "
        "for the gap between them."
    ),
    "SHOULDER_RIGHT": (
        "Place the right hand over the right shoulder and reach down the back. Reach the "
        "left hand up the back from below. Measure the fingertip distance: enter a "
        "positive value if the fingers overlap, 0 if they just touch, or a negative value "
        "for the gap between them."
    ),
    "DEEP_SQUAT": (
        "Stand with feet about shoulder-width apart and squat as low as you can with "
        "control, keeping the heels on the floor and avoiding pain. Choose the score "
        "that best matches your best controlled attempt:\n"
        "5 — Hips below knee level, heels down, knees aligned with the feet, good control.\n"
        "4 — Reaches knee level or lower with only a small alignment or balance issue.\n"
        "3 — Reaches about parallel with a clear but manageable mobility/control limitation.\n"
        "2 — Stops above parallel because of limited mobility or balance.\n"
        "1 — Very shallow squat or requires external support.\n"
        "0 — Unable to perform safely or stopped because of pain."
    ),
    "BALANCE_LEFT": (
        "Stand near a wall or sturdy chair for safety without holding it. Place hands on "
        "hips and lift the right foot so you are balancing on the left leg. Start the "
        "timer when the foot leaves the floor. Stop if the lifted foot touches down, you "
        "use support, or 60 seconds is reached."
    ),
    "BALANCE_RIGHT": (
        "Stand near a wall or sturdy chair for safety without holding it. Place hands on "
        "hips and lift the left foot so you are balancing on the right leg. Start the "
        "timer when the foot leaves the floor. Stop if the lifted foot touches down, you "
        "use support, or 60 seconds is reached."
    ),
    "ENERGY_LEVEL": "Rate 1 for extremely low energy and 10 for very high energy.",
    "STRESS_LEVEL": "Rate 1 for very low stress and 10 for extremely high stress.",
    "SLEEP_QUALITY": "Rate 1 for very poor sleep quality and 10 for excellent sleep quality.",
    "DAILY_MOBILITY": "Rate 1 if daily movement feels very limited and 10 if you move freely and comfortably.",
    "FITNESS_CONFIDENCE": "Rate 1 for no confidence in your current fitness and 10 for very high confidence.",
    "MOTIVATION": "Rate 1 for very low motivation and 10 for very high motivation to exercise consistently.",
    "OVERALL_WELLBEING": "Rate 1 for very poor overall well-being and 10 for excellent overall well-being.",
}


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

    guidance_updated = 0

    for input_code, instructions in (
        SELF_ASSESSMENT_INPUT_GUIDANCE.items()
    ):
        input_name = frappe.db.get_value(
            "Club100 Assessment Input",
            {"input_code": input_code},
            "name",
        )

        if not input_name:
            frappe.throw(
                f"Assessment Input '{input_code}' was not found."
            )

        frappe.db.set_value(
            "Club100 Assessment Input",
            input_name,
            "instructions",
            instructions,
        )
        guidance_updated += 1

    frappe.db.commit()

    return {
        "standard_template": standard.name,
        "standard_assessment_mode": standard.assessment_mode,
        "self_template": doc.name,
        "self_template_action": action,
        "self_template_is_default": int(doc.is_default or 0),
        "self_template_metric_count": len(doc.metrics),
        "input_guidance_updated": guidance_updated,
    }
