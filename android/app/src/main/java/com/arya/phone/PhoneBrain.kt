package com.arya.phone

import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/** ARYA's small brain on the phone: when the laptop is out of reach, questions the phone's own skills (Skills.kt)
 *  can't answer go to a small language model running on the phone itself (Qwen2.5-1.5B-Instruct, Apache-2.0, the
 *  same family as the laptop's) instead of the cloud. Nothing leaves the phone. Downloaded once, on Wi-Fi only, when
 *  you ask ("download the offline brain"); PhoneBrainEngine.kt runs it.
 *
 *  This file is the pure part (tested in PhoneBrainTest): the prompt, the clean-up of what the model says, the
 *  conversation kept for follow-ups, and the sentences that manage the brain. */
object PhoneBrain {
    const val FILE = "Qwen2.5-1.5B-Instruct_multi-prefill-seq_q8_ekv1280.task"
    const val URL = "https://huggingface.co/litert-community/Qwen2.5-1.5B-Instruct/resolve/main/$FILE"
    const val SIZE = 1_597_913_616L
    const val SHA256 = "8d867a7c93a6acf2892f08e0174e2f6f351ad256b7e3cfb6d6cd9c89794b42e0"
    const val SIZE_TEXT = "1.6 GB"
    const val MAX_TOKENS = 1280                     // the model's context (ekv1280): prompt + answer
    private const val HISTORY_CHARS = 1800          // ≈ 450 tokens of earlier turns, so the answer has room

    data class Turn(val user: String, val arya: String)

    /** Qwen's chat format, written out in full (the engine adds nothing of its own). */
    fun prompt(question: String, history: List<Turn>, now: ZonedDateTime, city: String?): String {
        val sb = StringBuilder()
        sb.append("<|im_start|>system\n").append(system(now, city)).append("<|im_end|>\n")
        for (t in keep(history)) {
            sb.append("<|im_start|>user\n").append(t.user.trim()).append("<|im_end|>\n")
            sb.append("<|im_start|>assistant\n").append(t.arya.trim()).append("<|im_end|>\n")
        }
        sb.append("<|im_start|>user\n").append(question.trim()).append("<|im_end|>\n")
        sb.append("<|im_start|>assistant\n")
        return sb.toString()
    }

    fun system(now: ZonedDateTime, city: String?): String {
        val day = now.format(DateTimeFormatter.ofPattern("EEEE d MMMM yyyy, HH:mm", Locale.ENGLISH))
        val where = if (city.isNullOrBlank()) "" else " The user lives in ${city.trim()}."
        return "You are ARYA, the user's personal voice assistant. Right now the laptop where ARYA normally runs is " +
            "out of reach, so you are a small model running on the phone itself, with no internet and no access to " +
            "the user's files, calendar, e-mail or memories. It is $day.$where " +
            "Your reply is spoken aloud: answer in one to three short sentences, plainly, with no markdown, lists or " +
            "emojis, and numbers said the way a person would. If you are not sure of a fact, say so briefly instead " +
            "of guessing. If the user wants something only the laptop can do, say it will need the laptop."
    }

    /** The most recent turns that fit, oldest first. */
    fun keep(history: List<Turn>): List<Turn> {
        val out = ArrayList<Turn>()
        var used = 0
        for (t in history.asReversed()) {
            used += t.user.length + t.arya.length
            if (used > HISTORY_CHARS) break
            out.add(t)
        }
        return out.asReversed()
    }

    private val END = Regex("<\\|im_end\\|>|<\\|im_start\\|>|<\\|endoftext\\|>")
    private val MARKS = Regex("[*_#`]+|^\\s*(?:[-•]|\\d+[.)])\\s+", RegexOption.MULTILINE)

    /** What the model wrote, made fit to speak: stops at an end marker, no markdown, no list bullets. */
    fun clean(raw: String): String {
        val cut = END.find(raw)?.let { raw.substring(0, it.range.first) } ?: raw
        return MARKS.replace(cut, "").replace(Regex("\\s+"), " ").trim()
    }

    /** Whole sentences ready to speak from the text so far, and the rest (still being written). */
    fun sentences(text: String): Pair<List<String>, String> {
        val out = ArrayList<String>()
        var start = 0
        val m = Regex("(?<=[.!?])\\s+(?=\\S)").findAll(text)
        for (r in m) {
            val s = text.substring(start, r.range.first).trim()
            if (s.isNotEmpty()) out.add(s)
            start = r.range.last + 1
        }
        return out to text.substring(start)
    }

    // ── Sentences that manage the brain itself ────────────────────────────────

    enum class Manage { DOWNLOAD, DELETE, STATUS }

    private const val LEAD = "^\\W*(?:(?:hey|ok|okay|please|arya|now|so|and|can you|could you|would you)\\W+)*"
    private const val BRAIN = "(?:offline|phone|local|on-device|on device)\\s+(?:brain|model|ai|a\\.i\\.?)"
    private val DOWNLOAD = Regex("(?i)$LEAD(?:download|get|install|set up|setup)\\s+(?:the\\s+|your\\s+|an?\\s+)?$BRAIN\\W*$")
    private val DELETE = Regex("(?i)$LEAD(?:delete|remove|uninstall)\\s+(?:the\\s+|your\\s+)?$BRAIN\\W*$")
    private val STATUS = Regex("(?i)$LEAD(?:is\\s+(?:the\\s+|your\\s+)?$BRAIN\\s+(?:ready|there|downloaded|installed)|" +
        "(?:how(?:'s| is)\\s+(?:the\\s+)?(?:download\\s+of\\s+the\\s+)?$BRAIN(?:\\s+download)?(?:\\s+going)?))\\W*$")

    fun manage(text: String): Manage? = when {
        DOWNLOAD.containsMatchIn(text) -> Manage.DOWNLOAD
        DELETE.containsMatchIn(text) -> Manage.DELETE
        STATUS.containsMatchIn(text) -> Manage.STATUS
        else -> null
    }

    private val CLOUD = Regex("(?i)$LEAD(?:ask|use)\\s+(?:the\\s+)?(?:cloud|groq|internet|online model)\\W*" +
        "(?:(?:about|whether|if|to|for)\\s+|:\\s*|,\\s*)?(.*)$")

    /** "ask the cloud: who won the match" -> the question (empty when only "ask the cloud"); null otherwise. */
    fun cloudRequest(text: String): String? = CLOUD.find(text)?.groupValues?.get(1)?.trim()
}
