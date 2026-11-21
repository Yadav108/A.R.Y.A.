package com.arya.phone

/** The phone's own understanding of phone tasks, for when the laptop can't be reached (asleep, no Tailscale).
 *  The same tasks and the same answers as the laptop's parser (arya/server/phone_actions.py) — both are tested with
 *  one list of cases (src/test/resources/phone_cases.json), so they can't drift apart. Pure Kotlin: no Android. */
object PhoneCommands {
    data class Cmd(val action: String, val fields: Map<String, Any>, val say: String)

    private const val I = "(?i)"
    private const val LEAD = "^\\W*(?:(?:hey|ok|okay|please|arya|nj|now|so|and|can you|could you|would you)\\W+)*(?:please\\s+)?"

    private val ON_LAPTOP = Regex("$I\\b(?:on|from)\\s+(?:the|my)\\s+(?:laptop|computer|pc|desktop)\\b")
    private val REMIND = Regex("$I\\bremind\\b")
    private val ALARM = Regex("$I$LEAD(?:(?:set|create|make|put)\\s+(?:me\\s+)?(?:an?\\s+|the\\s+|my\\s+)?alarm\\b|" +
        "wake\\s+me(?:\\s+up)?\\b|alarm\\s+(?:for|at)\\b)")
    private val TIMER = Regex("$I$LEAD(?:(?:set|start)\\s+(?:a\\s+|an\\s+|the\\s+|my\\s+)?(?:\\w+\\s+){0,2}timer\\b|" +
        "timer\\s+(?:for\\s+)?\\d|(?:\\w+\\s+){0,2}timer\\s+(?:for\\s+)?(?:\\d|an?\\s|one|two|three|five|ten|fifteen|" +
        "twenty|thirty)|\\d+\\s*(?:-|\\s)?(?:min(?:ute)?s?|sec(?:ond)?s?|hours?)\\s+timer\\b)")
    private val BARE_TIMER = Regex("$I$LEAD(?:set|start)\\s+(?:a\\s+|the\\s+)?timer\\W*$")
    private val CALL = Regex("$I$LEAD(?:call|phone|ring|dial)\\s+(?!me\\b|it\\b|that\\b|this\\b|you\\b|back\\b|him\\b|her\\b)" +
        "(?<who>[\\w'.+ ()-]{2,40}?)(?:\\s+(?:on|using)\\s+(?<app>whatsapp|speaker))?\\W*$")
    private val NOT_A_CALLEE = Regex("$I\\b(?:an?\\s+|the\\s+)?(?:uber|taxi|cab|ambulance|function|method|api|meeting)\\b")
    private val SMS = Regex("$I$LEAD(?:(?:send\\s+(?:a\\s+)?(?:text|message|sms|whatsapp)(?:\\s+message)?\\s+to)|text|message|" +
        "(?<wa>whatsapp))\\s+(?<who>[A-Za-z][\\w'.-]*(?:\\s+(?-i:[A-Z][a-z][\\w'.-]*))?|\\+?\\d[\\d ]{5,})" +
        "(?:\\s+on\\s+(?<wa2>whatsapp))?\\s*[,:]?\\s+(?:(?:saying|that|to say|and say)\\s+)?(?<text>.+?)\\W*$")
    private val NAVIGATE = Regex("$I$LEAD(?:navigate|(?:give me\\s+|get\\s+)?directions|take me|drive me|route me|" +
        "show me the way)\\s+(?:to\\s+|home\\b)(?<to>.*?)\\W*$|$LEAD" + "how\\s+do\\s+i\\s+get\\s+to\\s+(?<to2>.+?)\\W*$")
    private val TORCH = Regex("$I\\b(?:flash\\s?light|torch)\\b")
    private val OFF = Regex("$I\\boff\\b")
    private val CAMERA = Regex("$I$LEAD(?:take\\s+(?:a\\s+|me\\s+a\\s+)?(?<kind>photo|picture|pic|selfie|video)|" +
        "(?:open|start)\\s+(?:the\\s+)?(?<cam>camera|selfie camera|front camera)|record\\s+(?:a\\s+)?video)\\b")
    private val SETTING = Regex("$I$LEAD(?:turn|switch|put)\\s+(?<state>on|off)\\s+(?:the\\s+|my\\s+)?(?<what>wi-?fi|wlan|" +
        "bluetooth|airplane mode|flight mode|mobile data|data|hotspot|location|gps|nfc)\\b|$LEAD" +
        "(?:turn|switch)\\s+(?:the\\s+|my\\s+)?(?<what2>wi-?fi|wlan|bluetooth|airplane mode|flight mode|mobile data|" +
        "hotspot|location|gps|nfc)\\s+(?<state2>on|off)\\b|$LEAD(?:open\\s+)?(?<what3>wi-?fi|bluetooth|battery|display|" +
        "sound|notification)\\s+settings\\b")
    private val VOLUME = Regex("$I$LEAD(?:(?:turn\\s+)?(?:the\\s+)?(?:volume|it|sound)\\s+(?<dir>up|down)|" +
        "(?<dir2>louder|quieter|softer)|(?<mute>mute|silence)\\s+(?:the\\s+|my\\s+)?phone|" +
        "(?:put\\s+)?(?:the\\s+|my\\s+)?phone\\s+(?:on\\s+)?(?<mode>silent|vibrate))\\W*$")
    private val PLAY = Regex("$I$LEAD" + "play\\s+(?<q>.+?)(?:\\s+on\\s+(?<app>spotify|youtube music|youtube))?\\W*$")
    private val OPEN = Regex("$I$LEAD(?:open|launch|start)\\s+(?:the\\s+|my\\s+)?(?<app>[\\w .'&+-]{2,30}?)(?:\\s+app)?\\W*$")
    private val NOT_AN_APP = Regex("$I\\b(?:tab|window|file|folder|document|pdf|link|website|page|door|mode|camera|" +
        "flash\\s?light|torch|settings?|timer|alarm)\\b|^(?:it|that|this|up|a new|the door)$")
    private val NOT_A_SONG = Regex("$I^(?:it|that|this|again|music|something|anything|the music|some music|the song|" +
        "the next song|next|previous|the video)$")

    private var awaiting = ""                    // "set a timer" -> "For how long?" -> "ten minutes"
    private var awaitingUntil = 0L

    fun parse(input: String, nowMs: Long = System.currentTimeMillis()): Cmd? {
        var t = input.trim()
        if (t.isEmpty() || t.length > 220 || ON_LAPTOP.containsMatchIn(t)) return null
        val waiting = if (nowMs < awaitingUntil) awaiting else ""
        awaiting = ""
        if (waiting == "timer" && seconds(t) != null) t = "set a timer for $t"
        else if (waiting == "alarm" && clock(t, anywhere = true) != null) t = "set an alarm at $t"

        if (ALARM.containsMatchIn(t) && !REMIND.containsMatchIn(t)) {
            val hm = clock(t) ?: return ask("alarm", "For what time?", nowMs)
            return Cmd("alarm", mapOf("hour" to hm.first, "minute" to hm.second),
                "Alarm set for %02d:%02d.".format(hm.first, hm.second))
        }
        if ((TIMER.containsMatchIn(t) || BARE_TIMER.containsMatchIn(t)) && !REMIND.containsMatchIn(t)) {
            val s = seconds(t) ?: return ask("timer", "For how long?", nowMs)
            return Cmd("timer", mapOf("seconds" to s, "label" to label(t)), "Timer set for ${spoken(s)}.")
        }
        SMS.find(t)?.let { m ->
            val who = m.groups["who"]!!.value.trim()
            if (who.lowercase() !in setOf("me", "you", "him", "her", "them")) {
                val wa = m.groups["wa"] != null || m.groups["wa2"] != null
                return Cmd("sms", mapOf("who" to who, "text" to m.groups["text"]!!.value.trim(),
                    "app" to if (wa) "whatsapp" else "sms"), "Here's the message to $who. Tap send when it looks right.")
            }
        }
        CALL.find(t)?.let { m ->
            val who = m.groups["who"]!!.value.trim()
            if (!NOT_A_CALLEE.containsMatchIn(who))
                return Cmd("call", mapOf("who" to who, "app" to (m.groups["app"]?.value?.lowercase() ?: "phone")),
                    "Calling $who.")
        }
        NAVIGATE.find(t)?.let { m ->
            val to = (m.groups["to"]?.value ?: m.groups["to2"]?.value ?: "").trim().ifEmpty { "home" }
            return Cmd("navigate", mapOf("to" to to), "Starting directions to $to.")
        }
        if (TORCH.containsMatchIn(t) && t.split(Regex("\\s+")).size <= 8) {
            val on = !OFF.containsMatchIn(t)
            return Cmd("torch", mapOf("on" to on), if (on) "Flashlight on." else "Flashlight off.")
        }
        CAMERA.find(t)?.let { m ->
            val kind = m.groups["kind"]?.value?.lowercase() ?: ""
            val video = kind == "video" || Regex("(?i)\\bvideo\\b").containsMatchIn(t)
            val selfie = kind == "selfie" || t.lowercase().contains("front") || t.lowercase().contains("selfie")
            return Cmd("camera", mapOf("video" to video, "selfie" to selfie),
                if (video) "Opening the video camera." else "Opening the camera.")
        }
        SETTING.find(t)?.let { m ->
            var what = (m.groups["what"]?.value ?: m.groups["what2"]?.value ?: m.groups["what3"]?.value ?: "")
                .lowercase().replace("wlan", "wifi")
            what = mapOf("wi-fi" to "wifi", "flight mode" to "airplane mode", "gps" to "location",
                         "data" to "mobile data")[what] ?: what
            val state = (m.groups["state"]?.value ?: m.groups["state2"]?.value ?: "").lowercase()
            return Cmd("settings", mapOf("what" to what, "state" to state),
                if (state.isEmpty()) "Here's the $what setting."
                else "Here's $what: tap to turn it $state. Android doesn't let apps switch it by themselves.")
        }
        VOLUME.find(t)?.let { m ->
            if (m.groups["mute"] != null || m.groups["mode"] != null) {
                val mode = m.groups["mode"]?.value?.lowercase() ?: "silent"
                return Cmd("volume", mapOf("dir" to mode), if (mode == "vibrate") "Phone on vibrate." else "Phone muted.")
            }
            val d = m.groups["dir"]?.value?.lowercase()
                ?: mapOf("louder" to "up", "quieter" to "down", "softer" to "down")[m.groups["dir2"]?.value?.lowercase()] ?: ""
            return Cmd("volume", mapOf("dir" to d), if (d == "up") "Louder." else "Quieter.")
        }
        PLAY.find(t)?.let { m ->
            val q0 = m.groups["q"]!!.value.trim()
            if (!NOT_A_SONG.matches(q0)) {
                val q = q0.replace(Regex("(?i)^(?:the\\s+song\\s+|the\\s+|some\\s+)"), "")
                return Cmd("play", mapOf("query" to q, "app" to (m.groups["app"]?.value?.lowercase() ?: "youtube music")),
                    "Playing $q.")
            }
        }
        OPEN.find(t)?.let { m ->
            val app = m.groups["app"]!!.value.trim()
            if (!NOT_AN_APP.containsMatchIn(app)) return Cmd("open_app", mapOf("app" to app), "Opening $app.")
        }
        return null
    }

    private fun ask(kind: String, say: String, nowMs: Long): Cmd {
        awaiting = kind
        awaitingUntil = nowMs + 60_000
        return Cmd("ask", emptyMap(), say)
    }

    // ── Numbers, clock times and durations ───────────────────────────────────

    private val WORDS = mapOf("a" to 1, "an" to 1, "one" to 1, "two" to 2, "three" to 3, "four" to 4, "five" to 5,
        "six" to 6, "seven" to 7, "eight" to 8, "nine" to 9, "ten" to 10, "eleven" to 11, "twelve" to 12,
        "fifteen" to 15, "twenty" to 20, "thirty" to 30, "forty" to 40, "fifty" to 50, "sixty" to 60, "ninety" to 90)
    private const val NUM = "(\\d+(?:[.,]\\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|" +
        "fifteen|twenty|thirty|forty|fifty|sixty|ninety)"
    private val UNIT = Regex("(?i)\\b$NUM(?:[\\s-]+(?:and\\s+)?(five))?[\\s-]*(seconds?|secs?|minutes?|mins?|hours?|hrs?)" +
        "(\\s+and\\s+a\\s+half)?\\b")
    private val HALF_HOUR = Regex("(?i)\\bhalf\\s+an?\\s+hour\\b")

    private fun value(w: String): Double? = w.replace(',', '.').toDoubleOrNull() ?: WORDS[w.lowercase()]?.toDouble()

    /** "10 minutes" -> 600; "an hour and a half" -> 5400; "1 hour 30 minutes" -> 5400. */
    fun seconds(text: String): Int? {
        var total = 0.0
        if (HALF_HOUR.containsMatchIn(text)) total += 1800.0
        val t = HALF_HOUR.replace(text, " ")                           // so "an hour" in it isn't counted again
        for (m in UNIT.findAll(t)) {
            var n = value(m.groupValues[1]) ?: continue
            if (m.groupValues[2].isNotEmpty()) n += 5                         // "forty five"
            val unit = m.groupValues[3].lowercase()
            val s = when { unit.startsWith("h") -> 3600.0; unit.startsWith("m") -> 60.0; else -> 1.0 }
            total += n * s + (if (m.groupValues[4].isNotEmpty()) s / 2 else 0.0)
        }
        return if (total > 0) total.toInt() else null
    }

    private val CLOCK = Regex("(?i)\\b(?:at|for|to)\\s+(\\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)" +
        "(?:[:.](\\d{2}))?\\s*(a\\.?\\s*m\\.?|p\\.?\\s*m\\.?|in the morning|in the evening|tonight)?(?!\\s*(?:minutes?|hours?|secs?|seconds?))\\b")
    private val CLOCK_ANY = Regex("(?i)^\\W*(?:at\\s+)?(\\d{1,2})(?:[:.](\\d{2}))?\\s*(a\\.?\\s*m\\.?|p\\.?\\s*m\\.?)?\\W*$")

    /** "at 6:45" -> (6, 45); "for 7:30 pm" -> (19, 30); "at seven" -> (7, 0). */
    fun clock(t: String, anywhere: Boolean = false): Pair<Int, Int>? {
        val m = CLOCK.findAll(t).lastOrNull() ?: (if (anywhere) CLOCK_ANY.find(t) else null) ?: return null
        var h = m.groupValues[1].toIntOrNull() ?: WORDS[m.groupValues[1].lowercase()] ?: return null
        val min = m.groupValues[2].toIntOrNull() ?: 0
        val ampm = m.groupValues[3].lowercase()
        if ((ampm.startsWith("p") || ampm.contains("evening") || ampm == "tonight") && h < 12) h += 12
        if (ampm.startsWith("a") && h == 12) h = 0
        if (h > 23 || min > 59) return null
        return h to min
    }

    private val LABEL = Regex("(?i)\\b([a-z]+)\\s+timer\\b")
    private val NOT_A_LABEL = setOf("a", "an", "the", "my", "set", "start", "make", "new", "another", "quick",
        "countdown", "and", "half", "minute", "minutes", "hour", "hours", "second", "seconds", "min", "mins", "hr", "hrs")

    private fun label(t: String): String {
        val w = LABEL.find(t)?.groupValues?.get(1)?.lowercase() ?: return ""
        return if (w in NOT_A_LABEL || w.all { it.isDigit() }) "" else w.replaceFirstChar { it.uppercase() }
    }

    private fun spoken(s: Int): String {
        val h = s / 3600; val m = (s % 3600) / 60; val sec = s % 60
        val parts = mutableListOf<String>()
        if (h > 0) parts += if (h == 1) "1 hour" else "$h hours"
        if (m > 0) parts += if (m == 1) "1 minute" else "$m minutes"
        if (sec > 0) parts += if (sec == 1) "1 second" else "$sec seconds"
        return parts.joinToString(" and ")
    }
}
