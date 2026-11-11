"""Club100 Stage 5C: finance-attested member referral rewards.

LOCAL PILOT ONLY. No public APIs, gateway refund POST, ambassador cash payout,
credit top-ups or third-party wallet use. Finance verification is mandatory;
gross provisional estimates are NEVER directly credited. Worker uses a verified
captured Razorpay payment and denies pending refunds before posting grants.

All wallet and reward modifications share the caller's database transaction.
"""
from contextlib import contextmanager
from decimal import Decimal
import re

import frappe
import requests
from frappe.utils import cint, getdate, now_datetime, nowdate

from club100_core.referrals.settlement_logic import (
    money, verified_amount, maturity_day, credit_validity, can_mature, date_value,
    recoverable_grant,
)

REFERRAL_KEY_PREFIX = 'REFERRAL:REWARD:'


def _enabled():
    return (frappe.local.site == 'club100.local'
            and str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test')


def _require_pilot():
    if not _enabled():
        frappe.throw('Referral settlement is disabled; local test mode and explicit feature flag required')


def _admin_only():
    _require_pilot()
    actor = frappe.session.user
    if actor != 'Administrator' and 'System Manager' not in frappe.get_roles(actor):
        frappe.throw('System Manager required for referral finance verification', frappe.PermissionError)


@contextmanager
def _reward_write():
    previous = getattr(frappe.flags, 'club100_referral_settlement_write', False)
    frappe.flags.club100_referral_settlement_write = True
    try:
        yield
    finally:
        frappe.flags.club100_referral_settlement_write = previous


@contextmanager
def _grant_issue():
    previous = getattr(frappe.flags, 'club100_referral_issue_grant', False)
    frappe.flags.club100_referral_issue_grant = True
    try:
        yield
    finally:
        frappe.flags.club100_referral_issue_grant = previous


def _lock_reward(reward_name):
    name = str(reward_name or '').strip()
    if not name or not frappe.db.sql(
            'SELECT name FROM `tabClub100 Referral Reward` WHERE name=%s FOR UPDATE', name):
        raise ValueError('Referral reward does not exist')
    return frappe.get_doc('Club100 Referral Reward', name)


def _lock_reward_payment(reward_name):
    """Align with refund's referred Member -> Enrollment -> reward locks.

    Without the enrollment lock, finance approval or maturity could race with
    preparation of a refund case and create credits after refund initiation.
    """
    identity = frappe.db.get_value('Club100 Referral Reward',
                    str(reward_name or '').strip(), ['referred_member','enrollment'], as_dict=True)
    if not identity or not identity.referred_member or not identity.enrollment:
        raise ValueError('Reward has no valid enrollment identity')
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', identity.referred_member)
    frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE', identity.enrollment)
    reward = _lock_reward(reward_name)
    if reward.referred_member != identity.referred_member or reward.enrollment != identity.enrollment:
        raise ValueError('Reward financial identity changed during lock acquisition')
    return reward


def _gateway_due(enrollment):
    hybrid = frappe.db.get_value('Club100 Hybrid Checkout',
        {'enrollment': enrollment.name},
        ['status','original_price','promotion_discount','funded_wallet_tender','gateway_due'],
        as_dict=True)
    if hybrid:
        gross = money(hybrid.original_price)
        discount = money(hybrid.promotion_discount)
        wallet = money(hybrid.funded_wallet_tender)
        gateway = money(hybrid.gateway_due)
        if (hybrid.status != 'Captured' or money(enrollment.amount_payable) != gross
                or gross-discount != wallet+gateway or gateway <= 0):
            raise ValueError('Hybrid referral gateway amount is not reconciled')
        if (not enrollment.sales_invoice or money(frappe.db.get_value(
                'Sales Invoice', enrollment.sales_invoice, 'grand_total')) != gross-discount):
            raise ValueError('Hybrid referral invoice is not reconciled')
        return gateway
    gross = money(enrollment.amount_payable)
    wallet = money(enrollment.get('wallet_applied_amount') or 0)
    derived = gross - wallet
    recorded = money(enrollment.get('gateway_due_amount') or 0)
    # Old Razorpay-only enrollments predate the gateway_due_amount field.
    if recorded and recorded != derived:
        raise ValueError('Enrollment gateway / wallet tender mismatch')
    if derived <= 0 or wallet < 0:
        raise ValueError('Membership has no qualifying externally paid Razorpay tender')
    return derived


def _payment_eligible(enrollment, reward):
    if (enrollment.name != reward.enrollment or enrollment.member != reward.referred_member
            or enrollment.payment_status != 'Paid' or enrollment.status == 'Cancelled'):
        raise ValueError('Reward payment is not a currently paid enrollment')
    if enrollment.payment_gateway != 'Razorpay' or not cint(enrollment.get('razorpay_capture_verified')):
        raise ValueError('Trusted captured Razorpay payment required')
    if not enrollment.payment_id or reward.payment_reference != enrollment.payment_id:
        raise ValueError('Payment reference no longer matches original reward')
    if cint(enrollment.membership_duration_months) < cint(frappe.db.get_value(
            'Club100 Referral Attribution', reward.attribution, 'minimum_duration_months')):
        raise ValueError('Membership is below captured referral minimum duration')
    if reward.currency != 'INR' or reward.event_key != 'RPAY:' + str(enrollment.payment_id):
        raise ValueError('Referral payment event identity or currency mismatch')
    attribution = frappe.get_doc('Club100 Referral Attribution', reward.attribution)
    partner = frappe.get_doc('Club100 Referral Partner', reward.partner)
    expected_track = 'Ambassador Cash' if partner.partner_type == 'Ambassador' else 'Member Credit'
    expected_rate = attribution.first_rate if reward.reward_event == 'First Purchase' else attribution.renewal_rate
    if (attribution.status != 'Captured' or attribution.referred_member != enrollment.member
            or attribution.partner != reward.partner or attribution.reward_track != reward.reward_track
            or expected_track != reward.reward_track
            or reward.reward_event not in ('First Purchase', 'Renewal')
            or Decimal(str(reward.rate)) != Decimal(str(expected_rate))):
        raise ValueError('Referral attribution, track or captured rate does not match reward')
    if frappe.db.exists('Club100 Wallet Refund Case', {'enrollment': enrollment.name}):
        raise ValueError('A refund case exists; reward must not mature')
    return _gateway_due(enrollment)


def _gateway_is_captured_without_refund(payment_id, expected_gateway_amount):
    """GET-only independent Razorpay check at maturity; never issue a charge/refund."""
    from club100_core.api.enrollment import _get_razorpay_config, RAZORPAY_API_BASE
    config = _get_razorpay_config()
    if not str(config.get('key_id') or '').startswith('rzp_test_'):
        raise ValueError('Referral settlement pilot requires Razorpay TEST credentials')
    response = requests.get(
        f'{RAZORPAY_API_BASE}/payments/{payment_id}',
        auth=(config['key_id'], config['key_secret']), timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    amount_paise = int(money(expected_gateway_amount) * 100)
    if (data.get('id') != payment_id or data.get('status') != 'captured'
            or data.get('currency') != 'INR' or data.get('amount') != amount_paise
            or int(data.get('amount_refunded') or 0) != 0
            or data.get('refund_status') not in (None, '', 'null')):
        raise ValueError('Gateway payment not captured, mismatched, or already refunded')
    return True


def _hold_days():
    value = cint(frappe.conf.get('club100_referral_hold_days') or 30)
    if not 14 <= value <= 180:
        raise ValueError('club100_referral_hold_days must be 14–180')
    return value


def finance_verify_reward(reward_name, verified_net_basis, finance_proof_reference,
                          validity_type='Lifetime', expires_on=None, commit=True):
    """Bench-only System Manager approval after checking invoice/tax evidence.

    No browser input, wallet credit or external gateway action is performed.
    The provided net-of-tax figure MUST be independently verified by Finance.
    """
    _admin_only()
    proof = str(finance_proof_reference or '').strip()
    if len(proof) < 6 or len(proof) > 140 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:/_. -]*', proof):
        raise ValueError('A real finance/invoice proof reference (6–140 characters) is required')
    reward = _lock_reward_payment(reward_name)
    if reward.status == 'Approved' and reward.basis_verification_status == 'Verified':
        if (money(reward.verified_basis_amount) != money(verified_net_basis)
                or reward.finance_proof_reference != proof
                or reward.credit_validity_type != validity_type
                or str(reward.credit_expires_on or '') != str(expires_on or '')):
            raise ValueError('Approval replay uses different financial or expiry terms')
        return _summary(reward)
    if reward.status != 'On Hold' or reward.basis_verification_status == 'Verified':
        raise ValueError('Only an unverified On Hold reward can be approved')
    enrollment = frappe.get_doc('Club100 Enrollment', reward.enrollment)
    gateway = _payment_eligible(enrollment, reward)
    amount = verified_amount(verified_net_basis, reward.rate, gateway)
    maturity = maturity_day(enrollment.payment_date or enrollment.creation,
                            enrollment.start_date or enrollment.enrollment_date or enrollment.creation,
                            _hold_days())
    expiry = credit_validity(validity_type, expires_on, maturity)
    with _reward_write():
        reward.verified_basis_amount = money(verified_net_basis)
        reward.verified_reward_amount = amount
        reward.finance_proof_reference = proof
        reward.basis_evidence_type = 'Finance'
        reward.finance_verified_by = frappe.session.user
        reward.finance_verified_on = now_datetime()
        reward.eligible_on = str(maturity)
        reward.credit_validity_type = validity_type
        reward.credit_expires_on = expiry
        reward.basis_verification_status = 'Verified'
        reward.status = 'Approved'
        reward.notes = (str(reward.notes or '') + '\nFinance-approved net-of-tax basis; wallet credit only after hold and gateway recheck.')[-1400:]
        reward.save(ignore_permissions=True)
    if commit:
        frappe.db.commit()
    return _summary(reward)




def _auto_enabled():
    # Only Member Credit, verified invoice basis and maturity may auto-approve.
    # Ambassador cash payouts still require explicit human approval.
    return _enabled()


def auto_approve_one(reward_name, commit=True):
    """Trusted scheduled approval from *independent submitted invoice* or
    from a frozen local-only PWA Self GST snapshot. Never use provisional
    gross reward estimates to issue wallet grants. No external gateway calls.
    """
    if not _auto_enabled():
        raise ValueError('Automatic referral approval disabled')
    reward = _lock_reward_payment(reward_name)
    if reward.status == 'Approved' and reward.basis_verification_status == 'Verified':
        return _summary(reward)
    if reward.status != 'On Hold' or reward.basis_verification_status not in ('Unverified', None, ''):
        raise ValueError('Only unverified On Hold rewards can be automatically approved')
    if reward.reward_track != 'Member Credit':
        raise ValueError('Ambassador commissions require separate cash-payable approval')
    enrollment = frappe.get_doc('Club100 Enrollment', reward.enrollment)
    gateway = _payment_eligible(enrollment, reward)
    # Provenance must be supported by a real submitted invoice or a checkout
    # tax snapshot that was frozen when the payment reward was created.
    from club100_core.referrals.auto_basis import verified_basis_evidence
    evidence = verified_basis_evidence(enrollment, reward, gateway)
    amount = verified_amount(evidence['basis'], reward.rate, gateway)
    maturity = maturity_day(enrollment.payment_date or enrollment.creation,
                            enrollment.start_date or enrollment.enrollment_date or enrollment.creation,
                            _hold_days())
    with _reward_write():
        reward.verified_basis_amount = evidence['basis']
        reward.verified_reward_amount = amount
        reward.finance_proof_reference = evidence['reference']
        reward.basis_evidence_type = evidence['type']
        # Do not impersonate a Finance employee for automated verification.
        reward.finance_verified_by = None
        reward.finance_verified_on = now_datetime()
        reward.eligible_on = str(maturity)
        reward.credit_validity_type = 'Lifetime'
        reward.credit_expires_on = None
        reward.basis_verification_status = 'Verified'
        reward.status = 'Approved'
        reward.notes = (str(reward.notes or '') + '\nAUTO-approved using ' + evidence['type']
                        + '. Net of tax and excluding wallet tender; subject to hold, refund and gateway checks.')[-1400:]
        reward.save(ignore_permissions=True)
    if commit:
        frappe.db.commit()
    return _summary(reward)


def scheduled_auto_approve_rewards(limit=100):
    """Auto-approve safe records only; blocked/unsupported records stay On Hold."""
    if not _auto_enabled():
        return {'status': 'disabled', 'approved': 0}
    names = frappe.get_all('Club100 Referral Reward', filters={
        'status': 'On Hold', 'basis_verification_status': 'Unverified',
        'reward_track': 'Member Credit',
    }, pluck='name', order_by='creation asc', limit_page_length=min(int(limit), 100))
    approved, blocked = [], []
    for name in names:
        frappe.db.savepoint('club100_ref_auto_approval')
        try:
            approved.append(auto_approve_one(name, commit=False))
        except Exception as exc:
            frappe.db.rollback(save_point='club100_ref_auto_approval')
            blocked.append({'reward': name, 'reason': str(exc)[:180]})
        else:
            frappe.db.commit()
    return {'status': 'processed', 'approved': len(approved), 'blocked': blocked}

def _summary(reward):
    return {'reward': reward.name, 'track': reward.reward_track,
            'status': reward.status, 'matures_on': str(reward.eligible_on or ''),
            'verified_basis': str(money(reward.get('verified_basis_amount') or 0)),
            'verified_reward': str(money(reward.get('verified_reward_amount') or 0)),
            'wallet_grant': reward.settlement_reference or None}


def _mature_wallet_only(reward_name, commit=True):
    """Mature approved MEMBER credit, idempotent with unique wallet source key."""
    _require_pilot()
    reward = _lock_reward_payment(reward_name)
    if reward.status == 'Available':
        return _summary(reward)
    if reward.status != 'Approved' or reward.basis_verification_status != 'Verified':
        raise ValueError('Reward is not finance-approved')
    if not reward.eligible_on or not can_mature(nowdate(), reward.eligible_on):
        raise ValueError('Reward hold period has not ended')
    enrollment = frappe.get_doc('Club100 Enrollment', reward.enrollment)
    gateway = _payment_eligible(enrollment, reward)
    verified_amount(reward.verified_basis_amount, reward.rate, gateway)
    if money(reward.verified_reward_amount) != verified_amount(reward.verified_basis_amount, reward.rate, gateway):
        raise ValueError('Verified reward amount does not match rate and basis')
    if reward.reward_track != 'Member Credit':
        # Ambassador cash commissions are approval-only. No payout engine yet.
        return {**_summary(reward), 'payout': 'deferred_no_cash_transfer'}
    attr = frappe.get_doc('Club100 Referral Attribution', reward.attribution)
    if attr.status != 'Captured' or attr.partner != reward.partner or attr.referred_member != reward.referred_member:
        raise ValueError('Captured attribution no longer matches reward')
    partner = frappe.get_doc('Club100 Referral Partner', reward.partner)
    if partner.status != 'Active' or partner.partner_type != 'Member Advocate' or not partner.member:
        raise ValueError('Referrer is not an active member advocate')
    if partner.member == reward.referred_member:
        raise ValueError('Self-referral reward is forbidden')
    credit_validity(reward.credit_validity_type, reward.credit_expires_on, reward.eligible_on)
    if reward.credit_expires_on and date_value(reward.credit_expires_on) < date_value(nowdate()):
        raise ValueError('Credit expiry has passed before maturity processing; finance review required')
    # Independently fetch current Razorpay status before posting a liability.
    _gateway_is_captured_without_refund(enrollment.payment_id, gateway)
    source_key = REFERRAL_KEY_PREFIX + reward.name
    existing = frappe.db.get_value('Club100 Wallet Grant', {'source_event_key': source_key}, 'name')
    if existing:
        grant = frappe.get_doc('Club100 Wallet Grant', existing)
        if (grant.referral_reward != reward.name or grant.member != partner.member
                or grant.source_type != 'Referral' or money(grant.amount) != money(reward.verified_reward_amount)):
            raise ValueError('Existing referral grant has conflicting financial identity')
        grant_name = existing
    else:
        # This flag can only be reached from the non-whitelisted internal service.
        with _grant_issue():
            grant = frappe.get_doc({
                'doctype': 'Club100 Wallet Grant',
                'member': partner.member, 'source_type': 'Referral',
                'source_event_key': source_key, 'referral_reward': reward.name,
                'funding_reference': 'REFERRAL:FINANCE:' + reward.name,
                'amount': money(reward.verified_reward_amount), 'currency': 'INR',
                'valid_from': nowdate(),
                'validity_type': reward.credit_validity_type,
                'expires_on': reward.credit_expires_on,
                'status': 'Active',
            })
            grant.insert(ignore_permissions=True)
        grant_name = grant.name
    with _reward_write():
        reward.settlement_reference = grant_name
        reward.settled_on = now_datetime()
        reward.status = 'Available'
        reward.save(ignore_permissions=True)
    if commit:
        frappe.db.commit()
    return _summary(reward)


# CLUB100_REFERRAL_ATOMIC_GL_V1: a matured credit is never committed without its GL liability.
def mature_one(reward_name, commit=True):
    """Atomically mature Member Credit, book liability and verify source backing.

    The original maturity validation is retained in _mature_wallet_only.  All
    grant/ledger/reward and Journal Entry/event writes share this transaction.
    If any posting/proof fails, roll back to this savepoint before propagating.
    """
    from club100_core.accounting import funding
    from club100_core.accounting import settlement as accounting_settlement
    from club100_core.accounting.wallet_backing import verify_wallet_grant_backing

    point = 'club100_referral_atomic_maturity_v1'
    frappe.db.savepoint(point)
    try:
        # Guard old Available rewards.  Do not silently "heal" legacy unbacked
        # credits by manufacturing a new Journal Entry on an idempotent replay.
        previous_status = frappe.db.get_value('Club100 Referral Reward', reward_name, 'status')
        outcome = _mature_wallet_only(reward_name, commit=False)
        if outcome.get('payout') == 'deferred_no_cash_transfer':
            # Ambassador payouts remain deferred; this is NOT a cash payout.
            return outcome
        if outcome.get('track') != 'Member Credit' or outcome.get('status') != 'Available':
            raise ValueError('Unexpected referral maturity outcome; accounting not posted')
        grant_name = outcome.get('wallet_grant')
        if not grant_name:
            raise ValueError('Available referral credit has no Wallet Grant')
        reward = frappe.get_doc('Club100 Referral Reward', reward_name)
        grant = frappe.get_doc('Club100 Wallet Grant', grant_name)
        if (reward.settlement_reference != grant.name or grant.referral_reward != reward.name
                or grant.source_type != 'Referral' or money(reward.verified_reward_amount) != money(grant.amount)):
            raise ValueError('Reward, grant and verified amount do not agree')

        booking = None
        if previous_status != 'Available':
            # The separate booking service requires verified submitted invoice,
            # correct non-stock liability/expense accounts and unspent credit.
            booking = funding.post_referral_issuance(grant_name, commit=False)
            if booking.get('status') not in ('Posted', 'Already Posted'):
                raise ValueError('Referral GL booking not confirmed')
        cfg = accounting_settlement._config()
        alloc = [{'grant': grant.name, 'source': 'Referral', 'amount': str(money(grant.amount))}]
        backed = verify_wallet_grant_backing(cfg, alloc)
        if money(backed.get('Referral', 0)) != money(grant.amount):
            raise ValueError('Referral source-specific GL backing does not match granted amount')
        accounting_settlement._require_wallet_liability_backing(
            cfg, {'Referral': money(grant.amount)}, alloc)

        # Match old idempotent behavior: a replay must NOT commit unrelated
        # caller work merely because reward was already Available.
        if commit and previous_status != 'Available':
            frappe.db.commit()
        return {**outcome, 'accounting_status': (booking or {}).get('status') or 'Verified Existing',
                'accounting_journal': (booking or {}).get('journal_entry')}
    except Exception:
        frappe.db.rollback(save_point=point)
        raise


def _rewards_for(enrollment_name):
    return frappe.get_all('Club100 Referral Reward',
                          filters={'enrollment': enrollment_name}, pluck='name',
                          order_by='creation asc')


def _referral_grant_state(reward):
    source_key = REFERRAL_KEY_PREFIX + reward.name
    grant_name = frappe.db.get_value('Club100 Wallet Grant', {'source_event_key': source_key}, 'name')
    if not grant_name or reward.settlement_reference != grant_name:
        raise ValueError('Issued referral grant does not match reward; manual review required')
    grant = frappe.get_doc('Club100 Wallet Grant', grant_name)
    partner_member = frappe.db.get_value('Club100 Referral Partner', reward.partner, 'member')
    if (grant.member != partner_member or grant.source_type != 'Referral'
            or grant.referral_reward != reward.name or money(grant.amount) != money(reward.verified_reward_amount)):
        raise ValueError('Issued referral grant has incorrect ownership/amount')
    net = frappe.db.sql('''SELECT COALESCE(SUM(amount),0) AS amount
        FROM `tabClub100 Wallet Ledger` WHERE `grant`=%s''', grant_name, as_dict=True)[0].amount
    active_hold = frappe.db.sql('''SELECT COALESCE(SUM(i.amount),0) AS amount
        FROM `tabClub100 Wallet Reservation Item` i
        JOIN `tabClub100 Wallet Reservation` r ON r.name=i.parent
        WHERE i.`grant`=%s AND r.status='Reserved' AND r.expires_at > %s''',
        (grant_name, now_datetime()), as_dict=True)[0].amount
    recoverable_grant(grant.amount, net, active_hold)
    return grant


def refund_preflight(enrollment_name):
    """Read-only check for Round 4B refund preview; never silently claw back spent funds."""
    for name in _rewards_for(enrollment_name):
        reward = frappe.get_doc('Club100 Referral Reward', name)
        if reward.status == 'Reversed':
            continue
        if reward.status in ('On Hold', 'Pending', 'Approved'):
            continue
        if reward.status == 'Available' and reward.reward_track == 'Member Credit':
            _referral_grant_state(reward)
            continue
        raise ValueError('Referral reward already paid, redeemed, or unsupported: manual finance recovery required')
    return True


def reverse_for_enrollment(enrollment_name):
    """Internal, same transaction as completed refund; never posts negative grants."""
    _require_pilot()
    results = []
    for name in _rewards_for(enrollment_name):
        reward = _lock_reward(name)
        if reward.status == 'Reversed':
            results.append({'reward': name, 'status': 'already_reversed'})
            continue
        if reward.status in ('On Hold', 'Pending', 'Approved'):
            pass
        elif reward.status == 'Available' and reward.reward_track == 'Member Credit':
            grant = _referral_grant_state(reward)
            # Preserve lock order used by wallet spending: Member -> Wallet.
            from club100_core.wallet.spending import _locked_wallet
            from club100_core.wallet.core import _internal_ledger_post
            wallet = _locked_wallet(grant.member)
            if grant.wallet != wallet:
                raise ValueError('Referral grant wallet changed')
            # Recheck under lock to avoid a spend between preview and reversal.
            _referral_grant_state(reward)
            event_key = 'REFERRAL:REVERSE:' + grant.name
            if frappe.db.exists('Club100 Wallet Ledger', {'event_key': event_key}):
                raise ValueError('Reversal event already exists without reversed reward status')
            with _internal_ledger_post():
                frappe.get_doc({
                    'doctype': 'Club100 Wallet Ledger', 'event_key': event_key,
                    'wallet': wallet, 'member': grant.member,
                    'grant': grant.name, 'entry_type': 'Reversal',
                    'amount': -money(grant.amount), 'currency': 'INR',
                    'reference': reward.name,
                    'notes': 'Full refund of referred enrollment ' + str(enrollment_name),
                }).insert(ignore_permissions=True)
        else:
            raise ValueError('Referral reward cannot be reversed automatically')
        with _reward_write():
            reward.status = 'Reversed'
            reward.notes = (str(reward.notes or '') + '\nReversed following verified full refund of ' + enrollment_name)[-1400:]
            reward.save(ignore_permissions=True)
        results.append({'reward': name, 'status': 'Reversed'})
    return results


def scheduled_mature_referral_rewards():
    """Daily job, always disabled unless explicit local pilot flag is on."""
    if not _enabled():
        return {'status': 'disabled'}
    approval_result = scheduled_auto_approve_rewards() if _auto_enabled() else {'status': 'disabled'}
    due = frappe.get_all('Club100 Referral Reward', filters={
        'status': 'Approved', 'basis_verification_status': 'Verified',
        'eligible_on': ['<=', nowdate()], 'reward_track': 'Member Credit',
    }, pluck='name', order_by='eligible_on asc, creation asc', limit_page_length=100)
    done, blocked = [], []
    for name in due:
        point = 'club100_referral_maturity'
        frappe.db.savepoint(point)
        try:
            done.append(mature_one(name, commit=False))
        except Exception as exc:
            frappe.db.rollback(save_point=point)
            blocked.append({'reward': name, 'reason': str(exc)[:180]})
        # Commit each successful reward separately; don't lose all due credits if
        # a subsequent item fails. Frappe scheduler also manages transaction.
        else:
            frappe.db.commit()
    return {'status': 'processed', 'auto_approval': approval_result,
            'completed': len(done), 'blocked': blocked}


def healthcheck():
    return {
        'site': frappe.local.site,
        'enabled': _enabled(), 'hold_days': _hold_days(),
        'auto_approval_enabled': _auto_enabled(),
        'local_gst_rate_percent': str(frappe.conf.get('club100_referral_local_gst_rate_percent') or ''),
        'on_hold': frappe.db.count('Club100 Referral Reward', {'status': 'On Hold'}),
        'approved': frappe.db.count('Club100 Referral Reward', {'status': 'Approved'}),
        'credited': frappe.db.count('Club100 Referral Reward', {'status': 'Available', 'reward_track': 'Member Credit'}),
        'ambassador_auto_payout_enabled': False,
        'requires_manual_tax_basis_verification': not _auto_enabled(),
        'manual_review_required_for_unsupported_or_missing_invoice_evidence': True,
        'automatic_modes': ['Submitted Invoice', 'Local Tax Snapshot (PWA Self only)'],
        'invoice_generation_in_checkout': False,
    }
