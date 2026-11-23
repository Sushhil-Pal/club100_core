"""Admin-only manual onboarding for paid online trainer-led memberships.

Do not change invoices, verified payment identities, or wallet transactions.
The actual first scheduled cohort session defines membership commencement.
"""
import frappe
from frappe.utils import add_days, add_months, cint, getdate, nowdate, now_datetime

CODES = ('group','group-plus','focus','coach')


def _admin():
    if frappe.session.user != 'Administrator' and 'System Manager' not in frappe.get_roles(frappe.session.user):
        frappe.throw('System Manager required for training onboarding', frappe.PermissionError)


def _locked(enrollment_id):
    name=str(enrollment_id or '').strip()
    member=frappe.db.get_value('Club100 Enrollment',name,'member')
    if not member:frappe.throw('Unknown membership')
    frappe.db.sql('SELECT name FROM `tabClub100 Member` WHERE name=%s FOR UPDATE',member)
    frappe.db.sql('SELECT name FROM `tabClub100 Enrollment` WHERE name=%s FOR UPDATE',name)
    doc=frappe.get_doc('Club100 Enrollment',name)
    code=frappe.db.get_value('Club100 Plan',doc.plan,'plan_code') if doc.plan else None
    if (doc.website_source!='PWA Commerce' or doc.pricing_model!='Online Plan'
            or code not in CODES or cint(doc.is_trial)
            or doc.payment_status!='Paid' or not doc.sales_invoice
            or frappe.db.get_value('Sales Invoice',doc.sales_invoice,'docstatus')!=1
            or abs(float(frappe.db.get_value('Sales Invoice',doc.sales_invoice,'outstanding_amount') or 0))>0.001):
        frappe.throw('Only fully accounted, paid trainer-led memberships can be onboarded')
    mapping=frappe.db.get_value('Club100 Membership Accounting',{'enrollment':doc.name},['status','sales_invoice'],as_dict=True)
    if not mapping or mapping.status!='Posted' or mapping.sales_invoice!=doc.sales_invoice:
        frappe.throw('Membership Accounting mapping is missing or not posted')
    return doc,code


@frappe.whitelist(methods=['POST'])
def start_onboarding(enrollment_id):
    _admin()
    doc,code=_locked(enrollment_id)
    if doc.training_onboarding_status=='Completed':
        frappe.throw('Completed onboarding cannot be restarted')
    if doc.status not in ('Scheduled','Active'):
        frappe.throw('Membership is not pending onboarding')
    if doc.status=='Active':
        frappe.throw('Legacy active membership needs finance review before onboarding')
    doc.training_onboarding_status='In Progress'
    doc.save(ignore_permissions=True)
    return {'enrollment':doc.name,'onboarding_status':'In Progress'}


def _next_future_session_date(cohort_id):
    """Select an assigned-trainer session starting strictly after site-local now.

    Comparing session_date alone would incorrectly select an earlier session
    on the same calendar day. TIME is stored in the Session start_time column.
    """
    rows=frappe.db.sql("""
        SELECT name, session_date, start_time
        FROM `tabClub100 Session`
        WHERE cohort=%s AND status IN ('Scheduled', 'Live')
          AND trainer IS NOT NULL AND trainer != ''
          AND session_date IS NOT NULL AND start_time IS NOT NULL
          AND TIMESTAMP(session_date, start_time) > %s
        ORDER BY session_date ASC, start_time ASC, name ASC
        LIMIT 1
    """, (cohort_id, now_datetime()), as_dict=True)
    if not rows:
        frappe.throw('Schedule a future trainer-led cohort session before completing onboarding')
    return getdate(rows[0].session_date)


@frappe.whitelist(methods=['POST'])
def complete_onboarding(enrollment_id, cohort_id):
    """Assign a preconfigured online cohort; first scheduled session starts paid validity."""
    _admin()
    doc,code=_locked(enrollment_id)
    if doc.training_onboarding_status=='Completed':
        frappe.throw('Onboarding already completed; use a controlled rescheduling workflow')
    if doc.status!='Scheduled':
        frappe.throw('Only scheduled, unstarted memberships can complete onboarding')
    if doc.start_date or doc.end_date:
        frappe.throw('Existing membership dates require manual reconciliation before onboarding')
    cohort=frappe.get_doc('Club100 Cohort',str(cohort_id or '').strip())
    if (cohort.plan!=doc.plan or cohort.program!=doc.program or cohort.society
            or cohort.delivery_mode not in ('Online','Hybrid')
            or cohort.status not in ('Open','Active') or not cohort.primary_trainer):
        frappe.throw('Select an open online cohort with the matching plan, program and assigned trainer')
    start=_next_future_session_date(cohort.name)
    end=add_days(add_months(start,cint(doc.membership_duration_months)),-1)
    doc.cohort=cohort.name
    doc.start_date=start
    doc.end_date=end
    doc.training_onboarding_status='Completed'
    doc.training_onboarding_completed_on=now_datetime()
    doc.status='Active' if start<=getdate(nowdate()) else 'Scheduled'
    previous=getattr(frappe.flags,'club100_training_onboarding_write',False)
    frappe.flags.club100_training_onboarding_write=True
    try:
        doc.save(ignore_permissions=True)  # capacity, plan, overlap validated by DocType
    finally:
        frappe.flags.club100_training_onboarding_write=previous
    return {'enrollment':doc.name,'onboarding_status':'Completed',
            'cohort':cohort.name,'commencement_date':str(start),
            'valid_until':str(end),'membership_status':doc.status}


@frappe.whitelist(methods=['POST'])
def reset_unstarted_legacy_membership(enrollment_id, reason):
    """Explicit one-off remediation for pre-onboarding paid online purchases.

    Finance/ops must confirm NO training sessions were delivered. Preserves the
    original invoice and payment. Does not silently rewrite historical invoices.
    """
    _admin()
    reason=str(reason or '').strip()
    if len(reason)<15:frappe.throw('Provide a reason of at least 15 characters')
    doc,code=_locked(enrollment_id)
    if doc.training_onboarding_status:
        frappe.throw('This membership is already under the new onboarding workflow')
    if doc.status not in ('Active','Scheduled') or doc.cohort:
        frappe.throw('Legacy reset applies only to unassigned Active/Scheduled memberships')
    if not doc.start_date or not doc.end_date:
        frappe.throw('Original membership dates are missing; requires manual finance review')
    old_start,old_end=str(doc.start_date),str(doc.end_date)
    doc.status='Scheduled'
    doc.start_date=None
    doc.end_date=None
    doc.training_onboarding_status='Pending'
    existing=str(doc.notes or '').strip()
    entry=('\n' if existing else '') + ('[Training onboarding reset %s] Original validity %s to %s. Reason: %s'
           % (now_datetime(),old_start,old_end,reason))
    doc.notes=existing+entry
    doc.save(ignore_permissions=True)
    return {'enrollment':doc.name,'onboarding_status':'Pending',
            'previous_start':old_start,'previous_end':old_end,
            'invoice_unchanged':doc.sales_invoice}


@frappe.whitelist(methods=['POST'])
def correct_unstarted_commencement(enrollment_id, reason, no_training_delivered=False):
    """Administrator-approved correction of a wrongly selected past session.

    For an already completed trainer-led onboarding only; does not cancel,
    edit or repost any financial voucher. Requires explicit operational
    confirmation because attendance systems can vary by delivery workflow.
    """
    _admin()
    if not cint(no_training_delivered):
        frappe.throw('Explicitly confirm that no training was delivered to this member')
    reason=str(reason or '').strip()
    if len(reason)<15:
        frappe.throw('Provide a correction reason of at least 15 characters')
    doc,code=_locked(enrollment_id)
    if (doc.training_onboarding_status!='Completed' or
            doc.status not in ('Active','Scheduled') or
            not doc.cohort or not doc.start_date or not doc.end_date):
        frappe.throw('Only completed, existing trainer-led onboarding can be corrected')
    if doc.payment_date and getdate(doc.start_date) < getdate(doc.payment_date):
        pass  # Historical start predates payment; eligible for explicit correction.
    elif doc.payment_date and getdate(doc.start_date) == getdate(doc.payment_date):
        pass  # Same-day past-session error; administrator has confirmed non-delivery.
    else:
        frappe.throw('This correction is restricted to commencement on/before payment date; review manually')
    cohort=frappe.get_doc('Club100 Cohort',doc.cohort)
    if (cohort.plan!=doc.plan or cohort.program!=doc.program or cohort.society
            or cohort.delivery_mode not in ('Online','Hybrid') or
            cohort.status not in ('Open','Active') or not cohort.primary_trainer):
        frappe.throw('Assigned cohort is no longer eligible for this membership')
    old_start,old_end=str(doc.start_date),str(doc.end_date)
    start=_next_future_session_date(cohort.name)
    end=add_days(add_months(start,cint(doc.membership_duration_months)),-1)
    if getdate(start) <= getdate(doc.start_date):
        frappe.throw('Replacement session must produce a later commencement date')
    doc.start_date=start
    doc.end_date=end
    doc.status='Scheduled'  # A later future session must never leave membership active.
    existing=str(doc.notes or '').strip()
    entry=('\n' if existing else '') + (
        '[Training commencement corrected %s] Original validity %s to %s; '
        'new validity %s to %s; no training delivered confirmed. Reason: %s' %
        (now_datetime(),old_start,old_end,start,end,reason))
    doc.notes=existing+entry
    prior=getattr(frappe.flags,'club100_training_onboarding_write',False)
    frappe.flags.club100_training_onboarding_write=True
    try:
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.club100_training_onboarding_write=prior
    return {'enrollment':doc.name, 'onboarding_status':doc.training_onboarding_status,
            'previous_start':old_start, 'previous_end':old_end,
            'commencement_date':str(start),'valid_until':str(end),
            'membership_status':doc.status,'invoice_unchanged':doc.sales_invoice}
