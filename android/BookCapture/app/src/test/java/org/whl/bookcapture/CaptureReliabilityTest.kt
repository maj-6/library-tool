package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CaptureReliabilityTest {

    @Test
    fun failedTrashMoveLeavesTheQueueCopyIntact() = withTempDir { root ->
        val queued = File(root, "queue/entry-1").apply { mkdirs() }
        File(queued, "photo_1.jpg").writeText("page")
        val trashed = File(root, "trash/entry-1").also { it.parentFile?.mkdirs() }

        val moved = moveDirectoryWithoutCopy(queued, trashed) { _, _ -> false }

        assertFalse(moved)
        assertTrue(File(queued, "photo_1.jpg").isFile)
        assertFalse(trashed.exists())
    }

    @Test
    fun successfulTrashMoveHasExactlyOneDirectoryCopy() = withTempDir { root ->
        val queued = File(root, "queue/entry-1").apply { mkdirs() }
        File(queued, "photo_1.jpg").writeText("page")
        val trashed = File(root, "trash/entry-1").also { it.parentFile?.mkdirs() }

        assertTrue(moveDirectoryWithoutCopy(queued, trashed))
        assertFalse(queued.exists())
        assertTrue(File(trashed, "photo_1.jpg").isFile)
    }

    @Test
    fun deletionFailureIsNotReportedAsMissingOrDeleted() = withTempDir { root ->
        val entry = File(root, "entry-1").apply { mkdirs() }
        File(entry, "photo_1.jpg").writeText("page")

        assertEquals(
            Entries.DeleteResult.DELETE_FAILED,
            deleteDirectoryResult(entry) { false },
        )
        assertTrue(entry.isDirectory)
    }

    @Test
    fun deletionDistinguishesSuccessFromAlreadyMissing() = withTempDir { root ->
        val entry = File(root, "entry-1").apply { mkdirs() }
        File(entry, "photo_1.jpg").writeText("page")

        assertEquals(Entries.DeleteResult.DELETED, deleteDirectoryResult(entry))
        assertEquals(Entries.DeleteResult.MISSING, deleteDirectoryResult(entry))
    }

    @Test
    fun processingKeepsTheResolutionPromisedByEachCaptureProfile() {
        assertEquals(1600, Pipeline.standardWidthForCaptureProfile(Prefs.CAMERA_PROFILE_FAST))
        assertEquals(2048, Pipeline.standardWidthForCaptureProfile(Prefs.CAMERA_PROFILE_DETAIL))
        assertEquals(2048, Pipeline.standardWidthForCaptureProfile(null))
        assertEquals(2048, Pipeline.standardWidthForCaptureProfile("future-profile"))
    }

    private fun withTempDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("whl-capture-reliability-").toFile()
        try {
            block(root)
        } finally {
            root.deleteRecursively()
        }
    }
}
