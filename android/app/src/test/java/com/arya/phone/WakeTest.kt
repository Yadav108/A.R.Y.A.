package com.arya.phone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class WakeTest {
    @Test
    fun magicPacketIsSixFfThenTheMacSixteenTimes() {
        val p = Wake.magicPacket("80:38:FB:8A:6C:D6")!!
        assertEquals(102, p.size)
        for (i in 0 until 6) assertEquals(0xFF.toByte(), p[i])
        val mac = byteArrayOf(0x80.toByte(), 0x38, 0xFB.toByte(), 0x8A.toByte(), 0x6C, 0xD6.toByte())
        for (i in 0 until 16) for (j in 0 until 6) assertEquals(mac[j], p[6 + i * 6 + j])
    }

    @Test
    fun dashesWorkToo() {
        assertEquals(102, Wake.magicPacket("80-38-fb-8a-6c-d6")!!.size)
    }

    @Test
    fun junkIsRefused() {
        assertNull(Wake.magicPacket(""))
        assertNull(Wake.magicPacket("80:38:FB:8A:6C"))
        assertNull(Wake.magicPacket("80:38:FB:8A:6C:ZZ"))
        assertNull(Wake.magicPacket("800:38:FB:8A:6C:D6"))
    }
}
