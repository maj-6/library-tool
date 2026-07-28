package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChMatchStoreTest {

    private val state = ChMatchState(
        searched = true,
        candidate = ChCandidate("ch:sha-abc", "Flora Medica", "Spratt, G.", "1829", 0.91),
        decision = ChDecision.APPROVED,
    )

    @Test
    fun `round-trips every field`() {
        val back = ChMatchStore.parse(ChMatchStore.encode(state))
        assertEquals(state.searched, back.searched)
        assertEquals(state.decision, back.decision)
        assertEquals(state.candidate, back.candidate)
    }

    @Test
    fun `absent or blank reads as not searched`() {
        assertEquals(ChMatchState.NOT_SEARCHED, ChMatchStore.parse(null))
        assertEquals(ChMatchState.NOT_SEARCHED, ChMatchStore.parse("   "))
    }

    @Test
    fun `damaged json does not crash the row`() {
        assertEquals(ChMatchState.NOT_SEARCHED, ChMatchStore.parse("{not json"))
        assertEquals(ChMatchState.NOT_SEARCHED, ChMatchStore.parse("[]"))
    }

    @Test
    fun `a foreign schema or future version re-runs the search instead of being guessed at`() {
        val foreign = """{"schema":"something.else","version":1,"decision":"approved"}"""
        assertEquals(ChMatchState.NOT_SEARCHED, ChMatchStore.parse(foreign))

        val future = ChMatchStore.encode(state).replace("\"version\":1", "\"version\":2")
        assertEquals(ChMatchState.NOT_SEARCHED, ChMatchStore.parse(future))
    }

    @Test
    fun `a candidate without a key is dropped rather than stored keyless`() {
        // A keyless candidate cannot be re-identified later, so it is not a
        // candidate — it would show a proposal that could never be re-checked.
        val keyless = ChMatchStore.encode(state).replace("\"key\":\"ch:sha-abc\"", "\"key\":\"\"")
        assertNull(ChMatchStore.parse(keyless).candidate)
    }

    @Test
    fun `a rejection keeps the candidate so the same book is not re-proposed blindly`() {
        val rejected = state.copy(decision = ChDecision.REJECTED)
        val back = ChMatchStore.parse(ChMatchStore.encode(rejected))
        assertEquals(ChDecision.REJECTED, back.decision)
        assertEquals("ch:sha-abc", back.candidate?.key)
    }

    @Test
    fun `an unparseable score falls back to zero rather than NaN`() {
        val nan = ChMatchStore.encode(state).replace("\"score\":0.91", "\"score\":\"high\"")
        val back = ChMatchStore.parse(nan)
        assertTrue(back.candidate!!.score.isFinite())
        assertEquals(0.0, back.candidate!!.score, 0.0)
    }

    @Test
    fun `searched defaults from the presence of a candidate for older records`() {
        val noFlag = """{"schema":"org.whl.bookcapture.ch-match","version":1,""" +
            """"candidate":{"key":"k","title":"T"}}"""
        assertTrue(ChMatchStore.parse(noFlag).searched)

        val empty = """{"schema":"org.whl.bookcapture.ch-match","version":1}"""
        assertFalse(ChMatchStore.parse(empty).searched)
    }
}
