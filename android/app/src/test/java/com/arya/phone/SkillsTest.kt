package com.arya.phone

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

class SkillsTest {
    private val now = ZonedDateTime.of(2026, 11, 22, 21, 30, 0, 0, ZoneId.of("Europe/Berlin"))

    // ── Clock ────────────────────────────────────────────────────────────────
    @Test fun clock() {
        assertEquals("It's 21:30.", Clock.answer("what time is it", now))
        assertEquals("It's 21:30.", Clock.answer("ARYA, what's the time?", now))
        assertEquals("In India it's 02:00 tomorrow.", Clock.answer("what time is it in India", now))
        assertEquals("In New York it's 15:30.", Clock.answer("time in New York", now))
        assertEquals("It's Sunday, 22 November.", Clock.answer("what's the date today", now))
        assertEquals("It's Sunday, 22 November.", Clock.answer("what day is it", now))
        assertTrue(Clock.answer("what time is it in Atlantis", now)!!.contains("don't know"))
        assertNull(Clock.answer("I had a great time today", now))
    }

    // ── Maths and conversions ────────────────────────────────────────────────
    @Test fun maths() {
        assertEquals("15 percent of 240 is 36.", Calc.answer("what is 15% of 240"))
        assertEquals("That's 84.", Calc.answer("what's 12 times 7"))
        assertEquals("That's 14.", Calc.answer("calculate 2 + 3 * 4"))
        assertEquals("That's 20.", Calc.answer("(2 + 3) * 4"))
        assertEquals("That's 1024.", Calc.answer("what is 2 to the power of 10"))
        assertEquals("That's 12.", Calc.answer("what is the square root of 144".replace("the ", "")))
        assertEquals("That's 2.5.", Calc.answer("what is 10 divided by 4"))
        assertEquals("That has no answer: it divides by zero.", Calc.answer("what is 5 divided by 0"))
        assertNull(Calc.answer("what is the capital of Peru"))
        assertNull(Calc.answer("what is PID control"))
        assertNull(Calc.answer("I have 2 brothers"))
    }

    @Test fun conversions() {
        assertEquals("5 miles is 8.05 kilometres.", Calc.answer("5 miles in km"))
        assertEquals("30 degrees Celsius is 86 degrees Fahrenheit.", Calc.answer("convert 30 celsius to fahrenheit"))
        assertEquals("1 inch is 2.54 centimetres.", Calc.answer("how many cm in an inch"))
        assertEquals("2 kilograms is 4.41 pounds.", Calc.answer("what is 2 kg in pounds"))
        assertEquals("100 km/h is 62.14 mph.", Calc.answer("100 km/h in mph"))
        assertNull(Calc.answer("5 miles in kilograms"))
    }

    // ── Notes, to-dos, reminders ─────────────────────────────────────────────
    @Test fun notes() {
        assertEquals(Notes.Cmd.Capture("remember to buy milk"), Notes.parse("remember to buy milk", now))
        assertEquals(Notes.Cmd.Capture("call the landlord"), Notes.parse("add call the landlord to my to-do list", now))
        assertEquals(Notes.Cmd.Capture("the parking spot is B12"), Notes.parse("take a note: the parking spot is B12", now))
        assertEquals(Notes.Cmd.List, Notes.parse("what's on my list", now))
        assertNull(Notes.parse("what is a note in music", now))
        assertNull(Notes.parse("I remember that day", now))
    }

    @Test fun reminders() {
        val r1 = Notes.parse("remind me in 20 minutes to check the oven", now) as Notes.Cmd.Remind
        assertEquals(now.plusMinutes(20).toInstant().toEpochMilli(), r1.atMs)
        assertEquals("check the oven", r1.text)
        val r2 = Notes.parse("remind me at 6 to call Mom", now) as Notes.Cmd.Remind    // 6:00 already passed: tomorrow
        assertEquals(now.plusDays(1).withHour(6).withMinute(0).toInstant().toEpochMilli(), r2.atMs)
        assertEquals("call Mom", r2.text)
        val r3 = Notes.parse("remind me to submit the report at 11:15 pm", now) as Notes.Cmd.Remind
        assertEquals(now.withHour(23).withMinute(15).toInstant().toEpochMilli(), r3.atMs)
        assertEquals("submit the report", r3.text)
        val r4 = Notes.parse("remind me tomorrow at 9 am about the dentist", now) as Notes.Cmd.Remind
        assertEquals(now.plusDays(1).withHour(9).withMinute(0).toInstant().toEpochMilli(), r4.atMs)
        assertEquals("the dentist", r4.text)
        assertNull(Notes.parse("remind me to call Mom", now))               // no time: the laptop's job
    }

    // ── Weather ──────────────────────────────────────────────────────────────
    @Test fun weatherQuestions() {
        assertEquals(WeatherSkill.Query(null, false, false), WeatherSkill.parse("what's the weather like"))
        assertEquals(WeatherSkill.Query("Berlin", true, false), WeatherSkill.parse("weather in Berlin tomorrow"))
        assertEquals(WeatherSkill.Query(null, false, true), WeatherSkill.parse("do I need an umbrella"))
        assertEquals(WeatherSkill.Query(null, true, true), WeatherSkill.parse("will it rain tomorrow"))
        assertNull(WeatherSkill.parse("what is the temperature of the sun"))
    }

    @Test fun weatherWords() {
        val f = JSONObject("""{"current":{"temperature_2m":4.3,"weather_code":3},
            "daily":{"weather_code":[61,0],"temperature_2m_max":[6.4,9.1],"temperature_2m_min":[1.6,2.2],
            "precipitation_probability_max":[70,5]}}""")
        assertEquals("It's 4 degrees and overcast in Schweinfurt. Today: 2 to 6 degrees, 70 percent chance of rain. " +
            "Take an umbrella.", WeatherSkill.speak(f, "Schweinfurt", WeatherSkill.Query(null, false, true)))
        assertEquals("Tomorrow in Berlin: clear, 2 to 9 degrees, 5 percent chance of rain.",
            WeatherSkill.speak(f, "Berlin", WeatherSkill.Query("Berlin", true, false)))
    }

    // ── The cloud question ───────────────────────────────────────────────────
    @Test fun cloud() {
        val body = JSONObject(CloudQuestion.body("mail aryan.test@example.org or call +49 176 1234567 about PID", "openai/gpt-oss-20b"))
        val q = body.getJSONArray("messages").getJSONObject(1).getString("content")
        assertEquals("mail [e-mail] or call [number] about PID", q)
        assertEquals("low", body.getString("reasoning_effort"))
        assertEquals("Lima is the capital.", CloudQuestion.answer(
            """{"choices":[{"message":{"role":"assistant","content":"**Lima** is the capital."}}]}"""))
        assertNull(CloudQuestion.answer("""{"error":{"message":"rate limit"}}"""))
    }

    // ── Routing ──────────────────────────────────────────────────────────────
    @Test fun brain() {
        fun kind(t: String) = OfflineBrain.decide(t, now)::class.simpleName
        assertEquals("Task", kind("set a timer for 5 minutes"))
        assertEquals("Note", kind("remind me in 5 minutes to stir the soup"))
        assertEquals("Note", kind("remember to buy milk"))
        assertEquals("Say", kind("what time is it"))
        assertEquals("Say", kind("what's 12 times 7"))
        assertEquals("Weather", kind("will it rain today"))
        assertEquals("AskCloud", kind("explain PID control"))
        assertEquals("AskCloud", kind("what's the capital of Peru"))
    }
}
