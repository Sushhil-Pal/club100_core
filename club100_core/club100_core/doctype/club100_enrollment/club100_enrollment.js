// Copyright (c) 2026, Karp Infotech and contributors
// For license information, please see license.txt

// frappe.ui.form.on("Club100 Enrollment", {
// 	refresh(frm) {

// 	},
// });

// CLUB100_TRAINER_ONBOARDING_ACTIONS_V1
frappe.ui.form.on('Club100 Enrollment', {
    refresh(frm) {
        if (frm.is_new() || frm.doc.payment_status !== 'Paid'
            || !['Group', 'Group Plus', 'Focus', 'Coach'].includes(frm.doc.plan)) return;
        if (frm.doc.training_onboarding_status === 'Completed') return;
        if (frm.doc.status !== 'Scheduled') return;
        frm.add_custom_button(__('Start Training Onboarding'), () => {
            frappe.call({method:'club100_core.api.training_onboarding.start_onboarding',
                args:{enrollment_id:frm.doc.name},callback:()=>frm.reload_doc()});
        }, __('Training Onboarding'));
        frm.add_custom_button(__('Complete Training Onboarding'), () => {
            frappe.prompt([{fieldname:'cohort_id',label:__('Online Cohort'),fieldtype:'Link',options:'Club100 Cohort',reqd:1}], values => {
                frappe.call({method:'club100_core.api.training_onboarding.complete_onboarding',
                    args:{enrollment_id:frm.doc.name,cohort_id:values.cohort_id},
                    callback: r => {if(r.message){frappe.msgprint(__('Training commencement: ') + r.message.commencement_date);frm.reload_doc();}}});
            }, __('Complete Training Onboarding'), __('Confirm'));
        }, __('Training Onboarding'));
    }
});
