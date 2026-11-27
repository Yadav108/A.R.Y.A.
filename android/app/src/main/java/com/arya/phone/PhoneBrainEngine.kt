package com.arya.phone

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import com.google.mediapipe.tasks.genai.llminference.LlmInference
import com.google.mediapipe.tasks.genai.llminference.LlmInferenceSession
import com.google.mediapipe.tasks.genai.llminference.PromptTemplates
import java.io.File
import java.security.MessageDigest
import java.time.ZonedDateTime
import kotlin.concurrent.thread

/** Runs PhoneBrain's model with Google's MediaPipe LLM engine (on the phone's CPU). The model file is downloaded
 *  by the app itself (from the laptop over Wi-Fi, or the internet; Wi-Fi only; resumes; progress notification),
 *  checked against its published SHA-256, and only then used. Loaded when the talk screen finds the laptop out of reach and
 *  let go when it closes, so the 1.6 GB is not held in memory the rest of the time. */
object PhoneBrainEngine {
    enum class Status { MISSING, DOWNLOADING, CHECKING, READY, FAILED }

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("arya_brain", Context.MODE_PRIVATE)

    fun file(ctx: Context): File =
        File(ctx.applicationContext.getExternalFilesDir(null) ?: ctx.applicationContext.filesDir, "brain/${PhoneBrain.FILE}")

    private fun part(ctx: Context) = File(file(ctx).path + ".part")

    @Volatile private var checking = false
    @Volatile private var downloading = false
    @Volatile private var stopDownload = false
    @Volatile var lastError: String? = null; private set
    @Volatile private var source = "internet"               // "laptop" / "internet": for the log line
    @Volatile private var waitingFor = ""                   // why it isn't moving right now ("" = moving)

    fun status(ctx: Context): Status {
        val f = file(ctx)
        val p = prefs(ctx)
        if (p.getString("verified", "") == PhoneBrain.SHA256 && f.length() == PhoneBrain.SIZE) return Status.READY
        if (checking) return Status.CHECKING
        if (downloading) return Status.DOWNLOADING
        if (p.getBoolean("failed", false)) return Status.FAILED
        if (f.length() == PhoneBrain.SIZE) { verify(ctx); return Status.CHECKING }
        return Status.MISSING
    }

    /** How far the download is, 0-100, or null when not downloading. */
    fun progress(ctx: Context): Int? =
        if (!downloading) null else ((part(ctx).length() * 100) / PhoneBrain.SIZE).toInt().coerceIn(0, 100)

    /** One line for the laptop's log (sent with /api/offline-config): "READY", "DOWNLOADING 42% from the laptop" ... */
    fun report(ctx: Context): String {
        val s = status(ctx)
        val extra = if (s == Status.DOWNLOADING)
            " ${progress(ctx) ?: 0}% from the $source" + (if (waitingFor.isNotEmpty()) " ($waitingFor)" else "") else ""
        return s.name + extra + (lastError?.let { " - $it" } ?: "")
    }

    private fun onWifi(ctx: Context): Boolean {
        val cm = ctx.getSystemService(android.net.ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI)
    }

    private val http = okhttp3.OkHttpClient.Builder()
        .connectTimeout(10, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    /** Starts the download in the app itself (the phone's own download manager can sit at "retrying" for ever on
     *  some phones). From the laptop over Wi-Fi when it's reachable (fast, and it proves itself first: Finder.kt),
     *  otherwise from the internet; Wi-Fi only; resumes after any drop; progress in a notification.
     *  Returns what to say when it can't start, or null when it started. Not on the main thread. */
    fun download(ctx: Context): String? {
        val app = ctx.applicationContext
        if (!onWifi(app)) return "Connect the phone to Wi-Fi first: the offline brain is ${PhoneBrain.SIZE_TEXT}."
        val f = file(app)
        f.parentFile?.mkdirs()
        val free = f.parentFile?.usableSpace ?: 0L
        if (free + part(app).length() < PhoneBrain.SIZE + 300_000_000L)
            return "There isn't room for it: the offline brain needs about two gigabytes free on the phone."
        cancelOldDownloadManager(app)
        if (downloading) {                                     // restart a stuck one
            stopDownload = true
            val until = System.currentTimeMillis() + 5_000L
            while (downloading && System.currentTimeMillis() < until) Thread.sleep(50)
        }
        stopDownload = false
        downloading = true
        lastError = null
        waitingFor = "starting"
        prefs(app).edit().remove("failed").remove("verified").apply()
        thread(name = "arya-brain-download") { runDownload(app) }
        return null
    }

    private fun runDownload(app: Context) {
        val prefs = Prefs(app)
        var tries = 0
        try {
            while (!stopDownload && part(app).length() < PhoneBrain.SIZE) {
                if (!onWifi(app)) { waitingFor = "waiting for Wi-Fi"; Thread.sleep(10_000); continue }
                val laptop = try { Finder.laptop(app, prefs) } catch (e: Exception) { null }
                val fromLaptop = laptop != null && laptopHasIt(laptop, prefs.key)
                val url = if (fromLaptop) laptop!!.trimEnd('/') + "/api/brain-file" else PhoneBrain.URL
                source = if (fromLaptop) "laptop" else "internet"
                try {
                    fetch(app, url, if (fromLaptop) prefs.key else null)
                    tries = 0
                } catch (e: Exception) {
                    tries++
                    waitingFor = "retrying: ${e.javaClass.simpleName}"
                    if (tries >= 30) { lastError = "download kept failing: ${e.message?.take(60)}"; break }
                    Thread.sleep(minOf(5_000L * tries, 60_000L))
                }
            }
        } catch (e: InterruptedException) { }
        val done = part(app).length() == PhoneBrain.SIZE
        if (done) { file(app).delete(); part(app).renameTo(file(app)) }
        downloading = false
        waitingFor = ""
        progressNotification(app, null)
        if (done) verify(app) { ok -> readyNotification(app, ok) }
        else if (!stopDownload) prefs(app).edit().putBoolean("failed", true).apply()
    }

    /** Laptop ready to hand the file over? (It fetches the file itself the first time it's asked: 202 meanwhile.) */
    private fun laptopHasIt(base: String, key: String): Boolean = try {
        http.newCall(okhttp3.Request.Builder().url(base.trimEnd('/') + "/api/brain-file").head()
            .header("Authorization", "Bearer $key").header("X-ARYA", "1").build()).execute().use { r ->
            if (r.code == 202) waitingFor = "the laptop is fetching it first"
            r.code == 200
        }
    } catch (e: Exception) { false }

    private fun fetch(app: Context, url: String, key: String?) {
        val have = part(app).length()
        val req = okhttp3.Request.Builder().url(url).header("Range", "bytes=$have-")
        if (key != null) req.header("Authorization", "Bearer $key").header("X-ARYA", "1")
        http.newCall(req.build()).execute().use { r ->
            if (r.code != 206 && r.code != 200) throw java.io.IOException("HTTP ${r.code}")
            val append = r.code == 206
            if (!append) part(app).delete()
            waitingFor = ""
            val input = (r.body ?: throw java.io.IOException("no body")).byteStream()
            java.io.FileOutputStream(part(app), append).use { out ->
                val buf = ByteArray(1 shl 16)
                var lastPct = -1
                while (!stopDownload) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    val pct = ((part(app).length() * 100) / PhoneBrain.SIZE).toInt()
                    if (pct != lastPct) { lastPct = pct; progressNotification(app, pct) }
                }
            }
        }
    }

    private const val NOTE_ID = 7301
    private const val CHANNEL = "brain_download"

    private fun progressNotification(ctx: Context, pct: Int?) {
        val nm = ctx.getSystemService(android.app.NotificationManager::class.java) ?: return
        if (pct == null) { nm.cancel(NOTE_ID); return }
        if (android.os.Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null)
            nm.createNotificationChannel(android.app.NotificationChannel(CHANNEL, "Offline brain download",
                android.app.NotificationManager.IMPORTANCE_LOW))
        val n = androidx.core.app.NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("ARYA offline brain")
            .setContentText("$pct% of ${PhoneBrain.SIZE_TEXT} - from the $source")
            .setProgress(100, pct, false).setOngoing(true).setOnlyAlertOnce(true).build()
        try { nm.notify(NOTE_ID, n) } catch (e: SecurityException) { }    // notifications not allowed: no bar
    }

    private fun readyNotification(ctx: Context, ok: Boolean) {
        Notifier.channels(ctx)
        Notifier.show(ctx, org.json.JSONObject().put("id", 9_100_000_000_000L).put("kind", "message")
            .put("title", if (ok) "ARYA's offline brain is ready" else "ARYA's offline brain didn't download right")
            .put("text", if (ok) "When the laptop is out of reach, ARYA now answers on the phone itself."
                         else "The file was damaged and has been removed. Say \"download the offline brain\" again."))
    }

    /** A download the earlier app version gave Android's download manager: stop it, it's replaced. */
    private fun cancelOldDownloadManager(ctx: Context) {
        val p = prefs(ctx)
        val id = p.getLong("download", -1L)
        if (id >= 0) {
            try { ctx.getSystemService(DownloadManager::class.java).remove(id) } catch (e: Exception) { }
            p.edit().remove("download").apply()
        }
    }

    /** Checks the downloaded file against its published SHA-256 (in the background; about 10-20 s). */
    fun verify(ctx: Context, done: ((Boolean) -> Unit)? = null) {
        if (checking) return
        checking = true
        val app = ctx.applicationContext
        thread(name = "arya-brain-check") {
            val ok = try {
                val md = MessageDigest.getInstance("SHA-256")
                file(app).inputStream().buffered(1 shl 20).use { s ->
                    val buf = ByteArray(1 shl 20)
                    while (true) { val n = s.read(buf); if (n < 0) break; md.update(buf, 0, n) }
                }
                md.digest().joinToString("") { "%02x".format(it) } == PhoneBrain.SHA256
            } catch (e: Exception) { false }
            val p = prefs(app).edit()
            if (ok) p.putString("verified", PhoneBrain.SHA256) else { file(app).delete(); p.remove("verified") }
            p.apply()
            checking = false
            done?.invoke(ok)
        }
    }

    /** "download / delete the offline brain", "is the offline brain ready?" - what to say back. Any thread. */
    fun manage(ctx: Context, m: PhoneBrain.Manage): String {
        val s = status(ctx)
        return when (m) {
            PhoneBrain.Manage.DOWNLOAD -> when (s) {
                Status.READY -> "The offline brain is already on the phone."
                Status.CHECKING -> "It's downloaded; I'm checking the file."
                else -> {
                    val restart = s == Status.DOWNLOADING
                    val problem = offMain { download(ctx) }
                    problem ?: ((if (restart) "Restarted the download" else "Downloading the offline brain") +
                        ": ${PhoneBrain.SIZE_TEXT}, from the laptop over Wi-Fi when it can, otherwise from the " +
                        "internet. The progress is in your notifications; it keeps going in the background.")
                }
            }
            PhoneBrain.Manage.DELETE -> if (s == Status.MISSING) "The offline brain isn't on the phone."
                else { delete(ctx); "Deleted the offline brain. That's ${PhoneBrain.SIZE_TEXT} free again." }
            PhoneBrain.Manage.STATUS -> when (s) {
                Status.READY -> "Yes, the offline brain is ready. When the laptop is out of reach, I answer on the phone."
                Status.DOWNLOADING -> "Still downloading: ${progress(ctx) ?: 0} percent, from the $source" +
                    (if (waitingFor.isNotEmpty()) " ($waitingFor)." else ".")
                Status.CHECKING -> "It's downloaded; I'm checking the file. Give it a few seconds."
                Status.FAILED -> "The download failed. Say \"download the offline brain\" to try again."
                Status.MISSING -> "It isn't on the phone yet. On Wi-Fi, say \"download the offline brain\"."
            }
        }
    }

    /** download() may wait for a stuck download to stop: never on the main thread. */
    private fun offMain(block: () -> String?): String? {
        if (android.os.Looper.myLooper() != android.os.Looper.getMainLooper()) return block()
        var out: String? = null
        val t = thread { out = block() }
        t.join(6_000)
        return out
    }

    fun delete(ctx: Context) {
        close()
        stopDownload = true
        cancelOldDownloadManager(ctx)
        file(ctx).delete()
        part(ctx).delete()
        prefs(ctx).edit().clear().apply()
    }

    /** Android's download manager finished one (only from the earlier app version): use it if it's whole. */
    fun downloaded(ctx: Context, id: Long) {
        if (id < 0 || id != prefs(ctx).getLong("download", -1L)) return
        prefs(ctx).edit().remove("download").apply()
        if (file(ctx).length() == PhoneBrain.SIZE) verify(ctx) { ok -> readyNotification(ctx, ok) }
    }

    // ── Running it ────────────────────────────────────────────────────────────

    private var llm: LlmInference? = null
    private val lock = Any()

    /** Loads the model (a few seconds) if it is downloaded; safe to call from any thread, any number of times. */
    fun warm(ctx: Context): Boolean = synchronized(lock) {
        if (llm != null) return true
        if (status(ctx) != Status.READY) return false
        val p = prefs(ctx)
        if (p.getBoolean("loading", false))                     // the app died while loading it last time
            lastError = "the app closed while loading the model last time (out of memory?)"
        p.edit().putBoolean("loading", true).commit()
        return try {
            llm = LlmInference.createFromOptions(ctx.applicationContext, LlmInference.LlmInferenceOptions.builder()
                .setModelPath(file(ctx).absolutePath)
                .setMaxTokens(PhoneBrain.MAX_TOKENS)
                .setMaxTopK(40)
                .setPreferredBackend(LlmInference.Backend.CPU)
                .build())
            lastError = null
            true
        } catch (e: Throwable) {
            llm = null
            lastError = "couldn't load the model: ${e.javaClass.simpleName}: ${e.message?.take(80)}"
            false
        } finally { p.edit().putBoolean("loading", false).apply() }
    }

    fun close() = synchronized(lock) {
        try { llm?.close() } catch (e: Throwable) { }
        llm = null
    }

    @Volatile private var session: LlmInferenceSession? = null

    /** Answers on a background thread. `onSentence` gets each finished sentence as it is written (to speak it
     *  straight away); `onDone` gets the whole answer, or null if the model couldn't run. */
    fun answer(ctx: Context, question: String, history: List<PhoneBrain.Turn>, city: String?,
               onSentence: (String) -> Unit, onDone: (String?) -> Unit) {
        thread(name = "arya-brain") {
            if (!warm(ctx)) { onDone(null); return@thread }
            val raw = StringBuilder()
            var spoken = 0                                   // characters of the cleaned text already handed out
            try {
                synchronized(lock) {
                    val engine = llm ?: throw IllegalStateException()
                    val s = LlmInferenceSession.createFromOptions(engine,
                        LlmInferenceSession.LlmInferenceSessionOptions.builder()
                            .setTopK(40).setTopP(0.9f).setTemperature(0.5f)
                            .setPromptTemplates(PromptTemplates.builder()            // the prompt is complete as is
                                .setUserPrefix("").setUserSuffix("").setModelPrefix("").setModelSuffix("")
                                .setSystemPrefix("").setSystemSuffix("").build())
                            .build())
                    session = s
                    try {
                        s.addQueryChunk(PhoneBrain.prompt(question, history, ZonedDateTime.now(), city))
                        s.generateResponseAsync { partial, _ ->
                            raw.append(partial)
                            val text = PhoneBrain.clean(raw.toString())
                            if (text.length > spoken) {
                                val (whole, rest) = PhoneBrain.sentences(text.substring(spoken))
                                for (w in whole) onSentence(w)
                                spoken = text.length - rest.length
                            }
                        }.get()
                    } finally { session = null; s.close() }
                }
                val text = PhoneBrain.clean(raw.toString())
                if (text.length > spoken) text.substring(spoken).trim().takeIf { it.isNotEmpty() }?.let(onSentence)
                onDone(text.ifEmpty { null })
            } catch (e: Throwable) {
                onDone(PhoneBrain.clean(raw.toString()).ifEmpty { null })
            }
        }
    }

    /** Stops an answer being written (the user tapped to interrupt, or closed the screen). */
    fun stop() {
        try { session?.cancelGenerateResponseAsync() } catch (e: Throwable) { }
    }
}

/** The offline brain's download finished (Android tells the app that started it). */
class BrainDownloadReceiver : android.content.BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: android.content.Intent) {
        if (intent.action != DownloadManager.ACTION_DOWNLOAD_COMPLETE) return
        PhoneBrainEngine.downloaded(ctx, intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L))
    }
}
