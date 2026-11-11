"""Club100 referral next-stage preflight (local rollback-only).

Copies to club100_core/referrals/next_positive_uat_local.py and executes via
  bench --site club100.local execute club100_core.referrals.next_positive_uat_local.run

Validates an atomic simulated mature referral grant + backed GL + Referral-only
held wallet allocation + hybrid financial preflight and a read-only renewal
classification check. DOES NOT charge Razorpay, create an order/invoice/payment,
create a renewal enrollment or persist credits. Never use on production.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate, nowdate

from club100_core.accounting import funding, settlement as accounting
from club100_core.accounting.hybrid_gst import current_rate, inclusive_breakdown
from club100_core.accounting.wallet_backing import verify_wallet_grant_backing
from club100_core.referrals import settlement as referral
from club100_core.referrals.settlement_logic import money
from club100_core.wallet import core, spending

SITE = 'club100.local'
REWARD = 'RPAY:pay_TxJNlIfJcAtJOm'
REFERRER = 'C100-MEM-00240'
REFERRED = 'C100-MEM-00242'
ENROLLMENT = 'C100-ENR-00244'
EXPECTED_REWARD = Decimal('76.10')
EXPECTED_BASIS = Decimal('760.95')
EXPECTED_GATEWAY = Decimal('799.00')
NEW_MEMBERSHIP_PRICE = Decimal('799.00')
EXPECTED_GATEWAY_SPLIT = Decimal('722.90')
SAVEPOINT = 'club100_referral_positive_preflight'


def _require(ok, msg):
    if not ok:
        raise AssertionError(msg)


def _amount(v):
    return Decimal(str(v or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _snapshot(source_key):
    reward = frappe.db.get_value('Club100 Referral Reward', REWARD,
        ['status','settlement_reference','settled_on','eligible_on','verified_reward_amount'], as_dict=True)
    return {
        'reward': dict(reward) if reward else None,
        'grant_names': frappe.get_all('Club100 Wallet Grant',
            filters={'source_event_key':source_key}, pluck='name', limit_page_length=10),
        'credits': frappe.db.count('Club100 Wallet Ledger',
            {'member':REFERRER, 'entry_type':'Credit'}),
        'reservations': frappe.db.count('Club100 Wallet Reservation', {'member':REFERRER}),
        'accounting_events': frappe.db.count('Club100 Wallet Accounting Event'),
        'journals': frappe.db.count('Journal Entry'),
        'gl_entries': frappe.db.count('GL Entry'),
    }


def _renewal_preflight():
    """Read-only check of facts used for next purchase's reward classification."""
    attr_id = frappe.db.get_value('Club100 Referral Attribution',
        {'referred_member':REFERRED, 'status':'Captured'}, 'name')
    _require(attr_id, 'Existing referral attribution not Captured')
    attr = frappe.get_doc('Club100 Referral Attribution', attr_id)
    _require(attr.reward_track == 'Member Credit' and _amount(attr.renewal_rate) == Decimal('2.00'),
             'Captured renewal policy is not Member Credit / 2%')
    prior = frappe.db.sql("""SELECT name, payment_id FROM `tabClub100 Enrollment`
        WHERE member=%s AND payment_status='Paid' AND is_trial=0
        ORDER BY COALESCE(payment_date, creation) DESC LIMIT 10""",
        (REFERRED,), as_dict=True)
    _require(any(p.name == ENROLLMENT for p in prior), 'The verified first paid enrollment is missing')
    first = frappe.get_doc('Club100 Enrollment', ENROLLMENT)
    _require(first.payment_status == 'Paid' and first.payment_gateway == 'Razorpay'
             and first.payment_id == REWARD.removeprefix('RPAY:')
             and bool(first.razorpay_capture_verified)
             and bool(first.sales_invoice), 'The paid first enrollment is not verified')
    _require(frappe.db.get_value('Sales Invoice', first.sales_invoice, 'docstatus') == 1,
             'First enrollment invoice is not submitted')
    # The second verified purchase's finalizer classifies prior Paid enrollments
    # as Renewal; no second purchase is created here.
    gst = _amount(current_rate())
    _require(gst == Decimal('5.00'),
             'This illustrative ₹799 renewal scenario expects configured 5% inclusive GST')
    verified_net = _amount(NEW_MEMBERSHIP_PRICE * Decimal('100') / (Decimal('100') + gst))
    verified_reward = _amount(verified_net * _amount(attr.renewal_rate) / Decimal('100'))
    _require(verified_net == Decimal('760.95') and verified_reward == Decimal('15.22'),
             'Unexpected renewal tax-exclusive basis or verified reward')
    return {
        'result': 'READ_ONLY_PRECHECK_PASS', 'referred_member':REFERRED,
        'prior_paid_enrollment':ENROLLMENT, 'captured_renewal_rate':'2.00',
        'hypothetical_new_gateway_payment':'799.00',
        'hypothetical_verified_ex_gst_basis':str(verified_net),
        'hypothetical_verified_renewal_reward':str(verified_reward),
        'classification_for_new_verified_paid_enrollment':'Renewal',
        'actual_new_enrollment_created':False,
        'actual_renewal_reward_created':False,
        'limitation':'The actual second checkout and reward callback are NOT tested here',
    }


def run(reward_name=REWARD):
    _require(frappe.local.site == SITE, 'LOCAL ONLY: club100.local required')
    _require(str(frappe.conf.get('razorpay_mode') or 'test').lower() == 'test',
             'Razorpay Test Mode required')
    _require(frappe.session.user == 'Administrator'
             or 'System Manager' in frappe.get_roles(frappe.session.user),
             'Administrator or System Manager required')
    _require(reward_name == REWARD, 'This UAT is pinned to the approved ₹76.10 reward')
    _require(referral._enabled() and funding._post_allowed(),
             'Referral and accounting local Test Mode services must be enabled')
    _require('CLUB100_REFERRAL_ATOMIC_GL_V1' in open(referral.__file__, encoding='utf-8').read(),
             'Atomic reward + GL settlement patch not installed')
    reward = frappe.get_doc('Club100 Referral Reward', REWARD)
    _require(reward.status == 'Approved' and reward.basis_verification_status == 'Verified'
             and reward.reward_track == 'Member Credit'
             and reward.reward_event == 'First Purchase', 'Reward must be verified Approved first purchase')
    _require(reward.referred_member == REFERRED and reward.enrollment == ENROLLMENT,
             'Unexpected referred member / original enrollment')
    _require(frappe.db.get_value('Club100 Referral Partner', reward.partner, 'member') == REFERRER,
             'Referrer is not the expected member')
    _require(money(reward.verified_reward_amount) == EXPECTED_REWARD
             and money(reward.verified_basis_amount) == EXPECTED_BASIS,
             'Verified referral reward amount changed')
    _require(str(reward.eligible_on)[:10] == '2026-12-07'
             and getdate(nowdate()) < getdate(reward.eligible_on),
             'Existing reward already mature or maturity date changed; do not spoof')
    _require(not reward.settlement_reference and not reward.settled_on,
             'Reward has already settled')
    _require(reward.payment_reference == REWARD.removeprefix('RPAY:'),
             'Unexpected original payment ID')
    renewal_check = _renewal_preflight()
    source_key = referral.REFERRAL_KEY_PREFIX + REWARD
    before = _snapshot(source_key)
    _require(not before['grant_names'], 'Existing referral already issued a real grant')
    simulated_day = str(getdate(reward.eligible_on) + timedelta(days=1))
    simulated_datetime = datetime.combine(getdate(simulated_day), time(12, 0, 0))
    temporary_grant = None
    temporary_journal = None
    temporary_reservation = None
    result = None
    res_key = 'UAT-REF-POS-' + uuid4().hex[:15]
    check_items = []
    frappe.db.savepoint(SAVEPOINT)
    try:
        def mock_gateway(payment_id, expected_amount):
            _require(payment_id == reward.payment_reference
                     and money(expected_amount) == EXPECTED_GATEWAY,
                     'Mock gateway got an unexpected ID or amount')
            return True
        def forbid_commit(*_a, **_kw):
            raise AssertionError('UAT attempted to COMMIT; this must be rollback-only')
        with patch.object(frappe.db, 'commit', side_effect=forbid_commit):
            try:
                referral.mature_one(REWARD, commit=False)
            except ValueError as exc:
                _require('hold period' in str(exc).lower(),
                         'Unexpected early maturity error: ' + str(exc))
            else:
                raise AssertionError('Unmatured reward was credited prematurely')
            _require(_snapshot(source_key) == before,
                     'Early maturity attempt altered state')
            check_items.append('Real-time premature settlement blocked')
            with patch.object(referral, 'nowdate', return_value=simulated_day), \
                 patch.object(referral, '_gateway_is_captured_without_refund', side_effect=mock_gateway), \
                 patch.object(spending, 'now_datetime', return_value=simulated_datetime), \
                 patch.object(core, 'now_datetime', return_value=simulated_datetime):
                settled = referral.mature_one(REWARD, commit=False)
                _require(settled.get('status') == 'Available'
                         and settled.get('accounting_status') in ('Posted', 'Already Posted'),
                         'Atomic maturity failed to book Referral liability')
                temporary_grant = settled.get('wallet_grant')
                temporary_journal = settled.get('accounting_journal')
                _require(temporary_grant and temporary_journal,
                         'Atomic settlement did not produce grant and JE')
                check_items.append('Temporary ₹76.10 Referral grant + posted liability in same transaction')
                cfg = accounting._config()
                allocation = {'grant': temporary_grant, 'source':'Referral', 'amount':'76.10'}
                backed = verify_wallet_grant_backing(cfg, [allocation])
                _require(money(backed.get('Referral')) == EXPECTED_REWARD,
                         'Source-backed grant verification failed')
                accounting._require_wallet_liability_backing(
                    cfg, {'Referral':EXPECTED_REWARD}, [allocation])
                check_items.append('Referral-specific posted GL and liability headroom verified')
                held = spending.reserve(REFERRER, res_key, 'LOCAL-UAT-REFERRAL-SPLIT',
                    '76.10', ttl_minutes=15, allowed_sources=('Referral',),
                    allowed_grants=(temporary_grant,))
                temporary_reservation = held.get('reservation')
                _require(held.get('status') == 'Reserved' and temporary_reservation,
                         'Referral-only reservation missing')
                alloc = held.get('allocations') or []
                _require(len(alloc)==1 and alloc[0]['grant']==temporary_grant
                         and alloc[0]['source']=='Referral'
                         and money(alloc[0]['amount'])==EXPECTED_REWARD,
                         'Reservation allocated the wrong grant/source/amount')
                accounting._require_wallet_liability_backing(
                    cfg, {'Referral':EXPECTED_REWARD}, alloc)
                held_again = spending.reserve(REFERRER, res_key, 'LOCAL-UAT-REFERRAL-SPLIT',
                    '76.10', allowed_sources=('Referral',), allowed_grants=(temporary_grant,))
                _require(held_again['reservation'] == temporary_reservation,
                         'Reservation replay allocated duplicate credit')
                check_items.append('Referral-only wallet hold and replay verified')
                taxes = accounting._ready(cfg, {'Referral':EXPECTED_REWARD}, EXPECTED_GATEWAY_SPLIT)
                _require(bool(taxes), 'No configured inclusive GST template')
                rate = _amount(current_rate())
                _require(rate == Decimal('5.00'), 'This UAT amount assumes configured 5% GST')
                breakdown = inclusive_breakdown(
                    str(NEW_MEMBERSHIP_PRICE), '0.00', '76.10', str(rate))
                _require(money(breakdown['invoice']) == NEW_MEMBERSHIP_PRICE
                         and money(breakdown['net']) == Decimal('760.95')
                         and money(breakdown['gst']) == Decimal('38.05'),
                         'Hybrid discount/tax model changed; review before a paid checkout')
                _require(NEW_MEMBERSHIP_PRICE - EXPECTED_REWARD == EXPECTED_GATEWAY_SPLIT,
                         'Hybrid quote does not reconcile')
                check_items.append('₹799 invoice + ₹76.10 Referral tender + ₹722.90 gateway preflight')
                again = referral.mature_one(REWARD, commit=False)
                _require(again.get('wallet_grant') == temporary_grant,
                         'Maturity replay attempted second grant')
                _require(frappe.db.count('Club100 Wallet Accounting Event',
                         {'grant':temporary_grant}) == 1,
                         'Maturity replay duplicated accounting event')
                check_items.append('Atomic settlement replay did not duplicate liability')
                result = {
                    'result':'PASS', 'site':SITE, 'reward':REWARD,
                    'referrer':REFERRER, 'referred_member':REFERRED,
                    'synthetic_future_date':simulated_day,
                    'temporary_wallet_grant':temporary_grant,
                    'temporary_referral_je':temporary_journal,
                    'temporary_reservation':temporary_reservation,
                    'invoice_amount':'799.00', 'referral_wallet_tender':'76.10',
                    'gateway_due':'722.90', 'gst_inclusive':'38.05',
                    'source_specific_gl_backing':'VERIFIED',
                    'checks':check_items, 'renewal':renewal_check,
                    'actual_razorpay_payment_or_invoice_created':False,
                    'actual_renewal_payment_tested':False,
                    'external_gateway_validation':'MOCKED for simulated maturity only',
                    'persisted':False,
                }
    finally:
        frappe.db.rollback(save_point=SAVEPOINT)
    _require(_snapshot(source_key)==before, 'CRITICAL: SAVEPOINT ROLLBACK DID NOT RESTORE STATE')
    if temporary_grant:
        _require(not frappe.db.exists('Club100 Wallet Grant', temporary_grant),
                 'Temporary Referral Wallet Grant remained after rollback')
    if temporary_journal:
        _require(not frappe.db.exists('Journal Entry', temporary_journal),
                 'Temporary GL Journal Entry remained after rollback')
    if temporary_reservation:
        _require(not frappe.db.exists('Club100 Wallet Reservation', temporary_reservation),
                 'Temporary wallet reservation remained after rollback')
    _require(result is not None, 'No result after UAT rollback')
    result['rollback_verified']=True
    return result
