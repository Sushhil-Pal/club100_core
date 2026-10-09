import hashlib
import hmac
import json
import re
from decimal import Decimal, ROUND_HALF_UP

import frappe
import requests
from frappe.utils import add_days, add_months, add_to_date, cint, getdate, now_datetime, nowdate


PROGRAMS = {
    "general-fitness": "General Fitness",
    "weight-management": "Weight Management",
    "active-50": "Active 50+",
    "sports-fitness": "Sports Fitness",
    "performance-training": "Performance Training",
}


# Final Club100 membership pricing.
#
# Pricing is calculated here on the server and is never trusted
# from the browser.
PRICING = {
    "self": {
        "name": "Self",
        "monthly": {
            1: 799,
            3: 699,
            6: 599,
            12: 499,
        },
    },
    "group": {
        "name": "Group",
        "monthly": {
            1: 1299,
            3: 1099,
            6: 999,
            12: 899,
        },
    },
    "coach": {
        "name": "Coach",
        "monthly": {
            1: 1799,
            3: 1599,
            6: 1399,
            12: 1199,
        },
    },
}


RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


def seed_website_programs():
    """
    Create/update the five website program masters.

    Safe to run multiple times.
    """

    results = []

    for program_code, program_name in PROGRAMS.items():
        existing = frappe.db.get_value(
            "Club100 Program",
            {"program_code": program_code},
            "name",
        )

        if existing:
            doc = frappe.get_doc(
                "Club100 Program",
                existing,
            )
            action = "updated"
        else:
            doc = frappe.new_doc(
                "Club100 Program"
            )
            action = "created"

        doc.program_name = program_name
        doc.program_code = program_code

        doc.delivery_mode = "Online"
        doc.audience = "Individual"

        # Club100's current weekly training structure.
        doc.sessions_per_week = 4
        doc.session_duration_minutes = 60
        doc.default_cohort_capacity = 25

        doc.assessment_included = 1
        doc.reassessment_included = 1
        doc.payment_required = 1

        doc.publish_to_app = 1
        doc.status = "Active"

        doc.save(
            ignore_permissions=True
        )

        results.append({
            "program_code": program_code,
            "program": doc.name,
            "action": action,
        })

    frappe.db.commit()

    return results


def _clean_mobile(value):
    value = str(value or "").strip()

    normalized = re.sub(
        r"\D",
        "",
        value,
    )

    if (
        len(normalized) < 7
        or len(normalized) > 15
    ):
        frappe.throw(
            "Please enter a valid mobile number."
        )

    return normalized


def _clean_email(value):
    value = str(value or "").strip().lower()

    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        value,
    ):
        frappe.throw(
            "Please enter a valid email address."
        )

    return value


def _get_program(program_code):
    if program_code not in PROGRAMS:
        frappe.throw(
            "Invalid Club100 program."
        )

    program = frappe.db.get_value(
        "Club100 Program",
        {
            "program_code": program_code,
            "status": "Active",
        },
        [
            "name",
            "program_name",
        ],
        as_dict=True,
    )

    if not program:
        frappe.throw(
            "The selected Club100 program is not currently available."
        )

    return program


def _get_price(plan_key, duration_months):
    if plan_key not in PRICING:
        frappe.throw(
            "Invalid Club100 plan."
        )

    duration_months = cint(
        duration_months
    )

    if duration_months not in (1, 3, 6, 12):
        frappe.throw(
            "Invalid membership duration."
        )

    plan = PRICING[plan_key]

    monthly_equivalent = (
        plan["monthly"][duration_months]
    )

    amount_payable = (
        monthly_equivalent
        * duration_months
    )

    return {
        "plan_name": plan["name"],
        "duration_months": duration_months,
        "monthly_equivalent": monthly_equivalent,
        "amount_payable": amount_payable,
    }


def _get_razorpay_config():
    key_id = str(
        frappe.conf.get("razorpay_key_id") or ""
    ).strip()

    key_secret = str(
        frappe.conf.get("razorpay_key_secret") or ""
    ).strip()

    mode = str(
        frappe.conf.get("razorpay_mode") or "test"
    ).strip().lower()

    if not key_id or not key_secret:
        frappe.throw(
            "Razorpay API credentials are not configured."
        )

    if mode not in ("test", "live"):
        frappe.throw(
            "Invalid Razorpay mode configuration."
        )

    expected_prefix = (
        "rzp_test_"
        if mode == "test"
        else "rzp_live_"
    )

    if not key_id.startswith(expected_prefix):
        frappe.throw(
            f"Razorpay Key ID does not match configured {mode} mode."
        )

    return {
        "key_id": key_id,
        "key_secret": key_secret,
        "mode": mode,
    }


def _get_razorpay_webhook_secret():
    secret = str(
        frappe.conf.get(
            "razorpay_webhook_secret"
        )
        or ""
    ).strip()

    if not secret:
        frappe.throw(
            "Razorpay webhook secret is not configured."
        )

    return secret


def _rupees_to_paise(amount):
    value = Decimal(
        str(amount or 0)
    )

    if value <= 0:
        frappe.throw(
            "Enrollment amount must be greater than zero."
        )

    paise = (
        value * Decimal("100")
    ).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )

    return int(paise)


def _razorpay_error_message(response):
    try:
        payload = response.json()
    except ValueError:
        return (
            "Razorpay returned an invalid response."
        )

    error = payload.get("error") or {}

    return (
        error.get("description")
        or error.get("reason")
        or error.get("code")
        or "Unable to create Razorpay order."
    )


def _split_member_name(full_name):
    full_name = str(
        full_name or ""
    ).strip()

    if not full_name:
        return "Member", ""

    parts = full_name.split(
        None,
        1,
    )

    first_name = parts[0]

    last_name = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    return first_name, last_name


def _ensure_member_for_paid_enrollment(
    enrollment,
):
    """
    Create or reuse the Club100 Member that represents a paid enrollment.

    We intentionally do NOT create a Frappe User here.

    The Club100 app's existing Create Account flow will find this member
    by mobile, create/link the User, set the chosen password and update
    the member profile. New paid members therefore start with:

        status = Active
        app_access_status = First Time
        onboarding_status = Not Started
    """

    mobile = _clean_mobile(
        enrollment.mobile
    )

    email = _clean_email(
        enrollment.email
    )

    member_name = frappe.db.get_value(
        "Club100 Member",
        {
            "mobile": mobile,
        },
        "name",
    )

    first_name, last_name = (
        _split_member_name(
            enrollment.full_name
        )
    )

    if member_name:
        member = frappe.get_doc(
            "Club100 Member",
            member_name,
        )

        #
        # A suspended member may represent an operational/admin block.
        # A new payment should not silently override that.
        #
        if member.status == "Suspended":
            frappe.throw(
                "This Club100 member is suspended. "
                "Please contact Club100 support."
            )

        member.status = "Active"

        #
        # Preserve an already-configured app account. If the member does
        # not yet have a User, make sure the Create Account flow sees the
        # member as First Time.
        #
        if not member.user:
            member.app_access_status = (
                "First Time"
            )

        if not member.onboarding_status:
            member.onboarding_status = (
                "Not Started"
            )

        if not member.first_name:
            member.first_name = first_name

        if (
            not member.last_name
            and last_name
        ):
            member.last_name = last_name

        if not member.email:
            member.email = email

        if not member.member_type:
            member.member_type = (
                "Individual"
            )

        if not member.current_fitness_level:
            member.current_fitness_level = (
                "Beginner"
            )

        if not member.joining_date:
            member.joining_date = (
                nowdate()
            )

        member.save(
            ignore_permissions=True
        )

        return member


    member = frappe.get_doc({
        "doctype": "Club100 Member",

        "first_name": first_name,
        "last_name": last_name,

        "mobile": mobile,
        "email": email,

        "member_type": "Individual",

        #
        # This is only the operational starting value required by the
        # existing Member DocType. Onboarding can replace it later.
        #
        "current_fitness_level":
            "Beginner",

        "joining_date":
            nowdate(),

        "status":
            "Active",

        #
        # No User is created here. The existing app Create Account flow
        # will create/link the User and change this to Active.
        #
        "app_access_status":
            "First Time",

        "onboarding_status":
            "Not Started",
    })

    member.insert(
        ignore_permissions=True
    )

    return member


def _finalize_paid_enrollment_doc(
    enrollment,
):
    """
    Finalize membership after verified payment.

    Safe to call repeatedly.
    """

    if enrollment.payment_status != "Paid":
        frappe.throw(
            "Only paid enrollments can be activated."
        )

    duration_months = cint(
        enrollment.membership_duration_months
    )

    if duration_months not in (
        1,
        3,
        6,
        12,
    ):
        frappe.throw(
            "Invalid membership duration."
        )

    member = (
        _ensure_member_for_paid_enrollment(
            enrollment
        )
    )

    if enrollment.member:
        if enrollment.member != member.name:
            frappe.throw(
                "Enrollment is already linked to a different member."
            )
    else:
        enrollment.member = member.name


    #
    # For v1 the paid membership starts on the payment date.
    # This keeps the purchased duration deterministic for Self, Group
    # and Coach. Cohort/session scheduling remains independent.
    #
    if enrollment.start_date:
        start_date = getdate(
            enrollment.start_date
        )
    elif enrollment.payment_date:
        start_date = getdate(
            enrollment.payment_date
        )
    else:
        start_date = getdate(
            nowdate()
        )

    end_date = add_days(
        add_months(
            start_date,
            duration_months,
        ),
        -1,
    )

    enrollment.start_date = start_date
    enrollment.end_date = end_date
    enrollment.status = "Active"

    enrollment.save(
        ignore_permissions=True
    )

    next_action = (
        "sign_in"
        if (
            member.user
            and member.app_access_status
            == "Active"
        )
        else "create_account"
    )

    return {
        "member": member.name,
        "member_status": member.status,
        "app_access_status":
            member.app_access_status,
        "onboarding_status":
            member.onboarding_status,
        "start_date":
            str(enrollment.start_date),
        "end_date":
            str(enrollment.end_date),
        "next_action":
            next_action,
    }


def finalize_paid_enrollment(
    enrollment_id=None,
):
    """
    Admin/internal utility for finalizing an enrollment that is already Paid.

    This is useful for recovery and for paid records created before automatic
    post-payment activation was introduced.
    """

    enrollment_id = str(
        enrollment_id or ""
    ).strip()

    if not enrollment_id:
        frappe.throw(
            "Enrollment ID is required."
        )

    if not frappe.db.exists(
        "Club100 Enrollment",
        enrollment_id,
    ):
        frappe.throw(
            "Club100 Enrollment not found."
        )

    frappe.db.sql(
        """
        SELECT name
        FROM `tabClub100 Enrollment`
        WHERE name = %s
        FOR UPDATE
        """,
        enrollment_id,
    )

    enrollment = frappe.get_doc(
        "Club100 Enrollment",
        enrollment_id,
    )

    result = (
        _finalize_paid_enrollment_doc(
            enrollment
        )
    )

    frappe.db.commit()

    return {
        "success": True,
        "enrollment":
            enrollment.name,
        "payment_status":
            enrollment.payment_status,
        "status":
            enrollment.status,
        **result,
    }


@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def create_pending_enrollment(
    full_name=None,
    mobile=None,
    email=None,
    program=None,
    plan=None,
    duration_months=None,
):
    """
    Create a pending Club100 website enrollment.

    Pricing is always calculated server-side.

    A Club100 Member is deliberately NOT created here.
    Member activation happens only after confirmed payment.
    """

    full_name = str(
        full_name or ""
    ).strip()

    if len(full_name) < 2:
        frappe.throw(
            "Please enter your full name."
        )

    mobile = _clean_mobile(
        mobile
    )

    email = _clean_email(
        email
    )

    program_doc = _get_program(
        program
    )

    pricing = _get_price(
        plan,
        duration_months,
    )

    #
    # Protect against accidental double-clicks / retries.
    #
    cutoff = add_to_date(
        now_datetime(),
        minutes=-15,
    )

    existing = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "mobile": mobile,
            "email": email,
            "program": program_doc.name,
            "plan": pricing["plan_name"],
            "membership_duration_months":
                pricing["duration_months"],
            "payment_status": "Pending",
            "creation": [">=", cutoff],
        },
        fields=["name"],
        order_by="creation desc",
        limit=1,
    )

    if existing:
        enrollment = frappe.get_doc(
            "Club100 Enrollment",
            existing[0].name,
        )

        return {
            "success": True,
            "reused": True,
            "enrollment": enrollment.name,
            "program": program_doc.program_name,
            "program_code": program,
            "plan": pricing["plan_name"],
            "duration_months":
                pricing["duration_months"],
            "monthly_equivalent":
                pricing["monthly_equivalent"],
            "amount_payable":
                pricing["amount_payable"],
            "status":
                enrollment.status,
            "payment_status":
                enrollment.payment_status,
        }

    enrollment = frappe.get_doc({
        "doctype": "Club100 Enrollment",

        # No member yet.
        "member": None,

        "full_name": full_name,
        "mobile": mobile,
        "email": email,

        "program": program_doc.name,

        "plan": pricing["plan_name"],

        "membership_duration_months":
            pricing["duration_months"],

        "monthly_equivalent":
            pricing["monthly_equivalent"],

        "amount_payable":
            pricing["amount_payable"],

        "enrollment_type":
            "Individual",

        "enrollment_date":
            nowdate(),

        "status":
            "Registered",

        "payment_status":
            "Pending",

        "website_source":
            "Website",
    })

    enrollment.insert(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "reused": False,
        "enrollment": enrollment.name,
        "program": program_doc.program_name,
        "program_code": program,
        "plan": pricing["plan_name"],
        "duration_months":
            pricing["duration_months"],
        "monthly_equivalent":
            pricing["monthly_equivalent"],
        "amount_payable":
            pricing["amount_payable"],
        "status":
            enrollment.status,
        "payment_status":
            enrollment.payment_status,
    }


@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def create_payment_order(
    enrollment_id=None,
    mobile=None,
    email=None,
):
    """
    Create or reuse a Razorpay Order for a pending Club100 Enrollment.

    The payable amount is read only from ERP. No amount supplied by
    the browser is accepted or trusted.

    The caller must also provide the enrollment mobile and email so
    sequential enrollment IDs alone cannot be used to start payments.
    """

    enrollment_id = str(
        enrollment_id or ""
    ).strip()

    if not enrollment_id:
        frappe.throw(
            "Enrollment ID is required."
        )

    mobile = _clean_mobile(
        mobile
    )

    email = _clean_email(
        email
    )

    if not frappe.db.exists(
        "Club100 Enrollment",
        enrollment_id,
    ):
        frappe.throw(
            "Club100 Enrollment not found."
        )

    #
    # Lock the enrollment row for this transaction so two simultaneous
    # requests cannot normally create two different Razorpay orders.
    #
    frappe.db.sql(
        """
        SELECT name
        FROM `tabClub100 Enrollment`
        WHERE name = %s
        FOR UPDATE
        """,
        enrollment_id,
    )

    enrollment = frappe.get_doc(
        "Club100 Enrollment",
        enrollment_id,
    )

    if (
        _clean_mobile(enrollment.mobile) != mobile
        or _clean_email(enrollment.email) != email
    ):
        frappe.throw(
            "Enrollment details do not match."
        )

    if enrollment.payment_status == "Paid":
        frappe.throw(
            "This enrollment has already been paid."
        )

    if enrollment.status in (
        "Cancelled",
        "Completed",
    ):
        frappe.throw(
            "This enrollment is not eligible for payment."
        )

    if enrollment.payment_status not in (
        "Pending",
        None,
        "",
    ):
        frappe.throw(
            f"Payment cannot be started while payment status is "
            f"{enrollment.payment_status}."
        )

    amount_paise = _rupees_to_paise(
        enrollment.amount_payable
    )

    config = _get_razorpay_config()

    #
    # Reuse an existing Razorpay order if one was already created.
    # Razorpay orders can be attempted again until payment succeeds.
    #
    if enrollment.payment_order_id:
        return {
            "success": True,
            "reused": True,
            "enrollment": enrollment.name,
            "key_id": config["key_id"],
            "mode": config["mode"],
            "order_id":
                enrollment.payment_order_id,
            "amount": amount_paise,
            "amount_rupees":
                enrollment.amount_payable,
            "currency": "INR",
        }

    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": enrollment.name,
        "notes": {
            "club100_enrollment":
                enrollment.name,
            "program":
                enrollment.program or "",
            "plan":
                enrollment.plan or "",
            "duration_months":
                str(
                    enrollment.membership_duration_months
                    or ""
                ),
        },
    }

    try:
        response = requests.post(
            f"{RAZORPAY_API_BASE}/orders",
            json=payload,
            auth=(
                config["key_id"],
                config["key_secret"],
            ),
            timeout=20,
        )
    except requests.RequestException:
        frappe.log_error(
            frappe.get_traceback(),
            "Club100 Razorpay Order Creation",
        )
        frappe.throw(
            "Unable to connect to Razorpay. Please try again."
        )

    if not response.ok:
        message = _razorpay_error_message(
            response
        )

        frappe.log_error(
            message=(
                f"Enrollment: {enrollment.name}\n"
                f"HTTP Status: {response.status_code}\n"
                f"Razorpay Error: {message}"
            ),
            title="Club100 Razorpay Order Creation",
        )

        frappe.throw(
            message
        )

    order = response.json()

    order_id = str(
        order.get("id") or ""
    ).strip()

    if not order_id:
        frappe.throw(
            "Razorpay did not return an Order ID."
        )

    #
    # Validate Razorpay's response before saving it.
    #
    if int(order.get("amount") or 0) != amount_paise:
        frappe.throw(
            "Razorpay order amount does not match the Club100 enrollment."
        )

    if order.get("currency") != "INR":
        frappe.throw(
            "Razorpay order currency does not match the Club100 enrollment."
        )

    enrollment.payment_gateway = "Razorpay"
    enrollment.payment_order_id = order_id

    enrollment.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "reused": False,
        "enrollment": enrollment.name,
        "key_id": config["key_id"],
        "mode": config["mode"],
        "order_id": order_id,
        "amount": amount_paise,
        "amount_rupees":
            enrollment.amount_payable,
        "currency": "INR",
    }


@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def verify_payment(
    enrollment_id=None,
    razorpay_payment_id=None,
    razorpay_order_id=None,
    razorpay_signature=None,
):
    """
    Verify Razorpay Checkout success server-side.

    Verification steps:
    1. Load the trusted Razorpay order ID from the Enrollment.
    2. Verify Razorpay's HMAC SHA256 signature.
    3. Fetch the payment directly from Razorpay.
    4. Confirm order, amount, currency and captured status.
    5. Mark the Club100 Enrollment as Paid.

    Member creation / activation is intentionally handled in the next
    enrollment-finalization step.
    """

    enrollment_id = str(
        enrollment_id or ""
    ).strip()

    payment_id = str(
        razorpay_payment_id or ""
    ).strip()

    checkout_order_id = str(
        razorpay_order_id or ""
    ).strip()

    signature = str(
        razorpay_signature or ""
    ).strip()

    if (
        not enrollment_id
        or not payment_id
        or not checkout_order_id
        or not signature
    ):
        frappe.throw(
            "Incomplete Razorpay payment response."
        )

    if not frappe.db.exists(
        "Club100 Enrollment",
        enrollment_id,
    ):
        frappe.throw(
            "Club100 Enrollment not found."
        )

    frappe.db.sql(
        """
        SELECT name
        FROM `tabClub100 Enrollment`
        WHERE name = %s
        FOR UPDATE
        """,
        enrollment_id,
    )

    enrollment = frappe.get_doc(
        "Club100 Enrollment",
        enrollment_id,
    )

    stored_order_id = str(
        enrollment.payment_order_id or ""
    ).strip()

    if not stored_order_id:
        frappe.throw(
            "No Razorpay order is linked to this enrollment."
        )

    #
    # Never use only the order ID supplied by the browser as the trusted
    # order ID. The stored ERP order ID is the source of truth.
    #
    if checkout_order_id != stored_order_id:
        frappe.throw(
            "Razorpay Order ID does not match the enrollment."
        )

    if (
        enrollment.payment_status == "Paid"
        and enrollment.payment_id == payment_id
    ):
        activation = (
            _finalize_paid_enrollment_doc(
                enrollment
            )
        )

        frappe.db.commit()

        return {
            "success": True,
            "reused": True,
            "enrollment": enrollment.name,
            "payment_id": enrollment.payment_id,
            "payment_status": enrollment.payment_status,
            "status": enrollment.status,
            **activation,
        }

    if enrollment.payment_status == "Paid":
        frappe.throw(
            "This enrollment has already been paid with a different payment."
        )

    config = _get_razorpay_config()

    expected_signature = hmac.new(
        config["key_secret"].encode("utf-8"),
        f"{stored_order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        signature,
    ):
        frappe.throw(
            "Razorpay payment signature verification failed."
        )

    #
    # Fetch the payment directly from Razorpay. Signature authenticity
    # alone does not tell us whether the payment is captured.
    #
    try:
        response = requests.get(
            f"{RAZORPAY_API_BASE}/payments/{payment_id}",
            auth=(
                config["key_id"],
                config["key_secret"],
            ),
            timeout=20,
        )
    except requests.RequestException:
        frappe.log_error(
            frappe.get_traceback(),
            "Club100 Razorpay Payment Verification",
        )
        frappe.throw(
            "Unable to verify payment with Razorpay. Please try again."
        )

    if not response.ok:
        message = _razorpay_error_message(
            response
        )

        frappe.log_error(
            message=(
                f"Enrollment: {enrollment.name}\n"
                f"Payment: {payment_id}\n"
                f"HTTP Status: {response.status_code}\n"
                f"Razorpay Error: {message}"
            ),
            title="Club100 Razorpay Payment Verification",
        )

        frappe.throw(
            "Unable to verify Razorpay payment."
        )

    payment = response.json()

    amount_paise = _rupees_to_paise(
        enrollment.amount_payable
    )

    if str(
        payment.get("order_id") or ""
    ) != stored_order_id:
        frappe.throw(
            "Razorpay payment is linked to a different order."
        )

    if int(
        payment.get("amount") or 0
    ) != amount_paise:
        frappe.throw(
            "Razorpay payment amount does not match the enrollment."
        )

    if payment.get("currency") != "INR":
        frappe.throw(
            "Razorpay payment currency does not match the enrollment."
        )

    payment_status = str(
        payment.get("status") or ""
    ).lower()

    #
    # Normally Auto Capture should be enabled in Razorpay.
    # If a valid payment is only authorised, capture it explicitly so
    # Club100 never activates a membership on an uncaptured payment.
    #
    if payment_status == "authorized":
        try:
            capture_response = requests.post(
                f"{RAZORPAY_API_BASE}/payments/{payment_id}/capture",
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                },
                auth=(
                    config["key_id"],
                    config["key_secret"],
                ),
                timeout=20,
            )
        except requests.RequestException:
            frappe.log_error(
                frappe.get_traceback(),
                "Club100 Razorpay Payment Capture",
            )
            frappe.throw(
                "Payment was authorised but could not be captured."
            )

        if not capture_response.ok:
            message = _razorpay_error_message(
                capture_response
            )

            frappe.log_error(
                message=(
                    f"Enrollment: {enrollment.name}\n"
                    f"Payment: {payment_id}\n"
                    f"Razorpay Error: {message}"
                ),
                title="Club100 Razorpay Payment Capture",
            )

            frappe.throw(
                "Payment was authorised but could not be captured."
            )

        payment = capture_response.json()

        payment_status = str(
            payment.get("status") or ""
        ).lower()

    if payment_status != "captured":
        frappe.throw(
            "Razorpay payment has not been captured yet."
        )

    enrollment.payment_gateway = "Razorpay"
    enrollment.payment_id = payment_id
    enrollment.payment_date = now_datetime()
    enrollment.payment_status = "Paid"

    #
    # Payment and membership activation are committed together.
    #
    activation = (
        _finalize_paid_enrollment_doc(
            enrollment
        )
    )

    frappe.db.commit()

    return {
        "success": True,
        "reused": False,
        "enrollment": enrollment.name,
        "payment_id": enrollment.payment_id,
        "payment_status": enrollment.payment_status,
        "status": enrollment.status,
        "amount_paid": enrollment.amount_payable,
        "currency": "INR",
        **activation,
    }


@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def razorpay_webhook(**kwargs):
    """
    Razorpay server-to-server webhook endpoint.

    Subscribe only to:
        payment.captured

    Security:
    - verifies X-Razorpay-Signature against the exact raw request body
    - uses a separate razorpay_webhook_secret
    - trusts ERP for the expected order and amount
    - is idempotent for webhook retries / duplicate deliveries

    Browser verify_payment() remains in place for the immediate customer
    experience. This webhook is the recovery / automation path when the
    browser closes, loses connectivity or does not complete verification.
    """

    raw_body = frappe.request.get_data(
        cache=True,
        as_text=False,
    )

    if not raw_body:
        frappe.throw(
            "Empty Razorpay webhook payload."
        )

    signature = str(
        frappe.request.headers.get(
            "X-Razorpay-Signature"
        )
        or ""
    ).strip()

    if not signature:
        frappe.throw(
            "Missing Razorpay webhook signature.",
            frappe.AuthenticationError,
        )

    webhook_secret = (
        _get_razorpay_webhook_secret()
    )

    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        signature,
    ):
        frappe.throw(
            "Invalid Razorpay webhook signature.",
            frappe.AuthenticationError,
        )

    try:
        event_payload = json.loads(
            raw_body.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        frappe.throw(
            "Invalid Razorpay webhook payload."
        )

    event_name = str(
        event_payload.get("event") or ""
    ).strip()

    event_id = str(
        frappe.request.headers.get(
            "X-Razorpay-Event-Id"
        )
        or ""
    ).strip()

    #
    # Club100 deliberately subscribes only to payment.captured.
    # Returning 2xx for unrelated valid Razorpay events prevents
    # needless retries if another event is accidentally enabled.
    #
    if event_name != "payment.captured":
        return {
            "success": True,
            "ignored": True,
            "event": event_name,
            "event_id": event_id or None,
            "reason": "unsupported_event",
        }

    payment = (
        event_payload
        .get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    payment_id = str(
        payment.get("id") or ""
    ).strip()

    order_id = str(
        payment.get("order_id") or ""
    ).strip()

    payment_status = str(
        payment.get("status") or ""
    ).strip().lower()

    currency = str(
        payment.get("currency") or ""
    ).strip().upper()

    try:
        amount_paise = int(
            payment.get("amount") or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        amount_paise = 0

    if (
        not payment_id
        or not order_id
    ):
        frappe.throw(
            "Razorpay captured-payment payload is incomplete."
        )

    if payment_status != "captured":
        frappe.throw(
            "Razorpay webhook payment is not captured."
        )

    if currency != "INR":
        frappe.throw(
            "Razorpay webhook currency is not INR."
        )

    enrollment_name = frappe.db.get_value(
        "Club100 Enrollment",
        {
            "payment_order_id": order_id,
        },
        "name",
    )

    if not enrollment_name:
        #
        # This is a genuine signed Razorpay event but it does not belong
        # to an order known to Club100. Log it, acknowledge it and do not
        # retry forever.
        #
        frappe.log_error(
            message=(
                f"Event: {event_name}\n"
                f"Event ID: {event_id}\n"
                f"Order: {order_id}\n"
                f"Payment: {payment_id}"
            ),
            title="Club100 Razorpay Webhook - Unknown Order",
        )

        return {
            "success": True,
            "ignored": True,
            "event": event_name,
            "event_id": event_id or None,
            "order_id": order_id,
            "reason": "unknown_order",
        }

    frappe.db.sql(
        """
        SELECT name
        FROM `tabClub100 Enrollment`
        WHERE name = %s
        FOR UPDATE
        """,
        enrollment_name,
    )

    enrollment = frappe.get_doc(
        "Club100 Enrollment",
        enrollment_name,
    )

    stored_order_id = str(
        enrollment.payment_order_id or ""
    ).strip()

    if stored_order_id != order_id:
        frappe.throw(
            "Razorpay webhook order does not match the enrollment."
        )

    expected_amount = _rupees_to_paise(
        enrollment.amount_payable
    )

    if amount_paise != expected_amount:
        frappe.throw(
            "Razorpay webhook amount does not match the enrollment."
        )

    #
    # Duplicate deliveries are normal. Also, the browser verification
    # can reach ERP before the webhook. In both cases simply ensure the
    # membership is finalized and return 2xx.
    #
    if enrollment.payment_status == "Paid":
        if (
            enrollment.payment_id
            and enrollment.payment_id
            != payment_id
        ):
            frappe.log_error(
                message=(
                    f"Enrollment: {enrollment.name}\n"
                    f"Stored Payment: {enrollment.payment_id}\n"
                    f"Webhook Payment: {payment_id}\n"
                    f"Order: {order_id}\n"
                    f"Event ID: {event_id}"
                ),
                title=(
                    "Club100 Razorpay Webhook - "
                    "Different Payment on Paid Enrollment"
                ),
            )

            return {
                "success": True,
                "ignored": True,
                "event": event_name,
                "event_id": event_id or None,
                "enrollment": enrollment.name,
                "reason":
                    "already_paid_with_different_payment",
            }

        activation = (
            _finalize_paid_enrollment_doc(
                enrollment
            )
        )

        frappe.db.commit()

        return {
            "success": True,
            "reused": True,
            "source": "webhook",
            "event": event_name,
            "event_id": event_id or None,
            "enrollment": enrollment.name,
            "payment_id":
                enrollment.payment_id
                or payment_id,
            "payment_status":
                enrollment.payment_status,
            "status":
                enrollment.status,
            **activation,
        }

    if enrollment.payment_status not in (
        "Pending",
        None,
        "",
    ):
        frappe.throw(
            f"Enrollment payment status is "
            f"{enrollment.payment_status}; expected Pending."
        )

    enrollment.payment_gateway = "Razorpay"
    enrollment.payment_id = payment_id
    enrollment.payment_date = (
        now_datetime()
    )
    enrollment.payment_status = "Paid"

    activation = (
        _finalize_paid_enrollment_doc(
            enrollment
        )
    )

    frappe.db.commit()

    return {
        "success": True,
        "reused": False,
        "source": "webhook",
        "event": event_name,
        "event_id": event_id or None,
        "enrollment": enrollment.name,
        "payment_id": enrollment.payment_id,
        "payment_status":
            enrollment.payment_status,
        "status": enrollment.status,
        "amount_paid":
            enrollment.amount_payable,
        "currency": "INR",
        **activation,
    }

