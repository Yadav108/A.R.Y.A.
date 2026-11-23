package com.arya.phone

import android.Manifest
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.provider.AlarmClock
import android.provider.ContactsContract
import android.provider.MediaStore
import android.provider.Settings
import android.telephony.PhoneNumberUtils
import android.telephony.TelephonyManager
import org.json.JSONObject

/** Phone tasks ARYA's laptop understood but the phone carries out (arya/server/phone_actions.py): alarms, timers,
 *  calls, messages, maps, apps, the torch, the camera, settings, volume and music — with Android's own apps.
 *  Contacts are looked up here, on the phone; they never go to the laptop.
 *  [run] returns null when it worked, else a sentence to say (spoken on the phone). */
object Actions {
    /** Actions that open another app: they run after ARYA has said "Calling Mom." */
    fun leavesApp(a: JSONObject) = a.optString("action") in
        setOf("call", "sms", "navigate", "open_app", "camera", "settings", "play", "voice_note")

    fun run(act: Activity, a: JSONObject): String? = try {
        when (a.optString("action")) {
            "alarm" -> alarm(act, a.optInt("hour"), a.optInt("minute"))
            "timer" -> timer(act, a.optInt("seconds"), a.optString("label"))
            "call" -> call(act, a.optString("who"), a.optString("app"))
            "sms" -> message(act, a.optString("who"), a.optString("text"), a.optString("app"))
            "navigate" -> start(act, Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + Uri.encode(a.optString("to")))),
                "I couldn't find a maps app.")
            "open_app" -> openApp(act, a.optString("app"))
            "torch" -> torch(act, a.optBoolean("on"))
            "camera" -> camera(act, a.optBoolean("video"), a.optBoolean("selfie"))
            "settings" -> settings(act, a.optString("what"))
            "volume" -> volume(act, a.optString("dir"))
            "play" -> play(act, a.optString("query"), a.optString("app"))
            "voice_note" -> { VoiceNotesActivity.open(act, a.optBoolean("record")); null }
            "ask" -> null
            else -> "I can't do that on the phone yet."
        }
    } catch (e: SecurityException) {
        "Android didn't allow that. Check ARYA's permissions in Settings."
    } catch (e: Exception) {
        "That didn't work on the phone."
    }

    private fun start(act: Activity, i: Intent, failure: String): String? = try {
        act.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); null
    } catch (e: ActivityNotFoundException) { failure }

    private fun alarm(act: Activity, h: Int, m: Int) = start(act, Intent(AlarmClock.ACTION_SET_ALARM)
        .putExtra(AlarmClock.EXTRA_HOUR, h).putExtra(AlarmClock.EXTRA_MINUTES, m)
        .putExtra(AlarmClock.EXTRA_MESSAGE, "ARYA").putExtra(AlarmClock.EXTRA_SKIP_UI, true),
        "I couldn't find a clock app for the alarm.")

    private fun timer(act: Activity, s: Int, label: String) = start(act, Intent(AlarmClock.ACTION_SET_TIMER)
        .putExtra(AlarmClock.EXTRA_LENGTH, s).putExtra(AlarmClock.EXTRA_MESSAGE, label.ifEmpty { "ARYA" })
        .putExtra(AlarmClock.EXTRA_SKIP_UI, true), "I couldn't find a clock app for the timer.")

    // ── Contacts (on the phone only) ─────────────────────────────────────────

    private val NICK = mapOf("mom" to listOf("mom", "mum", "mother", "mama", "maa", "mummy"),
                             "dad" to listOf("dad", "father", "papa", "daddy"))

    /** "Mom" -> the best matching contact's number (exact name, then a name word, then part of a name). */
    fun numberFor(ctx: Context, who: String): Pair<String, String>? {
        val digits = who.filter { it.isDigit() || it == '+' }
        if (digits.length >= 6 && who.none { it.isLetter() }) return who to digits
        if (ctx.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) return null
        val wanted = who.lowercase().trim()
        val aliases = NICK.entries.firstOrNull { wanted in it.value }?.value ?: listOf(wanted)
        var best: Triple<Int, String, String>? = null
        ctx.contentResolver.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER,
                    ContactsContract.CommonDataKinds.Phone.STARRED),
            null, null, null)?.use { c ->
            while (c.moveToNext()) {
                val name = c.getString(0) ?: continue
                val number = c.getString(1) ?: continue
                val n = name.lowercase()
                val words = n.split(Regex("[^\\p{L}\\p{N}]+"))
                var score = when {
                    aliases.any { n == it } -> 100
                    aliases.any { it in words } -> 70
                    aliases.any { n.contains(it) } -> 40
                    else -> 0
                }
                if (score == 0) continue
                if (c.getInt(2) == 1) score += 5                         // a favourite wins a tie
                if (best == null || score > best!!.first) best = Triple(score, name, number)
            }
        }
        return best?.let { it.second to it.third }
    }

    private fun call(act: Activity, who: String, app: String): String? {
        val found = numberFor(act, who) ?: return noContact(act, who)
        val (name, number) = found
        if (app == "whatsapp") return whatsapp(act, number, "")
        val direct = act.checkSelfPermission(Manifest.permission.CALL_PHONE) == PackageManager.PERMISSION_GRANTED
        return start(act, Intent(if (direct) Intent.ACTION_CALL else Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number))),
            "I couldn't open the phone app to call $name.")
    }

    private fun message(act: Activity, who: String, text: String, app: String): String? {
        val found = numberFor(act, who) ?: return noContact(act, who)
        if (app == "whatsapp") return whatsapp(act, found.second, text)
        return start(act, Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(found.second)))
            .putExtra("sms_body", text), "I couldn't open the messages app.")
    }

    private fun whatsapp(act: Activity, number: String, text: String): String? {
        val iso = (act.getSystemService(TelephonyManager::class.java)?.networkCountryIso ?: "").uppercase().ifEmpty { "DE" }
        val e164 = PhoneNumberUtils.formatNumberToE164(number, iso) ?: number
        val uri = "https://api.whatsapp.com/send?phone=" + e164.trimStart('+') +
            (if (text.isNotEmpty()) "&text=" + Uri.encode(text) else "")
        return start(act, Intent(Intent.ACTION_VIEW, Uri.parse(uri)), "I couldn't open WhatsApp.")
    }

    private fun noContact(act: Activity, who: String): String =
        if (act.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED)
            "I need access to your contacts to find $who. Allow it in ARYA's permissions."
        else "I couldn't find $who in your contacts."

    // ── Apps, torch, camera, settings, volume, music ─────────────────────────

    private fun openApp(act: Activity, wanted: String): String? {
        val pm = act.packageManager
        val launchers = pm.queryIntentActivities(
            Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
        val w = wanted.lowercase().replace(Regex("[^a-z0-9]"), "")
        val pick = launchers.map { it to it.loadLabel(pm).toString() }.maxByOrNull { (_, label) ->
            val l = label.lowercase().replace(Regex("[^a-z0-9]"), "")
            when { l == w -> 3; l.startsWith(w) || w.startsWith(l) -> 2; l.contains(w) -> 1; else -> 0 }
        }
        val label = pick?.second?.lowercase()?.replace(Regex("[^a-z0-9]"), "") ?: ""
        if (pick == null || !(label == w || label.startsWith(w) || w.startsWith(label) || label.contains(w)) || w.isEmpty())
            return "I couldn't find an app called $wanted."
        val intent = pm.getLaunchIntentForPackage(pick.first.activityInfo.packageName)
            ?: return "I couldn't open ${pick.second}."
        return start(act, intent, "I couldn't open ${pick.second}.")
    }

    private fun torch(ctx: Context, on: Boolean): String? {
        val cm = ctx.getSystemService(CameraManager::class.java)
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return "This phone has no flashlight I can reach."
        cm.setTorchMode(id, on)
        return null
    }

    private fun camera(act: Activity, video: Boolean, selfie: Boolean): String? {
        val i = Intent(if (video) MediaStore.INTENT_ACTION_VIDEO_CAMERA else MediaStore.INTENT_ACTION_STILL_IMAGE_CAMERA)
        if (selfie) {
            i.putExtra("android.intent.extras.CAMERA_FACING", 1)
                .putExtra("android.intent.extras.LENS_FACING_FRONT", 1)
                .putExtra("android.intent.extra.USE_FRONT_CAMERA", true)
        }
        return start(act, i, "I couldn't open the camera.")
    }

    private fun settings(act: Activity, what: String): String? {
        val action = when (what) {
            "wifi" -> if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_WIFI else Settings.ACTION_WIFI_SETTINGS
            "bluetooth" -> Settings.ACTION_BLUETOOTH_SETTINGS
            "airplane mode" -> Settings.ACTION_AIRPLANE_MODE_SETTINGS
            "mobile data" -> if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_INTERNET_CONNECTIVITY
                             else Settings.ACTION_DATA_ROAMING_SETTINGS
            "hotspot" -> Settings.ACTION_WIRELESS_SETTINGS
            "location" -> Settings.ACTION_LOCATION_SOURCE_SETTINGS
            "nfc" -> Settings.ACTION_NFC_SETTINGS
            "battery" -> Settings.ACTION_BATTERY_SAVER_SETTINGS
            "display" -> Settings.ACTION_DISPLAY_SETTINGS
            "sound" -> Settings.ACTION_SOUND_SETTINGS
            else -> Settings.ACTION_SETTINGS
        }
        return start(act, Intent(action), "I couldn't open that setting.")
    }

    private fun volume(ctx: Context, dir: String): String? {
        val am = ctx.getSystemService(AudioManager::class.java)
        when (dir) {
            "up" -> am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_RAISE, AudioManager.FLAG_SHOW_UI)
            "down" -> am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_LOWER, AudioManager.FLAG_SHOW_UI)
            "vibrate" -> am.ringerMode = AudioManager.RINGER_MODE_VIBRATE
            "silent" -> try { am.ringerMode = AudioManager.RINGER_MODE_SILENT }
                        catch (e: SecurityException) { am.ringerMode = AudioManager.RINGER_MODE_VIBRATE }
        }
        return null
    }

    private val MUSIC_APPS = mapOf("youtube music" to "com.google.android.apps.youtube.music",
                                   "spotify" to "com.spotify.music", "youtube" to "com.google.android.youtube")

    private fun play(act: Activity, query: String, app: String): String? {
        val pkg = MUSIC_APPS[app] ?: MUSIC_APPS["youtube music"]!!
        // Spotify and YouTube Music both start playing straight from a search ("play from search").
        val search = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH)
            .putExtra(android.app.SearchManager.QUERY, query).setPackage(pkg)
        if (search.resolveActivity(act.packageManager) != null) return start(act, search, "I couldn't start the music.")
        val web = when (app) {
            "spotify" -> "spotify:search:" + Uri.encode(query)
            "youtube" -> "https://www.youtube.com/results?search_query=" + Uri.encode(query)
            else -> "https://music.youtube.com/search?q=" + Uri.encode(query)
        }
        return start(act, Intent(Intent.ACTION_VIEW, Uri.parse(web)), "I couldn't open a music app.")
    }
}
