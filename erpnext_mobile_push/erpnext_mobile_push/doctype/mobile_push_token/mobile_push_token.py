import frappe
from frappe.model.document import Document


class MobilePushToken(Document):
	def validate(self):
		# The hash is what names the row, so a mismatch would mean a device
		# whose record can never be found again by the token it holds — it
		# would re-register as a second row on every launch.
		import hashlib

		expected = hashlib.sha256((self.token or "").encode("utf-8")).hexdigest()
		if self.token_hash != expected:
			frappe.throw(frappe._("The token hash does not match the token."))
