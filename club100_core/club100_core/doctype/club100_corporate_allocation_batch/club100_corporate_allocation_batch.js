frappe.ui.form.on('Club100 Corporate Allocation Batch', {
    refresh(frm) {
        if (frm.doc.docstatus !== 0) return;
        frm.set_query('member', 'allocations', () => ({
            // Eligibility is checked by the server; this is only a visual aid.
            filters: {}
        }));
        frm.add_custom_button(__('Paste CSV / TSV'), () => {
            const dialog = new frappe.ui.Dialog({
                title: __('Paste Employee Allocations'),
                fields: [{fieldname:'rows',fieldtype:'Long Text',label:__('Spreadsheet Data'),reqd:1,
                          description:__('Paste rows with columns: Member, Amount, Valid From (optional), Validity (optional), Expiry Date (optional), Notes (optional). Header row is optional. Comma or tab separated.') }],
                primary_action_label: __('Add Rows'),
                primary_action(values) {
                    const raw = (values.rows || '').trim();
                    const delimiter = raw.includes('\t') ? '\t' : ',';
                    const lines = parseClub100Separated(raw, delimiter);
                    if (!lines.length) { frappe.msgprint(__('No rows found')); return; }
                    let rows = lines;
                    if (/^member$/i.test((rows[0][0] || '').trim())) rows = rows.slice(1);
                    if (rows.length > 200 || rows.length + (frm.doc.allocations || []).length > 200) {
                        frappe.msgprint(__('Maximum 200 rows per allocation batch')); return;
                    }
                    const newRows = [];
                    for (let i=0;i<rows.length;i++) {
                        const parts = rows[i];
                        const member = (parts[0] || '').trim();
                        const amount = (parts[1] || '').trim();
                        if (!member || !/^[0-9]+(?:\.[0-9]{1,2})?$/.test(amount)) {
                            frappe.msgprint(__('Invalid member or amount at pasted row {0}', [i+1])); return;
                        }
                        newRows.push({member, amount, valid_from:(parts[2]||'').trim(),
                            validity_type:(parts[3]||'').trim(), expires_on:(parts[4]||'').trim(),
                            notes:(parts[5]||'').trim()});
                    }
                    newRows.forEach(item => frm.add_child('allocations', item));
                    frm.refresh_field('allocations');
                    dialog.hide();
                    frappe.show_alert({message:__('Added {0} allocation rows. Review and Submit to issue credits.', [newRows.length]),indicator:'green'});
                }
            });
            dialog.show();
        });
    }
});

function parseClub100Separated(raw, delimiter) {
    const result = [];
    let row = [], field = '', quoted = false;
    for (let i=0;i<raw.length;i++) {
        const ch = raw[i];
        if (ch === '"') {
            if (quoted && raw[i+1] === '"') { field += '"'; i++; }
            else quoted = !quoted;
        } else if (!quoted && ch === delimiter) {
            row.push(field); field = '';
        } else if (!quoted && (ch === '\n' || ch === '\r')) {
            if (ch === '\r' && raw[i+1] === '\n') i++;
            row.push(field); if (row.some(x => x.trim())) result.push(row);
            row = []; field = '';
        } else field += ch;
    }
    if (quoted) throw new Error('Unterminated quote in CSV');
    row.push(field); if (row.some(x => x.trim())) result.push(row);
    return result;
}
