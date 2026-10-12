import re

import frappe
from frappe.utils import add_days, add_to_date, cint, get_datetime, now_datetime, nowdate

from club100_core.society.service import (
    clean_text,
    update_society_interest_counts,
)


SOCIETY_PRICING_MODEL = "Society Batch"
SOCIETY_MEMBERSHIP_DURATION_MONTHS = 1
SOCIETY_PENDING_RESERVATION_MINUTES = 30


def clean_email(value):
    value = str(value or "").strip().lower()

    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        value,
    ):
        frappe.throw(
            "Please enter a valid email address."
        )

    return value


def get_interest_by_invite_token(
    invite_token,
    lock=False,
):
    invite_token = clean_text(
        invite_token
    )

    if not invite_token:
        frappe.throw(
            "Society enrollment invitation is required."
        )

    interest_name = frappe.db.get_value(
        "Club100 Society Interest",
        {
            "invite_token": invite_token,
        },
        "name",
    )

    if not interest_name:
        frappe.throw(
            "This Society enrollment invitation is invalid."
        )

    if lock:
        frappe.db.sql(
            """
            SELECT name
            FROM `tabClub100 Society Interest`
            WHERE name = %s
            FOR UPDATE
            """,
            interest_name,
        )

    interest = frappe.get_doc(
        "Club100 Society Interest",
        interest_name,
    )

    validate_interest_invite(
        interest
    )

    return interest


def validate_interest_invite(
    interest,
):
    if interest.status == "Not Interested":
        frappe.throw(
            "This Society registration is no longer active."
        )

    if interest.status == "Converted":
        frappe.throw(
            "This Society registration has already been converted "
            "to a Club100 enrollment."
        )

    if not interest.society:
        frappe.throw(
            "This Society registration is not linked to a Club100 Society."
        )

    if not interest.invite_token:
        frappe.throw(
            "This Society registration does not have an active invitation."
        )

    if (
        interest.invite_expires_on
        and get_datetime(
            interest.invite_expires_on
        ) < now_datetime()
    ):
        frappe.throw(
            "This Society enrollment invitation has expired. "
            "Please contact Club100 for a new invitation."
        )


def get_society_cohort(
    cohort_name,
    lock=False,
):
    cohort_name = clean_text(
        cohort_name
    )

    if not cohort_name:
        frappe.throw(
            "Society batch is required."
        )

    if not frappe.db.exists(
        "Club100 Cohort",
        cohort_name,
    ):
        frappe.throw(
            "Selected Society batch was not found."
        )

    if lock:
        frappe.db.sql(
            """
            SELECT name
            FROM `tabClub100 Cohort`
            WHERE name = %s
            FOR UPDATE
            """,
            cohort_name,
        )

    return frappe.get_doc(
        "Club100 Cohort",
        cohort_name,
    )


def validate_society_cohort(
    interest,
    cohort,
):
    if cohort.status != "Open":
        frappe.throw(
            "This Society batch is not currently open for enrollment."
        )

    if cohort.delivery_mode != "Onsite":
        frappe.throw(
            "Society enrollment is available only for onsite batches."
        )

    if not cint(
        cohort.society_pricing_enabled
    ):
        frappe.throw(
            "Society pricing is not enabled for this batch."
        )

    if not cohort.society:
        frappe.throw(
            "This batch is not linked to a Club100 Society."
        )

    if cohort.society != interest.society:
        frappe.throw(
            "This batch does not belong to your registered Society."
        )

    if cohort.plan:
        frappe.throw(
            "Society onsite batches cannot use an online membership Plan."
        )

    program_code = frappe.db.get_value(
        "Club100 Program",
        cohort.program,
        "program_code",
    )

    if program_code != "general-fitness":
        frappe.throw(
            "Society enrollment is currently available only for "
            "the General Fitness program."
        )

    pricing_member_count = cint(
        cohort.pricing_member_count
    )

    monthly_price = cint(
        cohort.monthly_price_per_member
    )

    if pricing_member_count < 5:
        frappe.throw(
            "This Society batch does not yet have a valid pricing member count."
        )

    if monthly_price <= 0:
        frappe.throw(
            "This Society batch does not yet have a valid member price."
        )

    if not cohort.pricing_tier:
        frappe.throw(
            "This Society batch does not yet have a pricing tier."
        )

    if (
        interest.invited_cohort
        and interest.invited_cohort
        != cohort.name
    ):
        frappe.throw(
            "This invitation is for a different Society batch."
        )

    return {
        "pricing_member_count":
            pricing_member_count,
        "pricing_tier":
            cohort.pricing_tier,
        "monthly_price_per_member":
            monthly_price,
    }


def get_batch_seat_limit(
    cohort,
):
    """
    Society pricing is based on the committed batch size.

    Therefore the current enrollment round cannot allocate more seats
    than Pricing Member Count, even if operational Cohort Capacity is higher.
    """

    capacity = cint(
        cohort.capacity
    )

    pricing_member_count = cint(
        cohort.pricing_member_count
    )

    if capacity <= 0:
        frappe.throw(
            "Society batch capacity is invalid."
        )

    if pricing_member_count <= 0:
        frappe.throw(
            "Society batch pricing member count is invalid."
        )

    return min(
        capacity,
        pricing_member_count,
    )


def get_society_pending_reservation_expiry(
    enrollment,
):
    """
    Pending Society enrollments reserve a batch seat for a limited time.

    No additional DocType field is required: the reservation window is
    derived from the Enrollment creation timestamp.
    """
    return add_to_date(
        get_datetime(
            enrollment.creation
        ),
        minutes=
            SOCIETY_PENDING_RESERVATION_MINUTES,
    )


def get_society_pending_reservation_seconds_remaining(
    enrollment,
):
    expires_on = (
        get_society_pending_reservation_expiry(
            enrollment
        )
    )

    remaining = int(
        (
            expires_on
            - now_datetime()
        ).total_seconds()
    )

    return max(
        remaining,
        0,
    )


def is_society_pending_reservation_active(
    enrollment,
):
    return (
        getattr(
            enrollment,
            "pricing_model",
            "",
        )
        == SOCIETY_PRICING_MODEL
        and enrollment.status
        == "Registered"
        and enrollment.payment_status
        in (
            "Pending",
            None,
            "",
        )
        and
        get_society_pending_reservation_seconds_remaining(
            enrollment
        )
        > 0
    )


def validate_society_pending_reservation(
    enrollment,
):
    """
    Called immediately before creating/reusing a Razorpay order.

    A stale Society enrollment must not reopen payment after its seat has
    already been released back to the batch.
    """
    if (
        getattr(
            enrollment,
            "pricing_model",
            "",
        )
        != SOCIETY_PRICING_MODEL
    ):
        return

    if not is_society_pending_reservation_active(
        enrollment
    ):
        frappe.throw(
            "Your Society seat reservation has expired. "
            "Please continue to payment again to refresh your seat."
        )


def count_occupied_society_seats(
    cohort_name,
    exclude_enrollment=None,
):
    """
    Count seats that are genuinely occupied.

    Active / On Hold members always occupy a seat.

    A Registered + Pending Society enrollment occupies a seat only during
    its short payment reservation window. This prevents abandoned payment
    attempts from blocking Society batches indefinitely.

    A rare Registered + Paid record is also counted defensively until the
    normal paid-enrollment finalization moves it to Active.
    """
    rows = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "cohort": cohort_name,
            "status": [
                "in",
                [
                    "Registered",
                    "Active",
                    "On Hold",
                ],
            ],
        },
        fields=[
            "name",
            "pricing_model",
            "status",
            "payment_status",
            "creation",
        ],
    )

    occupied = 0

    for row in rows:
        if (
            exclude_enrollment
            and row.name
            == exclude_enrollment
        ):
            continue

        if row.status in (
            "Active",
            "On Hold",
        ):
            occupied += 1
            continue

        if row.status != "Registered":
            continue

        if row.payment_status == "Paid":
            occupied += 1
            continue

        if is_society_pending_reservation_active(
            row
        ):
            occupied += 1

    return occupied


def assert_society_seat_available(
    cohort,
    exclude_enrollment=None,
):
    seat_limit = get_batch_seat_limit(
        cohort
    )

    occupied = count_occupied_society_seats(
        cohort.name,
        exclude_enrollment=exclude_enrollment,
    )

    if occupied >= seat_limit:
        frappe.throw(
            f"This Society batch is full "
            f"({seat_limit} members for the current pricing round)."
        )

    return {
        "seat_limit": seat_limit,
        "occupied": occupied,
        "available": seat_limit - occupied,
    }


def get_invite_context(
    invite_token,
):
    interest = get_interest_by_invite_token(
        invite_token
    )

    cohort_filters = {
        "society": interest.society,
        "delivery_mode": "Onsite",
        "society_pricing_enabled": 1,
        "status": "Open",
    }

    if interest.invited_cohort:
        cohort_filters["name"] = (
            interest.invited_cohort
        )

    cohort_rows = frappe.get_all(
        "Club100 Cohort",
        filters=cohort_filters,
        fields=[
            "name",
            "cohort_name",
            "program",
            "fitness_level",
            "capacity",
            "pricing_member_count",
            "pricing_tier",
            "monthly_price_per_member",
            "start_date",
            "status",
        ],
        order_by="start_date asc, cohort_name asc",
    )

    cohorts = []

    for row in cohort_rows:
        program_code = frappe.db.get_value(
            "Club100 Program",
            row.program,
            "program_code",
        )

        if program_code != "general-fitness":
            continue

        seat_limit = min(
            cint(row.capacity),
            cint(row.pricing_member_count),
        )

        occupied = count_occupied_society_seats(
            row.name
        )

        cohorts.append({
            "cohort": row.name,
            "cohort_name": row.cohort_name,
            "fitness_level": row.fitness_level,
            "start_date": (
                str(row.start_date)
                if row.start_date
                else None
            ),
            "pricing_member_count":
                cint(row.pricing_member_count),
            "pricing_tier":
                row.pricing_tier,
            "monthly_price_per_member":
                cint(row.monthly_price_per_member),
            "seat_limit":
                seat_limit,
            "available_seats":
                max(
                    seat_limit - occupied,
                    0,
                ),
        })

    society = frappe.db.get_value(
        "Club100 Society",
        interest.society,
        [
            "name",
            "society_name",
            "city_area",
        ],
        as_dict=True,
    )

    return {
        "interest": interest.name,
        "full_name": interest.full_name,
        "mobile": interest.mobile,
        "email": interest.email or "",
        "society": (
            society.name
            if society
            else interest.society
        ),
        "society_name": (
            society.society_name
            if society
            else interest.society_name
        ),
        "city_area": (
            society.city_area
            if society
            else interest.city_area
        ),
        "membership_duration_months":
            SOCIETY_MEMBERSHIP_DURATION_MONTHS,
        "cohorts": cohorts,
    }


def create_pending_society_enrollment(
    invite_token,
    cohort_name,
    email=None,
):
    """
    Create/reuse a one-month pending Society enrollment.

    The batch price is snapshotted onto Club100 Enrollment. Future changes
    to the Cohort price do not alter this pending/paid enrollment.
    """

    interest = get_interest_by_invite_token(
        invite_token,
        lock=True,
    )

    cohort = get_society_cohort(
        cohort_name,
        lock=True,
    )

    pricing = validate_society_cohort(
        interest,
        cohort,
    )

    email = clean_email(
        email or interest.email
    )

    # Reuse an existing unpaid enrollment for this exact Interest + Cohort.
    existing = frappe.get_all(
        "Club100 Enrollment",
        filters={
            "society_interest":
                interest.name,
            "cohort":
                cohort.name,
            "pricing_model":
                SOCIETY_PRICING_MODEL,
            "payment_status":
                "Pending",
            "status":
                "Registered",
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

        if is_society_pending_reservation_active(
            enrollment
        ):
            return _pending_enrollment_response(
                enrollment,
                reused=True,
            )

        #
        # The old record remains untouched for payment/audit recovery.
        # It no longer occupies a seat, and a fresh pending enrollment
        # can safely be created for this Interest + Cohort.
        #

    # Prevent a paid/active duplicate before collecting another payment.
    member_name = frappe.db.get_value(
        "Club100 Member",
        {
            "mobile": interest.mobile,
        },
        "name",
    )

    if member_name:
        active = frappe.db.exists(
            "Club100 Enrollment",
            {
                "member": member_name,
                "program": cohort.program,
                "status": [
                    "in",
                    [
                        "Registered",
                        "Active",
                        "On Hold",
                    ],
                ],
            },
        )

        if active:
            frappe.throw(
                "You already have an active Club100 enrollment "
                "for this program."
            )

    assert_society_seat_available(
        cohort
    )

    society = frappe.db.get_value(
        "Club100 Society",
        interest.society,
        [
            "organization",
        ],
        as_dict=True,
    )

    enrollment = frappe.get_doc({
        "doctype":
            "Club100 Enrollment",

        "member":
            None,

        "full_name":
            interest.full_name,
        "mobile":
            interest.mobile,
        "email":
            email,

        "pricing_model":
            SOCIETY_PRICING_MODEL,

        "program":
            cohort.program,

        "plan":
            None,

        "membership_duration_months":
            SOCIETY_MEMBERSHIP_DURATION_MONTHS,

        "monthly_equivalent":
            pricing[
                "monthly_price_per_member"
            ],

        "amount_payable":
            pricing[
                "monthly_price_per_member"
            ],

        "society_pricing_tier_snapshot":
            pricing[
                "pricing_tier"
            ],

        "society_pricing_member_count_snapshot":
            pricing[
                "pricing_member_count"
            ],

        "pricing_snapshot_on":
            now_datetime(),

        "cohort":
            cohort.name,

        "society":
            interest.society,

        "society_interest":
            interest.name,

        "organization": (
            society.organization
            if society
            else None
        ),

        "enrollment_type":
            "Individual",

        "enrollment_date":
            nowdate(),

        "status":
            "Registered",

        "payment_status":
            "Pending",

        "website_source":
            "Society Website",
    })

    enrollment.insert(
        ignore_permissions=True
    )

    # Keep the best email captured during enrollment.
    if (
        email
        and email != (interest.email or "")
    ):
        frappe.db.set_value(
            "Club100 Society Interest",
            interest.name,
            "email",
            email,
            update_modified=False,
        )

    frappe.db.commit()

    return _pending_enrollment_response(
        enrollment,
        reused=False,
    )


def _pending_enrollment_response(
    enrollment,
    reused,
):
    reservation_expires_on = (
        get_society_pending_reservation_expiry(
            enrollment
        )
    )

    reservation_seconds_remaining = (
        get_society_pending_reservation_seconds_remaining(
            enrollment
        )
    )

    return {
        "success": True,
        "reused": bool(reused),
        "enrollment": enrollment.name,
        "pricing_model":
            enrollment.pricing_model,
        "program":
            enrollment.program,
        "cohort":
            enrollment.cohort,
        "society":
            enrollment.society,
        "duration_months":
            cint(
                enrollment.membership_duration_months
            ),
        "pricing_tier":
            enrollment.society_pricing_tier_snapshot,
        "pricing_member_count":
            cint(
                enrollment.society_pricing_member_count_snapshot
            ),
        "monthly_equivalent":
            enrollment.monthly_equivalent,
        "amount_payable":
            enrollment.amount_payable,
        "status":
            enrollment.status,
        "payment_status":
            enrollment.payment_status,
        "reservation_minutes":
            SOCIETY_PENDING_RESERVATION_MINUTES,
        "reservation_expires_on":
            str(
                reservation_expires_on
            ),
        "reservation_seconds_remaining":
            reservation_seconds_remaining,
    }


def issue_invite(
    interest_id,
    cohort_name=None,
    validity_days=30,
):
    """
    Issue/reissue a secure Society enrollment invitation.

    This is an authenticated/admin operation. Public enrollment uses only
    the opaque invite token.
    """

    interest_id = clean_text(
        interest_id
    )

    if not interest_id:
        frappe.throw(
            "Society Interest is required."
        )

    if not frappe.db.exists(
        "Club100 Society Interest",
        interest_id,
    ):
        frappe.throw(
            "Club100 Society Interest was not found."
        )

    interest = frappe.get_doc(
        "Club100 Society Interest",
        interest_id,
    )

    if not interest.has_permission(
        "write"
    ):
        frappe.throw(
            "You do not have permission to invite this resident.",
            frappe.PermissionError,
        )

    if interest.status in (
        "Converted",
        "Not Interested",
    ):
        frappe.throw(
            "This Society Interest cannot be invited in its current status."
        )

    if not interest.society:
        frappe.throw(
            "Society Interest must be linked to a Club100 Society first."
        )

    cohort = None

    if cohort_name:
        cohort = get_society_cohort(
            cohort_name
        )

        validate_society_cohort(
            interest,
            cohort,
        )

    validity_days = cint(
        validity_days
    )

    if validity_days <= 0:
        validity_days = 30

    token = frappe.generate_hash(
        length=48
    )

    interest.invite_token = token
    interest.invite_created_on = (
        now_datetime()
    )
    interest.invite_expires_on = (
        add_days(
            now_datetime(),
            validity_days,
        )
    )
    interest.invited_cohort = (
        cohort.name
        if cohort
        else None
    )
    interest.status = "Invited"

    interest.save(
        ignore_permissions=True
    )

    update_society_interest_counts(
        interest.society
    )

    frappe.db.commit()

    return {
        "success": True,
        "interest": interest.name,
        "society": interest.society,
        "cohort": interest.invited_cohort,
        "invite_token": token,
        "invite_expires_on":
            str(
                interest.invite_expires_on
            ),
    }


def finalize_paid_society_enrollment(
    enrollment,
    member,
):
    """
    Complete Society-specific post-payment linking.

    The generic payment code remains responsible for Razorpay verification
    and activating the Club100 Enrollment/Member.
    """

    if (
        enrollment.pricing_model
        != SOCIETY_PRICING_MODEL
    ):
        return

    if not enrollment.society_interest:
        frappe.throw(
            "Society enrollment is missing the source Society Interest."
        )

    interest = frappe.get_doc(
        "Club100 Society Interest",
        enrollment.society_interest,
    )

    if (
        interest.society
        and enrollment.society
        and interest.society
        != enrollment.society
    ):
        frappe.throw(
            "Society enrollment does not match the source Society Interest."
        )

    # Idempotent for browser verification + Razorpay webhook retries.
    if (
        interest.status == "Converted"
        and interest.enrollment
        and interest.enrollment
        != enrollment.name
    ):
        frappe.throw(
            "This Society Interest has already been converted "
            "to a different enrollment."
        )

    interest.status = "Converted"
    interest.member = member.name
    interest.enrollment = enrollment.name
    interest.converted_on = (
        interest.converted_on
        or now_datetime()
    )

    if not interest.invited_cohort:
        interest.invited_cohort = (
            enrollment.cohort
        )

    interest.save(
        ignore_permissions=True
    )

    # If the Society is linked to an Organization, preserve that
    # relationship on the member as well.
    if enrollment.organization:
        if not member.organization:
            member.organization = (
                enrollment.organization
            )

        # Only switch the Member type to Society when Organization exists,
        # so installations with organization-required validation remain safe.
        member.member_type = "Society"

        member.save(
            ignore_permissions=True
        )

    update_society_interest_counts(
        enrollment.society
    )
