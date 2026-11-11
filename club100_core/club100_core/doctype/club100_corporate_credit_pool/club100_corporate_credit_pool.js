frappe.ui.form.on('Club100 Corporate Credit Pool', {
    refresh(frm) {
        if (!frm.is_new() && frm.doc.status === 'Funded' && frm.doc.funding_verified) {
            frm.add_custom_button(__('Allocate Credits'), () => {
                frappe.new_doc('Club100 Corporate Allocation Batch', {corporate_pool:frm.doc.name});
            });
        }
    }
});
;
// >>> CLUB100_CORPORATE_FUNDING_DESK_V1_1_BEGIN >>>
// Confirm funding from ERPNext Desk; never create a receipt or a grant here.
frappe.ui.form.on('Club100 Corporate Credit Pool', {
  refresh(frm) {
    if (frm.is_new() || !['Awaiting Funding', 'Funded'].includes(frm.doc.status)) return;
    const canConfirm = frappe.user.has_role('System Manager') || frappe.session.user === 'Administrator';
    if (!canConfirm) return;
    frm.add_custom_button(__(frm.doc.status === 'Funded' ? 'Reconcile Legacy Funding' : 'Confirm Funding'), () => {
      const dialog = new frappe.ui.Dialog({
        title: __('Confirm Corporate Funding'),
        fields: [
          {fieldname: 'payment_entry', fieldtype: 'Link', options: 'Payment Entry',
           label: __('Submitted employer receipt'), reqd: 1,
           get_query() {
             return {filters: {docstatus: 1, payment_type: 'Receive', party_type: 'Customer',
                               party: frm.doc.corporate_customer,
                               reference_no: frm.doc.funding_reference}};
           }},
          {fieldname: 'notice', fieldtype: 'HTML',
           options: `<p>${__('This uses an already submitted employer Payment Entry. It does not create money or prove external bank reconciliation.')}</p>`},
        ],
        primary_action_label: __('Validate and Confirm'),
        async primary_action(values) {
          const args = {pool_name: frm.doc.name, payment_entry_name: values.payment_entry};
          try {
            const pre = await frappe.call({
              method: 'club100_core.accounting.corporate_funding_workflow.preview', args,
              freeze: true
            });
            const result = pre.message || {};
            if (result.issues?.length || result.status !== 'Ready for controlled UAT') {
              frappe.msgprint({title: __('Funding blocked'), indicator: 'red',
                message: (result.issues || [__('Receipt validation failed')]).join('<br>')});
              return;
            }
            frappe.confirm(
              __('Post wallet liability for {0} against receipt {1}?',
                 [format_currency(frm.doc.amount, frm.doc.currency), values.payment_entry]),
              async () => {
                try {
                  const out = await frappe.call({
                    method: 'club100_core.accounting.corporate_funding_workflow.confirm',
                    args, freeze: true, freeze_message: __('Posting funding accounting…')
                  });
                  dialog.hide();
                  frappe.msgprint({title: __('Funding confirmation'), indicator: 'green',
                    message: __('Result: {0}. Journal Entry: {1}',
                      [out.message.status, out.message.journal_entry])});
                  await frm.reload_doc();
                } catch (e) { frappe.show_alert({message: __('Funding posting failed'), indicator: 'red'}); }
              }
            );
          } catch (e) { frappe.show_alert({message: __('Funding validation failed'), indicator: 'red'}); }
        }
      });
      dialog.show();
    }, __('Funding'));
  }
});
// <<< CLUB100_CORPORATE_FUNDING_DESK_V1_1_END <<<
