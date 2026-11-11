"""Stage D2C3B2C2B: READ-ONLY local funding audit and reservation preflight.

Uses existing Stage 5F source-event, posted voucher, grant ledger and liability
headroom verification. Source GL proof is *not* bank statement reconciliation.
No whitelisted methods; no hooks; no changes to grant, wallet, invoice or GL.
"""
import json
from decimal import Decimal
import frappe
from frappe.utils import getdate, get_datetime, now_datetime
from club100_core.accounting.settlement import _admin, _config, _local_pilot, _require_wallet_liability_backing
from club100_core.accounting.wallet_backing import verify_wallet_grant_backing
from club100_core.accounting.hybrid_reservations import _pair
from club100_core.accounting.hybrid_backing_rules import (
    money, validate_funded_allocations, validate_grant_for_reservation,
    validate_reservation_hold,
)
from club100_core.wallet.logic import is_active

def _guard():
    _admin()
    if not _local_pilot():
        raise ValueError('Grant backing preflight restricted to local test-mode site')

def healthcheck():
    _guard()
    return {
        'site': frappe.local.site,
        'stage': '5F-D2C3B2C2B',
        'source_event_and_gl_verifier_present': True,
        'per_grant_backing_preflight_installed': True,
        'grant_audit_read_only': True,
        'bank_statement_reconciled': False,
        'wallet_capture_enabled': False,
        'invoice_or_gl_posting_enabled': False,
        'membership_activation_enabled': False,
        'production_ready': False,
    }

def _source_grant(grant_name):
    grant=frappe.db.get_value('Club100 Wallet Grant', grant_name,
        ['name','member','wallet','source_type','amount','currency','status',
         'valid_from','validity_type','expires_on'], as_dict=True)
    if grant:
        grant['amount']=str(money(str(grant.amount)))  # normalize DB Currency float only
    return grant

def _source_proof(cfg, row):
    """Only positive source-specific evidence; no manual override or inference."""
    return verify_wallet_grant_backing(cfg,[row])

def audit_grants(limit=100):
    """Read-only source GL audit. Does not assert actual bank reconciliation."""
    _guard()
    if type(limit) is not int or limit < 1 or limit > 200:
        raise ValueError('limit must be an integer 1..200')
    cfg=_config()
    rows=frappe.get_all('Club100 Wallet Grant',
        filters={'source_type':['in',['Corporate','Referral']]},
        fields=['name','member','source_type','amount','status'],
        order_by='creation asc', limit_page_length=limit)
    items=[]
    for row in rows:
        record={'grant':row.name,'source':row.source_type,
            'member':row.member,'amount':str(money(str(row.amount))),
            'source_gl_document_status':'Blocked'}
        try:
            grant=_source_grant(row.name)
            valid=bool(grant and is_active(grant.valid_from,grant.validity_type,
                                                grant.expires_on,getdate()))
            validate_grant_for_reservation(grant,row.member,
                {'grant':row.name,'source':row.source_type,'amount':str(money(str(row.amount)))},valid)
            _source_proof(cfg,{'grant':row.name,'source':row.source_type,'amount':str(money(str(row.amount)))})
            record['source_gl_document_status']='Verified'
        except (ValueError,frappe.ValidationError) as exc:
            record['blocker']=str(exc)
        items.append(record)
    return {
        'site':frappe.local.site,'stage':'5F-D2C3B2C2B','read_only':True,
        'audited':len(items),'source_gl_verified':sum(x['source_gl_document_status']=='Verified' for x in items),
        'blocked':sum(x['source_gl_document_status']=='Blocked' for x in items),
        'items':items,'bank_statement_reconciled':False,
        'wallet_capture_authorized':False,'production_ready':False,
        'note':'A posted source event/GL voucher does not by itself prove bank statement reconciliation or authorize capture.'}

def review_enrollment(enrollment_id):
    """Inspect frozen D2B funded reservation + source GL. No locks or writes.

    This is a point-in-time preflight, NOT a settlement authorization: a
    future posting service must repeat checks inside the locked transaction.
    """
    _guard()
    name=str(enrollment_id or '').strip()
    if not name or len(name)>140:
        raise ValueError('Enrollment ID required')
    report={'site':frappe.local.site,'enrollment':name,'stage':'5F-D2C3B2C2B',
        'read_only':True,'source_gl_document_status':'Blocked',
        'bank_statement_reconciled':False,'wallet_capture_authorized':False,
        'invoice_or_gl_posting_enabled':False,'production_ready':False}
    try:
        hybrid_name=frappe.db.get_value('Club100 Hybrid Checkout',{'enrollment':name},'name')
        if not hybrid_name:
            raise ValueError('No D2B hybrid checkout for this enrollment')
        hybrid=frappe.get_doc('Club100 Hybrid Checkout',hybrid_name)
        if hybrid.status!='Reserved' or get_datetime(hybrid.expires_at)<=now_datetime():
            raise ValueError('Hybrid checkout not reserved or hold has expired')
        if not hybrid.member or hybrid.enrollment!=name:
            raise ValueError('Hybrid checkout identity mismatch')
        enrolled_member=frappe.db.get_value('Club100 Enrollment',name,'member')
        if enrolled_member!=hybrid.member:
            raise ValueError('Enrollment member no longer matches frozen checkout')
        all_legs=_pair(hybrid,allowed=('Reserved',))
        funded=all_legs['funded']
        allocations,totals=validate_funded_allocations(funded,str(hybrid.funded_wallet_tender))
        if not allocations:
            raise ValueError('No Corporate/Referral tender in reserved checkout')
        reservation=frappe.db.get_value('Club100 Wallet Reservation',
            {'reservation_key':hybrid.funded_reservation_key},
            ['name','member','reference','total_amount','status','expires_at'],as_dict=True)
        if reservation:
            reservation['total_amount']=str(money(str(reservation.total_amount)))
        validate_reservation_hold(reservation,{'member':hybrid.member,
            'enrollment':name,'funded_wallet_tender':str(hybrid.funded_wallet_tender)},now_datetime())
        cfg=_config()
        for row in allocations:
            grant=_source_grant(row['grant'])
            valid=bool(grant and is_active(grant.valid_from,grant.validity_type,
                                      grant.expires_on,getdate()))
            validate_grant_for_reservation(grant,hybrid.member,row,valid)
            if reservation and grant.wallet!=frappe.db.get_value('Club100 Wallet Reservation',
                     reservation.name,'wallet'):
                raise ValueError('Grant wallet does not match held reservation')
        _require_wallet_liability_backing(cfg,totals,allocations)
        report.update({'source_gl_document_status':'Verified',
            'hybrid_checkout':hybrid_name,
            'funded_wallet':str(money(str(hybrid.funded_wallet_tender))),
            'funded_by_source':{s:str(v) for s,v in sorted(totals.items())},
            'grant_count':len(allocations),
            'next_step':'Repeat source, hold, ledger and GL checks inside locked, atomic posting transaction'})
    except (ValueError,frappe.ValidationError) as exc:
        report['blocker']=str(exc)
    return report
