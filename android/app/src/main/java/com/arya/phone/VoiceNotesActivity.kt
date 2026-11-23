package com.arya.phone

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.content.FileProvider
import java.io.File
import kotlin.concurrent.thread

/** Record voice notes in the app and keep them: a list of your notes (play, share, delete) and a big record button.
 *  Each note is saved on the phone and sent to ARYA, which transcribes and files it (to-do, idea or note).
 *  Opened from the + menu on ARYA's page, the icon's long-press menu, or by saying "record a voice note". */
class VoiceNotesActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private val d by lazy { resources.displayMetrics.density }
    private lateinit var list: LinearLayout
    private lateinit var timer: TextView
    private lateinit var hint: TextView
    private lateinit var recordButton: TextView
    private lateinit var cancelButton: TextView
    private lateinit var levels: LevelView

    private var recorder: MediaRecorder? = null
    private var recordingFile: File? = null
    private var startedAt = 0L
    private var player: MediaPlayer? = null
    private var playing: File? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        build()
        refresh()
        thread(name = "arya-notes-send") { VoiceNotes.sendWaiting(this); ui.post { refresh() } }
        if (wantsRecording(intent)) startRecording()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (wantsRecording(intent) && recorder == null) startRecording()
    }

    private fun wantsRecording(i: Intent?) =
        i != null && (i.getBooleanExtra(EXTRA_RECORD, false) || i.action == ACTION_RECORD)

    // ── Layout ──────────────────────────────────────────────────────────────

    private fun build() {
        val pad = (20 * d).toInt()
        Theme.edgeToEdge(window)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = Theme.backdrop()
            fitsSystemWindows = true
        }
        // Top bar
        val top = FrameLayout(this).apply { setPadding(pad, (12 * d).toInt(), pad, 0) }
        top.addView(Theme.iconButton(this, "‹", "Back") { finish() }.apply { textSize = 22f },
            FrameLayout.LayoutParams((40 * d).toInt(), (40 * d).toInt(), Gravity.START or Gravity.CENTER_VERTICAL))
        top.addView(TextView(this).apply {
            text = "Voice notes"; textSize = 19f; setTextColor(Theme.TEXT)
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        }, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        root.addView(top, LinearLayout.LayoutParams(-1, (56 * d).toInt()))

        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, (8 * d).toInt(), pad, pad) }
        root.addView(ScrollView(this).apply { isVerticalScrollBarEnabled = false; addView(list) },
            LinearLayout.LayoutParams(-1, 0, 1f))

        // Recorder panel
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(pad, pad, pad, pad)
            background = Theme.glass(28 * d).apply { cornerRadii = floatArrayOf(28 * d, 28 * d, 28 * d, 28 * d, 0f, 0f, 0f, 0f) }
        }
        timer = TextView(this).apply {
            text = "00:00"; textSize = 34f; setTextColor(Theme.TEXT); gravity = Gravity.CENTER
            typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        }
        panel.addView(timer)
        levels = LevelView(this)
        panel.addView(levels, LinearLayout.LayoutParams(-1, (48 * d).toInt()).apply { topMargin = (6 * d).toInt() })
        hint = TextView(this).apply {
            text = "Tap to record. It's saved here and sent to ARYA."; textSize = 13.5f
            setTextColor(Theme.MUTED); gravity = Gravity.CENTER; setPadding(0, (8 * d).toInt(), 0, (14 * d).toInt())
        }
        panel.addView(hint)
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        cancelButton = Theme.iconButton(this, "✕", "Discard this recording") { discard() }.apply { visibility = View.INVISIBLE }
        recordButton = TextView(this).apply {
            gravity = Gravity.CENTER; textSize = 26f; setTextColor(Color.WHITE)
            contentDescription = "Record"
            elevation = 8 * d
            isClickable = true
            setOnClickListener { if (recorder == null) startRecording() else stopRecording() }
        }
        styleRecordButton(false)
        val side = (48 * d).toInt()
        row.addView(cancelButton, LinearLayout.LayoutParams(side, side))
        row.addView(recordButton, LinearLayout.LayoutParams((84 * d).toInt(), (84 * d).toInt()).apply {
            leftMargin = (32 * d).toInt(); rightMargin = (32 * d).toInt() })
        row.addView(View(this), LinearLayout.LayoutParams(side, side))      // keeps the button centred
        panel.addView(row)
        root.addView(panel, LinearLayout.LayoutParams(-1, -2))
        setContentView(root)
    }

    private fun styleRecordButton(recording: Boolean) {
        recordButton.text = if (recording) "■" else "●"
        recordButton.contentDescription = if (recording) "Stop and save" else "Record"
        recordButton.background = if (recording)
            GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.parseColor("#FF5A6E")) }
        else GradientDrawable(GradientDrawable.Orientation.TL_BR, intArrayOf(Theme.ACCENT, Theme.ACCENT2))
            .apply { shape = GradientDrawable.OVAL }
    }

    private fun refresh() {
        list.removeAllViews()
        val notes = VoiceNotes.all(this)
        if (notes.isEmpty()) {
            list.addView(TextView(this).apply {
                text = "No voice notes yet.\nTap the button below and just talk — ARYA files it as a to-do, an idea " +
                       "or a note."
                textSize = 15f; setTextColor(Theme.MUTED); gravity = Gravity.CENTER; setLineSpacing(0f, 1.25f)
                setPadding(0, (60 * d).toInt(), 0, 0)
            })
            return
        }
        notes.forEach { list.addView(row(it), LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = (10 * d).toInt() }) }
    }

    private fun row(n: VoiceNotes.Note): View {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding((12 * d).toInt(), (12 * d).toInt(), (8 * d).toInt(), (12 * d).toInt())
            background = Theme.glass(20 * d)
        }
        val isPlaying = playing == n.file
        val play = TextView(this).apply {
            text = if (isPlaying) "❚❚" else "▶"; textSize = 15f; gravity = Gravity.CENTER; setTextColor(Color.WHITE)
            contentDescription = if (isPlaying) "Pause" else "Play"
            background = GradientDrawable(GradientDrawable.Orientation.TL_BR, intArrayOf(Theme.ACCENT, Theme.ACCENT2))
                .apply { shape = GradientDrawable.OVAL }
            isClickable = true
            setOnClickListener { togglePlay(n.file) }
        }
        card.addView(play, LinearLayout.LayoutParams((44 * d).toInt(), (44 * d).toInt()))
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding((12 * d).toInt(), 0, 0, 0) }
        texts.addView(TextView(this).apply {
            text = java.text.SimpleDateFormat("EEE d MMM, HH:mm", java.util.Locale.getDefault()).format(java.util.Date(n.createdMs))
            textSize = 15.5f; setTextColor(Theme.TEXT)
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
        val (label, color) = when (n.status) {
            VoiceNotes.Status.SENT -> "✓ Sent to ARYA" to Color.parseColor("#3EE0A0")
            VoiceNotes.Status.SENDING -> "Sending…" to Theme.ACCENT
            VoiceNotes.Status.WAITING -> "Waiting for the laptop · tap to send" to Theme.WARN
            VoiceNotes.Status.FAILED -> "Not sent: ${n.detail.ifEmpty { "error" }} · tap to retry" to Color.parseColor("#FF6B7A")
        }
        texts.addView(TextView(this).apply {
            text = "${clock(n.durationMs)}  ·  $label"; textSize = 12.5f; setTextColor(color)
            if (n.status == VoiceNotes.Status.WAITING || n.status == VoiceNotes.Status.FAILED) {
                isClickable = true
                setOnClickListener { sendNow(n) }
            }
        })
        card.addView(texts, LinearLayout.LayoutParams(0, -2, 1f))
        card.addView(small("⤴", "Share") { share(n.file) })
        card.addView(small("🗑", "Delete") { confirmDelete(n) })
        return card
    }

    private fun small(glyph: String, label: String, onClick: () -> Unit) = TextView(this).apply {
        text = glyph; textSize = 17f; gravity = Gravity.CENTER; setTextColor(Theme.MUTED)
        contentDescription = label
        layoutParams = LinearLayout.LayoutParams((40 * d).toInt(), (40 * d).toInt())
        isClickable = true
        setOnClickListener { onClick() }
    }

    // ── Recording ───────────────────────────────────────────────────────────

    private fun startRecording() {
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), MIC)
            return
        }
        stopPlaying()
        val file = VoiceNotes.newFile(this)
        @Suppress("DEPRECATION")
        val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else MediaRecorder()
        try {
            r.setAudioSource(MediaRecorder.AudioSource.MIC)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioSamplingRate(44100)
            r.setAudioEncodingBitRate(64000)
            r.setAudioChannels(1)
            r.setMaxDuration(MAX_MS)
            r.setOutputFile(file.absolutePath)
            r.setOnInfoListener { _, what, _ -> if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) ui.post { stopRecording() } }
            r.prepare()
            r.start()
        } catch (e: Exception) {
            r.release(); file.delete()
            hint.text = "The microphone is busy (another app recording?)."
            return
        }
        recorder = r
        recordingFile = file
        startedAt = SystemClock.elapsedRealtime()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        styleRecordButton(true)
        cancelButton.visibility = View.VISIBLE
        hint.text = "Recording… tap ■ to save, ✕ to throw it away."
        levels.clear()
        tick()
    }

    private fun tick() {
        val r = recorder ?: return
        timer.text = clock(SystemClock.elapsedRealtime() - startedAt)
        levels.push(try { r.maxAmplitude / 20000f } catch (e: Exception) { 0f })
        ui.postDelayed({ tick() }, 80)
    }

    private fun stopRecording(keep: Boolean = true) {
        val r = recorder ?: return
        val file = recordingFile
        val ms = SystemClock.elapsedRealtime() - startedAt
        recorder = null
        recordingFile = null
        val ok = try { r.stop(); true } catch (e: Exception) { false }        // too short -> stop() throws
        r.release()
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        styleRecordButton(false)
        cancelButton.visibility = View.INVISIBLE
        timer.text = "00:00"
        levels.clear()
        if (file == null) return
        if (!keep || !ok || ms < 800) {
            file.delete()
            hint.text = if (keep) "That was too short to keep." else "Thrown away."
            return
        }
        val note = VoiceNotes.add(this, file, ms)
        hint.text = "Saved. Sending to ARYA…"
        refresh()
        thread(name = "arya-note-send") {
            val sent = VoiceNotes.send(this, note)
            ui.post {
                if (!isFinishing) {
                    hint.text = if (sent) "Saved and sent: ARYA will tell you where it filed it."
                                else "Saved on the phone. It goes to ARYA when the laptop is reachable."
                    refresh()
                }
            }
        }
    }

    private fun discard() = stopRecording(keep = false)

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        if (code == MIC && results.firstOrNull() == PackageManager.PERMISSION_GRANTED) startRecording()
        else if (code == MIC) hint.text = "ARYA needs the microphone to record (Settings → Apps → ARYA → Permissions)."
    }

    // ── Notes: play, send, share, delete ────────────────────────────────────

    private fun togglePlay(f: File) {
        if (playing == f) { stopPlaying(); refresh(); return }
        stopPlaying()
        if (recorder != null) return
        try {
            player = MediaPlayer().apply {
                setDataSource(f.absolutePath)
                setOnCompletionListener { stopPlaying(); refresh() }
                prepare()
                start()
            }
            playing = f
        } catch (e: Exception) {
            hint.text = "Couldn't play that note."
        }
        refresh()
    }

    private fun stopPlaying() {
        player?.let { try { it.stop() } catch (e: Exception) { }; it.release() }
        player = null
        playing = null
    }

    private fun sendNow(n: VoiceNotes.Note) {
        hint.text = "Sending…"
        thread(name = "arya-note-send") {
            val sent = VoiceNotes.send(this, n)
            ui.post { if (!isFinishing) { hint.text = if (sent) "Sent to ARYA." else "Still can't reach the laptop."; refresh() } }
        }
        ui.postDelayed({ refresh() }, 150)
    }

    private fun share(f: File) {
        try {
            val uri = FileProvider.getUriForFile(this, "$packageName.files", f)
            startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                type = "audio/mp4"
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }, "Share voice note"))
        } catch (e: Exception) {
            hint.text = "Couldn't share that note."
        }
    }

    private fun confirmDelete(n: VoiceNotes.Note) {
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("Delete this voice note?")
            .setMessage("The recording is removed from the phone for good." +
                if (n.status == VoiceNotes.Status.SENT) " What ARYA already filed on the laptop stays." else "")
            .setPositiveButton("Delete") { _, _ ->
                if (playing == n.file) stopPlaying()
                VoiceNotes.delete(this, n.file)
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    override fun onStop() {
        super.onStop()
        if (recorder != null && !isChangingConfigurations) stopRecording()   // leaving the screen saves the note
        stopPlaying()
    }

    private fun clock(ms: Long): String {
        val s = ms / 1000
        return if (s >= 3600) "%d:%02d:%02d".format(s / 3600, (s / 60) % 60, s % 60) else "%02d:%02d".format(s / 60, s % 60)
    }

    /** A scrolling row of bars: how loud the last few seconds were. */
    private class LevelView(ctx: Context) : View(ctx) {
        private val values = ArrayDeque<Float>()
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { strokeCap = Paint.Cap.ROUND }
        private val dens = ctx.resources.displayMetrics.density

        fun push(v: Float) { values.addLast(v.coerceIn(0f, 1f)); while (values.size > 120) values.removeFirst(); invalidate() }
        fun clear() { values.clear(); invalidate() }

        override fun onDraw(canvas: Canvas) {
            val step = 6 * dens
            paint.strokeWidth = 3 * dens
            val count = (width / step).toInt()
            val shown = values.toList().takeLast(count)
            val mid = height / 2f
            for (i in 0 until count) {
                val v = shown.getOrNull(i - (count - shown.size)) ?: 0f
                val h = maxOf(2 * dens, v * height * 0.9f) / 2
                paint.color = if (shown.isEmpty()) Color.argb(50, 255, 255, 255) else Theme.ACCENT
                if (i < count - shown.size) paint.color = Color.argb(40, 255, 255, 255)
                val x = i * step + step / 2
                canvas.drawLine(x, mid - h, x, mid + h, paint)
            }
        }
    }

    companion object {
        const val EXTRA_RECORD = "record"
        const val ACTION_RECORD = "com.arya.phone.RECORD_NOTE"
        private const val MIC = 21
        private const val MAX_MS = 30 * 60 * 1000

        fun open(ctx: Context, record: Boolean) {
            ctx.startActivity(Intent(ctx, VoiceNotesActivity::class.java).putExtra(EXTRA_RECORD, record)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        }
    }
}
