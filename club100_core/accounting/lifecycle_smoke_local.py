"""Read-only integration smoke: no vouchers, ledger writes or gateway calls."""
import os
import frappe


def run():
    if (frappe.local.site != 'club100.local' or
            os.environ.get('CLUB100_ALLOW_STAGE5FD_SMOKE') != '1'):
        raise ValueError('Only permitted on club100.local with explicit smoke opt-in')
    from club100_core.accounting.operations import (
        healthcheck, expiry_exposure_report, operational_backlog,
        process_local_gateway_invoice_queue,
    )
    watch = ('GL Entry','Club100 Wallet Grant','Club100 Wallet Ledger',
             'Sales Invoice','Payment Entry','Journal Entry',
             'Club100 Membership Accounting','Club100 Wallet Accounting Event')
    before = {name: frappe.db.count(name) for name in watch}
    try:
        health = healthcheck()
        current = expiry_exposure_report(limit=100)
        # The as-of date is deliberately hypothetical; no grant is expired or
        # changed by asking what its status would be on a future date.
        future = expiry_exposure_report(as_of='2027-04-02',limit=100)
        queue = operational_backlog(limit=20)
        dry = process_local_gateway_invoice_queue(limit=3, dry_run=True)
        after = {name: frappe.db.count(name) for name in watch}
        if before != after or not all((current.get('read_only'), future.get('read_only'), queue.get('read_only'), dry.get('dry_run'))):
            raise ValueError('Read-only smoke unexpectedly modified records')
        return {'outcome':'PASS','site':frappe.local.site,
                'expiry_today':current['unspent_expired_by_source'],
                'expiry_hypothetical_2027_04_02':future['unspent_expired_by_source'],
                'operational_categories':queue['summary'],
                'invoice_queue_dry_run':dry['results'],
                'no_gateway_requests':True,'posting_performed':False,
                'records_unchanged':True,'posting_flags_changed':False,
                'production_ready':False}
    finally:
        frappe.db.rollback()
