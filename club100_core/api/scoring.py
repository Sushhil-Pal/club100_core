import frappe
from decimal import Decimal, ROUND_HALF_UP
from frappe.utils import flt


def excel_round(value, digits=0):
    """
    Match Excel ROUND behavior rather than Python's banker's rounding.
    """
    quant = Decimal("1") if digits == 0 else Decimal("1." + ("0" * digits))
    return float(
        Decimal(str(value)).quantize(
            quant,
            rounding=ROUND_HALF_UP,
        )
    )


def clamp(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, value))


def score_to_rating(score):
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


def _number_or_none(value):
    """
    Frappe numeric child fields often return 0 when visually blank.
    For fields where 0 represents 'not supplied', normalize to None.
    """
    if value in (None, ""):
        return None

    value = flt(value)

    if value == 0:
        return None

    return value


def _age_matches(rule, age):
    age_from = _number_or_none(rule.age_from)
    age_to = _number_or_none(rule.age_to)

    # Generic rule
    if age_from is None and age_to is None:
        return True

    if age is None:
        return False

    age = int(age)

    if age_from is not None and age < age_from:
        return False

    if age_to is not None and age > age_to:
        return False

    return True


def _gender_matches(rule, gender):
    rule_gender = (rule.gender or "").strip()

    if not rule_gender or rule_gender == "Both":
        return True

    if not gender:
        return False

    return rule_gender.lower() == gender.strip().lower()


def _specificity(rule, gender, age):
    """
    Prefer:
      exact gender over Both/blank
      age-specific over generic
    """
    score = 0

    rule_gender = (rule.gender or "").strip()

    if (
        gender
        and rule_gender
        and rule_gender != "Both"
        and rule_gender.lower() == gender.lower()
    ):
        score += 10

    if _number_or_none(rule.age_from) is not None:
        score += 5

    if _number_or_none(rule.age_to) is not None:
        score += 5

    return score


def get_active_scoring_config(metric):
    configs = frappe.get_all(
        "Club100 Metric Scoring Config",
        filters={
            "metric": metric,
            "active": 1,
        },
        fields=["name", "version", "effective_from"],
        order_by="version desc, effective_from desc",
        limit=1,
    )

    if not configs:
        return None

    return frappe.get_doc(
        "Club100 Metric Scoring Config",
        configs[0].name,
    )


def get_candidate_rules(config, gender=None, age=None, component=None):
    rules = []

    for rule in config.rules:
        if not _gender_matches(rule, gender):
            continue

        if not _age_matches(rule, age):
            continue

        rule_component = (rule.component or "").strip()

        if component:
            if (
                rule_component
                and rule_component.lower() != component.lower()
            ):
                continue
        elif rule_component:
            # Component-specific rows should not be selected accidentally
            continue

        rules.append(rule)

    rules.sort(
        key=lambda r: (
            _specificity(r, gender, age),
            -(r.sequence or 0),
        ),
        reverse=True,
    )

    return rules


def _score_linear_higher(value, rule):
    excellent = flt(rule.excellent_value)
    risk = flt(rule.risk_value)
    poor = flt(rule.poor_value)

    if value >= excellent:
        return 100

    if value >= risk:
        return 80 + (
            20
            * (value - risk)
            / (excellent - risk)
        )

    if risk == poor:
        return 0

    return max(
        0,
        80
        * (value - poor)
        / (risk - poor),
    )


def _score_linear_lower(value, rule):
    excellent = flt(rule.excellent_value)
    risk = flt(rule.risk_value)
    poor = flt(rule.poor_value)

    if value <= excellent:
        return 100

    if value <= risk:
        return 100 - (
            20
            * (value - excellent)
            / (risk - excellent)
        )

    if poor == risk:
        return 0

    return max(
        0,
        80
        - (
            80
            * (value - risk)
            / (poor - risk)
        ),
    )


def _score_range(value, rule):
    lower = flt(rule.lower_limit)
    upper = flt(rule.upper_limit)
    factor_1 = flt(rule.factor_1)
    factor_2 = flt(rule.factor_2)

    if value < lower:
        return max(
            0,
            100 - ((lower - value) * factor_1),
        )

    if value <= upper:
        return 100

    return max(
        0,
        100 - ((value - upper) * factor_2),
    )


def _score_direct(value, rule):
    lower = flt(rule.lower_limit)
    upper = flt(rule.upper_limit)

    if upper == lower:
        return 0

    return clamp(
        (value - lower)
        / (upper - lower)
        * 100
    )


def _score_category(value, rules):
    """
    Category methods can have several rows, such as BP ranges.
    Find the row whose lower/upper limits contain the value.
    """
    for rule in rules:
        lower = flt(rule.lower_limit)
        upper = flt(rule.upper_limit)

        if lower <= value <= upper:
            return flt(rule.score), rule

    return None, None


def score_metric(
    metric,
    raw_value,
    gender=None,
    age=None,
    component=None,
    fitness_level=None,  # kept temporarily for backward compatibility
):
    """
    Score one Club100 Fitness Metric result.

    fitness_level is intentionally ignored; assessment score must not
    depend on the member's pre-existing fitness level.
    """

    if raw_value in (None, ""):
        return None

    value = flt(raw_value)

    config = get_active_scoring_config(metric)

    if not config:
        frappe.throw(
            f"No active scoring configuration found for {metric}"
        )

    rules = get_candidate_rules(
        config,
        gender=gender,
        age=age,
        component=component,
    )

    if not rules:
        frappe.throw(
            f"No applicable scoring rule found for {metric}"
        )

    method = (rules[0].method or "").strip()

    selected_rule = rules[0]

    if method == "Linear-Higher":
        score = _score_linear_higher(
            value,
            selected_rule,
        )

    elif method == "Linear-Lower":
        score = _score_linear_lower(
            value,
            selected_rule,
        )

    elif method == "Range":
        score = _score_range(
            value,
            selected_rule,
        )

    elif method == "Direct":
        score = _score_direct(
            value,
            selected_rule,
        )

    elif method == "Category":
        score, selected_rule = _score_category(
            value,
            rules,
        )

        if selected_rule is None:
            frappe.throw(
                f"No Category scoring range matched "
                f"{metric} value {value}"
            )

    else:
        frappe.throw(
            f"Unsupported scoring method '{method}' "
            f"for metric {metric}"
        )

    score = excel_round(
        clamp(score),
        0,
    )

    return {
        "metric": metric,
        "metricName": metric,
        "rawValue": value,
        "score": score,
        "rating": score_to_rating(score),
        "method": method,
        "config": config.name,
        "rule": selected_rule.name,
    }