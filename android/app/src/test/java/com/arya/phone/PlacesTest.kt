package com.arya.phone

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The phone's firing rule against the same cases as the laptop's (tests/test_places.py). */
class PlacesTest {
    private val cases = JSONObject(javaClass.classLoader!!.getResource("place_cases.json")!!.readText())

    @Test
    fun sharedMoves() {
        val r = cases.getJSONArray("reminders")
        val items = (0 until r.length()).map { r.getJSONObject(it) }.map {
            Places.Reminder(it.getLong("id"), it.getString("text"), it.getString("place"), it.getString("event"))
        }
        val moves = cases.getJSONArray("moves")
        for (i in 0 until moves.length()) {
            val m = moves.getJSONObject(i)
            val prev = if (m.isNull("prev")) null else m.getString("prev")
            val new = if (m.isNull("new")) null else m.getString("new")
            val want = m.getJSONArray("fires").let { a -> (0 until a.length()).map { a.getLong(it) } }
            assertEquals("$prev -> $new", want, Places.due(prev, new, items).map { it.id })
        }
    }

    @Test
    fun wifiNames() {
        assertEquals("Vodafone-531C", Places.clean("\"Vodafone-531C\""))
        assertNull(Places.clean("<unknown ssid>"))
        assertNull(Places.clean(""))
        assertEquals("lab", Places.labelFor("\"eduroam-lab\"", mapOf("lab" to "eduroam-lab", "home" to "Vodafone")))
        assertNull(Places.labelFor("Other", mapOf("lab" to "eduroam-lab")))
    }
}
