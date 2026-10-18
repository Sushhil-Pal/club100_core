"""Authenticated Self commerce checkout: backend-owned pricing and entitlement dates."""
import frappe
from frappe.utils import add_days, getdate, nowdate, cint
from club100_core.api.member import _get_current_member
from club100_core.api.enrollment import _get_price, create_payment_order, verify_payment


def _member_locked():
    member = _get_current_member()
    frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member.name)
    if member.status != "Active" or member.onboarding_status != "Completed":
        frappe.throw("Complete your active Club100 member profile first.")
    return member


def _self_plan():
    plan = frappe.db.get_value("Club100 Plan", {"plan_code": "self", "status": "Active"}, "name")
    if not plan:
        frappe.throw("The Self membership is not available.")
    return plan


def _quote(member, program, months):
    program = str(program or "").strip()
    if not frappe.db.exists("Club100 Program", program):
        frappe.throw("Select a valid Club100 fitness program.")
    months = cint(months)
    price = _get_price("self", months)
    plan = _self_plan()
    today = getdate(nowdate())
    trials = frappe.get_all("Club100 Enrollment", filters={"member": member.name,
         "program": program, "is_trial": 1}, fields=["name","end_date"], order_by="creation desc",limit=1)
    trial = trials[0] if trials else None
    start = add_days(getdate(trial.end_date),1) if trial and trial.end_date and getdate(trial.end_date)>=today else today
    # Block nontrial enrollments, including unpaid reservations and future paid orders.
    existing = frappe.get_all("Club100 Enrollment", filters={"member":member.name,
          "program":program,"plan":plan,"is_trial":0,
          "status":["in",["Registered","Scheduled","Active","On Hold"]]},
          fields=["name","payment_status","start_date","end_date"])
    if existing:
        frappe.throw("An existing Self membership or pending checkout already exists for this program. Please resume that checkout or contact Club100 support.")
    return {"program":program,"plan":plan,"start":start,"trial":trial,"price":price}


@frappe.whitelist(methods=["GET"])
def checkout_quote(program=None, duration_months=None):
    member = _get_current_member()
    if member.status != "Active" or member.onboarding_status != "Completed":
        frappe.throw("Complete your Club100 profile first.")
    data = _quote(member,program,duration_months)
    return {"program":data["program"],"durationMonths":cint(duration_months),
       "monthlyEquivalent":data["price"]["monthly_equivalent"],
       "amountPayable":data["price"]["amount_payable"],"currency":"INR",
       "paidStartDate":str(data["start"]),
       "trialEnrollmentId":data["trial"].name if data["trial"] else None}


@frappe.whitelist(methods=["GET"])
def member_checkout_state(program=None):
    """Read-only, member-owned recovery status; no client-provided enrollment IDs."""
    member = _get_current_member()
    if not program or not frappe.db.exists("Club100 Program", program):
        frappe.throw("Select a valid Club100 fitness program.")
    plan = _self_plan()
    rows = frappe.get_all("Club100 Enrollment", filters={
        "member": member.name, "program": program, "plan": plan,
        "website_source": "PWA Commerce", "is_trial": 0,
    }, fields=["name", "status", "payment_status", "payment_order_id",
               "membership_duration_months", "start_date", "amount_payable"],
       order_by="creation desc", limit_page_length=15)
    pending = next((r for r in rows if r.payment_status == "Pending" and r.status == "Registered"), None)
    paid = next((r for r in rows if r.payment_status == "Paid"), None)
    r = pending or paid
    if not r:
        return {"state":"none"}
    return {"state":"pending" if pending else "paid", "enrollment":r.name,
            "status":r.status, "paymentStatus":r.payment_status,
            "durationMonths":cint(r.membership_duration_months),
            "amountPayable":float(r.amount_payable or 0),
            "paidStartDate":str(r.start_date or "")}


@frappe.whitelist(methods=["POST"])
def resume_member_checkout(enrollment_id=None):
    """Resume the SAME pending Razorpay order, never make a new enrollment."""
    member = _member_locked()
    row = frappe.db.get_value("Club100 Enrollment", str(enrollment_id or ""),
       ["member","website_source","status","payment_status","mobile","email","start_date",
        "membership_duration_months","program"],as_dict=True)
    if not row or row.member != member.name or row.website_source != "PWA Commerce":
        frappe.throw("Checkout does not belong to this member.", frappe.PermissionError)
    if row.payment_status != "Pending" or row.status != "Registered":
        frappe.throw("This checkout is no longer pending. Refresh its status.")
    order=create_payment_order(str(enrollment_id),row.mobile,row.email)
    return {**order,"paidStartDate":str(row.start_date),
        "durationMonths":cint(row.membership_duration_months),"program":row.program}


@frappe.whitelist(methods=["POST"])
def create_member_checkout(program=None, duration_months=None):
    member = _member_locked()
    plan = _self_plan()
    pending = frappe.get_all("Club100 Enrollment", filters={
        "member":member.name,"program":str(program or ""),"plan":plan,
        "website_source":"PWA Commerce", "is_trial":0,
        "status":"Registered","payment_status":"Pending"
    },fields=["name","membership_duration_months","mobile","email","start_date"], limit_page_length=2)
    if pending:
        if len(pending)!=1 or cint(pending[0].membership_duration_months)!=cint(duration_months):
            frappe.throw("An unfinished checkout exists. Resume it before choosing another duration.")
        old=pending[0]
        order=create_payment_order(old.name,old.mobile,old.email)
        return {**order,"paidStartDate":str(old.start_date),
            "durationMonths":cint(old.membership_duration_months),"program":program}
    data = _quote(member,program,duration_months)
    price = data["price"]
    # This record is the durable checkout/order receipt. It has no entitlement
    # while Registered / Pending, and cannot be made Active until paid.
    enrollment = frappe.get_doc({
      "doctype":"Club100 Enrollment","member":member.name,
      "full_name":member.full_name or member.name,
      "mobile":member.mobile,"email":member.email,
      "program":data["program"],"plan":data["plan"],
      "pricing_model":"Online Plan","enrollment_type":"Individual",
      "enrollment_date":nowdate(),"status":"Registered","payment_status":"Pending",
      "start_date":data["start"],
      "membership_duration_months":price["duration_months"],
      "monthly_equivalent":price["monthly_equivalent"],
      "amount_payable":price["amount_payable"],
      "website_source":"PWA Commerce",
      "origin_trial_enrollment":data["trial"].name if data["trial"] else None,
    })
    enrollment.insert(ignore_permissions=True)
    # Existing gateway order creation is reused; it calculates amount from ERP.
    order = create_payment_order(enrollment.name, enrollment.mobile, enrollment.email)
    return {**order,"paidStartDate":str(data["start"]),
        "durationMonths":price["duration_months"],"program":data["program"]}


@frappe.whitelist(methods=["POST"])
def verify_member_checkout(enrollment_id=None, razorpay_payment_id=None,
                           razorpay_order_id=None, razorpay_signature=None):
    member = _get_current_member()
    row = frappe.db.get_value("Club100 Enrollment",str(enrollment_id or ""),
            ["member","website_source"],as_dict=True)
    if not row or row.member != member.name or row.website_source != "PWA Commerce":
        frappe.throw("Checkout does not belong to this member.",frappe.PermissionError)
    # Existing verification performs signature + captured-payment API checks,
    # stores payment identity, and finalizes idempotently under a DB row lock.
    return verify_payment(enrollment_id,razorpay_payment_id,
                          razorpay_order_id,razorpay_signature)


def activate_due_memberships():
    """Called by daily scheduler; safe to rerun after missed scheduler executions."""
    today = getdate(nowdate())
    names = frappe.get_all("Club100 Enrollment", filters={
        "status":"Scheduled","payment_status":"Paid",
        "website_source":"PWA Commerce","start_date":["<=",today]},
        pluck="name", limit_page_length=500)
    count = 0
    for name in names:
        frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE",name)
        doc = frappe.get_doc("Club100 Enrollment",name)
        if doc.status != "Scheduled" or doc.payment_status != "Paid":
            continue
        doc.status = "Active" if getdate(doc.end_date)>=today else "Completed"
        doc.save(ignore_permissions=True)
        count += 1
    return {"processed":count}
