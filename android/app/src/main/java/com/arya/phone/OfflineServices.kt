package com.arya.phone

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** The network and Android side of the phone's offline skills (Skills.kt). */
object OfflineServices {
    private val http = OkHttpClient.Builder().connectTimeout(6, TimeUnit.SECONDS).readTimeout(25, TimeUnit.SECONDS).build()

    // ── What the laptop hands the phone for when it's away (/api/offline-config) ────────────────────────────

    /** Home city and its coordinates (for the weather), and — only because you chose the cloud for questions when
     *  the laptop is away — the Groq key and model. Fetched whenever the laptop is reachable. */
    fun saveConfig(ctx: Context, cfg: JSONObject) {
        ctx.getSharedPreferences("arya", Context.MODE_PRIVATE).edit().putString("offline_config", cfg.toString()).apply()
    }

    fun config(ctx: Context): JSONObject = try {
        JSONObject(ctx.getSharedPreferences("arya", Context.MODE_PRIVATE).getString("offline_config", "{}")!!)
    } catch (e: Exception) { JSONObject() }

    // ── Notes and to-dos said while the laptop was away: kept here, handed over when it's back ───────────────

    private fun inbox(ctx: Context): JSONArray = try {
        JSONArray(ctx.getSharedPreferences("arya", Context.MODE_PRIVATE).getString("inbox", "[]")!!)
    } catch (e: Exception) { JSONArray() }

    private fun saveInbox(ctx: Context, a: JSONArray) =
        ctx.getSharedPreferences("arya", Context.MODE_PRIVATE).edit().putString("inbox", a.toString()).apply()

    @Synchronized
    fun capture(ctx: Context, text: String) {
        val a = inbox(ctx)
        a.put(JSONObject().put("id", System.currentTimeMillis()).put("text", text).put("at", System.currentTimeMillis()))
        saveInbox(ctx, a)
    }

    fun pending(ctx: Context): List<String> {
        val a = inbox(ctx)
        return (0 until a.length()).map { a.getJSONObject(it).optString("text") }
    }

    /** Hand everything waiting to the laptop (/api/inbox). Called when the laptop answers again. */
    @Synchronized
    fun flush(ctx: Context, base: String, key: String): Int {
        val a = inbox(ctx)
        if (a.length() == 0) return 0
        val req = Request.Builder().url(base.trimEnd('/') + "/api/inbox")
            .header("Authorization", "Bearer $key").header("X-ARYA", "1")
            .post(JSONObject().put("items", a).toString().toRequestBody("application/json".toMediaType())).build()
        http.newCall(req).execute().use { r -> if (!r.isSuccessful) return 0 }
        val sent = (0 until a.length()).map { a.getJSONObject(it).optLong("id") }.toSet()
        val left = JSONArray()
        val now = inbox(ctx)                                    // anything added meanwhile stays
        for (i in 0 until now.length()) if (now.getJSONObject(i).optLong("id") !in sent) left.put(now.getJSONObject(i))
        saveInbox(ctx, left)
        return sent.size
    }

    /** When the laptop answers: take its latest config and hand over the notes. Never throws. */
    fun syncWithLaptop(ctx: Context, base: String, key: String) {
        try {
            val r = Request.Builder().url(base.trimEnd('/') + "/api/offline-config")
                .header("Authorization", "Bearer $key").header("X-ARYA", "1").get().build()
            http.newCall(r).execute().use { if (it.isSuccessful) saveConfig(ctx, JSONObject(it.body!!.string())) }
        } catch (e: Exception) { }
        try { flush(ctx, base, key) } catch (e: Exception) { }
    }

    // ── Reminders that ring on the phone ──────────────────────────────────────

    fun remind(ctx: Context, atMs: Long, text: String) {
        val id = -(System.currentTimeMillis() % 1_000_000_000L) - 1              // negative: a phone-only reminder
        val prefs = ctx.getSharedPreferences("arya", Context.MODE_PRIVATE)
        val list = try { JSONArray(prefs.getString("reminders", "[]")) } catch (e: Exception) { JSONArray() }
        list.put(JSONObject().put("id", id).put("at", atMs).put("text", text))
        prefs.edit().putString("reminders", list.toString()).apply()
        schedule(ctx, id, atMs, text)
    }

    fun schedule(ctx: Context, id: Long, atMs: Long, text: String) {
        val am = ctx.getSystemService(AlarmManager::class.java)
        val pi = PendingIntent.getBroadcast(ctx, (id % Int.MAX_VALUE).toInt(),
            Intent(ctx, ReminderReceiver::class.java).putExtra("id", id).putExtra("text", text),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        if (Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()) {
            am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, pi)
        } else {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, pi)         // may come a few minutes late
        }
    }

    /** After a reboot: phone reminders still to come are set again. */
    fun rescheduleAll(ctx: Context) {
        val prefs = ctx.getSharedPreferences("arya", Context.MODE_PRIVATE)
        val list = try { JSONArray(prefs.getString("reminders", "[]")) } catch (e: Exception) { return }
        val keep = JSONArray()
        for (i in 0 until list.length()) {
            val r = list.getJSONObject(i)
            if (r.optLong("at") > System.currentTimeMillis()) { keep.put(r); schedule(ctx, r.optLong("id"), r.optLong("at"), r.optString("text")) }
        }
        prefs.edit().putString("reminders", keep.toString()).apply()
    }

    // ── Weather (Open-Meteo) and the cloud (Groq) ─────────────────────────────

    private fun get(url: String): JSONObject = http.newCall(Request.Builder().url(url).get().build()).execute().use {
        if (!it.isSuccessful) throw java.io.IOException("HTTP ${it.code}")
        JSONObject(it.body!!.string())
    }

    fun weather(ctx: Context, q: WeatherSkill.Query): String = try {
        val cfg = config(ctx)
        var place = q.place ?: cfg.optString("city", "")
        var lat = cfg.optDouble("lat", Double.NaN)
        var lon = cfg.optDouble("lon", Double.NaN)
        if (q.place != null || lat.isNaN()) {
            val hit = get(WeatherSkill.geocodeUrl(place.ifEmpty { "Berlin" })).optJSONArray("results")?.optJSONObject(0)
                ?: throw IllegalArgumentException()
            lat = hit.getDouble("latitude"); lon = hit.getDouble("longitude"); place = hit.optString("name", place)
        }
        WeatherSkill.speak(get(WeatherSkill.forecastUrl(lat, lon)), place.ifEmpty { "your area" }, q)
    } catch (e: IllegalArgumentException) {
        "I couldn't find that place."
    } catch (e: Exception) {
        "I couldn't get the weather. Is the phone online?"
    }

    fun askCloud(ctx: Context, question: String): String {
        val cfg = config(ctx)
        val key = cfg.optString("groq_key", "")
        if (key.isEmpty()) return "The cloud isn't set up on the phone yet. Open ARYA once while the laptop is reachable."
        val req = Request.Builder().url(CloudQuestion.URL).header("Authorization", "Bearer $key")
            .post(CloudQuestion.body(question, cfg.optString("cloud_model", "openai/gpt-oss-20b"))
                .toRequestBody("application/json".toMediaType())).build()
        return try {
            http.newCall(req).execute().use { r ->
                val body = r.body?.string() ?: ""
                if (r.code == 429) return "The cloud's free limit is used up for now. Try again in a minute."
                CloudQuestion.answer(body) ?: "The cloud didn't give an answer."
            }
        } catch (e: Exception) { "I couldn't reach the cloud. Is the phone online?" }
    }
}

/** A phone reminder is due: the same notification as the laptop's reminders (Snooze / Done work on the phone). */
class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val id = intent.getLongExtra("id", -1L)
        val text = intent.getStringExtra("text") ?: "Reminder"
        Notifier.channels(ctx)
        Notifier.show(ctx, JSONObject().put("kind", "reminder").put("id", id).put("title", "Reminder").put("text", text))
    }
}
