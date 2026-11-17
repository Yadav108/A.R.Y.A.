package com.arya.phone

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaPlayer
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.LinkedBlockingQueue
import kotlin.math.sqrt

/** Records one request: 16 kHz mono 16-bit, the format ARYA's Whisper wants. Starts keeping audio when you start
 *  speaking (with 300 ms before it), stops after 1.2 s of quiet, at the latest after 20 s; nothing said within
 *  [startTimeoutMs] -> null. Blocking: run it on a background thread; [finish] ends it early (tap), [cancel]
 *  throws it away. */
class Recorder {
    @Volatile private var finishing = false
    @Volatile private var cancelled = false
    @Volatile var level = 0f; private set

    fun finish() { finishing = true }
    fun cancel() { cancelled = true }

    @SuppressLint("MissingPermission")
    fun record(startTimeoutMs: Int = 7000, silenceMs: Int = 1200, maxMs: Int = 20000): ByteArray? {
        val rate = 16000
        val frame = rate / 50                                         // 20 ms
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(min, frame * 8))
        if (rec.state != AudioRecord.STATE_INITIALIZED) { rec.release(); return null }
        val buf = ShortArray(frame)
        val out = ByteArrayOutputStream()
        val preroll = ArrayDeque<ShortArray>()
        var floor = 0.0
        var frames = 0
        var loud = 0
        var quiet = 0
        var speaking = false
        rec.startRecording()
        try {
            while (!cancelled) {
                val n = rec.read(buf, 0, frame)
                if (n <= 0) continue
                frames++
                var sum = 0.0
                for (i in 0 until n) sum += buf[i] * buf[i].toDouble()
                val rms = sqrt(sum / n)
                level = (rms / 3000.0).coerceIn(0.0, 1.0).toFloat()
                if (frames <= 15) floor = maxOf(floor, rms)                 // first 300 ms: the room's noise
                val threshold = maxOf(350.0, floor * 2.5)
                val chunk = buf.copyOf(n)
                if (!speaking) {
                    preroll.addLast(chunk)
                    if (preroll.size > 15) preroll.removeFirst()
                    loud = if (rms > threshold) loud + 1 else 0
                    if (loud >= 3) {                                       // 60 ms of voice: you started
                        speaking = true
                        preroll.forEach { write(out, it) }
                        preroll.clear()
                    } else if (frames * 20 > startTimeoutMs || finishing) {
                        return null                                       // nothing said
                    }
                } else {
                    write(out, chunk)
                    quiet = if (rms < threshold) quiet + 1 else 0
                    if (quiet * 20 >= silenceMs || out.size() >= maxMs * 32 || finishing) break
                }
            }
        } finally {
            try { rec.stop() } catch (e: Exception) { }
            rec.release()
            level = 0f
        }
        return if (cancelled || !speaking) null else wav(out.toByteArray(), rate)
    }

    private fun write(out: ByteArrayOutputStream, samples: ShortArray) {
        val bb = ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        samples.forEach { bb.putShort(it) }
        out.write(bb.array())
    }

    private fun wav(pcm: ByteArray, rate: Int): ByteArray {
        val h = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        h.put("RIFF".toByteArray()).putInt(36 + pcm.size).put("WAVE".toByteArray())
        h.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(rate).putInt(rate * 2)
            .putShort(2).putShort(16)
        h.put("data".toByteArray()).putInt(pcm.size)
        return h.array() + pcm
    }
}

/** Plays ARYA's sentences one after another as they arrive (each a short WAV / MP3). */
class Player(private val context: Context, private val onIdle: () -> Unit) {
    private val queue = LinkedBlockingQueue<File>()
    @Volatile private var current: MediaPlayer? = null
    @Volatile var busy = false; private set
    private var counter = 0

    @Synchronized
    fun enqueue(audio: ByteArray, mime: String) {
        val f = File(context.cacheDir, "say-${counter++}." + if (mime.contains("mpeg")) "mp3" else "wav")
        f.writeBytes(audio)
        queue.add(f)
        if (!busy) next()
    }

    @Synchronized
    private fun next() {
        val f = queue.poll()
        if (f == null) {
            busy = false
            current = null
            onIdle()
            return
        }
        busy = true
        try {
            val mp = MediaPlayer()
            mp.setDataSource(f.absolutePath)
            mp.setOnCompletionListener { it.release(); f.delete(); next() }
            mp.setOnErrorListener { p, _, _ -> p.release(); f.delete(); next(); true }
            mp.prepare()
            mp.start()
            current = mp
        } catch (e: Exception) {
            f.delete()
            next()
        }
    }

    /** Barge-in: stop talking now, forget the rest. */
    @Synchronized
    fun stop() {
        try { current?.stop(); current?.release() } catch (e: Exception) { }
        current = null
        while (true) { queue.poll()?.delete() ?: break }
        busy = false
    }
}
