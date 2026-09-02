app_name = "erpnext_mobile_push"
app_title = "ERPNext Mobile Push"
app_publisher = "erpnext_mobile"
app_description = (
	"Delivers Frappe notifications to the erpnext_mobile app as real push "
	"notifications, through Firebase Cloud Messaging."
)
app_email = "support@example.com"
app_license = "mit"

# The one hook that makes this app do anything.
#
# `Notification Log` is the row Frappe writes for every assignment, mention,
# share, energy point and Notification-doctype alert — one place, already
# filtered to a single recipient in `for_user`, already carrying the document
# it is about. Hooking it means this app never has to know what any of those
# features are: anything the desk would show in its bell, the phone gets.
#
# `after_insert` rather than `on_update`: a notification is written once and
# then only updated to mark it read, and pushing on those updates would ring
# the phone again for something the user has just finished reading.
doc_events = {
	"Notification Log": {
		"after_insert": "erpnext_mobile_push.push.on_notification_log",
	}
}

scheduler_events = {
	"weekly": [
		# Tokens go stale silently — an app uninstalled, a phone wiped. FCM
		# says so on the send that fails and those are deleted there and then,
		# but a device that simply stops being used is never sent to and never
		# refuses, so it would sit in the table for good.
		"erpnext_mobile_push.push.delete_stale_tokens",
	]
}
