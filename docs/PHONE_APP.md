# ARYA on your phone (Android app)

Reminders, timers, alarms, calendar and meeting heads-ups, the morning briefing and the evening wrap-up arrive on
your phone as real notifications, even with the app closed, and you can talk to ARYA from the phone anywhere.

## 1. Tailscale (once, ~5 minutes): reach the laptop from anywhere

Tailscale makes a private, encrypted network between your own devices. Nothing is opened to the internet.

1. Laptop: install from <https://tailscale.com/download/windows>, sign in.
2. Phone: install **Tailscale** from the Play Store, sign in **with the same account**, switch it on.
3. Both now have a `100.x.y.z` address. ARYA notices within two minutes and listens on it too.

Without Tailscale the app still works, on your home Wi-Fi only.

## 2. The app

1. Open the latest **Android APK** run under the repository's *Actions* tab, download `ARYA-phone-N` and
   unzip it; copy the `.apk` to the phone (or open the run on the phone).
2. Install it (Android asks to allow installing from this source once).
3. On the laptop say **"connect my phone"**: a QR code opens.
4. In the app: **Scan the QR code**. Allow notifications, and allow it to run in the background
   (battery optimisation off), or Android may delay reminders.

That's it: a quiet "Connected to your laptop (Tailscale)" line stays in the notification shade; reminders ring
with **Snooze 10 min** and **Done** buttons.

## How it works

* `GET /api/wait?after=<id>` is a long poll: the laptop answers the moment something happens, or after 50 s;
  the reply holds everything after `<id>`, so a phone that was offline catches up (the last 200 events are kept
  in `%LOCALAPPDATA%\ARYA\phone_events.jsonl`).
* The pairing key from the QR code is sent as `Authorization: Bearer …`; "new phone link" on the laptop locks
  every paired phone out.
* The app tries the Tailscale address first and the home Wi-Fi address second, backing off 2 s → 60 s.
* The phone still can't touch files, the clipboard or commands on the laptop.

## Updates and signing

Each push to `android/` builds a new APK. To install updates over the old app, the APK must be signed with the
same key every time: create one (`keytool -genkeypair -keystore arya.jks -alias arya -keyalg RSA -keysize 3072
-validity 10000`) and add repository secrets `ARYA_KEYSTORE_B64` (the file, base64), `ARYA_KEYSTORE_PASSWORD`,
`ARYA_KEY_ALIAS`, `ARYA_KEY_PASSWORD`. Without them the APK is debug-signed and a new version needs an uninstall
first (and a new scan).
