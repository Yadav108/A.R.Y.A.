package com.arya.phone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject

/** ARYA's events as Android notifications. Channels (each can be tuned in Android's settings):
 *  reminders (reminders, timers, alarms — loud, with Snooze / Done), calendar (heads-ups, meeting mode),
 *  daily (briefing, wrap-up), messages (sent from the laptop, find my phone), connection (the quiet, permanent
 *  "connected" line Android requires for a background service). */
object Notifier {
    const val CH_REMINDERS = "reminders_v2"
    const val CH_CALENDAR = "calendar"
    const val CH_DAILY = "daily"
    const val CH_MESSAGES = "messages"
    const val CH_CONNECTION = "connection"
    const val CONNECTION_ID = 1

    private val SNOOZABLE = setOf("reminder", "timer", "alarm")

    fun channels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        // v1 had the alarm sound on the channel, and some phones (realme UI) make new channels silent anyway.
        // The sound is now played by the app (playAlarm), so the channel only shows and vibrates.
        nm.deleteNotificationChannel("reminders")
        val reminders = NotificationChannel(CH_REMINDERS, "Reminders, timers, alarms",
            NotificationManager.IMPORTANCE_HIGH).apply {
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 400, 200, 400, 200, 400)
            setSound(null, null)
        }
        nm.createNotificationChannels(listOf(
            reminders,
            NotificationChannel(CH_CALENDAR, "Calendar and meetings", NotificationManager.IMPORTANCE_HIGH),
            NotificationChannel(CH_DAILY, "Briefing and wrap-up", NotificationManager.IMPORTANCE_DEFAULT),
            NotificationChannel(CH_MESSAGES, "Messages from the laptop", NotificationManager.IMPORTANCE_HIGH),
            NotificationChannel(CH_CONNECTION, "Connection to ARYA", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
            },
        ))
    }

    private fun channelFor(kind: String) = when (kind) {
        "reminder", "timer", "alarm", "place" -> CH_REMINDERS
        "calendar", "meeting" -> CH_CALENDAR
        "briefing", "wrapup" -> CH_DAILY
        else -> CH_MESSAGES
    }

    private fun openApp(ctx: Context): PendingIntent = PendingIntent.getActivity(
        ctx, 0, Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    private fun actionIntent(ctx: Context, id: Long, action: String): PendingIntent = PendingIntent.getBroadcast(
        ctx, (id % Int.MAX_VALUE).toInt() * 2 + if (action == "snooze") 1 else 0,
        Intent(ctx, ActionReceiver::class.java).putExtra("id", id).putExtra("action", action),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    fun show(ctx: Context, item: JSONObject) {
        val kind = item.optString("kind")
        val id = item.optLong("id")
        val text = item.optString("text")
        val title = item.optString("title").ifEmpty {
            when (kind) { "ring" -> "ARYA is looking for you"; "message" -> "From your laptop"; else -> "ARYA" }
        }
        val b = NotificationCompat.Builder(ctx, channelFor(kind))
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(openApp(ctx))
            .setAutoCancel(true)
            .setCategory(if (kind in SNOOZABLE) NotificationCompat.CATEGORY_ALARM else NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
        if (kind in SNOOZABLE) {
            b.addAction(0, "Snooze 10 min", actionIntent(ctx, id, "snooze"))
            b.addAction(0, "Done", actionIntent(ctx, id, "done"))
        }
        if (Build.VERSION.SDK_INT < 33 || ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED) {
            NotificationManagerCompat.from(ctx).notify((id % Int.MAX_VALUE).toInt(), b.build())
        }
        when (kind) {
            "ring" -> playAlarm(ctx, 10_000, respectSilent = false)       // find my phone: always audible
            in SNOOZABLE -> playAlarm(ctx, 8_000, respectSilent = true)
        }
    }

    @Volatile private var playing: android.media.Ringtone? = null

    /** The phone's alarm tone for a few seconds, at alarm volume. A reminder respects the phone's mode: silent ->
     *  no sound, vibrate -> the notification's vibration only. Snooze / Done stop it at once. */
    private fun playAlarm(ctx: Context, millis: Long, respectSilent: Boolean) {
        if (respectSilent) {
            val audio = ctx.getSystemService(android.media.AudioManager::class.java)
            if (audio.ringerMode != android.media.AudioManager.RINGER_MODE_NORMAL) return
        }
        try {
            stopSound()
            val r = RingtoneManager.getRingtone(ctx, RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)) ?: return
            r.audioAttributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
            r.play()
            playing = r
            android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({ if (playing === r) stopSound() }, millis)
        } catch (e: Exception) { /* no ringtone available */ }
    }

    fun stopSound() {
        try { playing?.stop() } catch (e: Exception) { }
        playing = null
    }

    fun connection(ctx: Context, text: String) = NotificationCompat.Builder(ctx, CH_CONNECTION)
        .setSmallIcon(R.drawable.ic_notify)
        .setContentTitle("ARYA")
        .setContentText(text)
        .setOngoing(true)
        .setSilent(true)
        .setContentIntent(openApp(ctx))
        .build()

    fun cancel(ctx: Context, id: Long) = NotificationManagerCompat.from(ctx).cancel((id % Int.MAX_VALUE).toInt())
}
