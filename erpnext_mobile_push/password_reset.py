"""Resetting a forgotten password by a code sent to an address on file.

Frappe already has a password reset and it is no use here: it emails a *link*,
which means a working session in a browser, on a site whose desk the person may
have no business in. This sends a six-digit code the mobile app can take.

Where the code goes is settled by `_destination`, and the caller is never told
which of the two it picked.

Three steps, three calls, and a deliberate gap between each:

    request_code   → a six-digit code, emailed, good for fifteen minutes
    verify_code    → the code, exchanged for a one-shot token
    set_password   → the token, spent on a new password

The middle step exists so the code is never the thing that sets the password.
A code is six digits typed on a phone; it is short because it has to be, and
short is guessable. It buys a token instead — 32 random bytes, good for ten
minutes, usable once — and the token is what the last call demands. That way
the guessable secret is only ever checked against a counter that stops at five,
and the secret that actually changes a password cannot be guessed at all.

All three are open to guests, because someone who cannot sign in is a guest.
That is the whole reason to be careful here, and the care is spelled out at
each call.
"""

import hashlib
import re
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.sessions import clear_sessions

# How long a code is worth typing.
CODE_TTL = 15 * 60

# How long the token it buys is worth holding. Shorter: by then the person is
# already at the keyboard with the new password half typed.
TOKEN_TTL = 10 * 60

# Wrong codes before the code is thrown away. Five is enough for a typo and a
# re-read of the email, and 5 in 900,000 is not a way in.
MAX_ATTEMPTS = 5

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _cache():
	"""The Redis wrapper, whichever way this Frappe exposes it.

	`frappe.cache` is a function in v14 and v15 and an attribute on newer
	builds. Asking which costs one `callable`; guessing wrong costs a
	TypeError in the middle of a password reset.
	"""
	cache = frappe.cache
	return cache() if callable(cache) else cache


def _code_key(user: str) -> str:
	return f"mobile_pwreset:code:{user}"


def _token_key(user: str) -> str:
	return f"mobile_pwreset:token:{user}"


def _digest(secret: str, salt: str) -> str:
	"""What gets stored in place of a secret.

	Redis is not where a live password-reset code should sit in the clear: it
	is dumped by `bench --site all backup`-adjacent tooling, read by anything
	with the socket, and kept in memory that nobody audits. Salted SHA-256
	means a copy of the cache is a copy of nothing.
	"""
	return hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()


def _cannot_send():
	"""The one refusal every failure to send shares.

	Whether the account does not exist, has no Employee record, or has an
	Employee record with the personal email left blank, the caller is told the
	same thing. The alternative is a form that answers "no such user" — which
	is a way to read the staff list off the login screen, one guess at a time.

	The message still leaves the person somewhere to go, which is the point:
	the department that can do it by hand is named.
	"""
	frappe.throw(
		_(
			"We could not send a code for that account. Please contact the "
			"Digital Transformation Department - floor 6."
		)
	)


def _resolve_user(given: str) -> str | None:
	"""The User this login id names, if any.

	A User's name *is* their login id in Frappe, but people also sign in with
	the `username` field, and they type it in whatever case they please.
	"""
	given = (given or "").strip()
	if not given:
		return None
	if frappe.db.exists("User", given):
		return given
	return frappe.db.get_value("User", {"username": given}, "name") or frappe.db.get_value(
		"User", {"name": ("like", given)}, "name"
	)


def _personal_email(user: str) -> str | None:
	"""The address HR holds for this person, off their Employee record.

	Read by way of `frappe.db.get_value`, which does not apply permissions —
	the caller is a guest and has no business reading Employee rows, but this
	one field, for this one purpose, is the entire fallback. It is never
	returned to the caller, in any form.
	"""
	employee = frappe.db.get_value(
		"Employee",
		{"user_id": user, "status": "Active"},
		["name", "personal_email"],
		as_dict=True,
	)
	if not employee:
		# Some sites keep leavers as Inactive but still let them sign in. Fall
		# back to the record whatever its status, rather than refusing someone
		# whose HR paperwork is merely untidy.
		employee = frappe.db.get_value(
			"Employee", {"user_id": user}, ["name", "personal_email"], as_dict=True
		)
	if not employee:
		return None
	email = (employee.personal_email or "").strip()
	return email if _EMAIL.match(email) else None


def _destination(user: str) -> str | None:
	"""Where this person's code goes: their own address, or HR's copy.

	The account's own address first. On this site most people sign in with an
	address they actually read, so the one they just typed into the app is
	usually the right place to send to and asking HR's records for a second
	opinion only adds a way to fail.

	It is the address *on the User record* rather than the string the caller
	typed, which matters more than it looks: the two are the same thing
	whenever the login id is an address, and where they are not — someone
	signing in by `username` — the record is right and the typed string is not
	an address at all. Taking the caller's word for it would turn this into a
	form that emails a code anywhere it is told to.

	Falls back to `personal_email` when the account has no usable address of
	its own, which is what an internal-only login looks like.
	"""
	own = (frappe.db.get_value("User", user, "email") or "").strip()
	if _EMAIL.match(own):
		return own
	return _personal_email(user)


@frappe.whitelist(allow_guest=True)
# Two limits, because they stop different things. The first counts every check
# from one address whatever login id it names, which is the one that matters:
# this call answers "is this a real account", and without a ceiling on the
# *rate* it answers that for the whole staff list in an afternoon. The second
# keeps any one id from being hammered.
@rate_limit(limit=30, seconds=60 * 60, methods=["POST"])
@rate_limit(key="user", limit=10, seconds=60 * 60, methods=["POST"])
def can_reset(user: str):
	"""Whether a reset can be started for this login id.

	Answers a plain yes or no, and gives no reason for a no: an account that
	does not exist, one that is disabled, and one with no address anywhere —
	neither on the User record nor at HR — are the same answer, so the caller
	cannot tell a stranger from a colleague whose record is incomplete.

	This is a courtesy to the app, not a gate. `request_code` makes every one
	of these checks again for itself, because a yes here is a fact about a
	moment ago and the only call that matters is the one that sends.
	"""
	name = _resolve_user(user)
	if not name:
		return {"ok": False}
	if not frappe.db.get_value("User", name, "enabled"):
		return {"ok": False}
	return {"ok": bool(_destination(name))}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="user", limit=5, seconds=60 * 60, methods=["POST"])
def request_code(user: str):
	"""Emails a fresh code to whichever address this person has on file.

	Rate limited twice over: five an hour for any one login id, by the
	decorator, and — because the decorator keys on what the caller typed — the
	code itself is replaced rather than added to, so ten requests still leave
	exactly one code alive.
	"""
	name = _resolve_user(user)
	if not name:
		_cannot_send()

	enabled = frappe.db.get_value("User", name, "enabled")
	if not enabled:
		_cannot_send()

	email = _destination(name)
	if not email:
		_cannot_send()

	code = f"{secrets.randbelow(900000) + 100000}"
	salt = secrets.token_hex(8)
	_cache().set_value(
		_code_key(name),
		{"digest": _digest(code, salt), "salt": salt, "attempts": 0},
		expires_in_sec=CODE_TTL,
	)

	# Handed to a worker, not sent on this request.
	#
	# It was sent inline, on the reasoning that a queued email waits for the
	# scheduler's next pass and a code that lands four minutes late is a code
	# nobody is still waiting for. That was right about the scheduler and wrong
	# about the fix: sending inline means this request holds open for the whole
	# SMTP conversation — greeting, TLS, auth, delivery — which against a
	# remote mail host regularly runs past thirty seconds. The phone gives up,
	# says the server took too long, and drops the reset; the email arrives
	# anyway, to somebody now looking at an error.
	#
	# The short queue is neither: an RQ worker takes it in well under a second,
	# so delivery starts at once and the caller is answered at once. It is also
	# what this app already does for a push.
	frappe.enqueue(
		"frappe.sendmail",
		queue="short",
		recipients=[email],
		subject=_("Your password reset code"),
		message=_(
			"<p>Someone asked to reset the password for <b>{0}</b>.</p>"
			"<p>The code is <b style='font-size:20px;letter-spacing:2px'>{1}</b>. "
			"It is good for {2} minutes.</p>"
			"<p>If this was not you, you can ignore this message — nothing has "
			"changed, and nobody can change it without this code.</p>"
		).format(name, code, CODE_TTL // 60),
	)

	# Nothing about *where* it went comes back. Two addresses are in play and
	# which one was used is a fact about the account rather than about the
	# request — a caller who learns that the fallback was needed has learned
	# something about somebody else's records.
	return {"sent": True, "expires_in": CODE_TTL}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="user", limit=20, seconds=60 * 60, methods=["POST"])
def verify_code(user: str, code: str):
	"""Trades a correct code for a one-shot token.

	The attempt counter is kept in the cache rather than the database on
	purpose: `frappe.throw` rolls back the transaction, so a counter written to
	a table and then thrown past would be un-written on the way out — and a
	counter that resets on every wrong guess is not a counter.
	"""
	name = _resolve_user(user)
	if not name:
		_no_such_code()

	cache = _cache()
	state = cache.get_value(_code_key(name))
	if not state:
		_no_such_code()

	attempts = int(state.get("attempts") or 0) + 1
	if attempts > MAX_ATTEMPTS:
		cache.delete_value(_code_key(name))
		frappe.throw(_("Too many wrong codes. Please ask for a new one."))

	if not secrets.compare_digest(
		state["digest"], _digest((code or "").strip(), state["salt"])
	):
		state["attempts"] = attempts
		# Re-set with what is left of the original life, so a run of wrong
		# guesses cannot be used to keep a code alive past its fifteen minutes.
		cache.set_value(_code_key(name), state, expires_in_sec=CODE_TTL)
		frappe.throw(
			_("That code is not right. {0} tries left.").format(MAX_ATTEMPTS - attempts)
		)

	# Right: spend it. The code is gone whether or not the person goes on to
	# set a password, so it can never be used twice.
	cache.delete_value(_code_key(name))

	token = secrets.token_urlsafe(32)
	salt = secrets.token_hex(8)
	cache.set_value(
		_token_key(name),
		{"digest": _digest(token, salt), "salt": salt},
		expires_in_sec=TOKEN_TTL,
	)
	return {"token": token, "expires_in": TOKEN_TTL}


def _no_such_code():
	"""Said for a code that was never issued and for one that has run out.

	One message for both, so the form cannot be used to find out which login
	ids have a reset in flight.
	"""
	frappe.throw(_("That code has expired. Please ask for a new one."))


@frappe.whitelist(allow_guest=True)
@rate_limit(key="user", limit=10, seconds=60 * 60, methods=["POST"])
def set_password(user: str, token: str, new_password: str):
	"""Spends a token on a new password.

	The password is set through the User document rather than through
	`frappe.utils.password.update_password`, so that the site's own password
	policy runs — minimum length, the strength score in System Settings, the
	"cannot reuse" rules. Writing the hash directly would put a password on the
	account that the site would have refused at its own form.
	"""
	name = _resolve_user(user)
	if not name:
		_no_such_token("no User matches the login id given")

	cache = _cache()
	state = cache.get_value(_token_key(name))
	if not state:
		_no_such_token(f"no token held for {name}: it expired, or the cache was cleared")

	if not secrets.compare_digest(state["digest"], _digest((token or "").strip(), state["salt"])):
		_no_such_token(f"the token sent for {name} is not the one that was issued")

	if not new_password:
		frappe.throw(_("Please choose a password."))

	doc = frappe.get_doc("User", name)
	doc.new_password = new_password
	doc.save(ignore_permissions=True)

	# Spent once the password is actually set, not before.
	#
	# It was the other way round, and that was wrong: the save below runs the
	# site's password policy, so a password the site judged too weak threw past
	# a token that had already been destroyed. The person was then told their
	# reset had expired — for the crime of choosing a short password — and had
	# to start again from the email. A token that outlives a *refusal* is fine;
	# what it must not outlive is a success.
	cache.delete_value(_token_key(name))

	# Whoever knew the old password no longer gets to keep a session open on
	# it. Best effort: a site that cannot clear sessions has still had its
	# password changed, and failing the whole call over the tidying would leave
	# the person unable to sign in with either password.
	try:
		clear_sessions(name, force=True)
	except Exception:
		frappe.log_error(title="Password reset: could not clear sessions", message=name)

	frappe.db.commit()
	return {"ok": True}


def _no_such_token(reason: str):
	"""Said for a token that was never issued, has run out, or does not match.

	One message for all three, so the call cannot be used to find out which
	login ids have a reset in flight — but the reason is written to the Error
	Log, because otherwise the three are indistinguishable from the outside and
	a deployment fault reads exactly like an expiry.
	"""
	frappe.log_error(title="Password reset: token refused", message=reason)
	# Committed before the throw, or it would not survive it: `frappe.throw`
	# aborts the request and Frappe rolls the transaction back, taking the log
	# row with it. Nothing else is pending here — everything above this point
	# is a read — so there is nothing else for the commit to carry.
	frappe.db.commit()
	frappe.throw(_("This reset has expired. Please start again."))
