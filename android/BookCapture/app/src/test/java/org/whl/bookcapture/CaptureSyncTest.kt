package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CaptureSyncTest {

    @Test
    fun activeRequestIsIdempotentAndDoesNotAbsorbLaterCaptures() {
        val active = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("book-a", "book-b"),
            syncedIds = setOf("book-a"),
            blockedIds = emptySet(),
        )

        val start = beginCaptureSyncRecord(
            existing = active,
            targetIds = listOf("book-a", "book-b", "book-later"),
            newRequestId = "request-2",
        )

        assertFalse(start.created)
        assertSame(active, start.record)
        assertFalse("book-later" in start.record.targetIds)
    }

    @Test
    fun completedRequestAllowsANewFrozenBatch() {
        val completed = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.COMPLETE,
            targetIds = setOf("book-a"),
            syncedIds = setOf("book-a"),
            blockedIds = emptySet(),
        )

        val start = beginCaptureSyncRecord(
            existing = completed,
            targetIds = listOf("book-b", "book-b", "../escape"),
            newRequestId = "request-2",
        )

        assertTrue(start.created)
        assertEquals("request-2", start.record.requestId)
        assertEquals(setOf("book-b"), start.record.targetIds)
        assertEquals(CaptureSyncPhase.QUEUED, start.record.phase)
    }

    @Test
    fun emptyManualBatchCompletesWithoutStartingWork() {
        val start = beginCaptureSyncRecord(null, emptyList(), "request-empty")

        assertTrue(start.created)
        assertEquals(CaptureSyncPhase.COMPLETE, start.record.phase)
        assertTrue(start.record.targetIds.isEmpty())
    }

    @Test
    fun aggregateReportsBatchAndCurrentEligibilitySeparately() {
        val record = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.COMPLETE_WITH_ERRORS,
            targetIds = setOf("synced", "blocked", "waiting", "deleted"),
            syncedIds = setOf("synced", "not-in-batch"),
            blockedIds = setOf("blocked", "synced", "not-in-batch"),
        )

        val state = aggregateCaptureSyncState(
            record = record,
            eligibleIds = setOf("blocked", "waiting", "next-batch"),
            pendingIds = setOf("blocked", "waiting", "next-batch"),
        )

        assertEquals(CaptureSyncPhase.COMPLETE_WITH_ERRORS, state.phase)
        assertEquals(3, state.eligibleCount)
        assertEquals(4, state.requestedCount)
        assertEquals(1, state.syncedCount)
        assertEquals(1, state.blockedCount)
        assertEquals(2, state.remainingCount)
        assertEquals(1, state.skippedCount)
        assertFalse(state.active)
    }

    @Test
    fun legacyAndStaleWorkersCannotAuthorizeUploads() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val processing = File("src/main/java/org/whl/bookcapture/ProcessWorker.kt").readText()
        val workerGate = upload.indexOf("val syncRecord = authorizedSyncRecord(ctx)")
        val orphanRecovery = upload.indexOf("session.recoverOrphans(syncRecord.targetIds)")

        assertTrue(upload.contains("fun enqueueExplicitSync(ctx: Context): CaptureSyncState"))
        assertTrue(upload.contains("private fun resumeExplicitSync(ctx: Context)"))
        assertTrue(upload.contains("val active = Prefs.activeCaptureSyncRecord(ctx) ?: return"))
        assertTrue(upload.contains("manual-sync-required"))
        assertTrue(workerGate >= 0)
        assertTrue(orphanRecovery > workerGate)
        assertFalse(processing.contains("UploadWorker.kick(ctx)"))
    }

    @Test
    fun progressContractContainsAggregateCounts() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()

        assertTrue(upload.contains("UPLOAD_PROGRESS_TOTAL to state.requestedCount"))
        assertTrue(upload.contains("UPLOAD_PROGRESS_SYNCED to state.syncedCount"))
        assertTrue(upload.contains("UPLOAD_PROGRESS_BLOCKED to state.blockedCount"))
        assertTrue(upload.contains("UPLOAD_PROGRESS_REMAINING to state.remainingCount"))
        assertTrue(upload.contains("const val EXPLICIT_SYNC_WORK_NAME"))
    }

    @Test
    fun explicitUploadQueuesReviewSyncOnlyAfterTheCloudCaptureRowExists() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val cloudDelivery = upload.indexOf("val delivery = uploadEntry(client, dir, prepared)")
        val localCommit = upload.indexOf(
            "markUploaded(ctx, dir, delivery, syncRequestId)",
            cloudDelivery,
        )
        val reviewSync = upload.indexOf(
            "CaptureMetadataSyncWorker.enqueueExplicitSync(ctx)",
            localCommit,
        )

        assertTrue(cloudDelivery >= 0)
        assertTrue(localCommit > cloudDelivery)
        assertTrue(reviewSync > localCommit)
    }

    @Test
    fun deliveredRecoveryDurablyQueuesDirtyReviewBeforeClosingAccounting() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val recovery = upload.substringAfter("private suspend fun recoverDeliveredAccounting(")
            .substringBefore("private suspend fun finishUploadChain(")
        val dirtyCheck = recovery.indexOf("CaptureMetadataStore.hasPendingReviewSync(entry.dir)")
        val enqueue = recovery.indexOf(
            "CaptureMetadataSyncWorker.enqueueExplicitSyncDurably(ctx)",
        )
        val accounted = recovery.indexOf("Prefs.markCaptureSynced(ctx")
        val metadataWorker = File(
            "src/main/java/org/whl/bookcapture/CaptureMetadataSyncWorker.kt",
        ).readText()

        assertTrue(dirtyCheck >= 0)
        assertTrue(enqueue > dirtyCheck)
        assertTrue(accounted > enqueue)
        assertTrue(metadataWorker.contains("operation.result.get()"))
        assertTrue(upload.contains("val pendingReviewSync = CaptureMetadataStore.hasPendingReviewSync(dir)"))
        assertTrue(upload.contains("markDelivered(ctx, dir, delivery, \"pending\", syncRequestId, \"cloud\")"))
        assertTrue(upload.contains("markDelivered(ctx, dir, delivery, \"imported\", syncRequestId, \"lan\")"))
        assertTrue(metadataWorker.contains("entry.deliveryTransport == \"lan\""))
        assertTrue(metadataWorker.contains("entry.deliveryTransport == \"cloud\""))
    }

    @Test
    fun everyContinuationRecoversDeliveredAccountingBeforeSelectingItsNextCapture() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
            .replace("\r\n", "\n")
        val work = upload.substringAfter("override suspend fun doWork(): Result")
            .substringBefore("private data class PreparedCapture")
        val cursorGuard = work.indexOf("if (cursor == null) {")
        val orphanRecovery = work.indexOf(
            "session.recoverOrphans(syncRecord.targetIds)",
            cursorGuard,
        )
        val cursorGuardEnd = work.indexOf("\n        }", orphanRecovery)
        val deliveredRecovery = work.indexOf(
            "if (!recoverDeliveredAccounting(ctx, syncRecord))",
            cursorGuardEnd,
        )
        val pendingSelection = work.indexOf(
            "val candidate = nextPendingCapture(session, cursor)",
            deliveredRecovery,
        )

        assertTrue(cursorGuard >= 0)
        assertTrue(orphanRecovery > cursorGuard)
        assertTrue("orphan rescue remains limited to the first cursor", cursorGuardEnd > orphanRecovery)
        assertTrue(
            "a retry with cursor A must reconcile delivered B outside the cursor guard",
            deliveredRecovery > cursorGuardEnd,
        )
        assertTrue(
            "sent-entry recovery must precede the next pending-queue lookup",
            pendingSelection > deliveredRecovery,
        )
    }
}
