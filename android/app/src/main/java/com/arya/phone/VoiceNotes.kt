package com.arya.phone

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/** Voice notes recorded in the app. Each one stays on the phone (app storage, .m4a) and is sent to ARYA, which
 *  transcribes and files it (arya/core/attachments.py, as_note). Laptop out of reach: the note waits and is sent
 *  the next time the voice-notes screen opens or a note is recorded. Never more than the audio leaves the phone. */
object VoiceNotes {
    enum class Status { WAITING, SENDING, SENT, FAILED }

    data class Note(val file: File, val createdMs: Long, val durationMs: Long, val status: Status, val detail: String = "")

    fun dir(ctx: Context) = File(ctx.filesDir, "voice-notes").apply { mkdirs() }
    private fun index(ctx: Context) = File(dir(ctx), "index.json")

    @Synchronized
    fun all(ctx: Context): List<Note> {
        val arr = try { JSONArray(index(ctx).readText()) } catch (e: Exception) { JSONArray() }
        val out = mutableListOf<Note>()
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            val f = File(dir(ctx), o.optString("file"))
            if (!f.exists()) continue
            val st = try { Status.valueOf(o.optString("status")) } catch (e: Exception) { Status.WAITING }
            // a send that was cut off (app closed) counts as waiting again
            out.add(Note(f, o.optLong("created"), o.optLong("duration"), if (st == Status.SENDING) Status.WAITING else st,
                o.optString("detail")))
        }
        return out.sortedByDescending { it.createdMs }
    }

    @Synchronized
    private fun save(ctx: Context, notes: List<Note>) {
        val arr = JSONArray()
        notes.forEach {
            arr.put(JSONObject().put("file", it.file.name).put("created", it.createdMs).put("duration", it.durationMs)
                .put("status", it.status.name).put("detail", it.detail))
        }
        index(ctx).writeText(arr.toString())
    }

    @Synchronized
    fun add(ctx: Context, file: File, durationMs: Long): Note {
        val n = Note(file, System.currentTimeMillis(), durationMs, Status.WAITING)
        save(ctx, all(ctx) + n)
        return n
    }

    @Synchronized
    fun update(ctx: Context, file: File, status: Status, detail: String = "") {
        save(ctx, all(ctx).map { if (it.file == file) it.copy(status = status, detail = detail) else it })
    }

    @Synchronized
    fun delete(ctx: Context, file: File) {
        save(ctx, all(ctx).filter { it.file != file })
        file.delete()
    }

    /** A new file for the next recording, named by date and time: "2026-11-24 08.15.32.m4a". */
    fun newFile(ctx: Context): File {
        val name = java.text.SimpleDateFormat("yyyy-MM-dd HH.mm.ss", java.util.Locale.US).format(java.util.Date())
        return File(dir(ctx), "$name.m4a")
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS).writeTimeout(120, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** Sends one note to ARYA (tries every laptop address). Blocking: call on a background thread. */
    fun send(ctx: Context, note: Note): Boolean {
        val prefs = Prefs(ctx)
        if (!prefs.paired) return false
        update(ctx, note.file, Status.SENDING)
        var detail = "The laptop is out of reach: it will be sent later."
        for (base in prefs.urls) {
            try {
                val name = "Voice note " + note.file.name
                val url = base.trimEnd('/') + "/api/upload?name=" + android.net.Uri.encode(name) + "&note=1"
                val req = Request.Builder().url(url)
                    .header("Authorization", "Bearer ${prefs.key}").header("X-ARYA", "1")
                    .post(note.file.asRequestBody("audio/mp4".toMediaType())).build()
                client.newCall(req).execute().use { r ->
                    val body = try { JSONObject(r.body?.string() ?: "{}") } catch (e: Exception) { JSONObject() }
                    when {
                        r.isSuccessful && body.optString("error").isEmpty() -> {
                            update(ctx, note.file, Status.SENT, "ARYA is transcribing and filing it.")
                            return true
                        }
                        r.code == 401 -> { update(ctx, note.file, Status.FAILED, "Not paired any more."); return false }
                        r.code in 400..499 || body.optString("error").isNotEmpty() -> {
                            update(ctx, note.file, Status.FAILED, body.optString("error").ifEmpty { "HTTP ${r.code}" })
                            return false
                        }
                        else -> detail = body.optString("error").ifEmpty { "The laptop had a problem (${r.code})." }
                    }
                }
            } catch (e: Exception) { /* next address */ }
        }
        update(ctx, note.file, Status.WAITING, detail)
        return false
    }

    /** Everything still waiting (or failed for a passing reason), oldest first. */
    fun sendWaiting(ctx: Context) {
        all(ctx).filter { it.status == Status.WAITING }.sortedBy { it.createdMs }.forEach { if (!send(ctx, it)) return }
    }
}
