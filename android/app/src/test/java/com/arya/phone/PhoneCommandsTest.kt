package com.arya.phone

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/** The phone's offline parser against the same cases as the laptop's (tests/test_phone_actions.py). */
class PhoneCommandsTest {
    private val cases = JSONObject(javaClass.classLoader!!.getResource("phone_cases.json")!!.readText())

    @Test
    fun sharedTasks() {
        val tasks = cases.getJSONArray("tasks")
        for (i in 0 until tasks.length()) {
            val case = tasks.getJSONObject(i)
            val say = case.getString("say")
            val cmd = PhoneCommands.parse(say)
            assertNotNull(say, cmd)
            val want = case.getJSONObject("want")
            for (k in want.keys()) {
                val got: Any? = if (k == "action") cmd!!.action else cmd!!.fields[k]
                assertEquals("$say: $k", want.get(k).toString(), got.toString())
            }
        }
    }

    @Test
    fun sharedNotTasks() {
        val none = cases.getJSONArray("none")
        for (i in 0 until none.length()) assertNull(none.getString(i), PhoneCommands.parse(none.getString(i)))
    }

    @Test
    fun asksThenTakesTheAnswer() {
        val now = 1_000_000L
        assertEquals("ask", PhoneCommands.parse("set a timer", now)!!.action)
        assertEquals(600, PhoneCommands.parse("ten minutes", now + 5_000)!!.fields["seconds"])
        assertEquals("ask", PhoneCommands.parse("set an alarm", now)!!.action)
        val alarm = PhoneCommands.parse("6:45", now + 5_000)!!
        assertEquals(6, alarm.fields["hour"]); assertEquals(45, alarm.fields["minute"])
        assertNull(PhoneCommands.parse("ten minutes", now + 10_000))
    }

    @Test
    fun durationsAndClockTimes() {
        assertEquals(5400, PhoneCommands.seconds("1 hour 30 minutes"))
        assertEquals(1800, PhoneCommands.seconds("half an hour"))
        assertEquals(2700, PhoneCommands.seconds("forty five minutes"))
        assertEquals(90, PhoneCommands.seconds("90 seconds"))
        assertEquals(19 to 0, PhoneCommands.clock("wake me at 7 in the evening"))
        assertEquals(0 to 15, PhoneCommands.clock("alarm at 12:15 am"))
        assertNull(PhoneCommands.clock("timer for 10 minutes"))
    }
}
