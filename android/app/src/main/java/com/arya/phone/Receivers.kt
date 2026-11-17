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
        Notifier.cancel(context, id)
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
        if (Prefs(context).paired) AryaService.start(context)
    }
}
