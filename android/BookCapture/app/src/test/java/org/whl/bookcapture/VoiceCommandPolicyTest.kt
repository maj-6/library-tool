package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceCommandPolicyTest {

    @Test
    fun partialRecognitionOnlyAcceptsNonDestructiveCommands() {
        assertEquals("start", VoiceController.commandFromPartial("please start"))
        assertEquals("photo", VoiceController.commandFromPartial("photo"))
        assertNull(VoiceController.commandFromPartial("Edit"))
        assertNull(VoiceController.commandFromPartial("done"))
        assertNull(VoiceController.commandFromPartial("cancel"))
        assertNull(VoiceController.commandFromPartial("restart"))
        assertNull(VoiceController.commandFromPartial("undo"))
        assertNull(VoiceController.commandFromPartial("notes"))
    }

    @Test
    fun editIsAcceptedFromACompletedRecognitionWithoutMatchingLargerWords() {
        assertEquals("edit", VoiceController.commandFromFinal("edit"))
        assertNull(VoiceController.commandFromFinal("edition"))
    }
}
