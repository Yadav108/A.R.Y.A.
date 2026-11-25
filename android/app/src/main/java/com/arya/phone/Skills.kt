package com.arya.phone

import org.json.JSONArray
import org.json.JSONObject
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.abs
import kotlin.math.pow
import kotlin.math.roundToLong
import kotlin.math.sqrt

/** What the phone can do on its own when the laptop is out of reach — no AI, all on the phone. Pure Kotlin
 *  (tested in SkillsTest): the clock, maths and conversions, notes / to-dos / reminders, weather wording, and the
 *  question that may go to the cloud (only after a yes). */

private const val I = "(?i)"
private const val LEAD = "^\\W*(?:(?:hey|ok|okay|please|arya|nj|now|so|and|can you|could you|would you|tell me)\\W+)*" +
    "(?:please\\s+)?"

private fun num(x: Double): String {
    if (abs(x - x.roundToLong()) < 1e-9 && abs(x) < 1e15) return x.roundToLong().toString()
    val s = String.format(Locale.US, if (abs(x) >= 100) "%.1f" else if (abs(x) >= 1) "%.2f" else "%.4f", x)
    return s.trimEnd('0').trimEnd('.')
}

// ── The clock ─────────────────────────────────────────────────────────────────

object Clock {
    private val ZONES = linkedMapOf(
        "india|delhi|new delhi|mumbai|bombay|kolkata|calcutta|bangalore|bengaluru|chennai|hyderabad|pune|lucknow|" +
            "jaipur|patna|ahmedabad" to "Asia/Kolkata",
        "germany|berlin|munich|münchen|schweinfurt|frankfurt|hamburg|cologne|köln|jülich|juelich|aachen|würzburg|" +
            "wurzburg" to "Europe/Berlin",
        "london|uk|england|britain|united kingdom" to "Europe/London",
        "paris|france" to "Europe/Paris",
        "new york|nyc|boston|washington|us east|east coast" to "America/New_York",
        "chicago" to "America/Chicago",
        "california|san francisco|los angeles|seattle|us west|west coast" to "America/Los_Angeles",
        "toronto|canada" to "America/Toronto",
        "tokyo|japan" to "Asia/Tokyo",
        "beijing|shanghai|china" to "Asia/Shanghai",
        "singapore" to "Asia/Singapore",
        "dubai|uae|abu dhabi" to "Asia/Dubai",
        "nepal|kathmandu" to "Asia/Kathmandu",
        "sydney|melbourne|australia" to "Australia/Sydney",
        "moscow|russia" to "Europe/Moscow",
        "utc|gmt" to "UTC",
    )
    private val TIME = Regex("$I$LEAD(?:what(?:'s| is)\\s+the\\s+time|what\\s+time\\s+is\\s+it|(?:the\\s+)?time\\s+now|" +
        "(?:the\\s+)?current\\s+time|(?:the\\s+)?time(?=\\s+(?:in|at)\\b))(?:\\s+(?:now|right now))?" +
        "(?:\\s+(?:in|at)\\s+(?<place>[a-zäöü .]+?))?(?:\\s+(?:now|right now))?\\W*$")
    private val DATE = Regex("$I$LEAD(?:what(?:'s| is)\\s+(?:the\\s+|today'?s\\s+)?date(?:\\s+today)?|what\\s+day\\s+is\\s+" +
        "(?:it|today)|which\\s+day\\s+is\\s+(?:it|today)|today'?s\\s+date)\\W*$")

    fun answer(text: String, now: ZonedDateTime): String? {
        DATE.find(text)?.let {
            return "It's " + now.format(DateTimeFormatter.ofPattern("EEEE, d MMMM", Locale.ENGLISH)) + "."
        }
        val m = TIME.find(text) ?: return null
        val place = m.groups["place"]?.value?.trim()?.lowercase()
        if (place.isNullOrEmpty()) return "It's " + now.format(DateTimeFormatter.ofPattern("HH:mm")) + "."
        val zone = ZONES.entries.firstOrNull { (names, _) -> names.split("|").any { it == place } }?.value
            ?: return "I don't know the time zone of $place without the laptop."
        val there = now.withZoneSameInstant(ZoneId.of(zone))
        val day = when (there.toLocalDate().compareTo(now.toLocalDate())) { 0 -> ""; in 1..9 -> " tomorrow"; else -> " yesterday" }
        val name = place.split(" ").joinToString(" ") { w -> w.replaceFirstChar { it.uppercase() } }
            .replace("Uk", "the UK").replace("Uae", "the UAE").replace("Nyc", "New York")
        return "In $name it's " +
            there.format(DateTimeFormatter.ofPattern("HH:mm")) + "$day."
    }
}

// ── Maths and conversions ─────────────────────────────────────────────────────

object Calc {
    private val ASK = Regex("$I$LEAD(?:what(?:'s| is)|calculate|compute|how much is|work out)\\s+(?<e>.+?)\\W*$")
    private val PERCENT = Regex("$I(?<p>-?\\d+(?:[.,]\\d+)?)\\s*(?:%|percent|per cent)\\s+of\\s+(?<n>-?\\d+(?:[.,]\\d+)?)")
    private val CONVERT = Regex("$I$LEAD(?:convert\\s+|what(?:'s| is)\\s+)?(?<n>-?\\d+(?:[.,]\\d+)?|(?:an|a|one)\\b)\\s*(?<from>[a-z°/ ]+?)" +
        "\\s+(?:in|to|into|as)\\s+(?<to>[a-z°/ ]+?)\\W*$")
    private val HOW_MANY = Regex("$I$LEAD" + "how\\s+many\\s+(?<to>[a-z°/ ]+?)\\s+(?:are\\s+)?(?:in|is|make)\\s+(?:(?:an|a|one)\\b|(?<n>\\d+(?:[.,]\\d+)?))\\s*(?<from>[a-z°/ ]+?)\\W*$")

    private data class Unit(val dim: String, val factor: Double)
    private val UNITS: Map<String, Unit> = buildMap {
        fun add(names: String, dim: String, f: Double) = names.split("|").forEach { put(it, Unit(dim, f)) }
        add("mm|millimeter|millimeters|millimetre|millimetres", "len", 0.001)
        add("cm|centimeter|centimeters|centimetre|centimetres", "len", 0.01)
        add("m|meter|meters|metre|metres", "len", 1.0)
        add("km|kilometer|kilometers|kilometre|kilometres", "len", 1000.0)
        add("inch|inches|in", "len", 0.0254)
        add("foot|feet|ft", "len", 0.3048)
        add("yard|yards|yd", "len", 0.9144)
        add("mile|miles|mi", "len", 1609.344)
        add("g|gram|grams|gramme|grammes", "mass", 0.001)
        add("kg|kilo|kilos|kilogram|kilograms", "mass", 1.0)
        add("lb|lbs|pound|pounds", "mass", 0.45359237)
        add("oz|ounce|ounces", "mass", 0.028349523125)
        add("ml|milliliter|milliliters|millilitre|millilitres", "vol", 0.001)
        add("l|liter|liters|litre|litres", "vol", 1.0)
        add("gallon|gallons", "vol", 3.785411784)
        add("cup|cups", "vol", 0.24)
        add("km/h|kmh|kph|kilometers per hour|kilometres per hour", "speed", 1 / 3.6)
        add("mph|miles per hour", "speed", 0.44704)
        add("m/s|meters per second|metres per second", "speed", 1.0)
        add("c|°c|celsius|degrees celsius|degree celsius", "temp", 0.0)
        add("f|°f|fahrenheit|degrees fahrenheit|degree fahrenheit", "temp", 1.0)
        add("k|kelvin", "temp", 2.0)
        add("second|seconds|sec|secs", "time", 1.0)
        add("minute|minutes|min|mins", "time", 60.0)
        add("hour|hours|hr|hrs", "time", 3600.0)
        add("day|days", "time", 86400.0)
        add("week|weeks", "time", 604800.0)
    }
    private val SPOKEN = mapOf("km" to "kilometres", "m" to "metres", "cm" to "centimetres", "mm" to "millimetres",
        "kg" to "kilograms", "g" to "grams", "lb" to "pounds", "lbs" to "pounds", "oz" to "ounces", "l" to "litres",
        "ml" to "millilitres", "c" to "degrees Celsius", "f" to "degrees Fahrenheit", "k" to "kelvin", "mi" to "miles",
        "celsius" to "degrees Celsius", "°c" to "degrees Celsius", "degree celsius" to "degrees Celsius",
        "fahrenheit" to "degrees Fahrenheit", "°f" to "degrees Fahrenheit", "degree fahrenheit" to "degrees Fahrenheit",
        "ft" to "feet", "in" to "inches", "kmh" to "km/h", "kph" to "km/h")

    fun answer(text: String): String? = percent(text) ?: convert(text) ?: arithmetic(text)

    private fun d(s: String) = when (s.lowercase()) { "a", "an", "one" -> 1.0; else -> s.replace(',', '.').toDouble() }

    private fun percent(text: String): String? {
        val m = PERCENT.find(text) ?: return null
        val p = d(m.groups["p"]!!.value); val n = d(m.groups["n"]!!.value)
        return "${num(p)} percent of ${num(n)} is ${num(p / 100 * n)}."
    }

    private fun convert(text: String): String? {
        val m = CONVERT.find(text) ?: HOW_MANY.find(text) ?: return null
        val from = m.groups["from"]!!.value.trim().lowercase()
        val to = m.groups["to"]!!.value.trim().lowercase()
        val n = m.groups["n"]?.value?.let { d(it) } ?: 1.0
        val a = UNITS[from] ?: return null
        val b = UNITS[to] ?: return null
        if (a.dim != b.dim) return null
        val v = if (a.dim == "temp") temp(n, a.factor, b.factor) else n * a.factor / b.factor
        return "${num(n)} ${SPOKEN[from] ?: from} is ${num(v)} ${SPOKEN[to] ?: to}."
    }

    private fun temp(v: Double, from: Double, to: Double): Double {
        val c = when (from) { 0.0 -> v; 1.0 -> (v - 32) * 5 / 9; else -> v - 273.15 }
        return when (to) { 0.0 -> c; 1.0 -> c * 9 / 5 + 32; else -> c + 273.15 }
    }

    private fun arithmetic(text: String): String? {
        val m = ASK.find(text)
        val raw = (m?.groups?.get("e")?.value ?: text).trim()
        var e = " " + raw.lowercase() + " "
        listOf("plus" to "+", "minus" to "-", "times" to "*", "multiplied by" to "*", "x" to "*", "into" to "*",
               "divided by" to "/", "over" to "/", "to the power of" to "^", "power" to "^").forEach { (w, s) ->
            e = e.replace(Regex("\\s$w\\s"), " $s ")
        }
        e = e.replace(Regex("\\bsquare root of\\s+"), "√").replace(Regex("\\s+squared\\b"), "^2")
            .replace(Regex("\\s+cubed\\b"), "^3").replace('×', '*').replace('÷', '/').trim()
        if (!Regex("^[\\d.,+\\-*/^()√ ]+$").matches(e)) return null
        if (!Regex("\\d").containsMatchIn(e) || !Regex("[+\\-*/^√]").containsMatchIn(e.trimStart('-'))) return null
        if (m == null && !Regex("^\\W*[\\d(√]").containsMatchIn(raw)) return null
        val v = try { Expr(e.replace(",", ".").replace(" ", "")).parse() } catch (ex: Exception) { return null }
        if (v.isNaN() || v.isInfinite()) return "That has no answer: it divides by zero."
        return "That's ${num(v)}."
    }

    /** + - * / ^ √ ( ) with the usual precedence. */
    private class Expr(val s: String) {
        var i = 0
        fun parse(): Double { val v = sum(); if (i != s.length) throw IllegalArgumentException(); return v }
        fun sum(): Double {
            var v = product()
            while (i < s.length && (s[i] == '+' || s[i] == '-')) { val op = s[i++]; val r = product(); v = if (op == '+') v + r else v - r }
            return v
        }
        fun product(): Double {
            var v = power()
            while (i < s.length && (s[i] == '*' || s[i] == '/')) { val op = s[i++]; val r = power(); v = if (op == '*') v * r else v / r }
            return v
        }
        fun power(): Double { val b = unary(); return if (i < s.length && s[i] == '^') { i++; b.pow(power()) } else b }
        fun unary(): Double = when {
            i < s.length && s[i] == '-' -> { i++; -unary() }
            i < s.length && s[i] == '√' -> { i++; sqrt(unary()) }
            else -> atom()
        }
        fun atom(): Double {
            if (i < s.length && s[i] == '(') { i++; val v = sum(); if (s.getOrNull(i) != ')') throw IllegalArgumentException(); i++; return v }
            val start = i
            while (i < s.length && (s[i].isDigit() || s[i] == '.')) i++
            return s.substring(start, i).toDouble()
        }
    }
}

// ── Notes, to-dos, reminders ──────────────────────────────────────────────────

object Notes {
    sealed class Cmd {
        data class Capture(val text: String) : Cmd()
        data class Remind(val atMs: Long, val text: String) : Cmd()
        object List : Cmd()
    }

    private val CAPTURE = Regex("$I$LEAD(?:(?:remember|don'?t\\s+forget)\\s+(?<rem>to|that)\\s+|(?:take|make)\\s+a\\s+note(?:\\s+that)?[:,]?\\s+|" +
        "note(?:\\s+that)?[:,]\\s*|note\\s+that\\s+|(?:add|put)\\s+(?<add>.+?)\\s+(?:to|on)\\s+my\\s+(?:to-?\\s?do|todo|shopping|" +
        "work)\\s+list\\W*$|to-?\\s?do[:,]\\s*|idea[:,]\\s*)(?<text>.*?)\\W*$")
    private val REMIND = Regex("$I$LEAD" + "remind\\s+me\\s+(?<rest>.+?)\\W*$")
    private val LIST = Regex("$I$LEAD(?:what(?:'s| is| are)\\s+(?:on\\s+)?my\\s+(?:list|to-?\\s?dos?|notes|reminders)|" +
        "(?:read|show)\\s+(?:me\\s+)?my\\s+(?:list|to-?\\s?dos?|notes)|what\\s+did\\s+i\\s+(?:note|save))\\W*$")
    private val IN = Regex("$I\\bin\\s+(?:an\\b|a\\b|one\\b|\\d+(?:[.,]\\d+)?|two|three|five|ten|fifteen|twenty|thirty|forty|fifty|sixty)" +
        "\\s*(?:and\\s+a\\s+half\\s+)?(?:sec(?:ond)?s?|min(?:ute)?s?|hours?|hrs?)\\b(?:\\s+and\\s+a\\s+half)?")
    private val AT = Regex("$I\\b(?:at|by)\\s+(?:\\d{1,2}(?:[:.]\\d{2})?\\s*(?:a\\.?\\s*m\\.?|p\\.?\\s*m\\.?)?|noon|midnight)" +
        "(?:\\s+(?:in the morning|in the evening|tonight))?")
    private val TOMORROW = Regex("$I\\btomorrow\\b")

    fun parse(text: String, now: ZonedDateTime): Cmd? {
        if (LIST.containsMatchIn(text)) return Cmd.List
        REMIND.find(text)?.let { m -> return remind(m.groups["rest"]!!.value, now) }
        val m = CAPTURE.find(text) ?: return null
        val t = (m.groups["add"]?.value ?: m.groups["text"]?.value ?: "").trim()
        if (t.isEmpty()) return null
        val prefix = if (m.groups["rem"]?.value?.lowercase() == "to") "remember to " else ""
        return Cmd.Capture(prefix + t)
    }

    private fun remind(rest: String, now: ZonedDateTime): Cmd? {
        var whenMs: Long? = null
        var what = rest
        IN.find(rest)?.let { m ->
            val s = PhoneCommands.seconds(m.value) ?: return@let
            whenMs = now.toInstant().toEpochMilli() + s * 1000L
            what = what.replace(m.value, " ")
        }
        if (whenMs == null) AT.find(rest)?.let { m ->
            val v = m.value.lowercase()
            val hm = when {
                "noon" in v -> 12 to 0
                "midnight" in v -> 0 to 0
                else -> PhoneCommands.clock(v.replace(Regex("^by\\b"), "at"))
            } ?: return@let
            var t = now.withHour(hm.first).withMinute(hm.second).withSecond(0).withNano(0)
            if (TOMORROW.containsMatchIn(rest)) t = t.plusDays(1) else if (!t.isAfter(now)) t = t.plusDays(1)
            whenMs = t.toInstant().toEpochMilli()
            what = what.replace(m.value, " ")
        }
        val at = whenMs ?: return null                                 // "remind me" without a time: the laptop's job
        what = what.replace(TOMORROW, " ").replace(Regex("$I^\\s*(?:to|about|that|of)\\s+"), "")
            .replace(Regex("\\s+"), " ").trim().trim(',', '.')
        what = what.replace(Regex("$I^(?:to|about|that)\\s+"), "")
        return Cmd.Remind(at, what.ifEmpty { "your reminder" })
    }
}

// ── Weather (Open-Meteo, free and keyless; fetched in OfflineServices.kt) ─────────────────────────────────────

object WeatherSkill {
    data class Query(val place: String?, val tomorrow: Boolean, val rainQuestion: Boolean)

    private val ASK = Regex("$I\\b(?:weather|forecast|temperature\\s+(?:outside|today|tomorrow|now|in\\b)|how\\s+(?:hot|cold|warm)\\s+is\\s+it|" +
        "(?:is\\s+it|will\\s+it)\\s+(?:going\\s+to\\s+)?(?:rain|snow|be\\s+(?:sunny|cold|hot|warm))|" +
        "(?:do|should)\\s+i\\s+(?:need|take|bring)\\s+(?:an?\\s+)?(?:umbrella|jacket|coat))\\b")
    private val PLACE = Regex("$I\\b(?:in|at|for)\\s+(?<p>[a-zäöüß][a-zäöüß .'-]{1,40}?)(?:\\s+(?:today|tomorrow|now|right now|this week))?\\W*$")
    private val NOT_PLACES = setOf("the morning", "the evening", "the afternoon", "today", "tomorrow", "general", "detail")

    fun parse(text: String): Query? {
        if (!ASK.containsMatchIn(text)) return null
        val p = PLACE.find(text)?.groups?.get("p")?.value?.trim()?.takeIf { it.lowercase() !in NOT_PLACES }
        val rain = Regex("$I\\b(?:rain|umbrella|snow)\\b").containsMatchIn(text)
        return Query(p, Regex("$I\\btomorrow\\b").containsMatchIn(text), rain)
    }

    /** WMO weather codes in words. */
    fun words(code: Int): String = when (code) {
        0 -> "clear"; 1 -> "mostly clear"; 2 -> "partly cloudy"; 3 -> "overcast"; 45, 48 -> "foggy"
        51, 53, 55, 56, 57 -> "drizzly"; 61, 63, 66 -> "rainy"; 65, 67 -> "pouring"; 71, 73, 75, 77 -> "snowy"
        80, 81, 82 -> "showery"; 85, 86 -> "snow showers"; 95, 96, 99 -> "stormy"; else -> "changeable"
    }

    /** One spoken sentence or two from an Open-Meteo forecast (current + daily). */
    fun speak(forecast: JSONObject, place: String, q: Query): String {
        val daily = forecast.getJSONObject("daily")
        val i = if (q.tomorrow) 1 else 0
        val hi = daily.getJSONArray("temperature_2m_max").getDouble(i).roundToLong()
        val lo = daily.getJSONArray("temperature_2m_min").getDouble(i).roundToLong()
        val rain = daily.optJSONArray("precipitation_probability_max")?.optInt(i, -1) ?: -1
        val code = daily.getJSONArray("weather_code").getInt(i)
        val rainText = if (rain >= 0) ", $rain percent chance of rain" else ""
        if (q.tomorrow) return "Tomorrow in $place: ${words(code)}, $lo to $hi degrees$rainText."
        val cur = forecast.optJSONObject("current")
        val now = cur?.let { "It's ${it.getDouble("temperature_2m").roundToLong()} degrees and ${words(it.getInt("weather_code"))} in $place. " } ?: ""
        val umbrella = if (q.rainQuestion) when {
            rain >= 50 -> " Take an umbrella."
            rain in 0..20 -> " You won't need an umbrella."
            else -> " Maybe take an umbrella."
        } else ""
        return now + "Today: $lo to $hi degrees$rainText.$umbrella"
    }

    fun forecastUrl(lat: Double, lon: Double) = "https://api.open-meteo.com/v1/forecast?latitude=$lat&longitude=$lon" +
        "&current=temperature_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min," +
        "precipitation_probability_max&timezone=auto&forecast_days=2"

    fun geocodeUrl(place: String) = "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&name=" +
        java.net.URLEncoder.encode(place, "UTF-8")
}

// ── A question for the cloud (only after a yes) ───────────────────────────────

object CloudQuestion {
    const val URL = "https://api.groq.com/openai/v1/chat/completions"
    private val EMAIL = Regex("[\\w.+-]+@[\\w-]+\\.[\\w.]+")
    private val NUMBER = Regex("\\+?\\d[\\d ()/-]{6,}\\d")

    /** Only the question leaves the phone — no name, memories or history; e-mail addresses and phone numbers in
     *  it are replaced first (the same rule as the laptop's cloud requests). */
    fun redact(q: String) = NUMBER.replace(EMAIL.replace(q, "[e-mail]"), "[number]")

    fun body(question: String, model: String): String = JSONObject()
        .put("model", model)
        .put("temperature", 0.4)
        .put("max_tokens", 400)
        .put("messages", JSONArray()
            .put(JSONObject().put("role", "system").put("content",
                "You are a voice assistant. Your reply is spoken aloud: answer in one to four short sentences, " +
                "no markdown, lists or emojis, numbers said the way a person would."))
            .put(JSONObject().put("role", "user").put("content", redact(question))))
        .apply { if (model.startsWith("openai/gpt-oss")) put("reasoning_effort", "low") }
        .toString()

    fun answer(response: String): String? = try {
        JSONObject(response).getJSONArray("choices").getJSONObject(0).getJSONObject("message")
            .optString("content").trim().replace(Regex("[*_#`]+"), "").ifEmpty { null }
    } catch (e: Exception) { null }
}

// ── What to do with a sentence when the laptop is out of reach ────────────────

object OfflineBrain {
    sealed class Decision {
        data class Task(val cmd: PhoneCommands.Cmd) : Decision()
        data class Say(val text: String) : Decision()
        data class Weather(val q: WeatherSkill.Query) : Decision()
        data class Note(val cmd: Notes.Cmd) : Decision()
        data class AskCloud(val question: String) : Decision()
    }

    fun decide(text: String, now: ZonedDateTime, nowMs: Long = now.toInstant().toEpochMilli()): Decision {
        Notes.parse(text, now)?.let { return Decision.Note(it) }      // "remind me in 20 minutes …" before timers
        PhoneCommands.parse(text, nowMs)?.let { return Decision.Task(it) }
        Clock.answer(text, now)?.let { return Decision.Say(it) }
        WeatherSkill.parse(text)?.let { return Decision.Weather(it) }
        Calc.answer(text)?.let { return Decision.Say(it) }
        return Decision.AskCloud(text)
    }
}
