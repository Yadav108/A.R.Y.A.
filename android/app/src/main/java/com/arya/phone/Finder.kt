package com.arya.phone

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.Socket
import java.security.SecureRandom
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Finding your laptop on whatever Wi-Fi you're on — safely.
 *
 *  An address on a Wi-Fi (not Tailscale) is only trusted after the laptop there proves it holds the pairing key:
 *  the app sends a random challenge to /api/prove, and only your laptop can answer it (HMAC-SHA256 with the key).
 *  The key itself is sent only after that, so an old home address that is someone else's device on another
 *  network — or a stranger's server found while looking — never sees it. Tailscale addresses are already
 *  authenticated by Tailscale (only your own devices).
 *
 *  When no address the app knows answers, it looks around the Wi-Fi it's on (its own /24: at most 254 addresses,
 *  a quick knock on port 8001 each, ≈ 2–3 s) and asks each answer for the proof. Found: the address is kept. */
object Finder {
    // ── The proof (pure: tested in FinderTest, same as arya/server/phone.py prove()) ─────────────────────────

    fun proof(key: String, nonce: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key.toByteArray(), "HmacSHA256"))
        return mac.doFinal("arya-prove:$nonce".toByteArray()).joinToString("") { "%02x".format(it) }
    }

    fun nonce(): String = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }

    fun tailscale(base: String) =
        base.substringAfter("://").substringBefore("/").substringBefore(":").startsWith("100.")

    /** Same length, every byte compared: no early exit to time. */
    fun same(a: String, b: String): Boolean {
        if (a.length != b.length) return false
        var d = 0
        for (i in a.indices) d = d or (a[i].code xor b[i].code)
        return d == 0
    }

    /** The other addresses in this phone's own /24 (no network, broadcast or own address), nearest first. */
    fun neighbours(own: String): List<String> {
        val p = own.split(".").mapNotNull { it.toIntOrNull() }
        if (p.size != 4 || p.any { it !in 0..255 }) return emptyList()
        val net = "${p[0]}.${p[1]}.${p[2]}."
        return (1..254).filter { it != p[3] }.sortedBy { kotlin.math.abs(it - p[3]) }.map { net + it }
    }

    // ── On the network ────────────────────────────────────────────────────────

    private val quick = OkHttpClient.Builder()
        .connectTimeout(1200, TimeUnit.MILLISECONDS).readTimeout(2, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false).build()

    /** Is this your laptop? (No key sent.) */
    fun isLaptop(base: String, key: String): Boolean = try {
        val n = nonce()
        quick.newCall(Request.Builder().url(base.trimEnd('/') + "/api/prove?n=$n").get().build()).execute().use { r ->
            r.isSuccessful && same(JSONObject(r.body?.string() ?: "{}").optString("proof"), proof(key, n))
        }
    } catch (e: Exception) { false }

    /** Safe to send the key to: Tailscale, or a Wi-Fi address that proved it's the laptop. */
    fun trusted(base: String, key: String) = tailscale(base) || isLaptop(base, key)

    /** The phone's IPv4 address on the Wi-Fi it's on, or null (not on Wi-Fi). */
    fun wifiAddress(ctx: Context): String? {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return null
        for (n in cm.allNetworks) {
            val caps = cm.getNetworkCapabilities(n) ?: continue
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
            val lp = cm.getLinkProperties(n) ?: continue
            lp.linkAddresses.map { it.address }.firstOrNull { it is Inet4Address && it.isSiteLocalAddress }
                ?.let { return it.hostAddress }
        }
        return null
    }

    @Volatile private var lastLook = 0L
    @Volatile private var lastLookFrom = ""

    /** Looks for the laptop on this Wi-Fi. At most once a minute for the same Wi-Fi address. */
    fun look(ctx: Context, key: String, port: Int): String? {
        val own = wifiAddress(ctx) ?: return null
        val now = System.currentTimeMillis()
        if (own == lastLookFrom && now - lastLook < 60_000L) return null
        lastLook = now; lastLookFrom = own
        val found = AtomicReference<String?>(null)
        val pool = Executors.newFixedThreadPool(48)
        try {
            for (ip in neighbours(own)) pool.execute {
                if (found.get() != null) return@execute
                val open = try {
                    Socket().use { it.connect(InetSocketAddress(ip, port), 400); true }
                } catch (e: Exception) { false }
                if (open) {
                    val base = "http://$ip:$port"
                    if (isLaptop(base, key)) found.compareAndSet(null, base)
                }
            }
            pool.shutdown()
            pool.awaitTermination(15, TimeUnit.SECONDS)
        } finally { pool.shutdownNow() }
        return found.get()
    }

    /** The first address that answers and can be trusted — or, failing that, the laptop found on this Wi-Fi
     *  (then kept for next time). Null: out of reach. Any thread but the main one. */
    fun laptop(ctx: Context, prefs: Prefs): String? {
        val key = prefs.key
        for (base in prefs.urls) {
            if (tailscale(base)) { if (Api.reachable(base, key)) return base }
            else if (isLaptop(base, key)) return base
        }
        val port = prefs.urls.firstNotNullOfOrNull { Uri.parse(it).port.takeIf { p -> p > 0 } } ?: 8001
        val found = look(ctx, key, port) ?: return null
        val host = Uri.parse(found).host ?: return null
        prefs.urls = Prefs.mergeUrls(prefs.urls, listOf(host), port)
        return found
    }
}
