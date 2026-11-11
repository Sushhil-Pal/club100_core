frappe.ui.form.on('Club100 Wallet Grant', {
    source_type(frm) {
        if (frm.doc.source_type !== 'Corporate') {
            frm.set_value('corporate_pool', null);
            frm.set_value('corporate_customer', null);
            frm.set_value('funding_verified', 0);
        }
        if (frm.doc.source_type !== 'Referral') frm.set_value('referral_reward', null);
        if (frm.doc.source_type === 'Promotion') frm.set_value('funding_reference', null);
    },
    corporate_pool(frm) {
        if (frm.doc.source_type === 'Corporate') {
            frm.set_value('corporate_customer', null);
            frm.set_value('funding_reference', null);
            frm.set_value('funding_verified', 0);
        }
    }
});
