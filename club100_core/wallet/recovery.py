"""Club100 Round 4B, local-test-only, full-refund coordination.

No gateway refund POST is implemented. Finance initiates the gateway refund
in Razorpay TEST Dashboard; this service fetches independent gateway evidence
and only then restores wallet grants and cancels the scheduled enrollment.

Functions intentionally are NOT whitelisted. Use bench execute as an admin.
No automatic scheduler and no wallet/payment hooks are installed.
"""
from contextlib import contextmanager
from decimal import Decimal

import frappe
import requests
from frappe.utils import cint, now_datetime, getdate, nowdate

from club100_core.wallet.recovery_rules import refund_kind, check_refund, money
from club100_core.wallet.checkout import expected_gateway_due
from club100_core.wallet.spending import refund_full, release
from club100_core.api.enrollment import _get_razorpay_config, RAZORPAY_API_BASE


def _require_local_admin():
    if (frappe.local.site != 'club100.local'
            or str(frappe.conf.get('razorpay_mode') or 'test').lower() != 'test'
            or not cint(frappe.conf.get('club100_round4b_enabled', 0))):
        frappe.throw('Round 4B is disabled: local test site and test-mode flag required.')
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('Only System Managers may manage refunds.', frappe.PermissionError)


@contextmanager
def _authorized_case_write():
    before = getattr(frappe.flags, 'club100_recovery_write', False)
    frappe.flags.club100_recovery_write = True
    try:
        yield
    finally:
        frappe.flags.club100_recovery_write = before


def _lock_enrollment(name):
    row = frappe.db.get_value('Club100 Enrollment', name, ['member'], as_dict=True)
    if not row or not row.member:
        frappe.throw('Enrollment/member does not exist.')
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', row.member)
    frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE', name)
    return frappe.get_doc('Club100 Enrollment', name)


def _trial_for_refund(enrollment, kind):
    """Read-only, strict trial-link consistency check for a paid PWA order.

    Does not change trial dates, enrollment access, or any conversion record.
    """
    trial_name = str(enrollment.get('origin_trial_enrollment') or '').strip()
    if not trial_name:
        return None
    trial = frappe.db.get_value('Club100 Enrollment', trial_name,
        ['name', 'member', 'program', 'plan', 'is_trial', 'status', 'trial_status',
         'start_date', 'end_date', 'converted_enrollment'], as_dict=True)
    if not trial or not cint(trial.is_trial):
        raise ValueError('Original trial is missing or is not a trial; manual review required')
    if (trial.member != enrollment.member or trial.program != enrollment.program
            or trial.plan != enrollment.plan):
        raise ValueError('Original trial member, program, or plan differs from paid enrollment')
    if not trial.end_date or not enrollment.start_date or getdate(enrollment.start_date) <= getdate(trial.end_date):
        raise ValueError('Paid enrollment overlaps its trial or lacks valid dates')
    if kind == 'Paid':
        if trial.converted_enrollment != enrollment.name or trial.trial_status != 'Converted':
            raise ValueError('Trial conversion link or status differs; manual review required')
    elif kind == 'Captured Review':
        if trial.converted_enrollment or trial.trial_status == 'Converted':
            raise ValueError('Unsettled order has a converted trial; manual review required')
    else:
        raise ValueError('Unknown refund kind for trial')
    return trial


def _lock_trial_for_refund(enrollment):
    """Lock original trial within the same transaction as paid enrollment/case."""
    trial_name = str(enrollment.get('origin_trial_enrollment') or '').strip()
    if trial_name:
        frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE', trial_name)


def _reconcile_trial_after_refund(enrollment, kind):
    """Unlink cancelled conversion; never reactivate, extend or modify trial access."""
    if kind != 'Paid':
        return
    trial = _trial_for_refund(enrollment, kind)
    if not trial:
        return
    today = getdate(nowdate())
    ongoing = (trial.status == 'Active' and trial.start_date
               and getdate(trial.start_date) <= today <= getdate(trial.end_date))
    # trial.status, start/end dates, and workout journey are deliberately untouched.
    frappe.db.set_value('Club100 Enrollment', trial.name,
        {'converted_enrollment': None, 'trial_status': 'Active' if ongoing else 'Expired'},
        update_modified=False)


def _snapshot(enrollment):
    if enrollment.website_source != 'PWA Commerce':
        raise ValueError('Round 4B only supports authenticated PWA purchases')
    # Fail closed even if an Enrollment invoice link was accidentally cleared:
    # a durable accounting mapping proves that tax/GL reversal is required.
    if (frappe.db.exists('DocType', 'Club100 Membership Accounting')
            and frappe.db.exists('Club100 Membership Accounting', {'enrollment': enrollment.name})):
        raise ValueError('Membership has an accounting record; GST credit note and tender GL reversal must be coordinated before wallet refund')
    if enrollment.get('sales_invoice'):
        raise ValueError('Sales Invoice exists; accounting credit note must be handled before wallet refund')
    if enrollment.get('converted_enrollment'):
        raise ValueError('This enrollment is itself a trial conversion source; refund requires manual entitlement review')
    if not enrollment.wallet_reservation_key:
        raise ValueError('Only wallet-funded PWA orders are supported')
    gross = money(enrollment.amount_payable)
    wallet = money(enrollment.wallet_applied_amount)
    due = money(expected_gateway_due(enrollment))
    if gross <= 0 or wallet <= 0 or gross != wallet + due:
        raise ValueError('Tender mismatch')
    kind = refund_kind(enrollment.payment_status, enrollment.wallet_settlement_status,
                       due, bool(cint(enrollment.get('razorpay_capture_verified'))))
    if kind == 'Paid':
        if enrollment.status != 'Scheduled' or not enrollment.start_date or getdate(enrollment.start_date) <= getdate(nowdate()):
            raise ValueError('Only future Scheduled memberships qualify; already-started access requires entitlement review')
    elif enrollment.status != 'Registered':
        raise ValueError('Captured Review recovery requires an unactivated Registered enrollment')
    reservation = frappe.db.get_value('Club100 Wallet Reservation',
                    {'reservation_key': enrollment.wallet_reservation_key},
                    ['status','member','reference','total_amount'], as_dict=True)
    if (not reservation or reservation.member != enrollment.member
            or reservation.reference != enrollment.name or money(reservation.total_amount) != wallet):
        raise ValueError('Wallet reservation does not match the enrollment')
    if kind == 'Paid' and reservation.status != 'Captured':
        raise ValueError('Paid enrollment does not have a captured wallet reservation')
    if kind == 'Captured Review' and reservation.status == 'Captured':
        raise ValueError('Review has a captured wallet reservation; escalate')
    if due > 0 and (not str(enrollment.payment_id or '').startswith('pay_')
                    or enrollment.payment_gateway != 'Razorpay'):
        raise ValueError('Trusted Razorpay payment reference required')
    if due == 0 and not str(enrollment.payment_id or '').startswith('WALLET:'):
        raise ValueError('Wallet-only payment identity mismatch')
    if frappe.db.exists('DocType','Club100 Referral Reward') and frappe.db.exists(
            'Club100 Referral Reward', {'enrollment':enrollment.name}):
        from club100_core.referrals.settlement import _require_pilot, refund_preflight
        _require_pilot()  # no gateway refund while reward recovery engine is disabled
        refund_preflight(enrollment.name)
    _trial_for_refund(enrollment, kind)
    return {'enrollment': enrollment.name, 'member':enrollment.member,
            'case_kind':kind, 'payment_id':enrollment.payment_id or '',
            'wallet_reservation_key':enrollment.wallet_reservation_key,
            'wallet_to_restore':str(wallet if kind == 'Paid' else Decimal('0.00')),
            'gateway_to_refund':str(due), 'currency':'INR'}


def refund_preview(enrollment_id):
    """Read-only eligibility; no Razorpay request and no DB mutation."""
    _require_local_admin()
    doc = frappe.get_doc('Club100 Enrollment', str(enrollment_id))
    return {'eligible':True, **_snapshot(doc)}


def prepare_full_refund(enrollment_id, commit=True):
    """Create one durable refund case; NEVER contact or refund Razorpay here."""
    _require_local_admin()
    name = str(enrollment_id or '').strip()
    enrollment = _lock_enrollment(name)
    existing = frappe.db.get_value('Club100 Wallet Refund Case', {'enrollment':name}, 'name')
    if existing:
        case = frappe.get_doc('Club100 Wallet Refund Case', existing)
        return _result(case)
    snap = _snapshot(enrollment)
    case = frappe.get_doc({'doctype':'Club100 Wallet Refund Case', **snap, 'status':'Prepared'})
    with _authorized_case_write():
        case.insert(ignore_permissions=True)
    if commit: frappe.db.commit()
    return _result(case)


def _result(case):
    return {'case':case.name, 'enrollment':case.enrollment, 'kind':case.case_kind,
            'status':case.status, 'wallet_to_restore':str(money(case.wallet_to_restore)),
            'gateway_to_refund':str(money(case.gateway_to_refund)),
            'gateway_refund_id':case.gateway_refund_id or None,
            'message':'No Razorpay refund API calls are made by Club100 Round 4B.'}


def _get_gateway_refund(refund_id):
    """GET-only Razorpay evidence; never create a gateway refund."""
    config = _get_razorpay_config()
    response = requests.get(
        f'{RAZORPAY_API_BASE}/refunds/{refund_id}',
        auth=(config['key_id'], config['key_secret']), timeout=20)
    response.raise_for_status()
    return response.json()


def confirm_full_refund(case_name, gateway_refund_id=None, commit=True):
    """Reconcile a Dashboard-issued full refund, then restore wallet once.

    Wallet-only cases require no Razorpay; split cases require processed
    Razorpay refund matching exact payment ID and full gateway amount.
    An externally processed refund is recoverable by running this again.
    """
    _require_local_admin()
    case_name = str(case_name or '').strip()
    # Case name equals enrollment ID. Member -> Enrollment lock ordering is preserved.
    enrollment = _lock_enrollment(case_name)
    _lock_trial_for_refund(enrollment)
    frappe.db.sql('SELECT name FROM `tabClub100 Wallet Refund Case` WHERE name=%s FOR UPDATE',case_name)
    case = frappe.get_doc('Club100 Wallet Refund Case',case_name)
    if case.status == 'Completed':
        if gateway_refund_id and case.gateway_refund_id != gateway_refund_id:
            raise ValueError('Completed case cannot use a different gateway refund')
        return _result(case)
    if case.status == 'Gateway Failed':
        raise ValueError('Failed refund must be reviewed by finance; no automatic retry')
    if (case.enrollment != enrollment.name or case.member != enrollment.member
            or case.wallet_reservation_key != enrollment.wallet_reservation_key
            or case.payment_id != enrollment.payment_id):
        raise ValueError('Refund case no longer matches enrollment identity')
    snap = _snapshot(enrollment)
    if (snap['case_kind'] != case.case_kind
        or money(snap['wallet_to_restore']) != money(case.wallet_to_restore)
        or money(snap['gateway_to_refund']) != money(case.gateway_to_refund)):
        raise ValueError('Financial refund terms have changed')
    gateway_amount = money(case.gateway_to_refund)
    if gateway_amount:
        rid = str(gateway_refund_id or case.gateway_refund_id or '').strip()
        import re
        if not re.fullmatch(r'rfnd_[A-Za-z0-9]+', rid) or (case.gateway_refund_id and rid != case.gateway_refund_id):
            raise ValueError('Exact Razorpay refund ID required; no refund will be initiated by this service')
        data = _get_gateway_refund(rid)
        state = check_refund(data, case.payment_id, int(gateway_amount * 100), rid)
        if state in ('pending','failed'):
            with _authorized_case_write():
                case.gateway_refund_id = rid
                case.last_gateway_status = state
                case.status = 'Gateway Pending' if state == 'pending' else 'Gateway Failed'
                case.save(ignore_permissions=True)
            if commit: frappe.db.commit()
            return _result(case)
    else:
        if gateway_refund_id:
            raise ValueError('Wallet-only order cannot have Razorpay refund evidence')
        state = 'not_applicable'
    # No gateway POST above. Savepoint allows clean retry after an ERP failure.
    frappe.db.savepoint('club100_round4b_finalize')
    try:
        if frappe.db.exists('Club100 Referral Reward', {'enrollment': enrollment.name}):
            from club100_core.referrals.settlement import reverse_for_enrollment
            reverse_for_enrollment(enrollment.name)
        if case.case_kind == 'Paid':
            reversal = refund_full(case.wallet_reservation_key)
            if reversal['status'] != 'Refunded':
                raise ValueError('Wallet reversal did not complete')
        else:
            # Captured Razorpay + failed wallet hold => never post an unearned wallet refund.
            reservation = frappe.db.get_value('Club100 Wallet Reservation',
                {'reservation_key':case.wallet_reservation_key}, 'status')
            if reservation == 'Reserved':
                release(case.wallet_reservation_key)
        enrollment.status = 'Cancelled'
        enrollment.payment_status = 'Refunded'
        enrollment.wallet_settlement_status = ('Refunded' if case.case_kind == 'Paid' else 'None')
        enrollment.save(ignore_permissions=True)
        _reconcile_trial_after_refund(enrollment, case.case_kind)
        with _authorized_case_write():
            case.status = 'Completed'
            case.gateway_refund_id = str(gateway_refund_id or case.gateway_refund_id or '')
            case.last_gateway_status = state
            case.confirmed_at = now_datetime()
            case.save(ignore_permissions=True)
        if commit: frappe.db.commit()
    except Exception:
        frappe.db.rollback(save_point='club100_round4b_finalize')
        raise
    return _result(case)


def release_unordered_expired_checkout(enrollment_id, commit=True):
    """Conservatively cancel only expired holds with NO gateway order ever created.

    A checkout with a Razorpay order MUST undergo separate reconciliation;
    an absent captured callback does not prove that no payment occurred.
    """
    _require_local_admin()
    doc = _lock_enrollment(str(enrollment_id or '').strip())
    if (doc.website_source != 'PWA Commerce' or not doc.wallet_reservation_key
            or doc.payment_order_id or doc.payment_id or doc.payment_status != 'Pending'
            or doc.status not in ('Registered','Cancelled')
            or doc.wallet_settlement_status != 'Reserved'):
        raise ValueError('Only unpaid wallet checkout with no gateway order can be released')
    row = frappe.db.get_value('Club100 Wallet Reservation',
           {'reservation_key':doc.wallet_reservation_key}, ['status','expires_at'],as_dict=True)
    if not row:
        raise ValueError('Reservation missing')
    if row.status == 'Reserved':
        if frappe.utils.get_datetime(row.expires_at) > now_datetime():
            raise ValueError('Reservation still active; wait for expiry')
        release(doc.wallet_reservation_key)
    elif row.status not in ('Expired','Released'):
        raise ValueError('Reservation cannot be released')
    doc.status = 'Cancelled'
    doc.wallet_settlement_status = 'None'
    doc.save(ignore_permissions=True)
    if commit: frappe.db.commit()
    return {'enrollment':doc.name,'status':'Cancelled','hold':'Expired or Released','gateway_order':None}


def healthcheck():
    return {'site':frappe.local.site,
            'enabled':frappe.local.site == 'club100.local' and
              bool(cint(frappe.conf.get('club100_round4b_enabled',0))),
            'gateway_refund_post_enabled':False, 'refund_cases':frappe.db.count('Club100 Wallet Refund Case')
                if frappe.db.exists('DocType','Club100 Wallet Refund Case') else None,
            'policy':'Full refund only; Razorpay refund must be processed manually in Test Dashboard'}
