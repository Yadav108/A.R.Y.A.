package com.arya.phone

import android.content.Context
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject

/** Continuity (arya/core/places.py): places are Wi-Fi names you labelled ("this is the lab"); reminders fire on
 *  the phone when you arrive at or leave one, even when the laptop is out of reach. Reading the Wi-Fi name needs
 *  Android's location permission; nothing here uses GPS. */
object Places {
    data class Reminder(val id: Long, val text: String, val place: String, val event: String)

    /** Same rule as places.due() on the laptop: which reminders fire when the phone moves from `prev` to `new`
     *  (null = no Wi-Fi or a network without a name). */
    fun due(prev: String?, new: String?, items: List<Reminder>): List<Reminder> {
        if (prev == new) return emptyList()
        return items.filter {
            (it.event == "leave" && prev != null && it.place == prev) ||
                (it.event == "arrive" && new != null && it.place == new)
        }
    }

    fun labelFor(ssid: String?, places: Map<String, String>): String? {
        val s = clean(ssid) ?: return null
        return places.entries.firstOrNull { it.value == s }?.key
    }

    /** Android's "<unknown ssid>" (no permission / location off) and quotes removed; null when unknown. */
    fun clean(ssid: String?): String? {
        val s = ssid?.trim()?.removePrefix("\"")?.removeSuffix("\"") ?: return null
        return if (s.isEmpty() || s == WifiManager.UNKNOWN_SSID.trim('<', '>') || s == "<unknown ssid>") null else s
    }

    // ── What the laptop sent (kept on the phone) ───────────────────────────────

    fun store(ctx: Context, json: JSONObject) {
        prefs(ctx).edit().putString("places", (json.optJSONObject("places") ?: JSONObject()).toString())
            .putString("place_reminders", (json.optJSONArray("reminders") ?: JSONArray()).toString())
            .putInt("places_v", json.optInt("v", 0)).apply()
    }

    fun version(ctx: Context) = prefs(ctx).getInt("places_v", -1)

    fun places(ctx: Context): Map<String, String> {
        val o = JSONObject(prefs(ctx).getString("places", "{}")!!)
        return o.keys().asSequence().associateWith { o.getString(it) }
    }

    fun reminders(ctx: Context): List<Reminder> {
        val a = JSONArray(prefs(ctx).getString("place_reminders", "[]")!!)
        return (0 until a.length()).map { a.getJSONObject(it) }.map {
            Reminder(it.optLong("id"), it.optString("text"), it.optString("place"), it.optString("event"))
        }
    }

    private fun removeReminders(ctx: Context, ids: Set<Long>) {
        val a = JSONArray(prefs(ctx).getString("place_reminders", "[]")!!)
        val kept = JSONArray()
        for (i in 0 until a.length()) if (a.getJSONObject(i).optLong("id") !in ids) kept.put(a.getJSONObject(i))
        prefs(ctx).edit().putString("place_reminders", kept.toString()).apply()
    }

    /** Fired reminders not yet reported to the laptop (sent with the next presence report). */
    fun unreported(ctx: Context): List<Long> =
        prefs(ctx).getString("place_fired", "")!!.split(",").mapNotNull { it.toLongOrNull() }

    fun clearUnreported(ctx: Context, ids: List<Long>) {
        val left = unreported(ctx) - ids.toSet()
        prefs(ctx).edit().putString("place_fired", left.joinToString(",")).apply()
    }

    var current: String? = null
        private set

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("arya_places", Context.MODE_PRIVATE)

    fun canReadWifiName(ctx: Context) =
        ctx.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    /** The Wi-Fi the phone is on now, or null (needs location permission and location switched on). */
    @Suppress("DEPRECATION")
    fun currentSsid(ctx: Context): String? {
        if (!canReadWifiName(ctx)) return null
        val wifi = ctx.applicationContext.getSystemService(WifiManager::class.java) ?: return null
        return clean(wifi.connectionInfo?.ssid)
    }

    // ── Watching the Wi-Fi (from the background service) ───────────────────────

    private const val SETTLE_MS = 90_000L        // a Wi-Fi blip at home is not "leaving home"
    private var callback: ConnectivityManager.NetworkCallback? = null
    private val ui by lazy { Handler(Looper.getMainLooper()) }     // lazy: the pure parts run in unit tests
    private var pending: Runnable? = null
    private var report: ((String?) -> Unit)? = null

    /** `onPlace(label)` is called (on the main thread) after each settled change, for the laptop. */
    fun watch(ctx: Context, onPlace: (String?) -> Unit) {
        if (callback != null) return
        val app = ctx.applicationContext
        report = onPlace
        current = labelFor(currentSsid(app), places(app))
        val cm = app.getSystemService(ConnectivityManager::class.java) ?: return
        val request = NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build()
        val cb = if (Build.VERSION.SDK_INT >= 31) {
            object : ConnectivityManager.NetworkCallback(FLAG_INCLUDE_LOCATION_INFO) {
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                    val ssid = clean((caps.transportInfo as? WifiInfo)?.ssid) ?: return   // name hidden: ignore
                    settle(app, labelFor(ssid, places(app)))
                }
                override fun onLost(network: Network) = settle(app, null)
            }
        } else {
            object : ConnectivityManager.NetworkCallback() {
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                    val ssid = currentSsid(app) ?: return
                    settle(app, labelFor(ssid, places(app)))
                }
                override fun onLost(network: Network) = settle(app, null)
            }
        }
        try {
            cm.registerNetworkCallback(request, cb)
            callback = cb
        } catch (e: Exception) { /* too many callbacks / no permission: no places this run */ }
    }

    fun stop(ctx: Context) {
        val cb = callback ?: return
        try { ctx.applicationContext.getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(cb) }
        catch (e: Exception) { }
        callback = null
    }

    private fun settle(ctx: Context, label: String?) {
        ui.post {
            pending?.let { ui.removeCallbacks(it) }
            if (label == current) { pending = null; return@post }
            val r = Runnable { arrived(ctx, label) }
            pending = r
            ui.postDelayed(r, SETTLE_MS)
        }
    }

    private fun arrived(ctx: Context, label: String?) {
        val prev = current
        current = label
        val fired = due(prev, label, reminders(ctx))
        for (r in fired) {
            val where = if (r.place == "home") (if (r.event == "leave") "Leaving home" else "Home")
                        else (if (r.event == "leave") "Leaving the ${r.place}" else "At the ${r.place}")
            Notifier.show(ctx, JSONObject().put("id", 9_000_000_000_000L + r.id).put("kind", "place")
                .put("title", "ARYA · $where").put("text", r.text.replaceFirstChar { it.uppercase() }))
        }
        if (fired.isNotEmpty()) {
            removeReminders(ctx, fired.map { it.id }.toSet())
            val all = (unreported(ctx) + fired.map { it.id }).distinct()
            prefs(ctx).edit().putString("place_fired", all.joinToString(",")).apply()
        }
        report?.invoke(label)
    }
}
