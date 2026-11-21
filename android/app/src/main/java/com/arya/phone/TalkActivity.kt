package com.arya.phone

import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.app.KeyguardManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.os.Looper
import android.util.Base64
import android.view.Gravity
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import kotlin.concurrent.thread
import org.json.JSONObject

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
    @Volatile private var pendingAction: JSONObject? = null       // a phone task that opens another app: runs after
    @Volatile private var leftApp = false                         // ARYA said "Calling Mom." (Actions.kt)
    private var tts: TextToSpeech? = null                         // the phone's own voice, for what only the phone knows
    @Volatile private var offline: Boolean? = null                // laptop out of reach -> phone tasks on the phone alone
    private lateinit var ear: OfflineEar
    private val afterSpeech = java.util.concurrent.ConcurrentHashMap<String, () -> Unit>()
    private var utterances = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= 27) {                         // works from the lock screen, like Google's assistant
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        tts = TextToSpeech(this) { }
        tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) {}
            override fun onDone(id: String?) { afterSpeech.remove(id)?.let { ui.post(it) } }
            @Deprecated("Deprecated in Java") override fun onError(id: String?) { afterSpeech.remove(id)?.let { ui.post(it) } }
        })
        ear = OfflineEar(this)
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
        if (code == PEOPLE) {                                      // contacts / calls: now carry out the waiting task
            pendingAction?.let { a -> pendingAction = null; perform(a) }
            return
        }
        if (code == MIC && results.firstOrNull() == PackageManager.PERMISSION_GRANTED) listen(follow = false)
        else show(State.IDLE, "ARYA needs the microphone to hear you (Settings → Apps → ARYA → Permissions).")
    }

    // ── Phone tasks (Actions.kt) ─────────────────────────────────────────────

    private fun action(a: JSONObject) {
        if (Actions.leavesApp(a)) pendingAction = a                   // after "Calling Mom." has been said
        else ui.post { perform(a) }
    }

    private fun perform(a: JSONObject) {
        val needs = when (a.optString("action")) {
            "call" -> listOf(android.Manifest.permission.READ_CONTACTS, android.Manifest.permission.CALL_PHONE)
            "sms" -> listOf(android.Manifest.permission.READ_CONTACTS)
            else -> emptyList()
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        val isNumber = a.optString("who").none { it.isLetter() }
        if (needs.isNotEmpty() && !(isNumber && a.optString("action") == "call") && !prefs.askedPeople) {
            prefs.askedPeople = true                               // asked once; after that Actions.kt explains
            pendingAction = a
            requestPermissions(needs.toTypedArray(), PEOPLE)
            return
        }
        val go = {
            if (Actions.leavesApp(a)) leftApp = true
            val problem = Actions.run(this, a)
            if (problem != null) { leftApp = false; say(problem) }
        }
        val km = getSystemService(KeyguardManager::class.java)
        if (Actions.leavesApp(a) && km.isKeyguardLocked && Build.VERSION.SDK_INT >= 26) {
            km.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {   // unlock first
                override fun onDismissSucceeded() { go() }
            })
        } else go()
    }

    /** What only the phone knows ("I couldn't find Mom in your contacts"): said with the phone's own voice. */
    private fun say(text: String, then: (() -> Unit)? = null) {
        show(State.IDLE, text)
        ui.post { reply.text = text }
        val id = "arya-local-${utterances++}"
        if (then != null) afterSpeech[id] = then
        val ok = tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, id)
        if (ok != TextToSpeech.SUCCESS) afterSpeech.remove(id)?.let { ui.post(it) }      // no voice: carry on anyway
    }

    // ── The laptop out of reach: phone tasks on the phone alone (PhoneCommands.kt) ─────────────────────────────

    private fun listenOffline(follow: Boolean) {
        show(State.LISTENING, if (follow) "Anything else? (on the phone — the laptop is out of reach)"
                              else "Listening on the phone — the laptop is out of reach.")
        ear.listen { text ->
            if (text.isNullOrBlank()) show(State.IDLE, "Tap to talk again.")
            else offlineTurn(text)
        }
    }

    private fun offlineTurn(text: String) {
        ui.post { heard.text = "You: $text"; reply.text = "" }
        val cmd = PhoneCommands.parse(text)
        if (cmd == null) {
            say("The laptop is out of reach, so right now I can only do phone tasks: alarms, timers, calls, " +
                "messages, maps, apps, music and the flashlight.")
            return
        }
        val a = JSONObject(cmd.fields).put("action", cmd.action)
        when {
            cmd.action == "ask" -> say(cmd.say) { listenOffline(follow = true) }
            Actions.leavesApp(a) -> say(cmd.say) { perform(a) }               // "Calling Mom." then the call
            else -> { perform(a); say(cmd.say) }
        }
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
        if (offline == null) {                                     // first: is the laptop there? (≈1 s at most)
            show(State.THINKING, "Connecting…")
            thread(name = "arya-reach") {
                offline = urls.none { Api.reachable(it, prefs.key) }
                ui.post { listen(follow) }
            }
            return
        }
        if (offline == true) { listenOffline(follow); return }
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
            if (lastFailure is Api.Unpaired) show(State.IDLE, "Not paired any more: scan the new QR code in ARYA.")
            else {                                                     // the laptop went away: carry on with the phone
                offline = true
                ui.post { say("The laptop is out of reach. Say it again and I'll do it on the phone.") {
                    listenOffline(follow = false) } }
            }
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
            "action" -> action(e)
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
        pendingAction?.let { a ->                                   // "Calling Mom." said: now call
            pendingAction = null
            perform(a)
            if (leftApp || pendingAction != null) return             // gone to the other app, or asking permission
        }
        if (leftApp) return
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
        if (pendingAction == null) tts?.stop()
        ear.stop()
        if (!isChangingConfigurations) finish()                         // closed: stop listening, don't come back
    }

    override fun onDestroy() {
        tts?.shutdown()
        super.onDestroy()
    }

    companion object {
        private const val MIC = 11
        private const val PEOPLE = 12
    }
}
