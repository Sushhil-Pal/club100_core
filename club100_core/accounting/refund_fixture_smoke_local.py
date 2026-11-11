"""Stage 5F-C4: deterministic amounts, ROLLBACK-ONLY ERPNext wallet/refund fixture.

Creates synthetic UAT Bank Account, Payment Entry, corporate funding pool, employee
mapping, wallet grant/reservation, and a scheduled Self enrollment within ONE
uncommitted DB transaction. Uses actual ERPNext GL posting code for both funding
and the membership/refund vouchers, but NEVER represents a cleared bank deposit
or a real captured Razorpay payment. Split's refund GET is strictly mocked.

All fixture records must roll back. Internal commit calls are blocked. Never
run this on production or treat a passing test as bank-statement reconciliation.
No public/whitelisted method. No scheduler or checkout hook calls this module.
"""
import os
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import add_days, cint, getdate, now_datetime, nowdate

from club100_core.accounting.fixture_rules import scenario, check_refund_proof
from club100_core.accounting import accounted_refunds as refunds
from club100_core.accounting.settlement import (
    _admin, _local_pilot, _config, post_one, preview_one,
)
from club100_core.accounting.funding import post_corporate_receipt, CORPORATE_EVENT
from club100_core.wallet.spending import reserve, capture


# Changes are limited to frappe.conf for THIS bench process. Site config is
# not written. All three posting services retain their normal built-in guards.
TEMP_FLAGS = {
    'club100_accounting_post_enabled': 1,
    'club100_accounting_funding_post_enabled': 1,
    'club100_accounting_refund_post_enabled': 1,
    'club100_round4b_enabled': 1,
}


def _guard():
    _admin()
    if (not _local_pilot()
            or os.environ.get('CLUB100_ALLOW_STAGE5FC4_ROLLBACK_FIXTURE') != '1'
            or os.environ.get('CLUB100_ALLOW_STAGE5F_POST') != '1'
            or os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND') != '1'
            or os.environ.get('CLUB100_ALLOW_ACCOUNTED_REFUND_SMOKE') != '1'):
        raise ValueError('Stage5F-C4 requires local Test Mode plus four explicit CLI guards')
    if str(frappe.conf.get('club100_accounting_post_enabled') or 0) not in ('0', 'False', 'false'):
        raise ValueError('Disable persistent accounting posting flag before running rollback fixture')
    if str(frappe.conf.get('club100_accounting_refund_post_enabled') or 0) not in ('0', 'False', 'false'):
        raise ValueError('Disable persistent refund posting flag before running rollback fixture')
    if str(frappe.conf.get('club100_accounting_funding_post_enabled') or 0) not in ('0', 'False', 'false'):
        raise ValueError('Disable persistent corporate GL posting flag before running rollback fixture')
    if str(frappe.conf.get('club100_round4b_enabled') or 0) not in ('0', 'False', 'false'):
        raise ValueError('Disable persistent Round4B flag before running rollback fixture')


@contextmanager
def _temporary_flags():
    prior = {k: frappe.conf.get(k) for k in TEMP_FLAGS}
    try:
        for k, value in TEMP_FLAGS.items():
            frappe.conf[k] = value
        yield
    finally:
        for k, value in prior.items():
            if value is None:
                frappe.conf.pop(k, None)
            else:
                frappe.conf[k] = value


def _account_parent(company):
    rows = frappe.get_all('Account', filters={'company': company, 'is_group': 1,
                                              'root_type': 'Asset'},
                          fields=['name', 'account_name', 'lft'],
                          order_by='lft desc', limit_page_length=100)
    for row in rows:
        if 'current assets' in str(row.account_name).lower():
            return row.name
    if rows:
        return rows[0].name
    raise ValueError('No Asset group exists for a transient UAT-only Bank account')


def _receivable(company):
    row = frappe.db.get_value('Account', {'company': company, 'is_group': 0,
                                          'account_type': 'Receivable'}, 'name')
    if not row:
        raise ValueError('No receivable account available for UAT advance Payment Entry')
    return row


def _reference_enrollment():
    rows = frappe.get_all('Club100 Enrollment', filters={
        'pricing_model': 'Online Plan', 'website_source': 'PWA Commerce',
        'payment_status': 'Paid', 'is_trial': 0,
    }, fields=['name', 'plan', 'program'], order_by='creation desc', limit_page_length=50)
    for r in rows:
        if r.plan and r.program and frappe.db.get_value('Club100 Plan', r.plan, 'plan_code') == 'self':
            return r
    raise ValueError('A configured Self plan and program are required; no paid PWA Self template found')


def _create_member(nonce, company_customer):
    # A synthetic Member is safer than altering an existing person's enrollments,
    # reservations or wallet grants, even temporarily.
    mobile = '9' + str(int(nonce[:10], 16) % (10 ** 9)).zfill(9)
    member = frappe.get_doc({
        'doctype': 'Club100 Member', 'first_name': 'UAT Refund',
        'last_name': 'ROLLBACK ' + nonce[:8], 'mobile': mobile,
        'status': 'Active', 'member_type': 'Corporate',
        'organization': company_customer, 'current_fitness_level': 'Beginner',
        'joining_date': nowdate(),
        'notes': 'STAGE5FC4 SYNTHETIC TEST: ALWAYS ROLLBACK; NO REAL CUSTOMER',
    })
    member.insert(ignore_permissions=True)
    return member


def _create_funding(nonce, member, customer, cfg, value):
    """Simulated ERPNext accounting ONLY; no actual bank statement or receipt."""
    company = cfg['company']
    bank = frappe.get_doc({
        'doctype': 'Account', 'company': company,
        'account_name': 'UAT ROLLBACK Bank ' + nonce[:10],
        'parent_account': _account_parent(company),
        'account_type': 'Bank', 'account_currency': 'INR', 'is_group': 0,
    })
    bank.insert(ignore_permissions=True)
    receipt_reference = 'UAT-ROLLBACK-NOT-REAL-' + nonce[:10]
    payment = frappe.get_doc({
        'doctype': 'Payment Entry', 'company': company,
        'payment_type': 'Receive', 'party_type': 'Customer', 'party': customer,
        'posting_date': nowdate(), 'paid_from': _receivable(company),
        'paid_to': bank.name, 'paid_amount': float(value),
        'received_amount': float(value), 'source_exchange_rate': 1,
        'target_exchange_rate': 1, 'reference_no': receipt_reference,
        'reference_date': nowdate(),
        'remarks': 'SYNTHETIC ROLLBACK-ONLY UAT advance; NOT BANK FUNDS',
    })
    payment.insert(ignore_permissions=True)
    payment.submit()
    if payment.docstatus != 1:
        raise ValueError('Temporary UAT receipt did not create a submitted GL voucher')
    pool = frappe.get_doc({
        'doctype': 'Club100 Corporate Credit Pool',
        'funding_event_key': 'UAT5FC4:POOL:' + nonce,
        'corporate_customer': customer, 'funding_reference': receipt_reference,
        'amount': float(value), 'currency': 'INR',
        'funding_verified': 1, 'status': 'Funded',
        'notes': 'SYNTHETIC UAT POSTING; NOT A CLEARED BANK RECEIPT; ROLLBACK ONLY',
    })
    pool.insert(ignore_permissions=True)
    posted = post_corporate_receipt(pool.name, payment.name, commit=False)
    if posted.get('status') != 'Posted':
        raise ValueError('Corporate advance was not reclassified in test GL')
    employee = frappe.get_doc({
        'doctype': 'Club100 Corporate Employee', 'corporate_customer': customer,
        'member': member.name, 'status': 'Active',
        'notes': 'SYNTHETIC UAT EMPLOYEE; ROLLBACK ONLY',
    })
    employee.insert(ignore_permissions=True)
    grant = frappe.get_doc({
        'doctype': 'Club100 Wallet Grant', 'member': member.name,
        'source_event_key': 'UAT5FC4:GRANT:' + nonce,
        'source_type': 'Corporate', 'corporate_pool': pool.name,
        'amount': float(value), 'currency': 'INR',
        'valid_from': nowdate(), 'validity_type': 'Lifetime',
        'status': 'Active', 'notes': 'SYNTHETIC UAT; ROLLBACK ONLY',
    })
    grant.insert(ignore_permissions=True)
    if grant.corporate_pool != pool.name or grant.funding_verified != 1:
        raise ValueError('Temporary corporate grant not linked to funded UAT pool')
    return bank, payment, pool, grant


def _create_enrollment(nonce, member, values, reference):
    start = getdate(add_days(nowdate(), 45))
    end = getdate(add_days(start, 35))
    enrollment = frappe.get_doc({
        'doctype': 'Club100 Enrollment', 'member': member.name,
        'program': reference.program, 'plan': reference.plan,
        'enrollment_type': 'Individual', 'enrollment_date': nowdate(),
        'start_date': start, 'end_date': end,
        'status': 'Registered', 'payment_status': 'Pending',
        'pricing_model': 'Online Plan', 'website_source': 'PWA Commerce',
        'is_trial': 0, 'membership_duration_months': 1,
        'monthly_equivalent': float(values['gross']),
        'amount_payable': float(values['gross']),
        'notes': 'STAGE5FC4 SYNTHETIC SELF UAT; NO ACTUAL PAYMENT; ROLLBACK ONLY',
    })
    enrollment.insert(ignore_permissions=True)
    key = 'WALLETCHK:' + enrollment.name
    hold = reserve(member.name, key, enrollment.name, str(values['wallet']),
                   ttl_minutes=60, allowed_sources=('Corporate',))
    if hold['status'] != 'Reserved' or len(hold['allocations']) != 1:
        raise ValueError('UAT wallet reservation did not use exactly one corporate grant')
    captured = capture(key, enrollment.name)
    if captured['status'] != 'Captured':
        raise ValueError('UAT wallet reservation was not captured')
    enrollment.reload()
    enrollment.wallet_reservation_key = key
    enrollment.wallet_applied_amount = float(values['wallet'])
    enrollment.gateway_due_amount = float(values['gateway'])
    enrollment.wallet_settlement_status = 'Settled'
    enrollment.payment_status = 'Paid'
    enrollment.status = 'Scheduled'
    enrollment.payment_date = now_datetime()
    if values['gateway']:
        enrollment.payment_gateway = 'Razorpay'
        enrollment.payment_id = 'pay_UAT5FC4' + nonce
        enrollment.payment_order_id = 'order_UAT5FC4' + nonce
        enrollment.razorpay_capture_verified = 1  # SYNTHETIC, ROLLBACK ONLY
    else:
        enrollment.payment_gateway = 'Club100 Wallet'
        enrollment.payment_id = 'WALLET:' + enrollment.name
        enrollment.razorpay_capture_verified = 0
    enrollment.save(ignore_permissions=True)
    return enrollment, hold


def _check_preconditions(cfg):
    if any(not cfg.get(key) for key in ('company', 'customer', 'item', 'tax_template',
                                       'razorpay_clearing', 'Corporate')):
        raise ValueError('Stage5E accounting configuration incomplete')
    customer = frappe.db.get_value('Customer', {'name': 'NasdaQ'}, 'name')
    if not customer:
        # Use configured UAT customer if no separate corporate Customer exists.
        customer = cfg['customer']
    if not frappe.db.exists('Customer', customer):
        raise ValueError('No corporate UAT customer exists')
    if not frappe.db.exists('Account', cfg['Corporate']):
        raise ValueError('Corporate liability account not configured')
    return customer, _reference_enrollment()


def run(mode='wallet'):
    _guard()
    values = scenario(mode)
    cfg = _config()
    customer, reference = _check_preconditions(cfg)
    # All amounts are deterministic, but identifiers are randomized to avoid
    # collisions even if another rollback smoke is running concurrently.
    nonce = frappe.generate_hash(length=14)
    original_get = refunds._read_refund
    output = None
    stage = 'start'
    with (patch.object(frappe.db, 'commit', side_effect=RuntimeError(
            'Stage5F-C4 forbids ALL DB commits during rollback fixture')),
          patch('requests.sessions.Session.request', side_effect=RuntimeError(
            'Stage5F-C4 forbids network requests during rollback fixture'))):
        try:
            with _temporary_flags():
                stage = 'synthetic_member'
                member = _create_member(nonce, customer)
                stage = 'simulated_bank_and_corporate_gl'
                bank, receipt, pool, grant = _create_funding(
                    nonce, member, customer, cfg, values['wallet'])
                stage = 'synthetic_checkout'
                enrollment, reservation = _create_enrollment(nonce, member, values, reference)
                if reservation['allocations'][0]['grant'] != grant.name:
                    raise ValueError('UAT reservation used a different grant')
                stage = 'source_backing_preview'
                preview = preview_one(enrollment.name)
                if preview['status'] != 'Ready':
                    raise ValueError('Source-backed accounting preview blocked: ' + repr(preview['issues']))
                stage = 'real_erpnext_invoice_and_tender'
                posted = post_one(enrollment.name, commit=False)
                if posted['status'] != 'Posted' or posted['wallet'] != str(values['wallet']):
                    raise ValueError('ERPNext original membership vouchers not fully posted')
                mapping_name = frappe.db.get_value('Club100 Membership Accounting',
                              {'enrollment': enrollment.name}, 'name')
                if not mapping_name:
                    raise ValueError('Original membership accounting mapping missing')
                stage = 'actual_erpnext_refund_vouchers'
                refund_id = ('rfnd_UAT5FC4' + nonce) if values['gateway'] else None
                if refund_id:
                    def mocked_get(rid):
                        if rid != refund_id:
                            raise ValueError('Unexpected synthetic refund identity')
                        return {'id': refund_id, 'payment_id': enrollment.payment_id,
                                'status': 'processed', 'currency': 'INR',
                                'amount': int(values['gateway'] * 100)}
                    refunds._read_refund = mocked_get
                refunded = refunds.post_accounted_full_refund(enrollment.name, refund_id, commit=False)
                audit = check_refund_proof(refunded, values)
                stage = 'verify_inner_rollback'
                if frappe.db.get_value('Club100 Membership Accounting', mapping_name, 'status') != 'Posted':
                    raise ValueError('Refund simulation did not restore initial accounting mapping')
                if frappe.db.exists('Sales Invoice', refunded['credit_note']):
                    raise ValueError('Refund credit note survived inner simulation rollback')
                output = {'outcome': 'PASS', 'mode': mode,
                    'enrollment': enrollment.name,
                    'corporate_pool': pool.name, 'wallet_grant': grant.name,
                    'synthetic_original_payment': enrollment.payment_id,
                    'initial_invoice': posted['invoice'],
                    'refund_credit_note': refunded['credit_note'],
                    'voucher_reconciliation': audit,
                    'synthetic_payment_and_bank_receipt': True,
                    'source_backed_within_rollback_fixture': True,
                    'bank_statement_reconciled': False,
                    'external_razorpay_calls': 0,
                    'no_commit_allowed': True,
                    'permanent_financial_records': 0}
        except Exception as exc:
            output = {'outcome': 'FAIL', 'mode': mode, 'stage': stage,
                      'error_type': type(exc).__name__, 'detail': str(exc)[:650],
                      'permanent_financial_records': 0}
        finally:
            refunds._read_refund = original_get
            # We own the whole CLI transaction, not just a savepoint. This
            # removes even setup Account/Customer/Member records and GL rows.
            frappe.db.rollback()
    # Confirm no fixture identities survived rollback, even on early failure
    # before a Python doc variable was assigned. Never infer from counts alone.
    survivors = []
    checks = [
        ('Account', {'company': cfg['company'], 'account_name': 'UAT ROLLBACK Bank ' + nonce[:10]}),
        ('Club100 Corporate Credit Pool', {'funding_event_key': 'UAT5FC4:POOL:' + nonce}),
        ('Club100 Wallet Grant', {'source_event_key': 'UAT5FC4:GRANT:' + nonce}),
        ('Payment Entry', {'reference_no': 'UAT-ROLLBACK-NOT-REAL-' + nonce[:10]}),
        ('Club100 Wallet Accounting Event', {'event_key': CORPORATE_EVENT + getattr(locals().get('pool'), 'name', '__none__')}),
    ]
    for doctype, filters in checks:
        if frappe.db.exists(doctype, filters):
            survivors.append(doctype)
    for doctype, item in (('Club100 Member', locals().get('member')),
                          ('Club100 Enrollment', locals().get('enrollment'))):
        if item and frappe.db.exists(doctype, item.name):
            survivors.append(doctype)
    if survivors:
        raise ValueError('CRITICAL: rollback left synthetic records: ' + ', '.join(survivors))
    return output
