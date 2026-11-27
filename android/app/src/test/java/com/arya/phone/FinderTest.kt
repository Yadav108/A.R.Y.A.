package com.arya.phone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The laptop's proof and the addresses looked at (Finder.kt); the proof matches arya/server/phone.py prove(). */
class FinderTest {
    @Test
    fun proofMatchesTheLaptop() {
        // python: arya.server.phone.prove("00112233445566778899aabbccddeeff", "test-key-0123456789abcdef0123456789")
        assertEquals("5c923d08a9ac65bc935b1e74b8307ac0772d4590941e8c3b7df6b8b350bde7f2",
            Finder.proof("test-key-0123456789abcdef0123456789", "00112233445566778899aabbccddeeff"))
    }

    @Test
    fun nonceIsFreshHex() {
        val a = Finder.nonce(); val b = Finder.nonce()
        assertTrue(Regex("^[0-9a-f]{32}$").matches(a))
        assertFalse(a == b)
    }

    @Test
    fun sameComparesWholeStrings() {
        assertTrue(Finder.same("abc", "abc"))
        assertFalse(Finder.same("abc", "abd"))
        assertFalse(Finder.same("abc", "abcd"))
    }

    @Test
    fun neighboursAreTheOwnSlash24NearestFirst() {
        val n = Finder.neighbours("192.168.178.42")
        assertEquals(253, n.size)
        assertEquals(listOf("192.168.178.41", "192.168.178.43"), n.take(2).sorted())
        assertFalse("192.168.178.42" in n)
        assertFalse("192.168.178.0" in n || "192.168.178.255" in n)
        assertEquals(emptyList<String>(), Finder.neighbours("not an address"))
    }

    @Test
    fun tailscaleAddresses() {
        assertTrue(Finder.tailscale("http://100.74.82.21:8001"))
        assertFalse(Finder.tailscale("http://192.168.0.85:8001"))
    }
}
