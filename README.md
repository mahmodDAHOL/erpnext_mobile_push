# erpnext_mobile_push

A Frappe app that pushes the site's notifications to the `erpnext_mobile` app,
so they arrive on the phone whether or not the app is running.

Without it the mobile app can only poll: it asks the site for new notifications
every thirty seconds *while it is open and in front of someone*, and draws them
as a popup inside its own window. Close the app and nothing arrives at all.
This is the other half — the site pushes, and Android or iOS draws the
notification itself.

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
   Google Cloud console if it is not already on.

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
