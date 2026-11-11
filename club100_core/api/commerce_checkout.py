"""Authenticated Self commerce checkout: backend-owned pricing and entitlement dates."""
import frappe
from frappe.utils import add_days, getdate, nowdate, cint
from club100_core.api.member import _get_current_member
from club100_core.api.enrollment import _get_price, create_payment_order, verify_payment
from club100_core.wallet import checkout as wallet_checkout
from club100_core.wallet.checkout_math import split_total
from club100_core.wallet.logic import money


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
    # Paid Self activation initializes a Standard L1 (Core) training journey.
    # Reject unavailable programs BEFORE creating an order or taking payment.
    if not frappe.db.exists("Club100 Program Curriculum", {
        "program": program, "track": "Standard", "level_code": "L1", "active": 1,
    }):
        frappe.throw("Self checkout is unavailable for this program: configure an active Standard/Core curriculum first.")
    months = cint(months)
    price = _get_price("self", months)
    plan = _self_plan()
    today = getdate(nowdate())
    trials = frappe.get_all("Club100 Enrollment", filters={"member": member.name,
         "program": program, "is_trial": 1}, fields=["name","end_date"], order_by="creation desc",limit=1)
    trial = trials[0] if trials else None
    start = add_days(getdate(trial.end_date),1) if trial and trial.end_date and getdate(trial.end_date)>=today else today
    # Permit precisely one advance renewal of an existing paid Self membership.
    # A pending checkout must be resumed; a future paid renewal must not be
    # duplicated or silently stacked. Member is locked by create_member_checkout.
    existing = frappe.get_all("Club100 Enrollment", filters={
        "member": member.name, "program": program, "plan": plan, "is_trial": 0,
        "status": ["in", ["Registered", "Scheduled", "Active", "On Hold"]],
    }, fields=["name", "status", "payment_status", "start_date", "end_date"])
    active_paid = []
    for row in existing:
        if row.status == "Registered":
            frappe.throw("An unfinished Self checkout exists for this program. Resume or resolve that checkout first.")
        if row.status == "On Hold":
            frappe.throw("An On Hold membership exists. Contact Club100 support before renewing.")
        if row.status == "Scheduled":
            frappe.throw("A future membership is already scheduled. Further advance renewals are not yet supported.")
        if row.status == "Active":
            if row.payment_status != "Paid":
                frappe.throw("An active membership needs payment review before renewal.")
            if not row.end_date:
                frappe.throw("Active membership has no end date. Contact Club100 support.")
            if getdate(row.end_date) >= today:
                active_paid.append(row)
    if len(active_paid) > 1:
        frappe.throw("Multiple active memberships found. Resolve overlapping memberships before renewal.")
    if active_paid:
        start = max(getdate(start), add_days(getdate(active_paid[0].end_date), 1))
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
       "trialEnrollmentId":data["trial"].name if data["trial"] else None,
       "wallet": wallet_checkout.preview(member.name, data["price"]["amount_payable"])}


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
    # Active/Completed paid purchases are history, not an open checkout.
    # Returning them as "paid" prevents an eligible member from renewing.
    pending = next((r for r in rows if r.payment_status == "Pending" and r.status == "Registered"), None)
    paid = next((r for r in rows if r.payment_status == "Paid" and r.status == "Scheduled"), None)
    r = pending or paid
    if not r:
        return {"state":"none"}
    wallet_state = frappe.db.get_value("Club100 Enrollment", r.name, "wallet_settlement_status") if pending else None
    result_state = "review" if wallet_state == "Review" else ("pending" if pending else "paid")
    if pending and wallet_state == "Reserved":
        from frappe.utils import get_datetime, now_datetime
        from club100_core.wallet.spending import _key
        reskey = frappe.db.get_value("Club100 Enrollment", r.name, "wallet_reservation_key")
        expiry = frappe.db.get_value("Club100 Wallet Reservation", {"reservation_key":reskey}, "expires_at") if reskey else None
        if not expiry or get_datetime(expiry) <= now_datetime():
            result_state = "expired"
    return {"state":result_state, "enrollment":r.name,
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
    from club100_core.accounting.billing_customer import ensure_customer_for_member
    from club100_core.accounting.settlement import _config
    ensure_customer_for_member(member.name, _config()['company'])
    row_wallet = frappe.db.get_value("Club100 Enrollment", str(enrollment_id), "wallet_settlement_status")
    if row_wallet == "Review":
        frappe.throw("Captured payment requires manual wallet settlement review. Do not retry.")
    order=create_payment_order(str(enrollment_id),row.mobile,row.email)
    return {**order,"paidStartDate":str(row.start_date),
        "durationMonths":cint(row.membership_duration_months),"program":row.program}


@frappe.whitelist(methods=["POST"])
def create_member_checkout(program=None, duration_months=None, wallet_amount=None):
    member = _member_locked()
    # Billing party must exist before creating a gateway order or wallet-only receipt.
    from club100_core.accounting.billing_customer import ensure_customer_for_member
    from club100_core.accounting.settlement import _config
    ensure_customer_for_member(member.name, _config()['company'])
    requested = money(wallet_amount or 0)
    if requested > 0:
        wallet_checkout.require_enabled()
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
        check=frappe.db.get_value('Club100 Enrollment',old.name,
             ['wallet_settlement_status','razorpay_capture_verified','payment_id'],as_dict=True)
        if (check.wallet_settlement_status == 'Review' or check.razorpay_capture_verified or check.payment_id):
            frappe.throw('Captured payment is under accounting review. Do not retry checkout.')
        existing_wallet = money(frappe.db.get_value("Club100 Enrollment",old.name,"wallet_applied_amount") or 0)
        if requested != existing_wallet:
            frappe.throw("Existing checkout tender differs. Resume the original order or contact support.")
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
    if requested > 0:
        wallet_checkout.attach_reservation(enrollment, requested)
        if money(enrollment.gateway_due_amount) == 0:
            return {**wallet_checkout.wallet_only_paid(enrollment),
                    "paidStartDate":str(data["start"]),
                    "durationMonths":price["duration_months"],"program":data["program"]}
    # Gateway amount is the verified remainder only; gross stays on enrollment.
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
    """Activate paid PWA renewals when due; expire their older memberships first.

    Runs via daily scheduler, may be safely retried. Never activates unpaid
    enrollments or modifies another plan/program's membership.
    """
    today = getdate(nowdate())
    names = frappe.get_all("Club100 Enrollment", filters={
        "status": "Scheduled", "payment_status": "Paid",
        "website_source": "PWA Commerce", "start_date": ["<=", today],
    }, pluck="name", order_by="start_date asc, creation asc", limit_page_length=500)
    count = 0
    for name in names:
        # Match checkout's member-first lock order to reduce lock inversions.
        member = frappe.db.get_value("Club100 Enrollment", name, "member")
        if not member:
            continue
        frappe.db.sql("SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE", member)
        frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE", name)
        doc = frappe.get_doc("Club100 Enrollment", name)
        if (doc.status != "Scheduled" or doc.payment_status != "Paid"
                or doc.website_source != "PWA Commerce"
                or getdate(doc.start_date) > today):
            continue
        # The previous subscription may still be marked Active even though
        # its end date has passed. Close it before activating the renewal so
        # downstream membership selectors cannot see two Active rows.
        prior = frappe.get_all("Club100 Enrollment", filters={
            "member": doc.member, "program": doc.program, "plan": doc.plan,
            "status": "Active", "end_date": ["<", doc.start_date],
            "name": ["!=", doc.name],
        }, pluck="name")
        for old_name in prior:
            frappe.db.sql("SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE", old_name)
            old = frappe.get_doc("Club100 Enrollment", old_name)
            if (old.status == "Active" and old.end_date
                    and getdate(old.end_date) < getdate(doc.start_date)):
                old.status = "Completed"
                old.save(ignore_permissions=True)
        doc.status = "Active" if doc.end_date and getdate(doc.end_date) >= today else "Completed"
        doc.save(ignore_permissions=True)
        count += 1
    return {"processed": count}
