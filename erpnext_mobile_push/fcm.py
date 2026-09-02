"""Talking to Firebase Cloud Messaging.

Everything Google-facing lives here: minting an access token from the service
account, posting a message, and reading back what FCM says went wrong. The rest
of the app (`push.py`, `api.py`) deals in Frappe documents and never sees an
HTTP request.

## Why the token is minted by hand

FCM's HTTP v1 API authenticates with a short-lived OAuth2 access token, which
is obtained by signing a JWT with the service account's private key. Google
publishes a library that does this (`google-auth`), and it is one more thing
for an administrator to install into the bench and keep in step with a Python
upgrade. The exchange itself is thirty lines and uses only PyJWT and
`requests` — both already direct dependencies of Frappe — so it is done here.
"""

import json
import time

import frappe
import requests
from frappe import _

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

# Where the minted access token is kept between sends. Google issues them with
# an hour's life; this expires the cached copy early so a token is never used
# in the last few minutes of its validity, when a slow request could arrive at
# Google after it has lapsed.
ACCESS_TOKEN_CACHE_KEY = "erpnext_mobile_push:fcm_access_token"
ACCESS_TOKEN_TTL = 45 * 60

# How long to wait on Google. Sends run in a background worker, so this is not
# holding a user's request open, but a hung connection would hold a worker.
REQUEST_TIMEOUT = 20

# The FCM error codes that mean this particular token is dead — the app was
# uninstalled, the data cleared, the token replaced. The row is deleted rather
# than retried; there is nothing at the other end of it any more.
DEAD_TOKEN_ERRORS = {"UNREGISTERED", "INVALID_ARGUMENT", "SENDER_ID_MISMATCH"}


class PushNotConfigured(frappe.ValidationError):
	"""Raised when the site has no usable Firebase credentials."""


def get_settings():
	"""The site's push settings, as a plain dict.

	`site_config.json` wins over the DocType when both are present. An
	administrator who would rather not have a private key sitting in a database
	column — a reasonable position, and the Frappe convention for credentials —
	puts it in the site config and leaves the field blank.
	"""
	doc = frappe.get_cached_doc("Mobile Push Settings")
	raw = frappe.conf.get("mobile_push_service_account") or doc.service_account_json

	return frappe._dict(
		enabled=bool(doc.enabled),
		service_account=raw,
	)


def get_service_account():
	"""The parsed service account, or a readable error saying what is missing."""
	settings = get_settings()
	if not settings.enabled:
		raise PushNotConfigured(_("Mobile push notifications are switched off in Mobile Push Settings."))

	raw = settings.service_account
	if not raw:
		raise PushNotConfigured(
			_(
				"No Firebase service account. Paste the JSON key into Mobile Push Settings, "
				"or set 'mobile_push_service_account' in site_config.json."
			)
		)

	if isinstance(raw, dict):
		account = raw
	else:
		try:
			account = json.loads(raw)
		except ValueError:
			raise PushNotConfigured(
				_("The Firebase service account is not valid JSON. Paste the whole downloaded key file.")
			) from None

	missing = [key for key in ("project_id", "client_email", "private_key") if not account.get(key)]
	if missing:
		raise PushNotConfigured(
			_("The Firebase service account is missing: {0}").format(", ".join(missing))
		)

	return account


def get_access_token(force_refresh: bool = False) -> str:
	"""A bearer token for the FCM API, minted from the service account.

	Cached, because every notification would otherwise cost two round trips to
	Google instead of one — and a busy site writes a great many Notification
	Log rows.
	"""
	cache = frappe.cache()
	if not force_refresh:
		cached = cache.get_value(ACCESS_TOKEN_CACHE_KEY)
		if cached:
			return cached

	import jwt  # noqa: PLC0415 — a Frappe dependency, imported where it is used

	account = get_service_account()
	now = int(time.time())
	assertion = jwt.encode(
		{
			"iss": account["client_email"],
			"scope": FCM_SCOPE,
			"aud": account.get("token_uri") or GOOGLE_TOKEN_URL,
			"iat": now,
			"exp": now + 3600,
		},
		account["private_key"],
		algorithm="RS256",
	)

	response = requests.post(
		account.get("token_uri") or GOOGLE_TOKEN_URL,
		data={
			"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
			"assertion": assertion,
		},
		timeout=REQUEST_TIMEOUT,
	)
	if response.status_code != 200:
		raise PushNotConfigured(
			_("Google refused the Firebase service account ({0}): {1}").format(
				response.status_code, response.text[:500]
			)
		)

	payload = response.json()
	token = payload.get("access_token")
	if not token:
		raise PushNotConfigured(_("Google returned no access token for the Firebase service account."))

	# A shade under what Google grants, so a send that starts just before the
	# cache expires still finishes with a valid token.
	cache.set_value(ACCESS_TOKEN_CACHE_KEY, token, expires_in_sec=ACCESS_TOKEN_TTL)
	return token


def send(token: str, message: dict) -> tuple[bool, str]:
	"""Sends one already-built message to one device.

	Returns `(delivered, error_code)`. `error_code` is FCM's own string for a
	failure — `UNREGISTERED` and the rest of [DEAD_TOKEN_ERRORS] are the ones
	the caller acts on — and empty on success.

	Never raises for a per-device failure: a hundred devices are sent to in a
	loop and one dead token must not stop the other ninety-nine.
	"""
	account = get_service_account()
	body = {"message": {**message, "token": token}}

	try:
		response = _post(body, account)
		# One retry on 401 only, and only after forcing a fresh token: the
		# cached one can have been invalidated by a key rotation, and the whole
		# site would otherwise stop delivering until the cache expired.
		if response.status_code == 401:
			response = _post(body, account, force_refresh=True)
	except requests.RequestException as e:
		return False, f"NETWORK: {e}"

	if 200 <= response.status_code < 300:
		return True, ""

	return False, _error_code(response)


def _post(body: dict, account: dict, force_refresh: bool = False):
	return requests.post(
		FCM_ENDPOINT.format(project_id=account["project_id"]),
		json=body,
		headers={
			"Authorization": f"Bearer {get_access_token(force_refresh=force_refresh)}",
			"Content-Type": "application/json; UTF-8",
		},
		timeout=REQUEST_TIMEOUT,
	)


def _error_code(response) -> str:
	"""FCM's machine-readable reason for a refusal.

	The interesting one is nested in `error.details[].errorCode`; `error.status`
	is the generic gRPC name and is the fallback. Anything unparseable comes
	back as the status line, which at least says something in the log.
	"""
	try:
		error = response.json().get("error", {})
	except ValueError:
		return f"HTTP {response.status_code}: {response.text[:200]}"

	for detail in error.get("details") or []:
		code = detail.get("errorCode")
		if code:
			return code

	return error.get("status") or f"HTTP {response.status_code}"
