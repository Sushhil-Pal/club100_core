import frappe

from frappe.auth import LoginManager
from frappe.sessions import get_csrf_token
from frappe.utils import now_datetime, today, add_to_date
from frappe.utils.password import update_password

import secrets



# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _split_name(full_name):
    full_name = (full_name or "").strip()

    if not full_name:
        frappe.throw("Full name is required")

    parts = full_name.split(maxsplit=1)

    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    return first_name, last_name


def _validate_password(password):
    if not password:
        frappe.throw("Password is required")

    if len(password) < 8:
        frappe.throw(
            "Password must be at least 8 characters long"
        )


def _internal_user_id(mobile):
    """
    Internal Frappe User ID.

    Members never need to know this value.
    Their Club100 login remains mobile + password.
    """
    return f"{mobile}@members.club100.fit".lower()


def _create_member_user(
    mobile,
    first_name,
    last_name,
):
    user_id = _internal_user_id(mobile)

    # Reuse if an orphan/internal user already exists.
    if frappe.db.exists("User", user_id):
        user = frappe.get_doc(
            "User",
            user_id,
        )

        if not user.enabled:
            user.enabled = 1
            user.save(ignore_permissions=True)

        return user

    user = frappe.get_doc(
        {
            "doctype": "User",

            # Frappe User identity is email-shaped.
            "email": user_id,

            "first_name": first_name,
            "last_name": last_name,

            "enabled": 1,

            # Member does not need Desk access.
            "user_type": "Website User",

            # We control onboarding ourselves.
            "send_welcome_email": 0,
        }
    )

    user.insert(ignore_permissions=True)

    return user


def _login_user(user_id, password):
    login_manager = LoginManager()

    login_manager.authenticate(
        user=user_id,
        pwd=password,
    )

    login_manager.post_login()


def _get_member_for_user(user):
    if not user or user == "Guest":
        return None

    return frappe.db.get_value(
        "Club100 Member",
        {
            "user": user,
            "status": "Active",
            "app_access_status": "Active",
        },
        [
            "name",
            "full_name",
            "onboarding_status",
        ],
        as_dict=True,
    )


def _get_trainer_for_user(user):
    if not user or user == "Guest":
        return None

    return frappe.db.get_value(
        "Club100 Trainer",
        {
            "user": user,
            "status": "Active",
        },
        [
            "name",
            "trainer_name",
            "trainer_type",
        ],
        as_dict=True,
    )


def _get_app_user_context(user=None):

    if user is None:
        user = frappe.session.user

    if not user or user == "Guest":
        return {
            "user": None,
            "roles": [],
            "member": None,
            "trainer": None,
        }

    member = _get_member_for_user(user)
    trainer = _get_trainer_for_user(user)

    roles = []

    if member:
        roles.append("member")

    if trainer:
        roles.append("trainer")

    return {
        "user": user,
        "roles": roles,

        "member": (
            {
                "id": member.name,
                "fullName": member.full_name,
                "onboardingStatus":
                    member.onboarding_status
                    or "Not Started",
            }
            if member
            else None
        ),

        "trainer": (
            {
                "id": trainer.name,
                "trainerName": trainer.trainer_name,
                "trainerType": trainer.trainer_type,
            }
            if trainer
            else None
        ),
    }

# ---------------------------------------------------------
# CSRF
# ---------------------------------------------------------

@frappe.whitelist(
    allow_guest=True,
    methods=["GET"],
)
def csrf_token():
    return {
        "csrfToken": get_csrf_token(),
    }


# ---------------------------------------------------------
# Registration
# ---------------------------------------------------------

@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def register(
    full_name,
    mobile,
    password,
    email=None,
):
    full_name = (full_name or "").strip()
    mobile = (mobile or "").strip()
    email = (email or "").strip()

    if not mobile:
        frappe.throw(
            "Mobile number is required"
        )

    _validate_password(password)

    first_name, last_name = _split_name(
        full_name
    )

    # -----------------------------------------------------
    # Look for an existing Club100 Member
    # -----------------------------------------------------

    member_name = frappe.db.get_value(
        "Club100 Member",
        {
            "mobile": mobile,
        },
        "name",
    )

    if member_name:
        member = frappe.get_doc(
            "Club100 Member",
            member_name,
        )

        # Existing member must still be operationally active.
        if member.status != "Active":
            frappe.throw(
                "This Club100 membership is not currently active."
            )

        app_status = (
            member.app_access_status
            or "First Time"
        )

        if app_status == "Disabled":
            frappe.throw(
                "App access is disabled for this membership."
            )

        if app_status == "Active":
            frappe.throw(
                "This mobile number is already registered. "
                "Please sign in or use Forgot Password."
            )

        if app_status != "First Time":
            frappe.throw(
                "This membership cannot be registered for app access."
            )

        # -------------------------------------------------
        # Existing member: create/link Frappe User if needed
        # -------------------------------------------------

        user = None

        if (
            member.user
            and frappe.db.exists(
                "User",
                member.user,
            )
        ):
            user = frappe.get_doc(
                "User",
                member.user,
            )

            if not user.enabled:
                user.enabled = 1
                user.save(
                    ignore_permissions=True
                )

        else:
            user = _create_member_user(
                mobile,
                member.first_name
                or first_name,
                member.last_name
                or last_name,
            )

            member.user = user.name

        # -------------------------------------------------
        # Update member profile from registration form
        # -------------------------------------------------

        member.first_name = first_name
        member.last_name = last_name
        member.mobile = mobile

        if email:
            member.email = email

        user.first_name = first_name
        user.last_name = last_name
        user.save(ignore_permissions=True)   

        # Set the user's selected password.
        update_password(
            user=user.name,
            pwd=password,
        )

        member.app_access_status = "Active"

        if not member.first_app_access_on:
            member.first_app_access_on = (
                now_datetime()
            )

        member.save(
            ignore_permissions=True
        )

        # Authenticate immediately after registration.
        _login_user(
            user.name,
            password,
        )

        context = _get_app_user_context(
            frappe.session.user
        )

        return {
            "success": True,
            "registrationType": "ExistingMember",
            **context,
        }

    # -----------------------------------------------------
    # No member exists: direct registration
    # -----------------------------------------------------

    user = _create_member_user(
        mobile,
        first_name,
        last_name,
    )

    update_password(
        user=user.name,
        pwd=password,
    )

    member = frappe.get_doc(
        {
            "doctype": "Club100 Member",

            "first_name": first_name,
            "last_name": last_name,

            "mobile": mobile,
            "email": email,

            "member_type": "Individual",

            "user": user.name,

            "joining_date": today(),

            "status": "Active",

            "app_access_status": "Active",

            "first_app_access_on":
                now_datetime(),
        }
    )

    member.insert(
        ignore_permissions=True
    )

    _login_user(
        user.name,
        password,
    )

    context = _get_app_user_context(
        frappe.session.user
    )

    return {
        "success": True,
        "registrationType": "NewMember",
        **context,
    }


# ---------------------------------------------------------
# Login
# ---------------------------------------------------------

@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def login(
    password,
    login_id=None,
    mobile=None,
):
    login_id = (
        login_id
        or mobile
        or ""
    ).strip()

    if not login_id or not password:
        frappe.throw(
            "Login ID and password are required",
            frappe.ValidationError,
        )

    user_id = None

    # -----------------------------------------------------
    # 1. Try Club100 Member login by mobile
    # -----------------------------------------------------

    member = frappe.db.get_value(
        "Club100 Member",
        {
            "mobile": login_id,
            "status": "Active",
        },
        [
            "name",
            "user",
            "app_access_status",
        ],
        as_dict=True,
    )

    if (
        member
        and member.user
        and member.app_access_status == "Active"
    ):
        user_id = member.user

    # -----------------------------------------------------
    # 2. Try Club100 Trainer login
    # -----------------------------------------------------

    if not user_id:
        trainer = frappe.db.get_value(
            "Club100 Trainer",
            {
                "mobile": login_id,
                "status": "Active",
            },
            [
                "name",
                "user",
            ],
            as_dict=True,
        )

        if not trainer:
            trainer = frappe.db.get_value(
                "Club100 Trainer",
                {
                    "email": login_id,
                    "status": "Active",
                },
                [
                    "name",
                    "user",
                ],
                as_dict=True,
            )

        if trainer and trainer.user:
            user_id = trainer.user

    if not user_id:
        frappe.throw(
            "Invalid login ID or password",
            frappe.AuthenticationError,
        )

    try:
        _login_user(
            user_id,
            password,
        )

    except frappe.AuthenticationError:
        frappe.throw(
            "Invalid login ID or password",
            frappe.AuthenticationError,
        )

    context = _get_app_user_context(
        frappe.session.user
    )

    if not context["roles"]:
        frappe.local.login_manager.logout()

        frappe.throw(
            "This account does not have Club100 app access.",
            frappe.AuthenticationError,
        )

    return {
        "success": True,
        **context,
    }


# ---------------------------------------------------------
# Session status
# ---------------------------------------------------------

@frappe.whitelist(
    allow_guest=True,
    methods=["GET"],
)
def session_status():
    user = frappe.session.user

    if user == "Guest":
        return {
            "authenticated": False,
            "user": None,
            "roles": [],
            "member": None,
            "trainer": None,
        }

    context = _get_app_user_context(user)

    return {
        "authenticated": True,
        **context,
    }


# ---------------------------------------------------------
# Logout
# ---------------------------------------------------------

@frappe.whitelist(
    methods=["POST"],
)
def logout():
    if frappe.session.user != "Guest":
        frappe.local.login_manager.logout()

    return {
        "success": True,
    }

def _get_app_base_url():
    """
    Development fallback.

    In production we'll set:
    club100_app_url = https://app.club100.fit
    """
    return (
        frappe.conf.get("club100_app_url")
        or "http://club100.local:5173"
    ).rstrip("/")


def _generate_reset_token():
    return secrets.token_urlsafe(32)

@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def request_password_reset(mobile):
    mobile = (mobile or "").strip()

    if not mobile:
        frappe.throw(
            "Mobile number is required",
            frappe.ValidationError,
        )

    # -----------------------------------------------------
    # Look for member
    # -----------------------------------------------------

    member = frappe.db.get_value(
        "Club100 Member",
        {
            "mobile": mobile,
            "status": "Active",
        },
        [
            "name",
            "user",
            "app_access_status",
        ],
        as_dict=True,
    )

    reset_token = None
    reset_link = None
    expires_at = None

    # -----------------------------------------------------
    # Only generate a usable token for a valid app member
    # -----------------------------------------------------

    if (
        member
        and member.user
        and member.app_access_status == "Active"
    ):
        reset_token = _generate_reset_token()

        expires_at = add_to_date(
            now_datetime(),
            hours=24,
        )

        reset_link = (
            f"{_get_app_base_url()}"
            f"/reset-password/{reset_token}"
        )

    # -----------------------------------------------------
    # Record request regardless
    #
    # This prevents the API response from revealing whether
    # a mobile number exists.
    # -----------------------------------------------------

    request_doc = frappe.get_doc(
        {
            "doctype":
                "Club100 Password Reset Request",

            "member":
                member.name if member else None,

            "mobile":
                mobile,

            "user":
                member.user if member else None,

            "requested_at":
                now_datetime(),

            "status":
                "Requested",

            "reset_token":
                reset_token,

            "reset_link":
                reset_link,

            "expires_at":
                expires_at,
        }
    )

    request_doc.insert(
        ignore_permissions=True
    )

    frappe.db.commit()

    # IMPORTANT:
    # Never tell the public caller whether this mobile exists.
    return {
        "success": True,
        "message":
            "If this mobile number is registered with Club100, "
            "you will receive a password reset link shortly.",
    }


@frappe.whitelist(
    allow_guest=True,
    methods=["POST"],
)
def reset_password(
    token,
    password,
):
    token = (token or "").strip()

    if not token:
        frappe.throw(
            "Invalid password reset link",
            frappe.ValidationError,
        )

    _validate_password(password)

    # -----------------------------------------------------
    # Find reset request
    # -----------------------------------------------------

    request_name = frappe.db.get_value(
        "Club100 Password Reset Request",
        {
            "reset_token": token,
            "status": ["in", ["Requested", "Sent"]],
        },
        "name",
    )

    if not request_name:
        frappe.throw(
            "This password reset link is invalid or has already been used.",
            frappe.ValidationError,
        )

    request_doc = frappe.get_doc(
        "Club100 Password Reset Request",
        request_name,
    )

    # -----------------------------------------------------
    # Validate expiry
    # -----------------------------------------------------

    if (
        request_doc.expires_at
        and now_datetime()
        > request_doc.expires_at
    ):
        request_doc.status = "Expired"
        request_doc.save(
            ignore_permissions=True
        )

        frappe.db.commit()

        frappe.throw(
            "This password reset link has expired.",
            frappe.ValidationError,
        )

    if not request_doc.user:
        frappe.throw(
            "This password reset request is invalid.",
            frappe.ValidationError,
        )

    # -----------------------------------------------------
    # Set new password
    # -----------------------------------------------------

    update_password(
        user=request_doc.user,
        pwd=password,
    )

    # -----------------------------------------------------
    # Complete this request
    # -----------------------------------------------------

    request_doc.status = "Completed"

    request_doc.completed_at = (
        now_datetime()
    )

    # Token is no longer usable.
    request_doc.reset_token = None

    request_doc.save(
        ignore_permissions=True
    )

    # -----------------------------------------------------
    # Cancel any other outstanding requests for same user
    # -----------------------------------------------------

    other_requests = frappe.get_all(
        "Club100 Password Reset Request",
        filters={
            "user": request_doc.user,
            "status": ["in", ["Requested", "Sent"]],
            "name": ["!=", request_doc.name],
        },
        pluck="name",
    )

    for name in other_requests:
        frappe.db.set_value(
            "Club100 Password Reset Request",
            name,
            "status",
            "Cancelled",
        )

    frappe.db.commit()

    return {
        "success": True,
        "message":
            "Your password has been reset successfully.",
    }

# ---------------------------------------------------------
# PWA installation tracking
# ---------------------------------------------------------

@frappe.whitelist(
    methods=["POST"],
)
def mark_pwa_installed():
    user = frappe.session.user

    if not user or user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.AuthenticationError,
        )

    installed_on = now_datetime()

    member_name = frappe.db.get_value(
        "Club100 Member",
        {
            "user": user,
            "status": "Active",
            "app_access_status": "Active",
        },
        "name",
    )

    trainer_name = frappe.db.get_value(
        "Club100 Trainer",
        {
            "user": user,
            "status": "Active",
        },
        "name",
    )

    updated = []

    if member_name:
        member_installed_on = frappe.db.get_value(
            "Club100 Member",
            member_name,
            "pwa_installed_on",
        )

        values = {
            "pwa_installed": 1,
        }

        if not member_installed_on:
            values["pwa_installed_on"] = installed_on

        frappe.db.set_value(
            "Club100 Member",
            member_name,
            values,
            update_modified=False,
        )

        updated.append("member")

    if trainer_name:
        trainer_installed_on = frappe.db.get_value(
            "Club100 Trainer",
            trainer_name,
            "pwa_installed_on",
        )

        values = {
            "pwa_installed": 1,
        }

        if not trainer_installed_on:
            values["pwa_installed_on"] = installed_on

        frappe.db.set_value(
            "Club100 Trainer",
            trainer_name,
            values,
            update_modified=False,
        )

        updated.append("trainer")

    if not updated:
        frappe.throw(
            "This account does not have active Club100 app access.",
            frappe.PermissionError,
        )

    return {
        "success": True,
        "updated": updated,
    }