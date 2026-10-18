"""Read-only member commerce catalog. No enrollment or payment mutations."""
import frappe
from club100_core.api.member import _get_current_member
from club100_core.api.enrollment import PRICING, _get_price

@frappe.whitelist(methods=["GET"])
def membership_offers(plan_code="self"):
    """Offer prices are computed in ERP; frontend must never calculate payable totals."""
    _get_current_member()
    code = str(plan_code or "self").strip().lower()
    if code not in PRICING:
        frappe.throw("Selected membership plan is not available.", frappe.ValidationError)
    plan = frappe.db.get_value(
        "Club100 Plan", {"plan_code": code, "status": "Active"},
        ["name", "plan_name", "plan_code"], as_dict=True,
    )
    if not plan:
        frappe.throw("Selected membership plan is not active.", frappe.ValidationError)
    prices = [_get_price(code, months) for months in (1, 3, 6, 12)]
    return {"plan": {"id": plan.name, "name": plan.plan_name, "code": plan.plan_code},
            "currency": "INR", "offers": [{"durationMonths": p["duration_months"],
                "monthlyEquivalent": p["monthly_equivalent"], "amountPayable": p["amount_payable"]}
                for p in prices]}
