"""Local-only savepoint rollback test for employer pool and wallet grants."""
import os
import uuid
import frappe
from club100_core.wallet.logic import money


def run():
    if frappe.local.site != "club100.local" or os.environ.get("CLUB100_ALLOW_CORPORATE_TEST") != "1":
        frappe.throw("Local-only test: run on club100.local with CLUB100_ALLOW_CORPORATE_TEST=1")
    if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("System Manager required", frappe.PermissionError)

    members = frappe.get_all("Club100 Member", pluck="name", limit=1)
    customers = frappe.get_all("Customer", pluck="name", limit=1)
    if not members or not customers:
        return {"outcome": "SKIP", "reason": "At least one Club100 Member and one ERPNext Customer required"}
    member, company = members[0], customers[0]
    types = ("Club100 Corporate Credit Pool", "Club100 Corporate Employee", "Club100 Wallet", "Club100 Wallet Grant", "Club100 Wallet Ledger")
    before = {dt: frappe.db.count(dt) for dt in types}
    key = uuid.uuid4().hex[:16].upper()
    point = 'club100_corporate_' + key.lower()
    frappe.db.savepoint(point)
    passed = []
    outcome = None
    try:
        pool = frappe.get_doc({
            'doctype': 'Club100 Corporate Credit Pool',
            'corporate_customer': company,
            'funding_event_key': 'LOCAL-SMOKE-FUND-'+key,
            'funding_reference': 'LOCAL-TEST-DO-NOT-USE-'+key,
            'funding_verified': 1,
            'amount': '1000.00',
            'currency': 'INR',
            'status': 'Funded',
        }).insert(ignore_permissions=True)
        passed.append('Verified funding pool created')
        current = frappe.db.get_value('Club100 Corporate Employee', {'corporate_customer': company, 'member': member}, ['name','status'], as_dict=True)
        if current and current.status != 'Active':
            outcome = {'outcome': 'SKIP', 'reason': 'Selected member is not active for selected employer'}
        else:
            if not current:
                frappe.get_doc({'doctype':'Club100 Corporate Employee','corporate_customer':company,'member':member,'status':'Active'}).insert(ignore_permissions=True)
            passed.append('Eligible employee linked')
            for suffix, amount in (('A', '600.00'), ('B', '400.00')):
                grant = frappe.get_doc({
                    'doctype':'Club100 Wallet Grant',
                    'member':member,'source_type':'Corporate','corporate_pool':pool.name,
                    'source_event_key':'LOCAL-SMOKE-ALLOC-'+key+suffix,
                    'amount':amount,'currency':'INR','validity_type':'Lifetime',
                    'valid_from':frappe.utils.today(), 'status':'Active',
                }).insert(ignore_permissions=True)
                assert frappe.db.exists('Club100 Wallet Ledger', {'grant':grant.name, 'entry_type':'Credit'}), 'Missing ledger credit'
            passed.append('Two verified grants and ledger credits created')
            from club100_core.wallet.corporate import pool_summary
            s=pool_summary(pool.name)
            assert money(s['allocated']) == money('1000.00') and money(s['unallocated']) == money('0.00'), s
            passed.append('Pool balance exactly exhausted')
            try:
                frappe.get_doc({
                    'doctype':'Club100 Wallet Grant',
                    'member':member,'source_type':'Corporate','corporate_pool':pool.name,
                    'source_event_key':'LOCAL-SMOKE-ALLOC-'+key+'OVER',
                    'amount':'0.01','currency':'INR','validity_type':'Lifetime',
                    'valid_from':frappe.utils.today(), 'status':'Active',
                }).insert(ignore_permissions=True)
                raise AssertionError('Overspend was accepted')
            except frappe.ValidationError:
                passed.append('Overspend rejected')
            outcome = {'outcome':'PASS', 'passed':passed, 'count':len(passed)}
    finally:
        # Keep financial test data out of the database even after expected failure.
        frappe.db.rollback(save_point=point)
    after = {dt: frappe.db.count(dt) for dt in types}
    if before != after:
        raise AssertionError('SAVEPOINT ROLLBACK CHECK FAILED: ' + repr((before, after)))
    outcome['rollback_verified_by_record_counts'] = True
    return outcome
