import json

import frappe
from frappe import _
from frappe.model.document import Document


class MobilePushSettings(Document):
	def validate(self):
		self.check_service_account()

	def check_service_account(self):
		"""Refuses a key that could never work, at the point somebody can fix it.

		Without this the first sign of a mistyped or half-pasted key is a
		notification that silently never arrives, hours later, with the reason
		in the Error Log where nobody is looking.
		"""
		if not self.enabled:
			return

		raw = frappe.conf.get("mobile_push_service_account") or self.service_account_json
		if not raw:
			frappe.throw(
				_(
					"Paste the Firebase service account JSON below, or set "
					"'mobile_push_service_account' in site_config.json, before turning this on."
				)
			)

		if isinstance(raw, dict):
			account = raw
		else:
			try:
				account = json.loads(raw)
			except ValueError:
				frappe.throw(_("That is not valid JSON. Paste the whole key file as it was downloaded."))

		missing = [k for k in ("project_id", "client_email", "private_key") if not account.get(k)]
		if missing:
			frappe.throw(
				_("The service account is missing {0}. Paste the whole key file as it was downloaded.").format(
					", ".join(missing)
				)
			)
