package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class StructuredNoteTest {

    @Test
    fun punctuationAndCaseProduceCompactRowsInTranscriptOrder() {
        val note = StructuredNote.inProgress(
            "Found in the attic. PRICE: $12.50; PAGES—xii, 320. " +
                "condition: Good. ILLUSTRATIONS, 24 plates; ReMaRk - signed.",
        )

        assertEquals("Found in the attic.", note.unclassifiedText)
        assertEquals(
            listOf(
                StructuredNoteRow(StructuredNoteField.PRICE, "$12.50"),
                StructuredNoteRow(StructuredNoteField.PAGES, "xii, 320"),
                StructuredNoteRow(StructuredNoteField.CONDITION, "Good"),
                StructuredNoteRow(StructuredNoteField.ILLUSTRATIONS, "24 plates"),
                StructuredNoteRow(StructuredNoteField.REMARK, "signed"),
            ),
            note.rows,
        )
        assertFalse(note.isCompleted)
    }

    @Test
    fun revisedTranscriptIsReparsedRetroactivelyWithoutLosingLeadingSpeech() {
        val firstHypothesis = StructuredNote.inProgress("Bought at the garden fair")
        val revised = firstHypothesis.updateTranscript(
            "Bought at the garden fair. Price: twenty dollars. Pages: three hundred",
        )

        assertEquals("Bought at the garden fair", firstHypothesis.unclassifiedText)
        assertTrue(firstHypothesis.rows.isEmpty())
        assertEquals("Bought at the garden fair.", revised.unclassifiedText)
        assertEquals(
            listOf(
                StructuredNoteRow(StructuredNoteField.PRICE, "twenty dollars"),
                StructuredNoteRow(StructuredNoteField.PAGES, "three hundred"),
            ),
            revised.rows,
        )
    }

    @Test
    fun labelsDoNotMatchInsideLongerWords() {
        val speech = "A priceless webpages conditioner has illustrationsque details " +
            "and a remarkable binding"
        val note = StructuredNote.inProgress(speech)

        assertFalse(note.hasStructuredRows)
        assertEquals(speech, note.unclassifiedText)

        val boundaryMatch = note.updateTranscript("A remarkable binding. Remark: keep jacket")
        assertEquals("A remarkable binding.", boundaryMatch.unclassifiedText)
        assertEquals(
            listOf(StructuredNoteRow(StructuredNoteField.REMARK, "keep jacket")),
            boundaryMatch.rows,
        )
    }

    @Test
    fun anInProgressTrailingLabelRemainsVisibleAsAnEmptyRow() {
        val note = StructuredNote.inProgress("Initial observation. Condition:")

        assertEquals("Initial observation.", note.unclassifiedText)
        assertEquals(
            listOf(StructuredNoteRow(StructuredNoteField.CONDITION, "")),
            note.rows,
        )
        assertEquals(StructuredNoteStatus.IN_PROGRESS, note.status)
    }

    @Test
    fun completionCanUseFinalHypothesisAndThenFreezesTheNote() {
        val draft = StructuredNote.inProgress("Price twenty")
        val completed = draft.complete("Price: twenty dollars. Pages: 240")

        assertTrue(completed.isCompleted)
        assertEquals(StructuredNoteStatus.COMPLETED, completed.status)
        assertEquals(
            listOf(
                StructuredNoteRow(StructuredNoteField.PRICE, "twenty dollars"),
                StructuredNoteRow(StructuredNoteField.PAGES, "240"),
            ),
            completed.rows,
        )
        assertSame(completed, completed.updateTranscript("Price: overwritten"))
        assertSame(completed, completed.complete("Price: overwritten"))
    }

    @Test
    fun completedFactoryAlsoKeepsUnclassifiedOnlyNotesVisible() {
        val completed = StructuredNote.completed("No structured details were dictated")

        assertTrue(completed.isCompleted)
        assertFalse(completed.hasStructuredRows)
        assertEquals("No structured details were dictated", completed.unclassifiedText)
    }
}
