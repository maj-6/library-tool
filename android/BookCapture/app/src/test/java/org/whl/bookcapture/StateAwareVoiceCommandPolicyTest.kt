package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class StateAwareVoiceCommandPolicyTest {

    @Test
    fun onlyStartAndPhotoAreEligibleFromStableIdlePartials() {
        assertNull(evaluate("start", VoiceRecognitionStability.UNSTABLE_PARTIAL))
        assertEquals(
            PolicyVoiceCommand.START,
            evaluate("please START!", VoiceRecognitionStability.STABLE_PARTIAL)?.command,
        )
        assertEquals(
            PolicyVoiceCommand.PHOTO,
            evaluate("done, then photo", VoiceRecognitionStability.STABLE_PARTIAL)?.command,
        )

        for (command in listOf("done", "cancel", "restart", "undo", "notes")) {
            assertNull(evaluate(command, VoiceRecognitionStability.STABLE_PARTIAL))
        }
    }

    @Test
    fun everyIdleCommandIsAvailableFromFinalRecognition() {
        val expected = mapOf(
            "start" to PolicyVoiceCommand.START,
            "photo" to PolicyVoiceCommand.PHOTO,
            "done" to PolicyVoiceCommand.DONE,
            "cancel" to PolicyVoiceCommand.CANCEL,
            "restart" to PolicyVoiceCommand.RESTART,
            "undo" to PolicyVoiceCommand.UNDO,
            "notes" to PolicyVoiceCommand.NOTES,
        )

        expected.forEach { (spoken, command) ->
            assertEquals(
                command,
                evaluate("please, $spoken!", VoiceRecognitionStability.FINAL)?.command,
            )
        }
    }

    @Test
    fun longestPhrasePreventsEndNotesFromBecomingIdleNotes() {
        assertNull(evaluate("end notes", VoiceRecognitionStability.FINAL))
        assertEquals(
            PolicyVoiceCommand.NOTES,
            evaluate("notes", VoiceRecognitionStability.FINAL)?.command,
        )

        val active = StateAwareVoiceCommandPolicy.evaluate(
            "Price twenty dollars. END, NOTES!!!",
            VoiceCommandState.NOTE_ACTIVE,
            VoiceRecognitionStability.FINAL,
        )
        assertEquals(PolicyVoiceCommand.END_NOTES, active?.command)
        assertEquals("Price twenty dollars.", active?.transcriptBeforeCommand)
    }

    @Test
    fun noteCommandsMustBeFinalStandaloneAndTrailing() {
        assertNull(StateAwareVoiceCommandPolicy.evaluate(
            "keep this undo",
            VoiceCommandState.NOTE_ACTIVE,
            VoiceRecognitionStability.STABLE_PARTIAL,
        ))
        assertNull(StateAwareVoiceCommandPolicy.evaluate(
            "undo this last sentence",
            VoiceCommandState.NOTE_ACTIVE,
            VoiceRecognitionStability.FINAL,
        ))
        assertEquals(
            PolicyVoiceCommand.UNDO,
            StateAwareVoiceCommandPolicy.evaluate(
                "keep this. Undo!",
                VoiceCommandState.NOTE_ACTIVE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
        assertEquals(
            PolicyVoiceCommand.RESTART,
            StateAwareVoiceCommandPolicy.evaluate(
                "draft text — RESTART...",
                VoiceCommandState.NOTE_ACTIVE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
        assertNull(StateAwareVoiceCommandPolicy.evaluate(
            "photo",
            VoiceCommandState.NOTE_ACTIVE,
            VoiceRecognitionStability.FINAL,
        ))
    }

    @Test
    fun commandWordsNeverMatchInsideLongerWords() {
        assertNull(evaluate(
            "starter photograph undone restartable notations cancellation",
            VoiceRecognitionStability.FINAL,
        ))
        assertNull(StateAwareVoiceCommandPolicy.evaluate(
            "a weekend notes section with undoable edits",
            VoiceCommandState.NOTE_ACTIVE,
            VoiceRecognitionStability.FINAL,
        ))
    }

    @Test
    fun noteResultExposesExactConsumedPositionAndCleanTranscriptPrefix() {
        val text = "Price: $20 — end notes...  "
        val result = StateAwareVoiceCommandPolicy.evaluate(
            text,
            VoiceCommandState.NOTE_ACTIVE,
            VoiceRecognitionStability.FINAL,
        )!!

        assertEquals("Price: $20", result.transcriptBeforeCommand)
        assertEquals("end notes", text.substring(
            result.consumption.commandStart,
            result.consumption.commandEndExclusive,
        ))
        assertEquals(text.length, result.consumption.consumedThroughExclusive)
        assertTrue(result.consumption.commandStart < result.consumption.commandEndExclusive)
    }

    @Test
    fun stablePartialAndFinalProduceTheSameDebounceKeyForTheSameIdleCommand() {
        val partial = evaluate("please photo!", VoiceRecognitionStability.STABLE_PARTIAL)!!
        val final = evaluate("please photo!", VoiceRecognitionStability.FINAL)!!

        assertEquals(partial.consumption, final.consumption)
    }

    private fun evaluate(
        transcript: String,
        stability: VoiceRecognitionStability,
    ): VoiceCommandPolicyResult? = StateAwareVoiceCommandPolicy.evaluate(
        transcript,
        VoiceCommandState.IDLE,
        stability,
    )
}
