"""Read-write local Frappe smoke using savepoint rollback; never use in production."""
import os
import uuid
import frappe
from frappe.utils import today
from club100_core.wallet.logic import money
from club100_core.wallet.admin_refs import pool_event_key, batch_allocation_key


def run():
    if frappe.local.site != 'club100.local' or os.environ.get('CLUB100_ALLOW_WALLET_ADMIN_TEST') != '1':
        frappe.throw('Local-only test requires club100.local and CLUB100_ALLOW_WALLET_ADMIN_TEST=1')
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required', frappe.PermissionError)
    members = frappe.get_all('Club100 Member', pluck='name', limit=30)
    customers = frappe.get_all('Customer', pluck='name', limit=30)
    selected = None
    for company in customers:
        available = []
        for member in members:
            existing=frappe.db.get_value('Club100 Corporate Employee',{'corporate_customer':company,'member':member},'status')
            if existing in (None, 'Active'):
                available.append(member)
            if len(available)==2:break
        if len(available)==2:
            selected=(company,available[0],available[1]);break
    if not selected:
        return {'outcome':'SKIP','reason':'Need a Customer and two Members without inactive corporate association'}
    company,a,b=selected
    doctype_names=(
        'Club100 Corporate Credit Pool', 'Club100 Corporate Employee',
        'Club100 Corporate Allocation Batch','Club100 Corporate Allocation Line',
        'Club100 Wallet','Club100 Wallet Grant','Club100 Wallet Ledger'
    )
    before={dt:frappe.db.count(dt) for dt in doctype_names}
    marker=uuid.uuid4().hex[:14]
    savepoint='c100_wallet_admin_'+marker
    frappe.db.savepoint(savepoint)
    passed=[]
    try:
        proof='LOCAL-VERIFIED-SMOKE-'+marker
        pool=frappe.get_doc({'doctype':'Club100 Corporate Credit Pool',
            'corporate_customer':company,'funding_reference':proof,
            'amount':'800.00','currency':'INR','funding_verified':1,'status':'Funded'}).insert(ignore_permissions=True)
        assert pool.funding_event_key==pool_event_key(company,proof)
        passed.append('Corporate funding key generated from verified proof')
        for member in (a,b):
            if not frappe.db.exists('Club100 Corporate Employee',{'corporate_customer':company,'member':member}):
                frappe.get_doc({'doctype':'Club100 Corporate Employee','corporate_customer':company,'member':member,'status':'Active'}).insert(ignore_permissions=True)
        batch=frappe.get_doc({'doctype':'Club100 Corporate Allocation Batch',
            'corporate_pool':pool.name,'valid_from':today(),'validity_type':'Lifetime',
            'allocations':[{'member':a,'amount':'500.00'},
                           {'member':b,'amount':'300.00','validity_type':'Expires On','expires_on':'2027-12-31'}]
        }).insert(ignore_permissions=True)
        assert money(batch.allocation_total)==money('800.00')
        assert not frappe.db.exists('Club100 Wallet Grant',{'source_event_key':['like','CORP:BATCH:'+batch.name+'%']})
        passed.append('Draft batch does not issue credits')
        batch.submit()
        passed.append('Submitting batch issues credits')
        for row,member,expected in zip(batch.allocations,(a,b),('500.00','300.00')):
            key=batch_allocation_key(batch.name,row.name)
            g=frappe.db.get_value('Club100 Wallet Grant',{'source_event_key':key},
                ['name','member','amount','corporate_customer','funding_reference','corporate_pool','validity_type','expires_on'], as_dict=True)
            assert g and g.member==member and money(g.amount)==money(expected)
            assert g.corporate_customer==company and g.funding_reference==proof and g.corporate_pool==pool.name
            assert frappe.db.exists('Club100 Wallet Ledger',{'grant':g.name,'entry_type':'Credit'})
            assert frappe.db.get_value('Club100 Corporate Allocation Line',row.name,'wallet_grant')==g.name
        passed.append('Both grants have automated references, inherited funding and ledger credits')
        from club100_core.wallet.corporate import pool_summary
        s=pool_summary(pool.name)
        assert money(s['allocated'])==money('800.00') and money(s['unallocated'])==money('0')
        passed.append('Pool capacity exactly exhausted')
        from club100_core.club100_core.doctype.club100_corporate_allocation_batch.club100_corporate_allocation_batch import Club100CorporateAllocationBatch
        # Server-side replay is idempotent, including a completely exhausted pool.
        batch.on_submit()
        assert frappe.db.count('Club100 Wallet Grant',{'corporate_pool':pool.name})==2
        passed.append('Retry does not reissue credits')
        over=frappe.get_doc({'doctype':'Club100 Corporate Allocation Batch',
            'corporate_pool':pool.name,'valid_from':today(),'validity_type':'Lifetime',
            'allocations':[{'member':a,'amount':'0.01'}]}).insert(ignore_permissions=True)
        try:
            over.submit()
            raise AssertionError('Over-allocation accepted')
        except frappe.ValidationError:
            passed.append('Over-allocation rejected')
        promo=frappe.get_doc({'doctype':'Club100 Wallet Grant',
            'source_type':'Promotion','member':a,'amount':'25.00','currency':'INR',
            'valid_from':today(),'validity_type':'Lifetime','status':'Active','notes':'Local promotion smoke test'}).insert(ignore_permissions=True)
        assert promo.source_event_key.startswith('PROMOTION:ADMIN:')
        assert promo.funding_reference.startswith('PROMO:ADMIN:')
        passed.append('Manual promotion gets automated allocation and approval identifiers')
        outcome={'outcome':'PASS','passed':passed,'count':len(passed)}
    finally:
        frappe.db.rollback(save_point=savepoint)
    after={dt:frappe.db.count(dt) for dt in doctype_names}
    if before!=after:
        raise AssertionError('Smoke rollback record counts changed: '+repr((before,after)))
    outcome['rollback_verified_by_record_counts']=True
    outcome['site']=frappe.local.site
    return outcome
