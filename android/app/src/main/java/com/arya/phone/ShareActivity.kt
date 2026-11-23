package com.arya.phone

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.util.Base64
import android.view.Gravity
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.source
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/** Share -> ARYA from any app: a link, text, a PDF / Word / PowerPoint file or a photo. A panel slides up over the
 *  app you're in with quick actions (summarize, explain, save as note, add to-do, remind me, ask). The laptop does
 *  the work (arya/core/share.py; a link: the laptop reads the page itself, no cloud AI). The answer shows here, can
 *  be read aloud in ARYA's voice, and is saved in the phone chat; close the panel early and a notification brings
 *  the answer when it's ready. */
class ShareActivity : Activity() {
    private sealed class Item {
        data class Link(val url: String, val note: String, val subject: String) : Item()
        data class Text(val text: String, val subject: String) : Item()
        data class File(val uri: Uri, val name: String, val mime: String, val photo: Boolean) : Item()
    }

    private val ui = Handler(Looper.getMainLooper())
    private val d by lazy { resources.displayMetrics.density }
    private lateinit var prefs: Prefs
    private lateinit var panel: LinearLayout
    private lateinit var body: LinearLayout
    private lateinit var orb: OrbView
    private var item: Item? = null
    @Volatile private var busy = false
    @Volatile private var gone = false                 // the panel was closed while ARYA was still working
    private var answer = ""
    private var player: Player? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        Notifier.channels(this)
        item = parse(intent)
        build()
        val it = item
        when {
            !prefs.paired -> message("Open ARYA and pair it with the laptop first (scan the QR code).", warn = true)
            it == null -> message("I can take links, text, PDFs, Word and PowerPoint files and photos.", warn = true)
            else -> actions(it)
        }
        panel.post {                                              // slide up
            panel.translationY = panel.height.toFloat()
            panel.animate().translationY(0f).setDuration(260).start()
        }
    }

    // ── What was shared ─────────────────────────────────────────────────────

    private fun parse(i: Intent?): Item? {
        if (i == null || i.action != Intent.ACTION_SEND) return null
        val subject = i.getStringExtra(Intent.EXTRA_SUBJECT).orEmpty().trim()
        @Suppress("DEPRECATION")
        val stream = i.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
        val text = i.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString().orEmpty().trim()
        if (stream != null) {
            val mime = i.type ?: contentResolver.getType(stream) ?: ""
            val name = displayName(stream, mime)
            return Item.File(stream, name, mime, mime.startsWith("image/"))
        }
        if (text.isEmpty()) return null
        val url = Regex("https?://[^\\s<>\"]+", RegexOption.IGNORE_CASE).find(text)?.value?.trimEnd('.', ',', ')', ';')
        if (url != null) {
            val note = text.replace(url, "").trim().trim('-', '|', ':').trim()
            return Item.Link(url, note, subject)
        }
        return Item.Text(text, subject)
    }

    private fun displayName(uri: Uri, mime: String): String {
        var name = try {
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        } catch (e: Exception) { null } ?: (uri.lastPathSegment ?: "shared")
        if (!name.contains('.')) {
            val ext = when {
                mime == "application/pdf" -> ".pdf"
                mime.contains("wordprocessingml") -> ".docx"
                mime.contains("presentationml") -> ".pptx"
                mime == "image/png" -> ".png"
                mime.startsWith("image/") -> ".jpg"
                mime == "text/markdown" -> ".md"
                mime.startsWith("text/") -> ".txt"
                else -> ""
            }
            name += ext
        }
        return name
    }

    // ── Layout ──────────────────────────────────────────────────────────────

    private fun build() {
        window.statusBarColor = Color.TRANSPARENT
        val pad = (20 * d).toInt()
        val dim = FrameLayout(this).apply {
            setBackgroundColor(Color.argb(150, 0, 0, 0))
            setOnClickListener { close() }
        }
        panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, (14 * d).toInt(), pad, pad)
            background = Theme.glass(28 * d, Color.parseColor("#F00C0E15"))
                .apply { cornerRadii = floatArrayOf(28 * d, 28 * d, 28 * d, 28 * d, 0f, 0f, 0f, 0f) }
            isClickable = true                                     // taps inside don't close it
            elevation = 16 * d
        }
        // grab handle
        panel.addView(View(this).apply {
            background = Theme.glass(3 * d, Color.argb(70, 255, 255, 255))
        }, LinearLayout.LayoutParams((40 * d).toInt(), (4 * d).toInt()).apply {
            gravity = Gravity.CENTER_HORIZONTAL; bottomMargin = (12 * d).toInt() })

        // header: orb, ARYA, close
        val head = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        orb = OrbView(this)
        head.addView(orb, LinearLayout.LayoutParams((44 * d).toInt(), (44 * d).toInt()))
        head.addView(Theme.wordmark(this, 16f).apply { setPadding((8 * d).toInt(), 0, 0, 0) },
            LinearLayout.LayoutParams(0, -2, 1f))
        head.addView(Theme.iconButton(this, "✕", "Close") { close() },
            LinearLayout.LayoutParams((36 * d).toInt(), (36 * d).toInt()))
        panel.addView(head)

        // what was shared
        item?.let { panel.addView(preview(it), LinearLayout.LayoutParams(-1, -2).apply { topMargin = (12 * d).toInt() }) }

        body = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        panel.addView(body, LinearLayout.LayoutParams(-1, -2).apply { topMargin = (14 * d).toInt() })
        dim.addView(panel, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        setContentView(dim)
    }

    private fun preview(it: Item): View {
        val (icon, title, sub) = when (it) {
            is Item.Link -> Triple("🔗", it.subject.ifEmpty { it.note.ifEmpty { host(it.url) } }, it.url)
            is Item.Text -> Triple("📝", it.subject.ifEmpty { it.text.lineSequence().first().take(80) },
                "${it.text.length} characters")
            is Item.File -> Triple(if (it.photo) "🖼" else "📄", it.name, if (it.photo) "Photo" else "Document")
        }
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding((12 * d).toInt(), (10 * d).toInt(), (12 * d).toInt(), (10 * d).toInt())
            background = Theme.glass(16 * d)
        }
        card.addView(TextView(this).apply { text = icon; textSize = 22f; setPadding(0, 0, (12 * d).toInt(), 0) })
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        texts.addView(TextView(this).apply {
            text = title; textSize = 15f; setTextColor(Theme.TEXT); maxLines = 2
            ellipsize = android.text.TextUtils.TruncateAt.END
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
        texts.addView(TextView(this).apply {
            text = sub; textSize = 12.5f; setTextColor(Theme.MUTED); maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        })
        card.addView(texts, LinearLayout.LayoutParams(0, -2, 1f))
        return card
    }

    private fun actions(it: Item) {
        body.removeAllViews()
        orb.mode = OrbView.Mode.IDLE
        val photo = it is Item.File && it.photo
        val late = java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY) >= 19
        val list = listOf(
            Triple("summarize", if (photo) "✨ Describe" else "✨ Summarize", true),
            Triple("explain", "💡 Explain", false),
            Triple("note", "📝 Save as note", false),
            Triple("todo", "✅ Add to-do", false),
            Triple("remind", if (late) "⏰ Remind me tomorrow" else "⏰ Remind me tonight", false),
            Triple("ask", "💬 Ask a question", false),
        )
        var row: LinearLayout? = null
        list.forEachIndexed { i, (action, label, primary) ->
            if (i % 2 == 0) {
                row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
                body.addView(row, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = (10 * d).toInt() })
            }
            val b = Theme.pill(this, label, primary) { if (action == "ask") askBox(it) else run(it, action, "") }
            b.textSize = 14.5f
            b.setPadding((10 * d).toInt(), (14 * d).toInt(), (10 * d).toInt(), (14 * d).toInt())
            row!!.addView(b, LinearLayout.LayoutParams(0, -2, 1f).apply {
                if (i % 2 == 0) rightMargin = (5 * d).toInt() else leftMargin = (5 * d).toInt() })
        }
        body.addView(TextView(this).apply {
            text = if (it is Item.Link) "The laptop reads the page itself — nothing goes to a cloud AI."
                   else "Sent to your laptop's ARYA only."
            textSize = 12f; setTextColor(Theme.MUTED); gravity = Gravity.CENTER
            setPadding(0, (4 * d).toInt(), 0, 0)
        })
    }

    private fun askBox(it: Item) {
        body.removeAllViews()
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val input = EditText(this).apply {
            hint = "What do you want to know?"; textSize = 15.5f
            setTextColor(Theme.TEXT); setHintTextColor(Theme.MUTED)
            background = Theme.glass(18 * d)
            setPadding((16 * d).toInt(), (13 * d).toInt(), (16 * d).toInt(), (13 * d).toInt())
            imeOptions = EditorInfo.IME_ACTION_SEND
            isSingleLine = true
        }
        val send = { if (input.text.isNotBlank()) run(it, "ask", input.text.toString()) }
        input.setOnEditorActionListener { _, a, _ -> if (a == EditorInfo.IME_ACTION_SEND) { send(); true } else false }
        row.addView(input, LinearLayout.LayoutParams(0, -2, 1f))
        row.addView(Theme.pill(this, "➤", primary = true) { send() }.apply {
            setPadding((18 * d).toInt(), (13 * d).toInt(), (18 * d).toInt(), (13 * d).toInt()) },
            LinearLayout.LayoutParams(-2, -2).apply { leftMargin = (8 * d).toInt() })
        body.addView(row)
        input.requestFocus()
        window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
    }

    private fun message(text: String, warn: Boolean = false) {
        body.removeAllViews()
        body.addView(TextView(this).apply {
            this.text = text; textSize = 15f; setTextColor(if (warn) Theme.WARN else Theme.TEXT)
            setPadding(0, (4 * d).toInt(), 0, (8 * d).toInt())
        })
    }

    // ── Doing it ────────────────────────────────────────────────────────────

    private fun run(it: Item, action: String, question: String) {
        if (busy) return
        busy = true
        orb.mode = OrbView.Mode.THINKING
        message(when {
            action in setOf("note", "todo", "remind") -> "Saving…"
            it is Item.Link -> "The laptop is reading the page…"
            it is Item.File -> "Sending ${it.name}…"
            else -> "Thinking…"
        })
        val label = mapOf("summarize" to "Summary", "explain" to "Explained", "ask" to "Question",
            "note" to "Saved as note", "todo" to "Added to to-dos", "remind" to "Reminder")[action] ?: "Shared"
        val app = applicationContext
        thread(name = "arya-share") {
            val (ok, text, title) = try {
                val body = JSONObject().put("action", action).put("question", question)
                when (it) {
                    is Item.Link -> body.put("url", it.url).put("text", it.note).put("subject", it.subject)
                    is Item.Text -> body.put("text", it.text).put("subject", it.subject)
                    is Item.File -> {
                        val up = upload(it)
                        ui.post { if (!gone) message(if (action in setOf("note", "todo", "remind")) "Saving…" else "Thinking…") }
                        body.put("upload", JSONObject().put("kind", up.optString("kind")).put("title", up.optString("title")))
                    }
                }
                val r = post("/api/share", body)
                Triple(true, r.optString("reply"), r.optString("title"))
            } catch (e: ShareFailed) {
                Triple(false, e.message ?: "That didn't work.", "")
            }
            busy = false
            if (gone) {                                     // the panel was closed: bring the answer as a notification
                if (ok) Notifier.show(app, JSONObject().put("kind", "share").put("id", System.currentTimeMillis())
                    .put("title", "📎 ${title.ifEmpty { "Shared" }} · $label").put("text", text))
                return@thread
            }
            ui.post { if (ok) showAnswer(text) else { orb.mode = OrbView.Mode.IDLE; message(text, warn = true); retry(it) } }
        }
    }

    private fun retry(it: Item) {
        body.addView(Theme.pill(this, "Try again", primary = false) { actions(it) }, LinearLayout.LayoutParams(-1, -2))
    }

    private fun showAnswer(text: String) {
        answer = text
        orb.mode = OrbView.Mode.IDLE
        body.removeAllViews()
        val maxH = (resources.displayMetrics.heightPixels * 0.45f).toInt()
        val scroll = object : ScrollView(this) {
            override fun onMeasure(w: Int, h: Int) =
                super.onMeasure(w, View.MeasureSpec.makeMeasureSpec(maxH, View.MeasureSpec.AT_MOST))
        }
        scroll.addView(TextView(this).apply {
            this.text = text; textSize = 16.5f; setTextColor(Theme.TEXT); setLineSpacing(0f, 1.2f)
            setTextIsSelectable(true)
        })
        body.addView(scroll, LinearLayout.LayoutParams(-1, -2))
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val listen = Theme.pill(this, "🔊 Listen", primary = false) { listen() }
        val chat = Theme.pill(this, "Open chat", primary = false) {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            close()
        }
        val done = Theme.pill(this, "Done", primary = true) { close() }
        listOf(listen, chat, done).forEachIndexed { i, b ->
            b.textSize = 14f
            b.setPadding((8 * d).toInt(), (13 * d).toInt(), (8 * d).toInt(), (13 * d).toInt())
            row.addView(b, LinearLayout.LayoutParams(0, -2, 1f).apply { if (i > 0) leftMargin = (8 * d).toInt() })
        }
        body.addView(row, LinearLayout.LayoutParams(-1, -2).apply { topMargin = (14 * d).toInt() })
    }

    /** ARYA's own voice from the laptop, sentence by sentence. */
    private fun listen() {
        if (answer.isEmpty()) return
        player?.stop()
        orb.mode = OrbView.Mode.THINKING
        val p = Player(this) { ui.post { orb.mode = OrbView.Mode.IDLE } }
        player = p
        thread(name = "arya-share-speak") {
            try {
                val r = post("/api/speak", JSONObject().put("text", answer))
                val audio = r.optJSONArray("audio")
                ui.post { orb.mode = OrbView.Mode.SPEAKING }
                if (audio == null || audio.length() == 0) ui.post { orb.mode = OrbView.Mode.IDLE }
                else for (i in 0 until audio.length()) p.enqueue(Base64.decode(audio.getString(i), Base64.DEFAULT), "audio/wav")
            } catch (e: ShareFailed) {
                ui.post { orb.mode = OrbView.Mode.IDLE }
            }
        }
    }

    private fun close() {
        if (busy) gone = true
        player?.stop()
        panel.animate().translationY(panel.height.toFloat()).setDuration(200).withEndAction { finish() }.start()
    }

    @Deprecated("Activity API")
    override fun onBackPressed() = close()

    override fun finish() {
        if (busy) gone = true
        super.finish()
    }

    // ── Network ─────────────────────────────────────────────────────────────

    private class ShareFailed(msg: String) : Exception(msg)

    private val client = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS).writeTimeout(120, TimeUnit.SECONDS)
        .readTimeout(200, TimeUnit.SECONDS)                      // reading a page + the model's answer
        .build()

    private fun request(base: String, path: String) = Request.Builder().url(base.trimEnd('/') + path)
        .header("Authorization", "Bearer ${prefs.key}").header("X-ARYA", "1")

    /** Every laptop address in turn (Tailscale, home Wi-Fi) until one answers. */
    private fun send(build: (String) -> Request): JSONObject {
        for (base in prefs.urls) {
            try {
                client.newCall(build(base)).execute().use { r ->
                    val json = try { JSONObject(r.body?.string() ?: "{}") } catch (e: Exception) { JSONObject() }
                    if (r.code == 401) throw ShareFailed("The laptop doesn't know this phone any more: pair it again in ARYA.")
                    if (!r.isSuccessful) throw ShareFailed(json.optString("error").ifEmpty { "The laptop had a problem (${r.code})." })
                    return json
                }
            } catch (e: ShareFailed) {
                throw e
            } catch (e: Exception) { /* not reachable at this address: try the next */ }
        }
        throw ShareFailed("I can't reach the laptop. Is it awake with ARYA running (and Tailscale on)?")
    }

    private fun post(path: String, json: JSONObject): JSONObject =
        send { base -> request(base, path).post(json.toString().toRequestBody("application/json".toMediaType())).build() }

    private fun upload(f: Item.File): JSONObject = send { base ->
        val body = object : RequestBody() {
            override fun contentType(): MediaType = (f.mime.ifEmpty { "application/octet-stream" }).toMediaType()
            override fun writeTo(sink: BufferedSink) {
                contentResolver.openInputStream(f.uri)?.use { sink.writeAll(it.source()) }
                    ?: throw java.io.IOException("can't open the file")
            }
        }
        request(base, "/api/upload?name=" + Uri.encode(f.name)).post(body).build()
    }

    private fun host(url: String) = (Uri.parse(url).host ?: url).removePrefix("www.")
}
