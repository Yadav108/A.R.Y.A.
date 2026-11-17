package com.arya.phone

import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.view.Gravity
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import kotlin.concurrent.thread

/** Talking to ARYA. Opens listening; you speak; ARYA's answer plays sentence by sentence as it arrives (its own
 *  voice, from the laptop); then it listens again for a follow-up. Tap the circle to interrupt ARYA, or to send
 *  before the pause. Opened from the app, the Quick Settings tile, the widget, the icon's long-press menu, or as
 *  Android's assistant (long-press the power button). */
class TalkActivity : Activity() {
    private enum class State { IDLE, LISTENING, THINKING, SPEAKING }

    private lateinit var prefs: Prefs
    private lateinit var orb: TextView
    private lateinit var hint: TextView
    private lateinit var heard: TextView
    private lateinit var reply: TextView
    private lateinit var player: Player
    private val ui = Handler(Looper.getMainLooper())
    @Volatile private var state = State.IDLE
    @Volatile private var recorder: Recorder? = null
    @Volatile private var streamDone = true
    @Volatile private var followUp = false
    @Volatile private var lastError = false
    private var focus: AudioFocusRequest? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        player = Player(this) { ui.post { spokenAll() } }
        build()
        if (!prefs.paired) {
            show(State.IDLE, "Pair the app first: open ARYA and scan the QR code.")
            return
        }
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), MIC)
        } else {
            listen(follow = false)
        }
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        if (code == MIC && results.firstOrNull() == PackageManager.PERMISSION_GRANTED) listen(follow = false)
        else show(State.IDLE, "ARYA needs the microphone to hear you (Settings → Apps → ARYA → Permissions).")
    }

    private fun build() {
        val pad = (20 * resources.displayMetrics.density).toInt()
        val size = (170 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(pad, pad * 3, pad, pad)
            setBackgroundColor(Color.parseColor("#0F1115"))
        }
        orb = TextView(this).apply {
            gravity = Gravity.CENTER
            textSize = 22f
            setTextColor(Color.WHITE)
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.parseColor("#2A3350")) }
            layoutParams = LinearLayout.LayoutParams(size, size)
            setOnClickListener { tapped() }
        }
        hint = TextView(this).apply {
            gravity = Gravity.CENTER; textSize = 15f; setTextColor(Color.parseColor("#9AA0AA")); setPadding(0, pad, 0, pad)
        }
        heard = TextView(this).apply { textSize = 16f; setTextColor(Color.parseColor("#7AA2FF")) }
        reply = TextView(this).apply { textSize = 18f; setTextColor(Color.parseColor("#E8E8EA")); setPadding(0, pad / 2, 0, 0) }
        val scroll = ScrollView(this).apply {
            addView(LinearLayout(this@TalkActivity).apply {
                orientation = LinearLayout.VERTICAL; addView(heard); addView(reply)
            })
        }
        root.addView(orb)
        root.addView(hint)
        root.addView(scroll, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(root)
    }

    private fun show(s: State, text: String) = ui.post {
        state = s
        orb.text = when (s) { State.LISTENING -> "Listening"; State.THINKING -> "…"; State.SPEAKING -> "ARYA"; State.IDLE -> "Talk" }
        (orb.background as GradientDrawable).setColor(Color.parseColor(when (s) {
            State.LISTENING -> "#3B6FE0"; State.THINKING -> "#4B4F7A"; State.SPEAKING -> "#2E8B6E"; State.IDLE -> "#2A3350"
        }))
        hint.text = text
    }

    private fun tapped() {
        when (state) {
            State.SPEAKING -> { player.stop(); releaseFocus(); listen(follow = false) }      // interrupt ARYA
            State.LISTENING -> recorder?.finish()                                            // send now
            State.THINKING -> { }
            State.IDLE -> listen(follow = false)
        }
    }

    private fun listen(follow: Boolean) {
        val urls = prefs.urls
        if (urls.isEmpty()) return
        followUp = follow
        show(State.LISTENING, if (follow) "Anything else? (just speak)" else "Speak now — I'll send it when you pause.")
        val r = Recorder()
        recorder = r
        thread(name = "arya-listen") {
            val wav = r.record(startTimeoutMs = if (follow) 6000 else 8000)
            recorder = null
            if (wav == null) {
                show(State.IDLE, if (follow) "Tap to talk again." else "I didn't hear anything. Tap to try again.")
                return@thread
            }
            send(wav)
        }
    }

    private fun send(wav: ByteArray) {
        show(State.THINKING, "Thinking…")
        ui.post { heard.text = ""; reply.text = "" }
        streamDone = false
        lastError = false
        var lastFailure: Exception? = null
        for (base in prefs.urls) {
            try {
                Api.voice(base, prefs.key, wav) { e -> event(e) }
                lastFailure = null
                break
            } catch (e: Api.Unpaired) {
                lastFailure = e; break
            } catch (e: Exception) {
                lastFailure = e                                         // try the next address (home Wi-Fi)
            }
        }
        streamDone = true
        if (lastFailure != null) {
            lastError = true
            show(State.IDLE, if (lastFailure is Api.Unpaired) "Not paired any more: scan the new QR code in ARYA."
                             else "Can't reach the laptop. Is it awake, and Tailscale on?")
        } else if (!player.busy) ui.post { spokenAll() }
    }

    private fun event(e: org.json.JSONObject) {
        when (e.optString("type")) {
            "heard" -> ui.post { heard.text = "You: " + e.optString("text") }
            "say" -> {
                val text = e.optString("text")
                ui.post { reply.append((if (reply.text.isEmpty()) "" else " ") + text) }
                val audio = e.optString("audio")
                if (audio.isNotEmpty()) {
                    if (state != State.SPEAKING) { show(State.SPEAKING, "Tap to interrupt."); takeFocus() }
                    player.enqueue(Base64.decode(audio, Base64.DEFAULT), e.optString("mime", "audio/wav"))
                }
            }
            "done" -> {
                val err = e.optString("error")
                if (err.isNotEmpty()) { lastError = true; show(State.IDLE, "$err Tap to talk.") }
                if (e.optBoolean("confirm")) ui.post { hint.text = "Say yes or no." }
            }
        }
    }

    /** All sentences played and the answer complete: listen for a follow-up, like ARYA does on the laptop. */
    private fun spokenAll() {
        if (!streamDone || player.busy) return
        releaseFocus()
        if (state == State.SPEAKING || state == State.THINKING) {
            if (lastError) show(State.IDLE, "Tap to talk.") else listen(follow = true)
        }
    }

    private fun takeFocus() {
        val am = getSystemService(AudioManager::class.java)
        val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()).build()
        am.requestAudioFocus(req)
        focus = req
    }

    private fun releaseFocus() {
        focus?.let { getSystemService(AudioManager::class.java).abandonAudioFocusRequest(it) }
        focus = null
    }

    override fun onStop() {
        super.onStop()
        recorder?.cancel()
        player.stop()
        releaseFocus()
        if (!isChangingConfigurations) finish()                         // closed: stop listening, don't come back
    }

    companion object {
        private const val MIC = 11
    }
}
