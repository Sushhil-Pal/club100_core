"""Society weekly session generator, ERP-only. Never reschedules or deletes records."""
from datetime import date, timedelta, datetime
import time
import frappe
from frappe.utils import getdate, nowdate, cint

WEEKDAY = {v:k for k,v in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])}
METHOD = 'club100_core.curriculum.society_weekly_schedule.daily_generate'

def _time_seconds(value):
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if hasattr(value,'hour'):
        return value.hour*3600+value.minute*60+value.second
    p=str(value).split(':')
    return int(p[0])*3600+int(p[1])*60+int(float(p[2])) if len(p)>2 else int(p[0])*3600+int(p[1])*60

def _generate_once(cohort_name, dry_run=True, from_date=None, days=28):
    """Single attempt; exclusive cohort lock precedes ALL snapshot reads."""
    dry_run = str(dry_run).lower() not in ('false','0','no')
    days = cint(days)
    if days < 1 or days > 28:
        frappe.throw('Rolling window must be 1–28 days')
    # Lock first: reading the cohort or sessions before FOR UPDATE establishes a
    # stale repeatable-read snapshot and can race with another generator.
    if not dry_run:
        frappe.db.sql('SELECT name FROM `tabClub100 Cohort` WHERE name=%s FOR UPDATE', (cohort_name,))
    cohort = frappe.get_doc('Club100 Cohort', cohort_name)
    if not cint(cohort.society_schedule_enabled) or cohort.status not in ('Planned','Open','Active'):
        return {'cohort':cohort_name,'skipped':'schedule disabled or cohort closed','created':0}
    cohort.validate_society_weekly_schedule()
    start=max(getdate(from_date or nowdate()),getdate(cohort.start_date),getdate(cohort.society_curriculum_start_date))
    finish=getdate(from_date or nowdate())+timedelta(days=days-1)
    if cohort.end_date:
        finish=min(finish,getdate(cohort.end_date))
    if start>finish:
        return {'cohort':cohort_name,'created':0,'candidates':[]}
    slots={WEEKDAY[r.weekday]:r for r in cohort.society_weekly_schedule}
    existing=frappe.get_all('Club100 Session',filters={'cohort':cohort_name},fields=['name','session_date','start_time','society_original_slot_date','society_original_slot_time','society_curriculum_position','status'],limit_page_length=100000)
    occupied_dates={str(r.session_date) for r in existing}
    reserved_dates={str(r.society_original_slot_date) for r in existing if r.society_original_slot_date}
    assigned=[r for r in existing if cint(r.society_curriculum_position)>0]
    last=max(assigned,key=lambda r:cint(r.society_curriculum_position)) if assigned else None
    last_key=(str(last.session_date),_time_seconds(last.start_time)) if last else None
    candidates=[]; created=[]; skipped=[]
    current=start
    while current<=finish:
        row=slots.get(current.weekday())
        if row:
            seconds=_time_seconds(row.start_time)
            key=(current.isoformat(),seconds)
            if key[0] in occupied_dates or key[0] in reserved_dates:
                skipped.append({'date':key[0],'reason':'existing session at slot'})
            elif last_key and key<=last_key:
                skipped.append({'date':key[0],'reason':'before last curriculum session'})
            elif seconds+cint(row.duration_minutes)*60 >= 86400:
                skipped.append({'date':key[0],'reason':'session ends after midnight'})
            else:
                item={'date':key[0],'start_time':str(timedelta(seconds=seconds)),'end_time':str(timedelta(seconds=seconds+cint(row.duration_minutes)*60))}
                candidates.append(item)
                if not dry_run:
                    doc=frappe.get_doc({'doctype':'Club100 Session','cohort':cohort.name,'program':cohort.program,'trainer':cohort.primary_trainer,'delivery_mode':'Onsite','session_date':item['date'],'start_time':item['start_time'],'end_time':item['end_time'],'status':'Scheduled','society_original_slot_date':item['date'],'society_original_slot_time':item['start_time']})
                    doc.insert(ignore_permissions=True)
                    created.append({'name':doc.name,'position':doc.society_curriculum_position,**item})
                    last_key=key
                    occupied_dates.add(key[0])
                    reserved_dates.add(key[0])
        current+=timedelta(days=1)
    return {'cohort':cohort_name,'dry_run':dry_run,'candidates':candidates,'created':created if not dry_run else [],'skipped':skipped}


def _retryable_db_conflict(exc):
    """Recognize Frappe-wrapped transaction deadlocks and lock conflicts."""
    if isinstance(exc, frappe.QueryDeadlockError):
        return True
    # Error 1020: record changed since last read; 1205: lock wait timeout;
    # 1213: deadlock. Driver exceptions may be wrapped by Frappe.
    cur = exc
    while cur is not None:
        args = getattr(cur, 'args', ())
        if args and args[0] in (1020, 1205, 1213):
            return True
        cur = getattr(cur, '__cause__', None)
    return False


def generate_for_cohort(cohort_name, dry_run=True, from_date=None, days=28):
    """Preview or atomically generate sessions for ONE cohort.

    In write mode this function owns its transaction: it commits on success and
    rolls back on failure. Call from an isolated scheduler job / bench execute,
    NOT inside another caller's uncommitted business transaction.
    """
    is_dry_run = str(dry_run).lower() not in ('false', '0', 'no')
    if is_dry_run:
        return _generate_once(cohort_name, True, from_date, days)
    for attempt in range(3):
        try:
            result = _generate_once(cohort_name, False, from_date, days)
            frappe.db.commit()  # Release cohort lock before another generator rechecks.
            return result
        except Exception as exc:
            frappe.db.rollback()  # Discard ALL inserts/locks from this attempt.
            if not _retryable_db_conflict(exc) or attempt == 2:
                raise
            time.sleep((0.1, 0.25)[attempt])
    raise RuntimeError('Unreachable Society generator retry exit')

def daily_generate():
    """Run from scheduler once daily. Errors isolated by cohort."""
    cohorts=frappe.get_all('Club100 Cohort',filters={'society_schedule_enabled':1,'status':['in',['Planned','Open','Active']]},pluck='name',limit_page_length=10000)
    output=[]
    for name in cohorts:
        try:
            result=generate_for_cohort(name,dry_run=False)
            frappe.db.commit()
            output.append({'cohort':name,'created':len(result['created'])})
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title='Club100 Society weekly schedule: '+name, message=frappe.get_traceback())
            output.append({'cohort':name,'error':'see Error Log'})
    return output

def install_daily_job():
    """One-time scheduler setup after migrate. Does not generate sessions."""
    if frappe.db.exists('Scheduled Job Type',{'method':METHOD}):
        return 'already installed'
    doc=frappe.get_doc({'doctype':'Scheduled Job Type','method':METHOD,'frequency':'Daily'})
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name
