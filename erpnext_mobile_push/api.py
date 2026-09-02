"""What the mobile app calls.

Three whitelisted methods, all of which act on the *session's own* user and
never on a user named in the request. That is the whole security model here and
it is worth stating: a registration says "send my notifications to this device",
and the only device it can be is the one holding the session cookie. There is
no parameter for whose notifications to route, so there is nothing to tamper
with.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_mobile_push import fcm, push

def _require_user() -> str:
	"""The signed-in user, or a refusal.

	Session users only. A token registered for Guest would be a device asking
	for notifications belonging to nobody — and `Guest` is a real User row that
	would happily hold one.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("You must be signed in to register a device."), frappe.PermissionError)
	return user


def token_name(token: str) -> str:
	"""The document name for an FCM token.

	The token itself cannot be the name: Frappe names are 140 characters and an
	FCM token is longer than that, sometimes well over 200. Its SHA-256 is 64
	hex characters, fits, and — being the primary key — makes "one row per
	device" a thing the database enforces rather than something this code has
	to remember to check under a race.
	"""
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


@frappe.whitelist()
def register_device(token: str, platform: str = "", device_name: str = ""):
	"""Records this device as somewhere to send the signed-in user's notifications.

	Called after signing in and on every launch, so it must be idempotent — and
	it must be able to *move* a token between users. The same phone signing in
	as somebody else keeps its FCM token, and if the row went on naming the
	previous user, that user's notifications would ring on a phone they are no
	longer signed in to.
	"""
	user = _require_user()
	token = (token or "").strip()
	if not token:
		frappe.throw(_("No device token was given."))

	name = token_name(token)
	values = {
		"user": user,
		"token": token,
		"token_hash": name,
		"platform": (platform or "").strip()[:20],
		"device_name": (device_name or "").strip()[:140],
		"last_seen": now_datetime(),
		"enabled": 1,
	}

	if frappe.db.exists("Mobile Push Token", name):
		doc = frappe.get_doc("Mobile Push Token", name)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		# Named from `token_hash` by the DocType's own naming rule, so this does
		# not set `name` itself — Frappe would discard it.
		doc = frappe.get_doc({"doctype": "Mobile Push Token", **values})
		doc.insert(ignore_permissions=True)

	return {"registered": True, "device": doc.name}


@frappe.whitelist()
def unregister_device(token: str = ""):
	"""Stops sending to this device. Called when the user signs out.

	Deletes by token when one is given, and otherwise every device belonging to
	the session's user — the fallback matters for a sign-out on a phone whose
	Firebase token could not be read back, which would otherwise leave the site
	pushing that user's work to a phone somebody else is now holding.
	"""
	user = _require_user()
	token = (token or "").strip()

	if token:
		name = token_name(token)
		# Scoped to the session's user: a token is a 64-character hash, but
		# knowing one must still not let anyone delete somebody else's device.
		if frappe.db.get_value("Mobile Push Token", name, "user") == user:
			frappe.delete_doc(
				"Mobile Push Token", name, force=True, ignore_permissions=True, delete_permanently=True
			)
			return {"unregistered": 1}
		return {"unregistered": 0}

	names = frappe.get_all("Mobile Push Token", filters={"user": user}, pluck="name")
	for name in names:
		frappe.delete_doc(
			"Mobile Push Token", name, force=True, ignore_permissions=True, delete_permanently=True
		)
	return {"unregistered": len(names)}


@frappe.whitelist()
def send_test_notification():
	"""Pushes a notification to the caller's own devices, and says what happened.

	Setting this up spans a Firebase console, a JSON key, a site config and an
	app build, and until something arrives on a phone there is no way to tell
	which of them is wrong. This reports each device separately with FCM's own
	error string, so the answer is the message rather than a guess.
	"""
	user = _require_user()

	devices = frappe.get_all(
		"Mobile Push Token",
		filters={"user": user, "enabled": 1},
		fields=["name", "token", "platform", "device_name"],
	)
	if not devices:
		return {
			"sent": 0,
			"message": _(
				"No devices are registered for you. Sign in on the mobile app first, and allow "
				"notifications when it asks."
			),
		}

	message = {
		"notification": {
			"title": _("Test notification"),
			"body": _("Push notifications are working on this device."),
		},
		"data": {
			# A real Notification Log name is what the app expects here; this is
			# deliberately not one, and carries no document, so tapping it opens
			# the app and nothing else.
			"notification_id": f"test-{frappe.generate_hash(length=10)}",
			"title": _("Test notification"),
			"body": _("Push notifications are working on this device."),
			"document_type": "",
			"document_name": "",
			"type": "Alert",
		},
		"android": {
			"priority": "high",
			"notification": {"channel_id": push.ANDROID_CHANNEL_ID, "default_sound": True},
		},
		"apns": {"headers": {"apns-priority": "10"}, "payload": {"aps": {"sound": "default"}}},
	}

	results = []
	for device in devices:
		delivered, error = fcm.send(device.token, message)
		results.append(
			{
				"device": device.device_name or device.platform or device.name,
				"delivered": delivered,
				"error": error,
			}
		)

	return {"sent": sum(1 for r in results if r["delivered"]), "results": results}
