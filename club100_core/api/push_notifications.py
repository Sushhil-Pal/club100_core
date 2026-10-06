import frappe
from frappe.utils import now_datetime
import json

from pywebpush import (
    webpush,
    WebPushException,
)


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


@frappe.whitelist(methods=["POST"])
def subscribe(
    endpoint=None,
    p256dh_key=None,
    auth_key=None,
    device_name=None,
    user_agent=None,
):
    member = _get_current_member()
    user = frappe.session.user

    if not endpoint:
        frappe.throw(
            "Push subscription endpoint is required",
            frappe.ValidationError,
        )

    if not p256dh_key:
        frappe.throw(
            "P256DH key is required",
            frappe.ValidationError,
        )

    if not auth_key:
        frappe.throw(
            "Auth key is required",
            frappe.ValidationError,
        )

    # -----------------------------------------------------
    # Check whether this browser/device is already stored
    # -----------------------------------------------------

    existing_name = frappe.db.get_value(
        "Club100 Push Subscription",
        {
            "endpoint": endpoint,
        },
        "name",
    )

    if existing_name:
        subscription = frappe.get_doc(
            "Club100 Push Subscription",
            existing_name,
        )

        # The endpoint may previously have belonged to
        # the same user but with refreshed browser keys.
        subscription.member = member.name
        subscription.user = user
        subscription.p256dh_key = p256dh_key
        subscription.auth_key = auth_key
        subscription.device_name = (
            device_name or ""
        )
        subscription.user_agent = (
            user_agent or ""
        )
        subscription.active = 1
        subscription.last_seen_on = (
            now_datetime()
        )

        subscription.save(
            ignore_permissions=True
        )

    else:
        subscription = frappe.get_doc({
            "doctype":
                "Club100 Push Subscription",

            "member":
                member.name,

            "user":
                user,

            "endpoint":
                endpoint,

            "p256dh_key":
                p256dh_key,

            "auth_key":
                auth_key,

            "active":
                1,

            "device_name":
                device_name or "",

            "user_agent":
                user_agent or "",

            "last_seen_on":
                now_datetime(),
        })

        subscription.insert(
            ignore_permissions=True
        )

    _update_member_push_status(member.name, update_last_subscription=True,)

    frappe.db.commit()

    
    return {
        "success": True,
        "subscriptionId":
            subscription.name,
        "active": True,
    }


@frappe.whitelist(methods=["POST"])
def unsubscribe(
    endpoint=None,
):
    member = _get_current_member()

    if not endpoint:
        frappe.throw(
            "Push subscription endpoint is required",
            frappe.ValidationError,
        )

    subscription_name = frappe.db.get_value(
        "Club100 Push Subscription",
        {
            "endpoint": endpoint,
            "member": member.name,
        },
        "name",
    )

    if not subscription_name:
        # Already absent = effectively unsubscribed.
        _update_member_push_status(member.name)

        frappe.db.commit()

        return {
            "success": True,
            "active": False,
        }

    subscription = frappe.get_doc(
        "Club100 Push Subscription",
        subscription_name,
    )

    subscription.active = 0
    subscription.last_seen_on = (
        now_datetime()
    )

    subscription.save(
        ignore_permissions=True
    )
    _update_member_push_status(member.name)
    frappe.db.commit()

    return {
        "success": True,
        "active": False,
    }


@frappe.whitelist(methods=["GET"])
def status(
    endpoint=None,
):
    member = _get_current_member()

    if not endpoint:
        return {
            "subscribed": False,
        }

    subscription = frappe.db.get_value(
        "Club100 Push Subscription",
        {
            "endpoint": endpoint,
            "member": member.name,
            "active": 1,
        },
        [
            "name",
            "last_seen_on",
        ],
        as_dict=True,
    )

    if not subscription:
        return {
            "subscribed": False,
        }

    return {
        "subscribed": True,
        "subscriptionId":
            subscription.name,
        "lastSeenOn":
            subscription.last_seen_on,
    }


def _get_vapid_config():
    public_key = frappe.conf.get(
        "club100_vapid_public_key"
    )

    private_key = frappe.conf.get(
        "club100_vapid_private_key"
    )

    subject = frappe.conf.get(
        "club100_vapid_subject"
    )

    if not public_key:
        frappe.throw(
            "VAPID public key is not configured"
        )

    if not private_key:
        frappe.throw(
            "VAPID private key is not configured"
        )

    if not subject:
        frappe.throw(
            "VAPID subject is not configured"
        )

    return {
        "public_key": public_key,
        "private_key": private_key,
        "subject": subject,
    }


def send_push_to_subscription(
    subscription,
    title,
    body,
    url="/dashboard",
):
    vapid = _get_vapid_config()

    subscription_info = {
        "endpoint":
            subscription.endpoint,

        "keys": {
            "p256dh":
                subscription.p256dh_key,

            "auth":
                subscription.auth_key,
        },
    }

    payload = {
        "title": title,
        "body": body,
        "url": url,
    }

    try:
        webpush(
            subscription_info=
                subscription_info,

            data=json.dumps(
                payload
            ),

            vapid_private_key=
                vapid["private_key"],

            vapid_claims={
                "sub":
                    vapid["subject"],
            },
        )

        subscription.last_notification_on = (
            frappe.utils.now_datetime()
        )

        subscription.save(
            ignore_permissions=True
        )

        return True

    except WebPushException as exc:
        status_code = (
            exc.response.status_code
            if exc.response
            else None
        )

        if status_code in (
            404,
            410,
        ):
            subscription.active = 0

            subscription.save(
                ignore_permissions=True
            )
            _update_member_push_status(subscription.member)

        frappe.log_error(
            message=str(exc),
            title=
                "Club100 Push Notification Error",
        )

        return False

    
@frappe.whitelist(
    methods=["POST"]
)
def send_test():
    member = _get_current_member()

    result = send_push_to_member(
        member=member.name,
        title="Club100",
        body="Push notifications are working.",
        url="/dashboard",
    )

    frappe.db.commit()

    return {
        "success": result["sent"] > 0,
        **result,
    }

def send_push_to_member(
    member,
    title,
    body,
    url="/dashboard",
):
    subscription_names = frappe.get_all(
        "Club100 Push Subscription",
        filters={
            "member": member,
            "active": 1,
        },
        pluck="name",
    )

    sent = 0
    failed = 0

    for name in subscription_names:
        subscription = frappe.get_doc(
            "Club100 Push Subscription",
            name,
        )

        success = send_push_to_subscription(
            subscription=subscription,
            title=title,
            body=body,
            url=url,
        )

        if success:
            sent += 1
        else:
            failed += 1

    return {
        "sent": sent,
        "failed": failed,
    }

def _update_member_push_status(
    member,
    update_last_subscription=False,
):
    if not member:
        return

    active_count = frappe.db.count(
        "Club100 Push Subscription",
        {
            "member": member,
            "active": 1,
        },
    )

    values = {
        "push_notifications_enabled":
            1 if active_count > 0 else 0,
    }

    if update_last_subscription:
        values[
            "last_push_subscription_on"
        ] = frappe.utils.now_datetime()

    frappe.db.set_value(
        "Club100 Member",
        member,
        values,
        update_modified=False,
    )