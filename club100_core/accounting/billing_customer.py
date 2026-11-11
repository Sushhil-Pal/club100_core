"""Member-owned ERPNext Customer provisioning for Club100 membership billing.

Customer links are owned by Club100 Member, not by a global UAT/customer config.
Only authenticated checkout/registration or trusted internal posting may call
this private module. Never resolve customers by user-supplied mobile/email.
"""
import frappe
from frappe.utils import cint


def _valid_company(company):
    name = str(company or '').strip()
    if not name or not frappe.db.exists('Company', name):
        raise ValueError('Club100 billing Company is not configured')
    return name


def _master(doctype, *values):
    for candidate in values:
        candidate = str(candidate or '').strip()
        if candidate and frappe.db.exists(doctype, candidate):
            return candidate
    raise ValueError('ERPNext ' + doctype + ' master is not configured for member billing')


def _member(member_id):
    member_id = str(member_id or '').strip()
    if not member_id:
        raise ValueError('An authenticated Club100 Member is required')
    row = frappe.db.get_value('Club100 Member', member_id,
        ['name','full_name','first_name','last_name','mobile','email','member_type',
         'customer','contact','status'], as_dict=True)
    if not row or not (str(row.full_name or '').strip() or str(row.first_name or '').strip()):
        raise ValueError('Member billing name is missing')
    if not str(row.mobile or '').strip():
        raise ValueError('Member mobile is required for customer billing')
    return row


def _one_member_customer(customer, member_id):
    # An organization account or the shared historical UAT Customer must not
    # accidentally be reused as an individual membership's invoice party.
    other = frappe.db.get_value('Club100 Member',
        {'customer': customer, 'name': ['!=', member_id]}, 'name')
    if other:
        raise ValueError('ERPNext Customer is shared by another Club100 Member; review billing mapping')
    if customer == str(frappe.conf.get('club100_accounting_test_customer') or '').strip():
        raise ValueError('Shared UAT Customer cannot be used as a member billing Customer')


def require_existing_for_member(member_id, company):
    """Posting-time invariant; NEVER create a new Customer after payment capture."""
    _valid_company(company)
    member = _member(member_id)
    customer = str(member.customer or '').strip()
    if not customer or not frappe.db.exists('Customer', customer):
        raise ValueError('Member has no ERPNext billing Customer; no invoice may post')
    if cint(frappe.db.get_value('Customer', customer, 'disabled') or 0):
        raise ValueError('Member billing Customer is disabled')
    if frappe.db.get_value('Customer', customer, 'customer_type') != 'Individual':
        raise ValueError('Member checkout requires a personal, not organization, Customer')
    _one_member_customer(customer, member.name)
    return customer


def ensure_customer_for_member(member_id, company):
    """Provision Customer and primary Contact before any new Razorpay order.

    Call only after server-side member authentication + member-row lock, or
    immediately after the Member was created during validated registration.
    This operation joins the surrounding checkout/registration DB transaction.
    """
    _valid_company(company)
    # Serialize mapping decisions; every checkout caller already uses the
    # member-row lock. Registration reaches here immediately after insert.
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE', member_id)
    member = _member(member_id)
    if member.customer:
        return require_existing_for_member(member.name, company)

    name = str(member.full_name or (' '.join(filter(None, [member.first_name, member.last_name])))).strip()
    group = _master('Customer Group',
         frappe.conf.get('club100_billing_customer_group'),
         frappe.db.get_single_value('Selling Settings', 'customer_group'),
         'Individual', 'All Customer Groups')
    territory = _master('Territory',
         frappe.conf.get('club100_billing_territory'),
         frappe.db.get_single_value('Selling Settings', 'territory'),
         'All Territories')
    # Intentionally create a separate Customer per member: names and mobile
    # numbers are not reliable proof that two memberships belong to one buyer.
    customer = frappe.get_doc({'doctype':'Customer', 'customer_name':name,
         'customer_type':'Individual', 'customer_group':group, 'territory':territory})
    customer.insert(ignore_permissions=True)
    contact = frappe.get_doc({'doctype':'Contact',
         'first_name':str(member.first_name or name.split()[0]),
         'last_name':str(member.last_name or ''),
         'links':[{'link_doctype':'Customer','link_name':customer.name}],
         'phone_nos':[{'phone':str(member.mobile).strip(),'is_primary_mobile_no':1}],
         'email_ids':([{'email_id':str(member.email).strip(),'is_primary':1}]
                      if str(member.email or '').strip() else [])})
    contact.insert(ignore_permissions=True)
    # Save only these links, not the member's onboarding, status or identity.
    frappe.db.set_value('Club100 Member', member.name,
          {'customer':customer.name, 'contact':contact.name}, update_modified=False)
    return customer.name


def audit_member(member_id):
    """Desk-only, read-only member billing link inspection (no customer creation)."""
    user = frappe.session.user
    if user != 'Administrator' and 'System Manager' not in frappe.get_roles(user):
        frappe.throw('Finance administrator permission required', frappe.PermissionError)
    member = _member(member_id)
    company = str(frappe.conf.get('club100_accounting_company') or '').strip()
    try:
        customer = require_existing_for_member(member.name, company)
        status = 'READY'
        problem = None
    except Exception as exc:
        customer = str(member.customer or '')
        status = 'BLOCKED'
        problem = str(exc)
    return {'member': member.name, 'member_full_name': member.full_name,
            'customer': customer or None,
            'customer_name': (frappe.db.get_value('Customer', customer, 'customer_name')
                              if customer and frappe.db.exists('Customer',customer) else None),
            'contact': member.contact or None,
            'status':status, 'issue':problem, 'read_only':True}
