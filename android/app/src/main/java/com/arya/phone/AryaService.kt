package com.arya.phone

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import kotlin.concurrent.thread

/** Keeps the connection to ARYA: one long-poll (/api/wait) after another. A new reminder on the laptop answers the
 *  waiting request at once; the reply also holds everything missed while the phone was offline. On errors it
 *  tries the next address (Tailscale, then home Wi-Fi) and backs off 2 s -> 60 s; it never gives up by itself.
 *  Runs as a foreground service (Android's rule for staying connected), restarted after a reboot. */
class AryaService : Service() {
    @Volatile private var running = false
    private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Notifier.channels(this)
        // Started again (app opened, notification tapped): show the real state, not "Connecting…" again.
        val note = Notifier.connection(this, currentStatus)
        ServiceCompat.startForeground(this, Notifier.CONNECTION_ID, note,
            if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING else 0)
        if (!running) {
            running = true
            worker = thread(name = "arya-poll", isDaemon = true) { loop() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        super.onDestroy()
    }

    private fun status(text: String) {
        currentStatus = text
        try {
            NotificationManagerCompat.from(this).notify(Notifier.CONNECTION_ID, Notifier.connection(this, text))
        } catch (e: SecurityException) { /* notifications not allowed */ }
    }

    private fun loop() {
        val prefs = Prefs(this)
        var backoff = 2_000L
        var index = 0
        var connected = false
        while (running) {
            val urls = prefs.urls
            if (!prefs.paired || urls.isEmpty()) { stopSelf(); return }
            val base = urls[index % urls.size]
            try {
                if (!connected) {
                    // A quick hello first: the long wait below can take 50 s to answer when nothing happens, and
                    // the status line would say "Connecting…" all that time although the laptop is reachable.
                    Api.hello(base, prefs.key)
                    status("Connected to your laptop" + if (base.contains("://100.")) " (Tailscale)" else " (home Wi-Fi)")
                    connected = true
                    // Back in reach: notes said meanwhile go to the laptop; its home city / cloud settings come back.
                    OfflineServices.syncWithLaptop(this, base, prefs.key)
                }
                val r = Api.waitFor(base, prefs.key, prefs.lastId)
                backoff = 2_000L
                val items = r.optJSONArray("items")
                val kinds = NOTIFY
                var last = prefs.lastId
                if (items != null) for (i in 0 until items.length()) {
                    val item = items.getJSONObject(i)
                    val id = item.optLong("id")
                    if (id <= last) continue
                    last = id
                    if (item.optString("kind") in kinds && !item.optBoolean("done")) Notifier.show(this, item)
                }
                prefs.lastId = maxOf(last, prefs.lastId)
            } catch (e: Api.Unpaired) {
                status("Not paired any more: open ARYA and scan the new QR code")
                prefs.key = ""
                stopSelf()
                return
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                Log.w("ARYA", "poll failed on $base: ${e.message}")
                if (connected) status("Reconnecting… (is the laptop awake and Tailscale on?)")
                connected = false
                index++                                         // next address next time
                try { Thread.sleep(backoff) } catch (ie: InterruptedException) { return }
                backoff = minOf(backoff * 2, 60_000L)
            }
        }
    }

    companion object {
        @Volatile var currentStatus = "Connecting…"
        val NOTIFY = setOf("reminder", "timer", "alarm", "calendar", "meeting", "briefing", "wrapup", "ring", "message")

        fun start(ctx: Context) {
            val i = Intent(ctx, AryaService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, AryaService::class.java))
    }
}
