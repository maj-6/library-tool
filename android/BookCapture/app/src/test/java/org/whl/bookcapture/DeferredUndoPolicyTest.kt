package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Test

class DeferredUndoPolicyTest {
    @Test
    fun committedTargetIsTheOnlyPageEligibleForRemoval() {
        assertEquals(
            DeferredUndoDisposition.REMOVE_TARGET,
            deferredUndoDisposition(targetPage = 4, committedPhotoCount = 4, targetExists = true),
        )
        assertEquals(
            DeferredUndoDisposition.INVALID,
            deferredUndoDisposition(targetPage = 4, committedPhotoCount = 5, targetExists = true),
        )
    }

    @Test
    fun failedOrOrphanedTargetIsAlreadyUndone() {
        assertEquals(
            DeferredUndoDisposition.ALREADY_UNDONE,
            deferredUndoDisposition(targetPage = 4, committedPhotoCount = 3, targetExists = false),
        )
        assertEquals(
            DeferredUndoDisposition.INVALID,
            deferredUndoDisposition(targetPage = null, committedPhotoCount = 3, targetExists = false),
        )
    }
}
