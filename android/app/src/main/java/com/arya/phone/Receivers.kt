package com.arya.phone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.widget.Toast
import kotlin.concurrent.thread

/** "Snooze 10 min" / "Done" on a reminder notification: tells the laptop (it re-arms the reminder). */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val id = intent.getLongExtra("id", 0L)
        val action = intent.getStringExtra("action") ?: return
        val prefs = Prefs(context)
        val pending = goAsync()
        Notifier.stopSound()
        Notifier.cancel(context, id)
        if (id < 0) {                                       // a reminder set on the phone while the laptop was away
            if (action == "snooze") {
                val p = context.getSharedPreferences("arya", Context.MODE_PRIVATE)
                val list = try { org.json.JSONArray(p.getString("reminders", "[]")) } catch (e: Exception) { org.json.JSONArray() }
                val text = (0 until list.length()).map { list.getJSONObject(it) }.firstOrNull { it.optLong("id") == id }
                    ?.optString("text") ?: "Reminder"
                OfflineServices.schedule(context, id, System.currentTimeMillis() + 10 * 60_000L, text)
            }
            Toast.makeText(context, if (action == "snooze") "Snoozed for 10 minutes." else "Done.", Toast.LENGTH_SHORT).show()
            pending.finish()
            return
        }
        thread {
            var reply = "Couldn't reach the laptop"
            for (base in prefs.urls) {
                try {
                    reply = Api.action(base, prefs.key, id, action).optString("reply", "Done.")
                    break
                } catch (e: Exception) { /* try the next address */ }
            }
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                Toast.makeText(context, reply, Toast.LENGTH_SHORT).show()
                pending.finish()
            }
        }
    }
}

/** After a reboot (or an app update), reconnect without opening the app. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        OfflineServices.rescheduleAll(context)              // phone reminders survive a reboot
        if (Prefs(context).paired) AryaService.start(context)
    }
}
