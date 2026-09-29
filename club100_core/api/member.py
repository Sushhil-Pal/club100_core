import frappe


def _get_current_member():
    user = frappe.session.user

    if user == "Guest":
        frappe.throw(
            "Authentication required",
            frappe.PermissionError,
        )

    member_name = frappe.db.get_value(
        "Club100 Member",
        {"user": user},
        "name",
    )

    if not member_name:
        frappe.throw(
            "Club100 member profile not found"
        )

    return frappe.get_doc(
        "Club100 Member",
        member_name,
    )


@frappe.whitelist()
def me():
    member = _get_current_member()

    return {
        "id": member.name,
        "fullName": member.full_name,
        "email": member.email,
        "mobile": member.mobile,
        "memberType": member.member_type,
        "organization": member.organization,
        "status": member.status,

        "dateOfBirth": member.date_of_birth,
        "gender": member.gender,

        "fitnessLevel":
            member.current_fitness_level,

        "fitnessGoal":
            member.fitness_goal,

        "preferredDeliveryMode":
            member.preferred_delivery_mode,

        "medicalNotes":
            member.medical_notes,

        "onboardingStatus":
            member.onboarding_status
            or "Not Started",

        "onboardingCompletedOn":
            member.onboarding_completed_on,
    }


@frappe.whitelist(
    methods=["POST"],
)
def update_profile(
    full_name=None,
    email=None,
    mobile=None,
):
    member = _get_current_member()

    if full_name is not None:
        full_name = full_name.strip()

        if not full_name:
            frappe.throw(
                "Full name is required"
            )

        name_parts = full_name.split(
            maxsplit=1
        )

        member.first_name = name_parts[0]

        member.last_name = (
            name_parts[1]
            if len(name_parts) > 1
            else ""
        )

    if email is not None:
        member.email = email.strip()

    if mobile is not None:
        member.mobile = mobile.strip()

    member.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "member": {
            "id": member.name,
            "fullName": member.full_name,
            "email": member.email,
            "mobile": member.mobile,
            "memberType": member.member_type,
            "fitnessLevel": member.current_fitness_level,
            "organization": member.organization,
            "status": member.status,
        },
    }


@frappe.whitelist(methods=["POST"])
def complete_onboarding(
    date_of_birth=None,
    gender=None,
    fitness_goal=None,
    current_fitness_level=None,
    preferred_delivery_mode=None,
    medical_notes=None,
):
    member = _get_current_member()

    # -----------------------------------------------------
    # Basic validation
    # -----------------------------------------------------

    if not date_of_birth:
        frappe.throw(
            "Date of birth is required",
            frappe.ValidationError,
        )

    if not gender:
        frappe.throw(
            "Gender is required",
            frappe.ValidationError,
        )

    if not fitness_goal:
        frappe.throw(
            "Fitness goal is required",
            frappe.ValidationError,
        )

    if not current_fitness_level:
        frappe.throw(
            "Current fitness level is required",
            frappe.ValidationError,
        )

    if not preferred_delivery_mode:
        frappe.throw(
            "Preferred delivery mode is required",
            frappe.ValidationError,
        )

    # -----------------------------------------------------
    # Update onboarding fields
    # -----------------------------------------------------

    member.date_of_birth = date_of_birth
    member.gender = gender

    member.fitness_goal = fitness_goal

    member.current_fitness_level = (
        current_fitness_level
    )

    member.preferred_delivery_mode = (
        preferred_delivery_mode
    )

    member.medical_notes = (
        medical_notes or ""
    )

    member.onboarding_status = "Completed"

    member.onboarding_completed_on = (
        frappe.utils.now_datetime()
    )

    member.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,

        "member": {
            "id": member.name,
            "fullName": member.full_name,

            "dateOfBirth":
                member.date_of_birth,

            "gender":
                member.gender,

            "fitnessLevel":
                member.current_fitness_level,

            "fitnessGoal":
                member.fitness_goal,

            "preferredDeliveryMode":
                member.preferred_delivery_mode,

            "medicalNotes":
                member.medical_notes,

            "onboardingStatus":
                member.onboarding_status,

            "onboardingCompletedOn":
                member.onboarding_completed_on,
        },
    }