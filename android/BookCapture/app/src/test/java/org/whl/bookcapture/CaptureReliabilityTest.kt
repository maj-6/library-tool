package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

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

    @Test
    fun queuePublicationAndOrphanRecoveryAreMutuallyExclusive() {
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val secondAttempted = CountDownLatch(1)
        val secondEntered = CountDownLatch(1)

        val first = thread(name = "capture-publication-test") {
            CaptureQueueLifecycle.exclusive {
                firstEntered.countDown()
                assertTrue(releaseFirst.await(2, TimeUnit.SECONDS))
            }
        }
        assertTrue(firstEntered.await(2, TimeUnit.SECONDS))

        val second = thread(name = "orphan-recovery-test") {
            secondAttempted.countDown()
            CaptureQueueLifecycle.exclusive { secondEntered.countDown() }
        }
        assertTrue(secondAttempted.await(2, TimeUnit.SECONDS))
        assertFalse(secondEntered.await(100, TimeUnit.MILLISECONDS))

        releaseFirst.countDown()
        assertTrue(secondEntered.await(2, TimeUnit.SECONDS))
        first.join(2_000)
        second.join(2_000)
        assertFalse(first.isAlive)
        assertFalse(second.isAlive)
    }

    @Test
    fun everyQueuePublicationPathUsesTheOrphanRecoveryGate() {
        val source = File("src/main/java/org/whl/bookcapture/CaptureSession.kt").readText()
        assertTrue(source.contains(
            "fun start(collection: BookCollection): String = CaptureQueueLifecycle.exclusive"
        ))
        assertTrue(source.contains(
            "fun restoreFromTrash(id: String): Boolean = CaptureQueueLifecycle.exclusive"
        ))
        assertTrue(source.contains(
            "fun recoverOrphans(): Int = CaptureQueueLifecycle.exclusive"
        ))
    }

    @Test
    fun captureCommitRunsOffTheCameraMainThreadCallback() {
        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val sessionSource = File("src/main/java/org/whl/bookcapture/CaptureSession.kt").readText()
        val savedCallback = source.substringAfter("private fun handleCaptureSaved(")
            .substringBefore("private fun finishCaptureCommit(")
        val completion = source.substringAfter("private fun finishCaptureCommit(")
            .substringBefore("private fun handleCaptureError(")

        assertTrue(savedCallback.contains("CAPTURE_COMMIT_EXECUTOR.execute"))
        assertTrue(savedCallback.contains("session.commitPhoto(reservation)"))
        assertTrue(savedCallback.contains("ContextCompat.getMainExecutor(this).execute"))
        assertFalse(completion.contains("session.commitPhoto(reservation)"))
        assertTrue(sessionSource.contains("photoCount = reservation.pageNumber"))
        assertFalse(sessionSource.contains("photoCount += 1"))
    }

    @Test
    fun captureScreenBoundsThumbnailWorkAndIgnoresTerminalWorkerHistory() {
        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val addThumbnail = source.substringAfter("private fun addThumbnail(")
            .substringBefore("private fun clearThumbnailStrip()")

        assertTrue(source.contains("private val thumbnailDecodeGate = Semaphore(permits = 2)"))
        assertTrue(source.contains("thumbnailDecodeGate.withPermit"))
        assertTrue(source.contains("maxWidth = h * 2"))
        assertTrue(source.contains("maxHeight = h"))
        assertTrue(source.contains("thumbnailBitmaps.values.forEach"))
        assertTrue(source.contains("bitmap.recycle()"))
        assertTrue(source.contains("getWorkInfosLiveData(activeUniqueWorkQuery("))
        assertFalse(source.contains("getWorkInfosForUniqueWorkLiveData"))
        assertTrue(addThumbnail.contains("finally {"))
        assertTrue(addThumbnail.contains(
            "unclaimedBitmap?.takeIf { !it.isRecycled }?.recycle()",
        ))
        assertTrue(
            addThumbnail.indexOf("thumbnailBitmaps[iv] = bitmap") <
                addThumbnail.indexOf("unclaimedBitmap = null"),
        )
    }

    @Test
    fun previousBookPreviewCoalescesRefreshesAndReusesUnchangedBitmap() {
        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val updateUi = source.substringAfter("private fun updateUi()")
            .substringBefore("/** An open capture is intentionally excluded.")
        val refresh = source.substringAfter("private fun refreshLastCapturedBook()")
            .substringBefore("private fun requestMicrophonePermission()")

        assertFalse(updateUi.contains("refreshLastCapturedBook()"))
        assertTrue(source.contains("lastBookPreviewRefreshPending"))
        assertTrue(source.contains("fingerprint != previousFingerprint"))
        assertTrue(source.contains("if (lastBookPreviewJob?.isActive == true) return"))
        assertTrue(source.contains("lastBookPreviewBitmap?.takeIf { !it.isRecycled }?.recycle()"))
        assertTrue(refresh.contains("finally {"))
        assertTrue(refresh.contains(
            "unclaimedBitmap?.takeIf { !it.isRecycled }?.recycle()",
        ))
        assertTrue(
            refresh.indexOf("renderLastCapturedBook(load)") <
                refresh.indexOf("unclaimedBitmap = null"),
        )
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
