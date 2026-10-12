frappe.ui.form.on("Club100 Society Interest", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		if (
			frm.doc.status === "Converted" ||
			frm.doc.status === "Not Interested"
		) {
			return;
		}

		if (has_active_invite(frm.doc)) {
			frm.add_custom_button(
				__("Copy Enrollment Link"),
				() => {
					show_enrollment_link_dialog(
						frm.doc.invite_token,
						frm.doc.invite_expires_on
					);
				},
				__("Society Enrollment")
			);
		}

		frm.add_custom_button(
			frm.doc.invite_token
				? __("Reissue Invite")
				: __("Invite Resident"),
			() => {
				open_invite_dialog(frm);
			},
			__("Society Enrollment")
		);
	},
});


function open_invite_dialog(frm) {
	if (!frm.doc.society) {
		frappe.msgprint(
			__(
				"Please link this Society Interest to a Club100 Society before issuing an enrollment invitation."
			)
		);
		return;
	}

	const is_reissue = Boolean(frm.doc.invite_token);

	const dialog = new frappe.ui.Dialog({
		title: is_reissue
			? __("Reissue Society Enrollment Invite")
			: __("Invite Resident"),
		fields: [
			{
				fieldtype: "Link",
				fieldname: "cohort",
				label: __("Society Batch"),
				options: "Club100 Cohort",
				reqd: 1,
				default: frm.doc.invited_cohort || "",
				get_query: () => ({
					filters: {
						society: frm.doc.society,
						delivery_mode: "Onsite",
						society_pricing_enabled: 1,
						status: "Open",
					},
				}),
			},
			{
				fieldtype: "Int",
				fieldname: "validity_days",
				label: __("Invite Validity (Days)"),
				default: 30,
				reqd: 1,
			},
			...(is_reissue
				? [
						{
							fieldtype: "HTML",
							fieldname: "reissue_note",
							options:
								'<div class="alert alert-warning" style="margin-bottom:0;">' +
								__(
									"Reissuing the invite will invalidate the resident's previous enrollment link."
								) +
								"</div>",
						},
					]
				: []),
		],
		primary_action_label: is_reissue
			? __("Reissue Invite")
			: __("Create Invite"),
		primary_action: async (values) => {
			const validity_days = cint(values.validity_days);

			if (validity_days <= 0) {
				frappe.msgprint(
					__("Invite validity must be greater than zero.")
				);
				return;
			}

			dialog.disable_primary_action();

			try {
				const response = await frappe.call({
					method:
						"club100_core.api.society_enrollment.issue_invite",
					args: {
						interest_id: frm.doc.name,
						cohort: values.cohort,
						validity_days,
					},
					freeze: true,
					freeze_message: is_reissue
						? __("Reissuing Society enrollment invite...")
						: __("Creating Society enrollment invite..."),
				});

				const result = response.message || {};

				if (!result.invite_token) {
					throw new Error(
						"ERP did not return an invitation token."
					);
				}

				dialog.hide();

				await frm.reload_doc();

				show_enrollment_link_dialog(
					result.invite_token,
					result.invite_expires_on
				);
			} catch (error) {
				console.error(
					"Unable to issue Society enrollment invite.",
					error
				);

				frappe.msgprint(
					__(
						"Unable to create the Society enrollment invitation. Please review the selected batch and try again."
					)
				);
			} finally {
				dialog.enable_primary_action();
			}
		},
	});

	dialog.show();
}


function show_enrollment_link_dialog(
	invite_token,
	invite_expires_on
) {
	if (!invite_token) {
		frappe.msgprint(
			__("No active Society enrollment invitation was found.")
		);
		return;
	}

	const enrollment_link =
		build_society_enrollment_link(invite_token);

	const expiry_text = invite_expires_on
		? frappe.datetime.str_to_user(invite_expires_on)
		: __("Not specified");

	const dialog = new frappe.ui.Dialog({
		title: __("Society Enrollment Link"),
		fields: [
			{
				fieldtype: "Data",
				fieldname: "enrollment_link",
				label: __("Enrollment Link"),
				default: enrollment_link,
				read_only: 1,
			},
			{
				fieldtype: "HTML",
				fieldname: "invite_details",
				options:
					'<div class="text-muted" style="margin-top:4px;">' +
					__("Invite expires on {0}", [expiry_text]) +
					"</div>" +
					'<div style="margin-top:10px;">' +
					'<a href="' +
					enrollment_link +
					'" target="_blank" rel="noopener noreferrer">' +
					__("Open enrollment page") +
					"</a>" +
					"</div>",
			},
		],
		primary_action_label: __("Copy Link"),
		primary_action: async () => {
			const copied = await copy_text(
				enrollment_link
			);

			if (copied) {
				frappe.show_alert(
					{
						message: __(
							"Enrollment link copied."
						),
						indicator: "green",
					},
					5
				);
			} else {
				frappe.msgprint(
					__(
						"Unable to copy automatically. Please copy the enrollment link from the field above."
					)
				);
			}
		},
	});

	dialog.show();
}


function build_society_enrollment_link(invite_token) {
	const hostname = (
		window.location.hostname || ""
	).toLowerCase();

	const is_local =
		hostname === "localhost" ||
		hostname === "127.0.0.1" ||
		hostname.endsWith(".local");

	const website_base_url = is_local
		? "http://localhost:8080"
		: "https://club100.fit";

	return (
		website_base_url +
		"/society-join/?token=" +
		encodeURIComponent(invite_token)
	);
}


function has_active_invite(doc) {
	if (!doc.invite_token) {
		return false;
	}

	if (!doc.invite_expires_on) {
		return true;
	}

	const normalized = String(
		doc.invite_expires_on
	).replace(" ", "T");

	const expires_at = new Date(normalized);

	if (Number.isNaN(expires_at.getTime())) {
		return true;
	}

	return expires_at.getTime() > Date.now();
}


async function copy_text(value) {
	try {
		if (
			navigator.clipboard &&
			window.isSecureContext
		) {
			await navigator.clipboard.writeText(
				value
			);
			return true;
		}
	} catch (error) {
		console.warn(
			"Clipboard API failed; using fallback.",
			error
		);
	}

	const text_area =
		document.createElement("textarea");

	text_area.value = value;
	text_area.setAttribute(
		"readonly",
		"readonly"
	);
	text_area.style.position = "fixed";
	text_area.style.opacity = "0";

	document.body.appendChild(
		text_area
	);

	text_area.select();

	let copied = false;

	try {
		copied = document.execCommand(
			"copy"
		);
	} catch (error) {
		console.warn(
			"Clipboard fallback failed.",
			error
		);
	}

	document.body.removeChild(
		text_area
	);

	return copied;
}
