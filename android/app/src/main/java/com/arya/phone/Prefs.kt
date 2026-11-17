package com.arya.phone

import android.content.Context
import android.net.Uri

/** What the app remembers: where ARYA is (Tailscale address first, home Wi-Fi as a fallback), the pairing key,
 *  and the last event it has shown (so a reconnect catches up without repeating anything). */
class Prefs(context: Context) {
    private val p = context.getSharedPreferences("arya", Context.MODE_PRIVATE)

    var urls: List<String>
        get() = p.getString("urls", "")!!.split("\n").filter { it.isNotBlank() }
        set(v) = p.edit().putString("urls", v.distinct().joinToString("\n")).apply()

    var key: String
        get() = p.getString("key", "")!!
        set(v) = p.edit().putString("key", v).apply()

    var lastId: Long
        get() = p.getLong("last_id", 0L)
        set(v) = p.edit().putLong("last_id", v).apply()

    var pairingUrl: String
        get() = p.getString("pairing_url", "")!!
        set(v) = p.edit().putString("pairing_url", v).apply()

    val paired: Boolean get() = key.isNotEmpty() && urls.isNotEmpty()

    fun forget() = p.edit().clear().apply()

    companion object {
        /** "http://100.88.1.7:8001/#k=KEY" -> ("http://100.88.1.7:8001", "KEY"), else null. */
        fun parsePairing(text: String): Pair<String, String>? {
            val uri = try { Uri.parse(text.trim()) } catch (e: Exception) { return null }
            if (uri.scheme != "http" && uri.scheme != "https") return null
            val host = uri.host ?: return null
            val key = (uri.fragment ?: "").removePrefix("k=")
            if (key.length < 32) return null
            val port = if (uri.port > 0) ":${uri.port}" else ""
            return Pair("${uri.scheme}://$host$port", key)
        }
    }
}
