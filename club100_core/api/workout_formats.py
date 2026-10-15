"""Standard trainer-led workout format preparation metadata."""
import frappe

DEFAULT_FORMATS = {
    "Power": ("Strength training", "Water bottle\nTowel\nTraining shoes\nComfortable workout clothes"),
    "Flow": ("Yoga and mobility", "Yoga mat\nWater bottle\nTowel\nComfortable clothing"),
    "Pulse": ("Music-based cardio", "Water bottle\nTowel\nSports shoes\nComfortable workout clothes"),
    "Play": ("Games and fitness challenges", "Water bottle\nTowel\nSports shoes\nComfortable workout clothes"),
}


def seed_workout_formats():
    """Create missing master records. Never overwrite administrator edits."""
    created = []
    for name, (description, items) in DEFAULT_FORMATS.items():
        if not frappe.db.exists("Club100 Workout Format", name):
            frappe.get_doc({"doctype": "Club100 Workout Format", "format_name": name,
                            "description": description, "default_items_to_bring": items}).insert(ignore_permissions=True)
            created.append(name)
    frappe.db.commit()
    return {"created": created}


def resolve_session_preparation(session):
    fmt = session.workout_format
    if not fmt and session.workout_content:
        fmt = frappe.db.get_value("Club100 Workout Content", session.workout_content, "format")
    defaults = frappe.db.get_value(
        "Club100 Workout Format", fmt,
        ["description", "default_items_to_bring", "preparation_instructions"], as_dict=True
    ) if fmt else None
    # Non-empty override replaces defaults; blank means inherit.
    items = session.items_to_bring_override or (defaults.default_items_to_bring if defaults else "")
    return {"format": fmt, "description": defaults.description if defaults else None,
            "itemsToBring": [line.strip() for line in (items or "").splitlines() if line.strip()],
            "preparationInstructions": defaults.preparation_instructions if defaults else None,
            "additionalInstructions": session.additional_instructions or None,
            "hasOverride": bool(session.items_to_bring_override)}
