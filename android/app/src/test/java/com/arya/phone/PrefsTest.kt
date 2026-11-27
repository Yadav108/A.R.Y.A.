package com.arya.phone

import org.junit.Assert.assertEquals
import org.junit.Test

/** The laptop's addresses the app keeps (Prefs.mergeUrls): Tailscale first, the newest Wi-Fi address next. */
class PrefsTest {
    @Test
    fun newWifiAddressIsLearnedAfterTailscale() {
        val old = listOf("http://100.74.82.21:8001", "http://192.168.0.85:8001")
        val merged = Prefs.mergeUrls(old, listOf("100.74.82.21", "192.168.178.40"), 8001)
        assertEquals(listOf("http://100.74.82.21:8001", "http://192.168.178.40:8001", "http://192.168.0.85:8001"), merged)
    }

    @Test
    fun nothingNewChangesNothing() {
        val old = listOf("http://100.74.82.21:8001", "http://192.168.0.85:8001")
        assertEquals(old, Prefs.mergeUrls(old, listOf("192.168.0.85"), 8001))
    }

    @Test
    fun keepsAtMostSix() {
        val old = listOf("http://100.74.82.21:8001") + (1..8).map { "http://10.0.0.$it:8001" }
        val merged = Prefs.mergeUrls(old, listOf("192.168.1.5"), 8001)
        assertEquals(Prefs.MAX_URLS, merged.size)
        assertEquals("http://100.74.82.21:8001", merged[0])
        assertEquals("http://192.168.1.5:8001", merged[1])
    }
}
