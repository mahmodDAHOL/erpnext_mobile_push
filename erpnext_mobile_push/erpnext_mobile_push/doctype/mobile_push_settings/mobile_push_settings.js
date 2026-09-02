// A button, because setting this up spans a Firebase console, a JSON key, a
// site config and an app build, and until something lands on a phone there is
// no way to tell which of them is wrong.
frappe.ui.form.on("Mobile Push Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Send Test Notification"), () => {
			frappe.call({
				method: "erpnext_mobile_push.api.send_test_notification",
				freeze: true,
				freeze_message: __("Sending…"),
				callback({ message }) {
					if (!message) return;

					if (!message.results) {
						frappe.msgprint({
							title: __("Nothing to send to"),
							message: message.message,
							indicator: "orange",
						});
						return;
					}

					// Each device separately, with Firebase's own error string:
					// one phone failing while another succeeds is the answer to
					// a different question than all of them failing.
					const rows = message.results
						.map(
							(r) =>
								`<tr><td>${frappe.utils.escape_html(r.device || "")}</td>` +
								`<td>${r.delivered ? __("Delivered") : frappe.utils.escape_html(r.error || __("Failed"))}</td></tr>`
						)
						.join("");

					frappe.msgprint({
						title: __("Sent to {0} of {1} devices", [
							message.sent,
							message.results.length,
						]),
						message: `<table class="table table-bordered"><thead><tr><th>${__("Device")}</th><th>${__("Result")}</th></tr></thead><tbody>${rows}</tbody></table>`,
						indicator: message.sent ? "green" : "red",
					});
				},
			});
		});
	},
});
