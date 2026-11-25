package com.arya.phone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

/** The pure part of the offline brain: prompt, clean-up, sentences, and the sentences that manage it. */
class PhoneBrainTest {
    private val now = ZonedDateTime.of(2026, 11, 25, 14, 5, 0, 0, ZoneId.of("Europe/Berlin"))

    @Test
    fun promptIsQwenChatFormatEndingWithTheAssistantTurn() {
        val p = PhoneBrain.prompt("What is entropy?", listOf(PhoneBrain.Turn("hi", "Hello!")), now, "Jülich")
        assertTrue(p.startsWith("<|im_start|>system\n"))
        assertTrue(p.contains("Wednesday 25 November 2026, 14:05"))
        assertTrue(p.contains("lives in Jülich"))
        assertTrue(p.contains("<|im_start|>user\nhi<|im_end|>\n<|im_start|>assistant\nHello!<|im_end|>\n"))
        assertTrue(p.endsWith("<|im_start|>user\nWhat is entropy?<|im_end|>\n<|im_start|>assistant\n"))
    }

    @Test
    fun onlyRecentHistoryIsKept() {
        val long = (1..20).map { PhoneBrain.Turn("question $it " + "x".repeat(100), "answer $it " + "y".repeat(100)) }
        val kept = PhoneBrain.keep(long)
        assertTrue(kept.size in 1..9)
        assertEquals(long.last(), kept.last())                     // newest kept, order kept
        assertEquals(long.takeLast(kept.size), kept)
    }

    @Test
    fun cleanStopsAtEndMarkerAndDropsMarkdown() {
        assertEquals("Entropy measures disorder.", PhoneBrain.clean("**Entropy** measures disorder.<|im_end|>\n<|im_start|>user"))
        assertEquals("First thing. Second thing.", PhoneBrain.clean("- First thing.\n- Second thing."))
        assertEquals("", PhoneBrain.clean("<|im_end|>"))
    }

    @Test
    fun sentencesAreHandedOutWhenFinished() {
        val (whole, rest) = PhoneBrain.sentences("It is three in Delhi. The sun is out! And then")
        assertEquals(listOf("It is three in Delhi.", "The sun is out!"), whole)
        assertEquals("And then", rest)
        assertEquals(emptyList<String>() to "No end yet", PhoneBrain.sentences("No end yet"))
    }

    @Test
    fun managingTheBrain() {
        assertEquals(PhoneBrain.Manage.DOWNLOAD, PhoneBrain.manage("download the offline brain"))
        assertEquals(PhoneBrain.Manage.DOWNLOAD, PhoneBrain.manage("Hey ARYA, set up the phone AI"))
        assertEquals(PhoneBrain.Manage.DELETE, PhoneBrain.manage("delete the phone model"))
        assertEquals(PhoneBrain.Manage.STATUS, PhoneBrain.manage("is the offline brain ready?"))
        assertEquals(PhoneBrain.Manage.STATUS, PhoneBrain.manage("how is the offline brain download going"))
        assertNull(PhoneBrain.manage("download the lecture"))
        assertNull(PhoneBrain.manage("what is a phone model"))
    }

    @Test
    fun askingTheCloudByName() {
        assertEquals("who won the match", PhoneBrain.cloudRequest("ask the cloud: who won the match"))
        assertEquals("the weather in Rome", PhoneBrain.cloudRequest("ask the cloud about the weather in Rome"))
        assertEquals("", PhoneBrain.cloudRequest("ask the cloud"))
        assertNull(PhoneBrain.cloudRequest("what is the cloud"))
    }
}
