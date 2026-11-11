"""LOCAL-ONLY Stage 2D-2 DB smoke. No Razorpay API calls. ALL test data rolled back."""
import os
import uuid
from decimal import Decimal
import frappe
from frappe.utils import nowdate
from club100_core.wallet.logic import money
from club100_core.wallet.core import wallet_balance
from club100_core.wallet.checkout import enabled, attach_reservation, wallet_only_paid, before_gateway_order
from club100_core.api.commerce_checkout import _quote


def run():
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_SPLIT_TEST') != '1':
        raise RuntimeError('Restricted to club100.local with CLUB100_ALLOW_SPLIT_TEST=1')
    if not enabled():
        return {'outcome': 'SKIP', 'reason': 'Set club100_split_checkout_enabled=1 with Razorpay test mode on club100.local'}
    plan = frappe.db.get_value('Club100 Plan', {'plan_code': 'self', 'status': 'Active'}, 'name')
    if not plan:
        return {'outcome': 'SKIP', 'reason': 'No active Self plan'}
    members = frappe.get_all('Club100 Member', filters={'status': 'Active', 'onboarding_status': 'Completed'},
                             fields=['name'], limit_page_length=100)
    # Only programs that can initialize the required Standard L1 journey.
    programs = sorted(set(frappe.get_all('Club100 Program Curriculum',
        filters={'track': 'Standard', 'level_code': 'L1', 'active': 1},
        pluck='program', limit_page_length=500)))
    if not programs:
        return {'outcome': 'SKIP', 'reason': 'No active Standard/Core (L1) curriculum configured for Self checkout. Configure program curriculum master data first.'}
    chosen = None
    for m in members:
        member = frappe.get_doc('Club100 Member', m.name)
        for program in programs:
            try:
                quote = _quote(member, program, 1)
                chosen = (member, quote)
                break
            except Exception:
                continue
        if chosen:
            break
    if not chosen:
        return {'outcome': 'SKIP', 'reason': 'No eligible active member/program combination with an active Standard/Core curriculum for a new Self purchase'}
    member, quote = chosen
    gross = money(quote['price']['amount_payable'])
    if gross <= Decimal('2.00'):
        return {'outcome': 'SKIP', 'reason': 'Unexpected Self price'}
    watch = ('Club100 Enrollment', 'Club100 Wallet', 'Club100 Wallet Grant',
             'Club100 Wallet Ledger', 'Club100 Wallet Reservation')
    before = {dt: frappe.db.count(dt) for dt in watch}
    token = uuid.uuid4().hex[:16]
    original_user = frappe.session.user
    frappe.db.savepoint('wallet_checkout_v2d2')
    steps = []
    try:
        frappe.set_user('Administrator')
        frappe.get_doc({'doctype': 'Club100 Wallet Grant', 'member': member.name,
                        'source_type': 'Promotion', 'source_event_key': 'SPLIT-SMOKE:'+token,
                        'funding_reference': 'LOCAL-ONLY-SMOKE:'+token,
                        'amount': str(gross), 'currency': 'INR', 'valid_from': nowdate(),
                        'validity_type': 'Lifetime', 'status': 'Active'}).insert(ignore_permissions=True)
        steps.append('Temporary lifetime grant and ledger credit inserted')
        initial = money(wallet_balance(member.name)['available'])
        def pending():
            return frappe.get_doc({'doctype':'Club100 Enrollment', 'member':member.name,
              'full_name':member.full_name or member.name, 'mobile':member.mobile, 'email':member.email,
              'program':quote['program'], 'plan':quote['plan'], 'pricing_model':'Online Plan',
              'enrollment_type':'Individual', 'enrollment_date':nowdate(),
              'status':'Registered', 'payment_status':'Pending', 'start_date':quote['start'],
              'membership_duration_months':1,
              'monthly_equivalent':quote['price']['monthly_equivalent'],
              'amount_payable':quote['price']['amount_payable'],
              'website_source':'PWA Commerce',
              'origin_trial_enrollment':quote['trial'].name if quote['trial'] else None}).insert(ignore_permissions=True)
        # First exercise a split without contacting Razorpay or marking Paid.
        frappe.db.savepoint('wallet_checkout_split_preview')
        split = pending()
        split_amount = gross - Decimal('1.00')
        attach_reservation(split, str(split_amount))
        assert before_gateway_order(split) == Decimal('1.00')
        assert money(wallet_balance(member.name)['available']) == initial - split_amount
        assert split.payment_status == 'Pending' and split.wallet_settlement_status == 'Reserved'
        steps.append('Split tender stored as gross, held wallet portion and INR 1 gateway remainder')
        frappe.db.rollback(save_point='wallet_checkout_split_preview')
        # Then wallet-only, explicitly bypass commit so the outer savepoint works.
        all_wallet = pending()
        attach_reservation(all_wallet, str(gross))
        result = wallet_only_paid(all_wallet, commit=False)
        assert result['success'] and result['wallet_only'] and result['payment_status'] == 'Paid'
        assert all_wallet.wallet_settlement_status == 'Settled'
        assert all_wallet.payment_gateway == 'Club100 Wallet'
        assert not all_wallet.get('razorpay_capture_verified')
        assert money(wallet_balance(member.name)['available']) == initial - gross
        assert frappe.db.exists('Club100 Wallet Ledger', {'reference':all_wallet.name,'entry_type':'Spend'})
        steps.append('Wallet-only purchase debited ledger and finalized paid enrollment without Razorpay')
    finally:
        frappe.db.rollback(save_point='wallet_checkout_v2d2')
        frappe.set_user(original_user)
    after = {dt: frappe.db.count(dt) for dt in watch}
    if before != after:
        raise RuntimeError('Smoke rollback failed: ' + repr({'before':before, 'after':after}))
    return {'site': frappe.local.site, 'outcome': 'PASS', 'passed':len(steps),
            'cases':steps, 'rollback_verified_by_record_counts': True,
            'note':'Razorpay verification/webhook and refund reconciliation still require sandbox testing.'}
