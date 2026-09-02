"""Turning a `Notification Log` row into a notification on someone's phone.

The shape of it: Frappe writes a `Notification Log` row for every assignment,
mention, share, energy point and Notification-doctype alert. `hooks.py` catches
the insert, this queues a background job, and the job sends one FCM message per
device that user has registered.

Nothing here decides *what* is worth notifying about. That decision was already
made — by an Assignment Rule, by a Notification, by someone typing an @mention
— at the point the row was written. This only carries it to the phone.
"""

import frappe
from frappe.utils import add_days, cint, now_datetime, strip_html

from erpnext_mobile_push import fcm

# The Android channel the app creates, and the one named in its manifest. If
# these three ever disagree, a notification arriving while the app is closed
# lands in a channel nobody configured: silent, no banner, easily missed.
ANDROID_CHANNEL_ID = "erpnext_mobile_notifications"

# A tray entry is two or three lines. `subject` is a sentence and `email_content`
# can be an entire HTML email; both are cut to something that reads as a
# summary, with the whole thing a tap away in the app.
MAX_TITLE = 120
MAX_BODY = 240

# Tokens not seen for this long are assumed gone. Long enough to survive a
# holiday, a spare phone, or a long stretch of leave.
STALE_TOKEN_DAYS = 120


def on_notification_log(doc, method=None):
	"""Queues a push for a freshly written `Notification Log` row.

	Queued rather than sent inline for two reasons, both of which have bitten
	somebody before. A round trip to Google — two, when the access token needs
	minting — would be added to whatever the user was actually doing: an
	assignment made from a form submit would hold that submit open for it. And
	an exception raised here rolls back the transaction that wrote the
	notification, so a Firebase outage would stop people being *assigned* work,
	not merely stop them hearing about it.

	`enqueue_after_commit` matters as much: the worker reads the row back by
	name, and without it the job can start before the transaction that created
	it has committed and find nothing there.
	"""
	if not doc.for_user:
		return

	# Cheap, and it keeps a site that has not configured Firebase — or has
	# turned push off — from queueing a job per notification forever.
	try:
		if not fcm.get_settings().enabled:
			return
	except Exception:
		return

	frappe.enqueue(
		"erpnext_mobile_push.push.send_for_notification_log",
		queue="short",
		enqueue_after_commit=True,
		notification_log=doc.name,
	)


def send_for_notification_log(notification_log: str):
	"""Sends one notification to every device its recipient has registered."""
	try:
		doc = frappe.get_doc("Notification Log", notification_log)
	except frappe.DoesNotExistError:
		# Deleted between the insert and this job running. Nothing to send.
		return

	tokens = frappe.get_all(
		"Mobile Push Token",
		filters={"user": doc.for_user, "enabled": 1},
		fields=["name", "token", "platform"],
	)
	if not tokens:
		return

	message = build_message(doc)

	for row in tokens:
		delivered, error = fcm.send(row.token, message)
		if delivered:
			continue

		if error in fcm.DEAD_TOKEN_ERRORS:
			# The device is gone — uninstalled, wiped, or the token replaced.
			# Deleting rather than disabling: a token is not a record of
			# anything, and a dead one kept around is a send attempted on every
			# future notification for this user, forever.
			frappe.delete_doc(
				"Mobile Push Token", row.name, force=True, ignore_permissions=True, delete_permanently=True
			)
			continue

		# Everything else — a network blip, a quota, a misconfigured project —
		# is the site's problem, not this device's, and is left alone to be
		# tried again by the next notification.
		frappe.log_error(
			title="Mobile push failed",
			message=f"user={doc.for_user} token={row.name} notification={doc.name} error={error}",
		)

	frappe.db.commit()  # nosemgrep — a background job owns its own transaction


def build_message(doc) -> dict:
	"""The FCM message for one `Notification Log` row.

	Both halves are filled in on purpose, and they do different jobs:

	`notification` is what Android and iOS draw by themselves when the app is
	not running. It is the only part that works with the app killed, and it is
	display text and nothing else — there is nowhere in it to say which record
	the message is about.

	`data` is what reaches the app's own code. It survives every delivery path
	there is, including a tap that starts the app from cold, and it is what
	carries the link to the document. Its values must all be strings; FCM
	rejects the message outright for anything else, including an integer.
	"""
	title = _clip(strip_html(doc.subject or ""), MAX_TITLE) or "Notification"
	body = _clip(strip_html(doc.email_content or ""), MAX_BODY)

	return {
		"notification": {"title": title, "body": body},
		"data": {
			"notification_id": doc.name,
			"title": title,
			"body": body,
			"document_type": doc.document_type or "",
			"document_name": doc.document_name or "",
			"type": doc.type or "",
		},
		"android": {
			# `high` is what wakes a dozing phone to deliver. `normal` lets
			# Android hold the message until the device next wakes on its own,
			# which for a phone in a pocket overnight can be hours.
			"priority": "high",
			"notification": {
				"channel_id": ANDROID_CHANNEL_ID,
				"default_sound": True,
			},
		},
		"apns": {
			"headers": {"apns-priority": "10"},
			"payload": {
				"aps": {
					"sound": "default",
					"badge": unread_count(doc.for_user),
					# Wakes the app briefly so it can note that the tray has
					# shown this, which is how it avoids telling the user the
					# same thing a second time when they next open it.
					"content-available": 1,
				}
			},
		},
	}


def unread_count(user: str) -> int:
	"""The number on the app's icon: how many notifications this user has not read."""
	return cint(
		frappe.db.count("Notification Log", filters={"for_user": user, "read": 0})
	)


def _clip(text: str, limit: int) -> str:
	text = " ".join((text or "").split())
	if len(text) <= limit:
		return text
	return text[: limit - 1].rstrip() + "\u2026"


def delete_stale_tokens():
	"""Drops tokens no app has checked in with for a long time.

	A dead token normally announces itself: FCM refuses the send and
	[send_for_notification_log] deletes it. A device that is simply never used
	again never gets sent to, never refuses, and so never announces anything —
	this is what clears those.
	"""
	cutoff = add_days(now_datetime(), -STALE_TOKEN_DAYS)
	stale = frappe.get_all(
		"Mobile Push Token",
		filters={"last_seen": ["<", cutoff]},
		pluck="name",
	)
	for name in stale:
		frappe.delete_doc(
			"Mobile Push Token", name, force=True, ignore_permissions=True, delete_permanently=True
		)
	if stale:
		frappe.db.commit()  # nosemgrep — a scheduled job owns its own transaction
