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

/** Runs PhoneBrain's model with Google's MediaPipe LLM engine (on the phone's CPU). The model file is fetched by
 *  Android's own download manager (Wi-Fi only, resumes by itself, shows its progress in the notifications), checked
 *  against its published SHA-256, and only then used. Loaded when the talk screen finds the laptop out of reach and
 *  let go when it closes, so the 1.6 GB is not held in memory the rest of the time. */
object PhoneBrainEngine {
    enum class Status { MISSING, DOWNLOADING, CHECKING, READY, FAILED }

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("arya_brain", Context.MODE_PRIVATE)

    fun file(ctx: Context): File =
        File(ctx.applicationContext.getExternalFilesDir(null) ?: ctx.applicationContext.filesDir, "brain/${PhoneBrain.FILE}")

    @Volatile private var checking = false

    fun status(ctx: Context): Status {
        val f = file(ctx)
        val p = prefs(ctx)
        if (p.getString("verified", "") == PhoneBrain.SHA256 && f.length() == PhoneBrain.SIZE) return Status.READY
        if (checking) return Status.CHECKING
        val id = p.getLong("download", -1L)
        if (id >= 0) {
            when (downloadState(ctx, id)) {
                DownloadManager.STATUS_PENDING, DownloadManager.STATUS_RUNNING, DownloadManager.STATUS_PAUSED ->
                    return Status.DOWNLOADING
                DownloadManager.STATUS_FAILED -> { p.edit().remove("download").apply(); return Status.FAILED }
            }
        }
        if (f.length() == PhoneBrain.SIZE) { verify(ctx); return Status.CHECKING }
        return Status.MISSING
    }

    /** How far the download is, 0–100, or null when not downloading. */
    fun progress(ctx: Context): Int? {
        val id = prefs(ctx).getLong("download", -1L).takeIf { it >= 0 } ?: return null
        val dm = ctx.getSystemService(DownloadManager::class.java) ?: return null
        dm.query(DownloadManager.Query().setFilterById(id))?.use { c ->
            if (!c.moveToFirst()) return null
            val done = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR))
            return ((done * 100) / PhoneBrain.SIZE).toInt().coerceIn(0, 100)
        }
        return null
    }

    private fun downloadState(ctx: Context, id: Long): Int? {
        val dm = ctx.getSystemService(DownloadManager::class.java) ?: return null
        dm.query(DownloadManager.Query().setFilterById(id))?.use { c ->
            if (c.moveToFirst()) return c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
        }
        return null
    }

    /** Starts the download (Wi-Fi only). False when there isn't room for it. */
    @Suppress("DEPRECATION")
    fun download(ctx: Context): Boolean {
        val app = ctx.applicationContext
        val f = file(app)
        f.parentFile?.mkdirs()
        val free = f.parentFile?.usableSpace ?: 0L
        if (free < PhoneBrain.SIZE + 300_000_000L) return false
        if (f.exists()) f.delete()
        val req = DownloadManager.Request(Uri.parse(PhoneBrain.URL))
            .setTitle("ARYA offline brain")
            .setDescription("${PhoneBrain.SIZE_TEXT}, Wi-Fi only — runs on the phone when the laptop is out of reach")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setAllowedNetworkTypes(DownloadManager.Request.NETWORK_WIFI)
            .setAllowedOverMetered(false)
            .setAllowedOverRoaming(false)
            .setDestinationUri(Uri.fromFile(f))
        val id = app.getSystemService(DownloadManager::class.java).enqueue(req)
        prefs(app).edit().putLong("download", id).remove("verified").apply()
        return true
    }

    /** Checks the downloaded file against its published SHA-256 (in the background; ≈ 10–20 s). */
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
            val p = prefs(app).edit().remove("download")
            if (ok) p.putString("verified", PhoneBrain.SHA256) else { file(app).delete(); p.remove("verified") }
            p.apply()
            checking = false
            done?.invoke(ok)
        }
    }

    /** "download / delete the offline brain", "is the offline brain ready?" — what to say back. */
    fun manage(ctx: Context, m: PhoneBrain.Manage): String {
        val s = status(ctx)
        return when (m) {
            PhoneBrain.Manage.DOWNLOAD -> when (s) {
                Status.READY -> "The offline brain is already on the phone."
                Status.DOWNLOADING -> "It's already downloading: ${progress(ctx) ?: 0} percent."
                Status.CHECKING -> "It's downloaded; I'm checking the file."
                else -> if (download(ctx))
                    "Downloading the offline brain: ${PhoneBrain.SIZE_TEXT}, on Wi-Fi only. The progress is in your " +
                        "notifications; it keeps going in the background."
                else "There isn't room for it: the offline brain needs about two gigabytes free on the phone."
            }
            PhoneBrain.Manage.DELETE -> if (s == Status.MISSING) "The offline brain isn't on the phone."
                else { delete(ctx); "Deleted the offline brain. That's ${PhoneBrain.SIZE_TEXT} free again." }
            PhoneBrain.Manage.STATUS -> when (s) {
                Status.READY -> "Yes, the offline brain is ready. When the laptop is out of reach, I answer on the phone."
                Status.DOWNLOADING -> "Still downloading: ${progress(ctx) ?: 0} percent. It waits for Wi-Fi."
                Status.CHECKING -> "It's downloaded; I'm checking the file. Give it a few seconds."
                Status.FAILED -> "The download failed. Say \"download the offline brain\" to try again."
                Status.MISSING -> "It isn't on the phone yet. On Wi-Fi, say \"download the offline brain\"."
            }
        }
    }

    fun delete(ctx: Context) {
        close()
        val p = prefs(ctx)
        p.getLong("download", -1L).takeIf { it >= 0 }?.let {
            try { ctx.getSystemService(DownloadManager::class.java).remove(it) } catch (e: Exception) { }
        }
        file(ctx).delete()
        p.edit().clear().apply()
    }

    /** Android's download manager says a download finished: if it's ours, check it and say how it went. */
    fun downloaded(ctx: Context, id: Long) {
        if (id < 0 || id != prefs(ctx).getLong("download", -1L)) return
        if (downloadState(ctx, id) != DownloadManager.STATUS_SUCCESSFUL) return     // failed: status() reports it
        verify(ctx) { ok ->
            Notifier.channels(ctx)
            Notifier.show(ctx, org.json.JSONObject().put("id", 9_100_000_000_000L).put("kind", "message")
                .put("title", if (ok) "ARYA's offline brain is ready" else "ARYA's offline brain didn't download right")
                .put("text", if (ok) "When the laptop is out of reach, ARYA now answers on the phone itself."
                             else "The file was damaged and has been removed. Say \"download the offline brain\" again."))
        }
    }

    // ── Running it ────────────────────────────────────────────────────────────

    private var llm: LlmInference? = null
    private val lock = Any()

    /** Loads the model (a few seconds) if it is downloaded; safe to call from any thread, any number of times. */
    fun warm(ctx: Context): Boolean = synchronized(lock) {
        if (llm != null) return true
        if (status(ctx) != Status.READY) return false
        return try {
            llm = LlmInference.createFromOptions(ctx.applicationContext, LlmInference.LlmInferenceOptions.builder()
                .setModelPath(file(ctx).absolutePath)
                .setMaxTokens(PhoneBrain.MAX_TOKENS)
                .setMaxTopK(40)
                .setPreferredBackend(LlmInference.Backend.CPU)
                .build())
            true
        } catch (e: Throwable) { llm = null; false }
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
