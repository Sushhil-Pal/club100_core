from frappe.utils import cint


SOCIETY_PRICING_TIERS = (
    {
        "min_members": 25,
        "max_members": None,
        "tier": "25+",
        "monthly_price_per_member": 1199,
    },
    {
        "min_members": 15,
        "max_members": 24,
        "tier": "15-24",
        "monthly_price_per_member": 1499,
    },
    {
        "min_members": 10,
        "max_members": 14,
        "tier": "10-14",
        "monthly_price_per_member": 1999,
    },
    {
        "min_members": 5,
        "max_members": 9,
        "tier": "5-9",
        "monthly_price_per_member": 2499,
    },
)


def get_society_pricing(member_count):
    """
    Return Society pricing for one onsite batch/cohort.

    Pricing is based on the number of members training in that batch,
    not on total Club100 participation across the residential society.
    """

    member_count = cint(member_count)

    if member_count <= 0:
        return {
            "member_count": 0,
            "tier": "Not Yet Set",
            "monthly_price_per_member": 0,
            "eligible": False,
        }

    for row in SOCIETY_PRICING_TIERS:
        if member_count < row["min_members"]:
            continue

        if (
            row["max_members"] is not None
            and member_count > row["max_members"]
        ):
            continue

        return {
            "member_count": member_count,
            "tier": row["tier"],
            "monthly_price_per_member": row["monthly_price_per_member"],
            "eligible": True,
        }

    return {
        "member_count": member_count,
        "tier": "Below Minimum",
        "monthly_price_per_member": 0,
        "eligible": False,
    }
