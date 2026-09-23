# erpnext_mobile_push

A Frappe app that pushes the site's notifications to the `erpnext_mobile` app,
so they arrive on the phone whether or not the app is running.

Without it the mobile app announces nothing: it asks the site for the unread
count every thirty seconds *while it is open and in front of someone*, which
only keeps the number on the bell honest. With it, the site pushes the moment
it writes the row and Android or iOS draws the notification itself, with the
app backgrounded, killed, or never opened since the last reboot.

## What it hooks

One hook, on `Notification Log`:

```python
doc_events = {"Notification Log": {"after_insert": "erpnext_mobile_push.push.on_notification_log"}}
```

`Notification Log` is the row Frappe already writes for every assignment,
mention, share, energy point and Notification-doctype alert — one recipient per
row, with the document it is about attached. Hooking it means this app never
has to know what any of those features are: whatever the desk would show in its
bell, the phone gets.

The push itself is queued (`enqueue_after_commit`), never sent inline. Two
reasons, both of which have caught people out elsewhere: a round trip to Google
would be added to whatever the user was actually doing — an assignment made
from a form submit would hold that submit open for it — and an exception raised
in a `doc_event` rolls back the transaction that wrote the notification, so a
Firebase outage would stop people being *assigned* work rather than merely stop
them hearing about it.

## Installing

```sh
cd ~/frappe-bench
bench get-app erpnext_mobile_push /path/to/erpnext_mobile/server/erpnext_mobile_push
bench --site your-site.example install-app erpnext_mobile_push
bench --site your-site.example migrate
```

It needs nothing that Frappe does not already ship. The FCM access token is
minted with PyJWT and `cryptography`, both direct Frappe dependencies, rather
than by adding `google-auth` to the bench.

## Setting up Firebase

The mobile app and this app have to point at **the same Firebase project**.

**Two different files, and they are easy to confuse.** `google-services.json`
(and `GoogleService-Info.plist`) is *client* config: it identifies the app to
Firebase, ships inside the APK, and is not a secret — the `api_key` in it is
public by design and restricted by the app's package name. The *service
account* key is a server credential that can push a notification to every
device in the project. It belongs on the site and nowhere else: never in the
mobile app's repository, never in the APK. Google scans public repositories for
these and revokes them on sight, which is the *better* outcome.

1. Create a project at <https://console.firebase.google.com> (or use an
   existing one).
2. **For the server — this app.** Open **Project settings → Service accounts →
   Generate new private key**. Keep the file that downloads; it is a
   credential, not a config, and it can send a notification to every device in
   the project.
3. **For the app.** In **Project settings → Your apps**, add an Android app
   with the application id from `android/app/build.gradle.kts`, download
   `google-services.json` and put it in `android/app/`. For iOS, add an iOS app
   with the bundle identifier, download `GoogleService-Info.plist` and add it to
   the Runner target in Xcode. See the push notifications section of the mobile
   app's `README.md`, which also covers the APNs key iOS needs.
4. Enable the **Firebase Cloud Messaging API (V1)** for the project in the
   Google Cloud console if it is not already on
   (`console.cloud.google.com/apis/library/fcm.googleapis.com`). Left off,
   every send fails with a 403 that does not obviously say so.

Then, on the site, open **Mobile Push Settings**:

- paste the whole service account file into **Service Account JSON**, and
- tick **Send Push Notifications**.

To keep the private key out of the database, leave the field blank and put the
key in `site_config.json` instead — the site config wins over the field when
both are set:

```json
{
  "mobile_push_service_account": {
    "type": "service_account",
    "project_id": "...",
    "client_email": "...",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
  }
}
```

## Checking it works

Sign in on the phone first and allow notifications when it asks — that is what
registers the device. Then use **Send Test Notification** on Mobile Push
Settings. It reports each registered device separately with Firebase's own
error string, which is the point: setting this up spans a console, a key, a
site config and an app build, and until something lands on a phone there is no
way to tell which of them is wrong.

From a shell, the same thing:

```sh
bench --site your-site.example execute erpnext_mobile_push.api.send_test_notification
```

Nothing registered? The app registers on sign-in and on every launch. If the
list stays empty, the app is either built without `google-services.json` (it
logs that it is, on startup) or the site has no `erpnext_mobile_push`
installed — the app logs that too, when its registration comes back 404.

## The DocTypes

**Mobile Push Token** — one row per device. Named by the SHA-256 of the FCM
token, because a token is longer than the 140 characters a Frappe name allows,
and because hashing it makes "one row per device" something the primary key
enforces rather than something the code has to remember under a race.

Rows are removed three ways: the app deletes its own on sign-out, a send that
FCM refuses with `UNREGISTERED` (app uninstalled, data cleared) deletes it
there and then, and a weekly job clears anything not seen for four months —
a phone that is simply never used again is never sent to, so it never refuses,
so nothing else would ever clear it.

**Mobile Push Settings** — a Single holding the service account and the on/off
switch. System Manager only, and neither exportable nor printable: the field
holds a private key.

## Security

The two methods the app calls, `register_device` and `unregister_device`, act
on `frappe.session.user` and take no parameter saying whose notifications to
route. There is nothing in the request to tamper with: a registration can only
ever mean "send *my* notifications to this device", and the only device it can
be is the one holding the session cookie.

`register_device` deliberately *moves* a token between users rather than
refusing a token that already belongs to someone else. The same phone signing
in as somebody else keeps its FCM token, and a row that went on naming the
previous user would ring that user's notifications on a phone they are no
longer signed in to.

## Resetting a forgotten password

`password_reset.py` is the site half of the mobile app's "Forget Password?"
flow. It sits in this app because this app is already the one the mobile client
talks to; nothing about it is push.

Frappe's own reset is no use here. It emails a *link*, which wants a browser
and a working session on a desk the person may have no business in. This emails
a six-digit code the app can take instead.

**Where the code goes.** The address on the User record first — on this site
most people sign in with an address they actually read, so the one they typed
into the app is usually the right place to send to. Where the account has no
usable address of its own, which is what an internal-only login looks like, it
falls back to `personal_email` on the matching Employee record.

It is the address *on the record*, never the string the caller typed. The two
are the same thing whenever the login id is an address; where they are not —
someone signing in by `username` — the record is right and the typed string is
not an address at all. Taking the caller's word for it would turn this into a
form that emails a reset code anywhere it is told to.

Which of the two was used is never reported, in the response or on screen. A
caller who learns that the fallback was needed has learned something about
somebody else's records.

Four whitelisted, guest-callable methods:

```
erpnext_mobile_push.password_reset.can_reset      user
erpnext_mobile_push.password_reset.request_code   user
erpnext_mobile_push.password_reset.verify_code    user, code
erpnext_mobile_push.password_reset.set_password   user, token, new_password
```

`can_reset` is a database lookup and nothing else — no mail — so the app can
wait on it before moving off the first screen and tell someone their login id
is not registered rather than sending them to a screen to wait for an email
that will never come. It is a courtesy, not a gate: `request_code` makes every
one of the same checks again for itself, because a yes from `can_reset` is a
fact about a moment ago.

**This is account enumeration, and it is deliberate** — it was asked for. A
form that answers "not registered" can be used to read the staff list one guess
at a time. What stops it being quick is the rate limit: thirty checks an hour
from one address *whatever login id they name*, on top of ten an hour for any
one id. `can_reset` gives no reason for a no, so an unknown account, a disabled
one and one with no address anywhere are indistinguishable.

The middle step is the point of the design. A code is six digits typed on a
phone; it is short because it has to be, and short is guessable. So the code
never sets a password — it buys a token (32 random bytes, ten minutes, one
use), and only the token is accepted by `set_password`. The guessable secret is
only ever checked against a counter that stops at five wrong tries and then
throws the code away; the secret that can actually change a password cannot be
guessed at all.

Codes and tokens live in Redis under a TTL, salted and hashed, never in the
database. They are secrets with a fifteen-minute life: a table of live password
reset codes is a thing worth not having, and a cache entry expires by itself
rather than needing a cleanup job that might not run.

**It does not say whether an account exists.** Every failure to send — no such
user, no address on the account and none on file at HR, a disabled account —
gives the same answer, naming the department that can do it by hand. A form
that answered "no such user" would be a way to read the staff list off the
login screen, one guess at a time.

Rate limited per login id per hour by `frappe.rate_limiter`: five codes, twenty
code checks, ten password sets.

The password itself is set through the User document rather than by writing a
hash, so the site's own policy runs — minimum length, the strength score in
System Settings, any reuse rules. Every session open on the old password is
closed afterwards.

### What it needs

An outgoing **Email Account** on the site, because the code is sent with
`now=True` rather than queued: a code that lands four minutes after it was
asked for is a code the person has already given up on. No DocType, no
migration, nothing to configure — `bench --site your-site.example migrate` and
a restart is enough.
