import frappe

from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

from club100_core.api.scoring import score_metric


class Club100Assessment(Document):

    # =========================================================
    # VALIDATION LIFECYCLE
    # =========================================================

    def validate(self):
        self.validate_template()

        # Generate snapshots only on initial setup.
        self.populate_template_metrics()
        self.populate_template_inputs()

        # A Draft assessment may be incomplete.
        # Completed assessments must contain required inputs.
        self.validate_required_inputs()

        # Raw trainer inputs -> calculated metric values.
        self.calculate_metric_values()

        # Metric values -> scores.
        self.score_metrics()

        # Metric scores -> categories -> overall fitness score.
        self.calculate_category_scores()

    # =========================================================
    # TEMPLATE VALIDATION
    # =========================================================

    def validate_template(self):
        if not self.template:
            frappe.throw("Assessment Template is required.")

        template = frappe.get_doc(
            "Club100 Assessment Template",
            self.template,
        )

        if not template.active:
            frappe.throw(
                f"Assessment Template "
                f"'{template.template_name}' is inactive."
            )

        # Once an assessment exists, its methodology/template
        # must remain immutable.
        if not self.is_new():
            old_doc = self.get_doc_before_save()

            if (
                old_doc
                and old_doc.template
                and old_doc.template != self.template
            ):
                frappe.throw(
                    "Assessment Template cannot be changed "
                    "after the assessment has been created."
                )

    # =========================================================
    # GENERATE METRIC SNAPSHOTS
    # =========================================================

    def populate_template_metrics(self):
        """
        Generate assessment metric rows from the selected template.

        Existing rows are never regenerated. This protects historical
        assessments if master data changes later.
        """

        if not self.template:
            return

        if self.metrics:
            return

        template = frappe.get_doc(
            "Club100 Assessment Template",
            self.template,
        )

        self.template_version = template.version or 1

        template_items = sorted(
            template.metrics,
            key=lambda row: row.sequence or row.idx,
        )

        for item in template_items:
            if not item.metric:
                continue

            metric = frappe.get_doc(
                "Club100 Fitness Metric",
                item.metric,
            )

            if not metric.active:
                frappe.throw(
                    f"Metric '{metric.metric_name}' is inactive."
                )

            instructions = (
                item.instructions_override
                or metric.instructions
                or ""
            )

            self.append(
                "metrics",
                {
                    "metric": metric.name,
                    "metric_name": metric.metric_name,
                    "category": metric.category,
                    "unit": metric.unit,
                    "required": item.required,
                    "include_in_score": item.include_in_score,
                    "weight": item.weight,
                    "instructions": instructions,
                },
            )

    # =========================================================
    # GENERATE TRAINER INPUT SNAPSHOTS
    # =========================================================

    def populate_template_inputs(self):
        """
        Generate trainer-facing input rows from all Fitness Metrics
        included in the selected Assessment Template.

        Inputs are deduplicated.

        An input becomes required when at least one required metric
        depends on it and the metric-input mapping marks it as
        required for calculation.
        """

        if not self.template:
            return

        # Never regenerate historical/input rows.
        if self.inputs:
            return

        template = frappe.get_doc(
            "Club100 Assessment Template",
            self.template,
        )

        input_map = {}

        template_items = sorted(
            template.metrics,
            key=lambda row: row.sequence or row.idx,
        )

        for template_item in template_items:
            if not template_item.metric:
                continue

            metric = frappe.get_doc(
                "Club100 Fitness Metric",
                template_item.metric,
            )

            if not metric.active:
                frappe.throw(
                    f"Metric '{metric.metric_name}' is inactive."
                )

            metric_inputs = sorted(
                metric.inputs,
                key=lambda row: row.sequence or row.idx,
            )

            for metric_input in metric_inputs:
                if not metric_input.input:
                    continue

                input_name = metric_input.input

                is_required = bool(
                    template_item.required
                    and metric_input.required_for_calculation
                )

                if input_name not in input_map:
                    input_map[input_name] = {
                        "required": is_required,
                    }
                else:
                    input_map[input_name]["required"] = (
                        input_map[input_name]["required"]
                        or is_required
                    )

        for input_name, input_info in input_map.items():
            input_doc = frappe.get_doc(
                "Club100 Assessment Input",
                input_name,
            )

            if not input_doc.active:
                frappe.throw(
                    f"Assessment Input "
                    f"'{input_doc.input_name}' is inactive."
                )

            self.append(
                "inputs",
                {
                    "input": input_doc.name,
                    "input_name": input_doc.input_name,
                    "category": input_doc.category,
                    "result_type": input_doc.result_type,
                    "unit": input_doc.unit,
                    "required": input_info["required"],
                },
            )

    # =========================================================
    # REQUIRED INPUT VALIDATION
    # =========================================================

    def validate_required_inputs(self):
        """
        Draft assessments may be partially completed.

        Required inputs become mandatory only when the assessment
        is being marked Completed.

        is_entered is the source of truth because numeric fields
        are stored as 0 even when the trainer has not entered them.
        """

        if self.status != "Completed":
            return

        missing = []

        for row in self.inputs:
            if not row.required:
                continue

            if not row.is_entered:
                missing.append(row.input_name or row.input)

        if missing:
            frappe.throw(
                "The following required assessment inputs "
                "must be completed before marking the "
                "assessment as Completed:<br><br>"
                + "<br>".join(f"• {name}" for name in missing)
            )

    # =========================================================
    # MEMBER SCORING CONTEXT
    # =========================================================

    def get_member_scoring_context(self):
        if not self.member:
            return {
                "gender": None,
                "age": None,
            }

        member = frappe.db.get_value(
            "Club100 Member",
            self.member,
            [
                "gender",
                "date_of_birth",
            ],
            as_dict=True,
        )

        if not member:
            frappe.throw(
                "Club100 Member could not be found."
            )

        return {
            "gender": member.gender,
            "age": self.calculate_age(
                member.date_of_birth
            ),
        }

    def calculate_age(self, date_of_birth):
        if not date_of_birth:
            return None

        dob = getdate(date_of_birth)

        reference_date = getdate(
            self.assessment_date
            or nowdate()
        )

        return (
            reference_date.year
            - dob.year
            - (
                (
                    reference_date.month,
                    reference_date.day,
                )
                <
                (
                    dob.month,
                    dob.day,
                )
            )
        )

    # =========================================================
    # INPUT LOOKUP
    # =========================================================

    def _build_input_values(self):
        """
        Build a lookup keyed by Assessment Input code.

        is_entered is carried with each value so that an untouched
        numeric field stored as 0 is not mistaken for genuine input.
        """

        result = {}

        for row in self.inputs:
            if not row.input:
                continue

            input_doc = frappe.db.get_value(
                "Club100 Assessment Input",
                row.input,
                [
                    "input_code",
                    "input_name",
                ],
                as_dict=True,
            )

            if not input_doc:
                continue

            code = (
                input_doc.input_code
                or input_doc.input_name
                or row.input
            )

            result[code] = {
                "value": row.value,
                "text_value": row.text_value,
                "name": input_doc.input_name,
                "is_entered": bool(
                    row.is_entered
                ),
            }

        return result

    def _get_numeric_input_value(
        self,
        input_values,
        input_code,
    ):
        data = input_values.get(
            input_code
        )

        if not data:
            return None

        if not data.get(
            "is_entered"
        ):
            return None

        value = data.get(
            "value"
        )

        if value in (
            None,
            "",
        ):
            return None

        return flt(value)

    def _get_text_input_value(
        self,
        input_values,
        input_code,
    ):
        data = input_values.get(
            input_code
        )

        if not data:
            return None

        if not data.get(
            "is_entered"
        ):
            return None

        value = data.get(
            "text_value"
        )

        if value in (
            None,
            "",
        ):
            return None

        return str(
            value
        ).strip()

    # =========================================================
    # CALCULATE METRIC VALUES
    # =========================================================

    def calculate_metric_values(self):
        """
        Convert trainer-entered raw inputs into Fitness Metric
        result values.

        Scoring is intentionally kept separate in score_metrics().
        """

        if not self.metrics:
            return

        input_values = self._build_input_values()

        for metric_row in self.metrics:
            if not metric_row.metric:
                continue

            metric = frappe.get_doc(
                "Club100 Fitness Metric",
                metric_row.metric,
            )

            method = (
                metric.calculation_method
                or "DIRECT"
            )

            # Always recalculate derived fields.
            metric_row.value = None
            metric_row.text_value = None
            metric_row.score = None
            metric_row.rating = None

            if method == "DIRECT":
                self._calculate_direct_metric(
                    metric,
                    metric_row,
                    input_values,
                )

            elif method == "BMI":
                self._calculate_bmi_metric(
                    metric_row,
                    input_values,
                )

            elif method == "HEART_RATE_RECOVERY":
                self._calculate_heart_rate_recovery(
                    metric_row,
                    input_values,
                )

            elif method == "BILATERAL_70_30":
                self._calculate_bilateral_display(
                    metric,
                    metric_row,
                    input_values,
                )

            elif method == "BLOOD_PRESSURE":
                self._calculate_blood_pressure_display(
                    metric_row,
                    input_values,
                )

            elif method == "SELF_ASSESSMENT":
                self._calculate_self_assessment_value(
                    metric_row,
                    input_values,
                )

            elif method == "PUSHUP_EFFECTIVE_REPS":
                self._calculate_pushups(
                    metric_row,
                    input_values,
                )

            else:
                frappe.throw(
                    f"Unsupported calculation method "
                    f"'{method}' for metric "
                    f"'{metric.metric_name}'."
                )

    # =========================================================
    # DIRECT
    # =========================================================

    def _calculate_direct_metric(
        self,
        metric,
        metric_row,
        input_values,
    ):
        if not metric.inputs:
            return

        ordered_inputs = sorted(
            metric.inputs,
            key=lambda row: row.sequence or row.idx,
        )

        input_doc = frappe.db.get_value(
            "Club100 Assessment Input",
            ordered_inputs[0].input,
            "input_code",
        )

        if not input_doc:
            return

        metric_row.value = (
            self._get_numeric_input_value(
                input_values,
                input_doc,
            )
        )

    # =========================================================
    # BMI
    # =========================================================

    def _calculate_bmi_metric(
        self,
        metric_row,
        input_values,
    ):
        height = self._get_numeric_input_value(
            input_values,
            "HEIGHT",
        )

        weight = self._get_numeric_input_value(
            input_values,
            "WEIGHT",
        )

        if (
            height is None
            or weight is None
            or height <= 0
        ):
            return

        height_m = height / 100

        bmi = weight / (
            height_m * height_m
        )

        metric_row.value = round(
            bmi,
            1,
        )

    # =========================================================
    # HEART RATE RECOVERY
    # =========================================================

    def _calculate_heart_rate_recovery(
        self,
        metric_row,
        input_values,
    ):
        workout_hr = (
            self._get_numeric_input_value(
                input_values,
                "WORKOUT_HR",
            )
        )

        recovery_hr = (
            self._get_numeric_input_value(
                input_values,
                "RECOVERY_HR",
            )
        )

        if (
            workout_hr is None
            or recovery_hr is None
        ):
            return

        metric_row.value = round(
            workout_hr - recovery_hr,
            1,
        )

    # =========================================================
    # BILATERAL DISPLAY
    # =========================================================

    def _calculate_bilateral_display(
        self,
        metric,
        metric_row,
        input_values,
    ):
        """
        Do NOT combine raw measurements here.

        The Club100 bilateral rule is:

            score each side independently
            70% weaker SIDE SCORE
            30% stronger SIDE SCORE

        That calculation happens later in score_metrics().
        """

        ordered_inputs = sorted(
            metric.inputs,
            key=lambda row: row.sequence or row.idx,
        )

        if len(ordered_inputs) < 2:
            return

        first_input = frappe.db.get_value(
            "Club100 Assessment Input",
            ordered_inputs[0].input,
            [
                "input_code",
                "input_name",
            ],
            as_dict=True,
        )

        second_input = frappe.db.get_value(
            "Club100 Assessment Input",
            ordered_inputs[1].input,
            [
                "input_code",
                "input_name",
            ],
            as_dict=True,
        )

        if not first_input or not second_input:
            return

        first_value = self._get_numeric_input_value(
            input_values,
            first_input.input_code,
        )

        second_value = self._get_numeric_input_value(
            input_values,
            second_input.input_code,
        )

        if (
            first_value is None
            or second_value is None
        ):
            return

        metric_row.text_value = (
            f"{first_input.input_name}: {first_value:g} / "
            f"{second_input.input_name}: {second_value:g}"
        )

        # No artificial combined raw value.
        metric_row.value = None

    # =========================================================
    # PUSHUPS
    # =========================================================

    def _calculate_pushups(
        self,
        metric_row,
        input_values,
    ):
        pushup_type = (
            self._get_text_input_value(
                input_values,
                "PUSHUP_TYPE",
            )
        )

        reps = self._get_numeric_input_value(
            input_values,
            "PUSHUP_REPS",
        )

        if (
            not pushup_type
            or reps is None
        ):
            return

        pushup_type_lower = (
            pushup_type.strip().lower()
        )

        if pushup_type_lower == "full":
            multiplier = 1.0

        elif pushup_type_lower == "knee":
            multiplier = 0.6

        else:
            frappe.throw(
                "Push-Up Type must be either "
                "'Full' or 'Knee'."
            )

        effective_reps = (
            reps * multiplier
        )

        metric_row.value = round(
            effective_reps,
            1,
        )

        metric_row.text_value = (
            f"{reps:g} ({pushup_type})"
        )

    # =========================================================
    # BLOOD PRESSURE DISPLAY
    # =========================================================

    def _calculate_blood_pressure_display(
        self,
        metric_row,
        input_values,
    ):
        systolic = (
            self._get_numeric_input_value(
                input_values,
                "BP_SYS",
            )
        )

        diastolic = (
            self._get_numeric_input_value(
                input_values,
                "BP_DIA",
            )
        )

        if (
            systolic is None
            or diastolic is None
        ):
            return

        metric_row.text_value = (
            f"{systolic:g}/{diastolic:g}"
        )

        # BP has two raw components, so there is intentionally
        # no single numeric raw value.
        metric_row.value = None

    # =========================================================
    # SELF ASSESSMENT
    # =========================================================

    def _calculate_self_assessment_value(
        self,
        metric_row,
        input_values,
    ):
        energy = self._get_numeric_input_value(
            input_values,
            "ENERGY_LEVEL",
        )

        stress = self._get_numeric_input_value(
            input_values,
            "STRESS_LEVEL",
        )

        sleep = self._get_numeric_input_value(
            input_values,
            "SLEEP_QUALITY",
        )

        mobility = self._get_numeric_input_value(
            input_values,
            "DAILY_MOBILITY",
        )

        confidence = self._get_numeric_input_value(
            input_values,
            "FITNESS_CONFIDENCE",
        )

        motivation = self._get_numeric_input_value(
            input_values,
            "MOTIVATION",
        )

        wellbeing = self._get_numeric_input_value(
            input_values,
            "OVERALL_WELLBEING",
        )

        values = [
            energy,
            stress,
            sleep,
            mobility,
            confidence,
            motivation,
            wellbeing,
        ]

        if any(
            value is None
            for value in values
        ):
            return

        score = (
            energy * 10
            + (10 - stress) * 10
            + sleep * 10
            + mobility * 10
            + confidence * 10
            + motivation * 10
            + wellbeing * 10
        ) / 7

        score = max(
            0,
            min(
                100,
                score,
            ),
        )

        metric_row.value = round(
            score,
            1,
        )

    # =========================================================
    # SCORE METRICS
    # =========================================================

    def score_metrics(self):
        context = (
            self.get_member_scoring_context()
        )

        gender = context.get("gender")
        age = context.get("age")

        input_values = self._build_input_values()

        for row in self.metrics:
            if not row.metric:
                continue

            metric = frappe.get_doc(
                "Club100 Fitness Metric",
                row.metric,
            )

            method = (
                metric.calculation_method
                or "DIRECT"
            )

            # ---------------------------------------------
            # Bilateral
            # ---------------------------------------------

            if method == "BILATERAL_70_30":
                result = (
                    self._score_bilateral_metric(
                        metric,
                        input_values,
                        gender,
                        age,
                    )
                )

                if result:
                    row.score = result["score"]
                    row.rating = result["rating"]

                continue

            # ---------------------------------------------
            # Blood Pressure
            # ---------------------------------------------

            if method == "BLOOD_PRESSURE":
                result = (
                    self._score_blood_pressure(
                        input_values,
                        gender,
                        age,
                    )
                )

                if result:
                    row.score = result["score"]
                    row.rating = result["rating"]

                continue

            # ---------------------------------------------
            # Self Assessment
            # ---------------------------------------------

            if method == "SELF_ASSESSMENT":
                if row.value is None:
                    continue

                row.score = round(
                    flt(row.value),
                    0,
                )

                row.rating = (
                    self._rating_from_score(
                        row.score
                    )
                )

                continue

            # ---------------------------------------------
            # Standard numeric metrics
            # ---------------------------------------------

            if row.value is None:
                continue

            result = score_metric(
                row.metric,
                row.value,
                gender=gender,
                age=age,
            )

            if result:
                row.score = result["score"]
                row.rating = result["rating"]

    # =========================================================
    # BILATERAL SCORING
    # =========================================================

    def _score_bilateral_metric(
        self,
        metric,
        input_values,
        gender,
        age,
    ):
        ordered_inputs = sorted(
            metric.inputs,
            key=lambda row: row.sequence or row.idx,
        )

        if len(ordered_inputs) < 2:
            return None

        first_input = frappe.db.get_value(
            "Club100 Assessment Input",
            ordered_inputs[0].input,
            "input_code",
        )

        second_input = frappe.db.get_value(
            "Club100 Assessment Input",
            ordered_inputs[1].input,
            "input_code",
        )

        if not first_input or not second_input:
            return None

        first_value = self._get_numeric_input_value(
            input_values,
            first_input,
        )

        second_value = self._get_numeric_input_value(
            input_values,
            second_input,
        )

        if (
            first_value is None
            or second_value is None
        ):
            return None

        first_result = score_metric(
            metric.name,
            first_value,
            gender=gender,
            age=age,
        )

        second_result = score_metric(
            metric.name,
            second_value,
            gender=gender,
            age=age,
        )

        first_score = flt(
            first_result["score"]
        )

        second_score = flt(
            second_result["score"]
        )

        weaker_score = min(
            first_score,
            second_score,
        )

        stronger_score = max(
            first_score,
            second_score,
        )

        final_score = (
            weaker_score * 0.70
            + stronger_score * 0.30
        )

        final_score = round(
            final_score,
            1,
        )

        return {
            "score": final_score,
            "rating": self._rating_from_score(
                final_score
            ),
            "first_score": first_score,
            "second_score": second_score,
        }

    # =========================================================
    # BLOOD PRESSURE SCORING
    # =========================================================

    def _score_blood_pressure(
        self,
        input_values,
        gender,
        age,
    ):
        systolic = (
            self._get_numeric_input_value(
                input_values,
                "BP_SYS",
            )
        )

        diastolic = (
            self._get_numeric_input_value(
                input_values,
                "BP_DIA",
            )
        )

        if (
            systolic is None
            or diastolic is None
        ):
            return None

        systolic_result = score_metric(
            "Blood Pressure",
            systolic,
            gender=gender,
            age=age,
            component="Systolic",
        )

        diastolic_result = score_metric(
            "Blood Pressure",
            diastolic,
            gender=gender,
            age=age,
            component="Diastolic",
        )

        systolic_score = flt(
            systolic_result["score"]
        )

        diastolic_score = flt(
            diastolic_result["score"]
        )

        final_score = min(
            systolic_score,
            diastolic_score,
        )

        return {
            "score": final_score,
            "rating": self._rating_from_score(
                final_score
            ),
        }

    # =========================================================
    # SCORE BAND
    # =========================================================

    def _rating_from_score(self, score):
        if score is None:
            return None

        score = flt(score)

        if score >= 85:
            return "Excellent"

        if score >= 70:
            return "Good"

        if score >= 55:
            return "Building"

        if score >= 40:
            return "Needs Improvement"

        return "Needs Attention"

    # =========================================================
    # CATEGORY + OVERALL SCORES
    # =========================================================

    def calculate_category_scores(self):
        """
        Calculate weighted category scores and Club100 Fitness Score.

        Missing/unscored metrics are excluded rather than treated
        as zero.
        """

        if not self.template:
            return

        template = frappe.get_doc(
            "Club100 Assessment Template",
            self.template,
        )

        category_weights = {}

        for row in template.category_weights:
            category_weights[row.category] = (
                flt(row.weight)
            )

        category_data = {}

        for row in self.metrics:
            if not row.get(
                "include_in_score"
            ):
                continue

            if row.score in (
                None,
                "",
            ):
                continue

            if not row.category:
                continue

            metric_weight = (
                flt(row.get("weight"))
                if row.get("weight") not in (
                    None,
                    "",
                )
                else 1
            )

            if metric_weight <= 0:
                continue

            if row.category not in category_data:
                category_data[row.category] = {
                    "weighted_score": 0,
                    "weight_total": 0,
                    "metrics_scored": 0,
                }

            category_data[
                row.category
            ]["weighted_score"] += (
                flt(row.score)
                * metric_weight
            )

            category_data[
                row.category
            ]["weight_total"] += (
                metric_weight
            )

            category_data[
                row.category
            ]["metrics_scored"] += 1

        self.set(
            "category_scores",
            [],
        )

        overall_weighted_score = 0
        overall_weight_total = 0

        for category, data in (
            category_data.items()
        ):
            if data["weight_total"] <= 0:
                continue

            category_score = round(
                data["weighted_score"]
                / data["weight_total"],
                1,
            )

            category_weight = (
                category_weights.get(
                    category,
                    0,
                )
            )

            self.append(
                "category_scores",
                {
                    "category": category,
                    "score": category_score,
                    "weight": category_weight,
                    "metrics_scored": (
                        data["metrics_scored"]
                    ),
                },
            )

            if category_weight > 0:
                overall_weighted_score += (
                    category_score
                    * category_weight
                )

                overall_weight_total += (
                    category_weight
                )

        if overall_weight_total > 0:
            self.fitness_score = round(
                overall_weighted_score
                / overall_weight_total,
                0,
            )
        else:
            self.fitness_score = None