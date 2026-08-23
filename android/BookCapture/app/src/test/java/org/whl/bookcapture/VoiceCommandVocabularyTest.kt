package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The spoken vocabulary lives in two places that must agree.
 *
 * `VoiceController.COMMANDS` is the Vosk decoding grammar: a phrase missing
 * there is never emitted by the recogniser, so a command the policy knows about
 * simply never fires — with no error anywhere to show why.
 */
class VoiceCommandVocabularyTest {

    private fun partial(text: String): String? =
        VoiceController.commandFromPartial(text)

    private fun final(text: String): String? =
        VoiceController.commandFromFinal(text)

    @Test
    fun everyPolicyPhraseIsInTheRecognizerGrammar() {
        val grammar = VoiceController.COMMANDS.map { it.lowercase() }.toSet()
        val missing = PolicyVoiceCommand.entries
            .filter { it != PolicyVoiceCommand.END_NOTES }
            .map { it.wireValue }
            .filterNot { it in grammar }
        assertTrue("phrases absent from the Vosk grammar: $missing", missing.isEmpty())
    }

    @Test
    fun roleCommandsAreRecognised() {
        assertEquals("spine", final("spine"))
        assertEquals("cover", final("cover"))
        assertEquals("title", final("title"))
    }

    @Test
    fun roleCommandsFireOnAStablePartialLikePhoto() {
        // A shutter command that waits for the final result feels broken in the
        // hand; these must behave exactly like "photo".
        assertEquals("photo", partial("photo"))
        assertEquals("spine", partial("spine"))
        assertEquals("cover", partial("cover"))
        assertEquals("title", partial("title"))
        assertNull(partial("scan"))
    }

    @Test
    fun titlePageIsTheSameCommandAsTitle() {
        assertEquals("title", final("title page"))
        assertEquals("title", final("title"))
    }

    @Test
    fun roleCommandsDeclareTheMatchingPhotoRole() {
        assertEquals(PhotoRole.SPINE, PolicyVoiceCommand.SPINE.declaredRole)
        assertEquals(PhotoRole.COVER, PolicyVoiceCommand.COVER.declaredRole)
        assertEquals(PhotoRole.TITLE_PAGE, PolicyVoiceCommand.TITLE.declaredRole)
    }

    @Test
    fun nonCaptureCommandsDeclareNoRole() {
        assertNull(PolicyVoiceCommand.PHOTO.declaredRole)
        assertNull(PolicyVoiceCommand.DONE.declaredRole)
        assertNull(PolicyVoiceCommand.CANCEL.declaredRole)
    }

    @Test
    fun existingCommandsStillResolve() {
        assertEquals("start", final("start"))
        assertEquals("photo", final("photo"))
        assertEquals("scan", final("scan"))
        assertEquals("a", final("a"))
        assertEquals("b", final("b"))
        assertEquals("c", final("c"))
        assertNull(partial("a"))
        assertEquals("done", final("done"))
        assertEquals("cancel", final("cancel"))
        assertEquals("restart", final("restart"))
        assertEquals("undo", final("undo"))
        assertEquals("edit", final("edit"))
    }

    @Test
    fun roleCommandsAreNotAcceptedWhileDictatingANote() {
        // While a note is open the user is dictating prose; "the cover is worn"
        // must stay in the note rather than trip the shutter.
        val result = StateAwareVoiceCommandPolicy.evaluate(
            transcript = "the cover is worn",
            state = VoiceCommandState.NOTE_ACTIVE,
            stability = VoiceRecognitionStability.FINAL,
        )
        assertNull(result)
    }

    @Test
    fun aRoleWordInsideALongerUtteranceStillResolves() {
        // Vosk emits a running transcript; the command is taken from the end.
        assertEquals("spine", final("photo spine"))
        assertNotNull(final("start"))
    }

    @Test
    fun roleWordsDoNotCollideWithOtherCommands() {
        // "cover" contains no other command; "title" must not be shadowed by a
        // substring match, and "restart" must still beat "start".
        assertEquals("restart", final("restart"))
        assertEquals("cover", final("cover"))
    }
}
