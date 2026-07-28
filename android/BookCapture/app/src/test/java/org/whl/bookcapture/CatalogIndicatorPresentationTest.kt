package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CatalogIndicatorPresentationTest {

    private val candidate = ChCandidate(
        key = "ch:sha-abc", title = "Flora Medica", author = "Spratt, G.", year = "1829", score = 0.91,
    )

    private fun row(
        ch: ChMatchState = ChMatchState.NOT_SEARCHED,
        whl: DesktopAvailability? = null,
        ia: DesktopAvailability? = null,
    ) = CatalogIndicatorPresenter.from(ch, whl, ia)

    private fun available(detail: String = "") =
        DesktopAvailability(DesktopAvailabilityState.AVAILABLE, detail = detail)

    private fun unavailable() = DesktopAvailability(DesktopAvailabilityState.UNAVAILABLE)

    @Test
    fun `slot order is fixed so position identifies the source`() {
        assertEquals(
            listOf(CatalogSource.CH, CatalogSource.WHL, CatalogSource.IA),
            row().indicators.map { it.source },
        )
    }

    @Test
    fun `an unreviewed candidate is the only actionable state`() {
        val proposed = row(ChMatchState(searched = true, candidate = candidate)).of(CatalogSource.CH)!!
        assertEquals(CatalogTone.PROPOSED, proposed.tone)
        assertTrue(proposed.actionable)
        assertTrue(proposed.description.contains("Tap to approve"))
    }

    @Test
    fun `approving settles the slot and removes the action`() {
        val approved = row(
            ChMatchState(searched = true, candidate = candidate, decision = ChDecision.APPROVED),
        ).of(CatalogSource.CH)!!
        assertEquals(CatalogTone.CONFIRMED, approved.tone)
        assertFalse(approved.actionable)
        assertTrue(approved.description.contains("Flora Medica"))
    }

    @Test
    fun `a rejected match reads as absent and cannot be toggled back by gesture`() {
        val rejected = row(
            ChMatchState(searched = true, candidate = candidate, decision = ChDecision.REJECTED),
        ).of(CatalogSource.CH)!!
        assertEquals(CatalogTone.ABSENT, rejected.tone)
        assertFalse(rejected.actionable)
    }

    @Test
    fun `not searched is distinct from searched and found nothing`() {
        // "ask again later" and "the answer is no" must not look the same.
        assertEquals(CatalogTone.PENDING, row().of(CatalogSource.CH)!!.tone)
        assertEquals(
            CatalogTone.ABSENT,
            row(ChMatchState(searched = true, candidate = null)).of(CatalogSource.CH)!!.tone,
        )
    }

    @Test
    fun `an unchecked availability is hidden rather than shown as absent`() {
        // A missing answer is not evidence of absence.
        assertEquals(CatalogTone.HIDDEN, row().of(CatalogSource.WHL)!!.tone)
        assertEquals(CatalogTone.HIDDEN, row().of(CatalogSource.IA)!!.tone)
        assertEquals(CatalogTone.ABSENT, row(whl = unavailable()).of(CatalogSource.WHL)!!.tone)
    }

    @Test
    fun `availability detail reaches the description when present`() {
        val whl = row(whl = available("published 1904")).of(CatalogSource.WHL)!!
        assertEquals(CatalogTone.CONFIRMED, whl.tone)
        assertTrue(whl.description.contains("published 1904"))

        val bare = row(whl = available()).of(CatalogSource.WHL)!!
        assertFalse(bare.description.endsWith(": "))
    }

    @Test
    fun `visible filters the hidden slots and hasAction tracks the ch slot`() {
        val nothing = row()
        assertEquals(listOf(CatalogSource.CH), nothing.visible.map { it.source })
        assertFalse(nothing.hasAction)

        val busy = row(ChMatchState(searched = true, candidate = candidate), available(), unavailable())
        assertEquals(3, busy.visible.size)
        assertTrue(busy.hasAction)
    }

    @Test
    fun `of returns null for a source that was not produced`() {
        assertNull(CatalogIndicatorRow(emptyList()).of(CatalogSource.CH))
    }
}
