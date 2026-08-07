package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.json.JSONObject
import java.io.File

class CaptureSyncTest {

    @Test
    fun cloudRoutesRequireConnectedWorkWhileLanAndUnresolvedAutoDoNot() {
        fun record(mode: String, resolved: String) = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("book-a"),
            syncedIds = emptySet(),
            blockedIds = emptySet(),
            transportMode = mode,
            resolvedTransport = resolved,
        )

        assertTrue(captureUploadRequiresConnectedNetwork(record("cloud", "cloud")))
        assertTrue(captureUploadRequiresConnectedNetwork(record("auto", "cloud")))
        assertTrue(captureUploadRequiresConnectedNetwork(record("cloud", "")))
        assertFalse(captureUploadRequiresConnectedNetwork(record("lan", "lan")))
        assertFalse(captureUploadRequiresConnectedNetwork(record("auto", "")))
        assertFalse(captureUploadRequiresConnectedNetwork(null))
    }

    @Test
    fun onlyCloudOnAnUnconstrainedWorkSpecNeedsAHandoff() {
        assertTrue(captureUploadNeedsConnectedHandoff("cloud", false))
        assertFalse(captureUploadNeedsConnectedHandoff("cloud", true))
        assertFalse(captureUploadNeedsConnectedHandoff("lan", false))
        assertFalse(captureUploadNeedsConnectedHandoff("", false))
    }

    @Test
    fun autoCloudResolutionHandsOffBeforeCreatingASupabaseClient() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val resolution = upload.indexOf("resolved = Prefs.resolveCaptureSyncTransport(")
        val handoff = upload.indexOf("if (captureUploadNeedsConnectedHandoff(", resolution)
        val cloudClient = upload.indexOf("val client = SupabaseClient(ctx, uploadOwner)", handoff)

        assertTrue(resolution >= 0)
        assertTrue(handoff > resolution)
        assertTrue(cloudClient > handoff)
        val handoffBody = upload.substring(handoff, cloudClient)
        assertTrue(handoffBody.contains("CaptureSyncPhase.RETRYING"))
    }

    @Test
    fun anExplicitPressUsesThePhaseAwareEnqueuePolicy() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val explicit = upload.substringAfter("internal fun enqueueExplicitSync")
            .substringBefore("internal fun captureSyncState")

        assertTrue(explicit.contains("captureSyncEnqueuePolicy(start)"))
        assertFalse(explicit.contains("ExistingWorkPolicy.REPLACE"))
    }

    @Test
    fun activeRequestIsReusedWhenItsOutstandingSetMatchesCurrentTargets() {
        val active = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("book-a", "book-b"),
            syncedIds = setOf("book-a"),
            blockedIds = emptySet(),
        )

        val start = beginCaptureSyncRecord(
            existing = active,
            targetIds = listOf("book-b", "book-b", "../escape"),
            newRequestId = "request-2",
            transportMode = "lan",
            lanHost = "new-host",
            cloudOwner = "new-owner",
        )

        assertFalse(start.created)
        assertSame(active, start.record)
    }

    @Test
    fun expandedOutstandingSetReconcilesWithinTheActiveGeneration() {
        val active = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RETRYING,
            targetIds = setOf("book-a", "book-b"),
            syncedIds = setOf("book-a"),
            blockedIds = emptySet(),
            transportMode = "cloud",
            cloudOwner = "old-owner",
        )

        val start = beginCaptureSyncRecord(
            existing = active,
            targetIds = listOf("book-b", "book-c"),
            newRequestId = "request-2",
            transportMode = "lan",
            lanHost = "new-host",
            cloudOwner = "new-owner",
        )

        assertFalse(start.created)
        assertEquals("request-1", start.record.requestId)
        assertEquals(setOf("book-a", "book-b", "book-c"), start.record.targetIds)
        assertEquals(setOf("book-a"), start.record.syncedIds)
        assertTrue(start.record.blockedIds.isEmpty())
        assertEquals("cloud", start.record.transportMode)
        assertEquals("", start.record.lanHost)
        assertEquals("old-owner", start.record.cloudOwner)
        assertEquals("cloud", start.record.resolvedTransport)
    }

    @Test
    fun activeTargetsStayMonotonicSoInFlightMovesRemainAccountable() {
        val active = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.WAITING_FOR_PROCESSING,
            targetIds = setOf("book-a", "book-stale", "book-blocked"),
            syncedIds = setOf("book-a"),
            blockedIds = setOf("book-blocked"),
        )

        val start = beginCaptureSyncRecord(
            existing = active,
            targetIds = listOf("book-current"),
            newRequestId = "request-2",
        )

        assertFalse(start.created)
        assertEquals("request-1", start.record.requestId)
        assertEquals(
            setOf("book-a", "book-stale", "book-blocked", "book-current"),
            start.record.targetIds,
        )
        assertEquals(setOf("book-a"), start.record.syncedIds)
        assertEquals(setOf("book-blocked"), start.record.blockedIds)
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
    fun claimingReopensOnlyTheMatchingCloudBatch() {
        val active = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("book-a", "book-b", "book-c"),
            syncedIds = emptySet(),
            blockedIds = setOf("book-a", "book-b"),
            transportMode = "auto",
            cloudOwner = "owner-1",
            resolvedTransport = "cloud",
        )

        val reopened = reopenCaptureSyncAfterCloudClaim(
            active,
            "owner-1",
            listOf("book-a", "outside-batch"),
        )

        assertEquals(CaptureSyncPhase.RETRYING, reopened?.phase)
        assertEquals(setOf("book-b"), reopened?.blockedIds)
        assertEquals("request-1", reopened?.requestId)
        assertEquals(
            null,
            reopenCaptureSyncAfterCloudClaim(active, "owner-2", listOf("book-a")),
        )
        assertEquals(
            null,
            reopenCaptureSyncAfterCloudClaim(
                active.copy(transportMode = "lan", resolvedTransport = "lan"),
                "owner-1",
                listOf("book-a"),
            ),
        )
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
        assertEquals(1, state.remainingCount)
        assertEquals(1, state.skippedCount)
        assertFalse(state.active)
    }

    @Test
    fun blockedTargetsAreNotAlsoCountedAsPending() {
        val state = aggregateCaptureSyncState(
            record = CaptureSyncRecord(
                requestId = "request-1",
                phase = CaptureSyncPhase.RUNNING,
                targetIds = setOf("synced", "blocked", "waiting"),
                syncedIds = setOf("synced"),
                blockedIds = setOf("blocked"),
            ),
            eligibleIds = setOf("blocked", "waiting"),
            pendingIds = setOf("blocked", "waiting"),
        )

        assertEquals(1, state.syncedCount)
        assertEquals(1, state.blockedCount)
        assertEquals(1, state.remainingCount)
        assertEquals(0, state.skippedCount)
    }

    @Test
    fun terminalReceiptReconciliationCoversBlockedAndMissingTargets() {
        val record = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("synced", "blocked", "pending", "missing"),
            syncedIds = setOf("synced"),
            blockedIds = setOf("blocked"),
        )

        assertEquals(
            setOf("blocked", "missing"),
            captureSyncTerminalReconciliationIds(record, pendingIds = setOf("pending")),
        )
    }

    @Test
    fun finishDecisionWaitsForDeferredOrRemainingWork() {
        val complete = CaptureSyncState(
            phase = CaptureSyncPhase.RUNNING,
            eligibleCount = 0,
            requestedCount = 2,
            syncedCount = 2,
            blockedCount = 0,
            remainingCount = 0,
            skippedCount = 0,
        )
        val remaining = complete.copy(syncedCount = 1, remainingCount = 1)

        assertEquals(
            CaptureSyncFinishDecision.WAIT,
            captureSyncFinishDecision(complete, sawDeferred = true, hadError = false),
        )
        assertEquals(
            CaptureSyncFinishDecision.WAIT,
            captureSyncFinishDecision(remaining, sawDeferred = false, hadError = false),
        )
    }

    @Test
    fun finishDecisionCompletesOnlyFullyAccountedWork() {
        val complete = CaptureSyncState(
            phase = CaptureSyncPhase.RUNNING,
            eligibleCount = 0,
            requestedCount = 2,
            syncedCount = 2,
            blockedCount = 0,
            remainingCount = 0,
            skippedCount = 0,
        )

        assertEquals(
            CaptureSyncFinishDecision.COMPLETE,
            captureSyncFinishDecision(complete, sawDeferred = false, hadError = false),
        )
        for (state in listOf(
            complete.copy(blockedCount = 1),
            complete.copy(skippedCount = 1),
            complete.copy(syncedCount = 1),
        )) {
            assertEquals(
                CaptureSyncFinishDecision.COMPLETE_WITH_ERRORS,
                captureSyncFinishDecision(state, sawDeferred = false, hadError = false),
            )
        }
        assertEquals(
            CaptureSyncFinishDecision.COMPLETE_WITH_ERRORS,
            captureSyncFinishDecision(complete, sawDeferred = false, hadError = true),
        )
    }

    @Test
    fun terminalTransitionIsRejectedWhenASecondPressExpandedTheBatch() {
        val expected = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("book-a"),
            syncedIds = setOf("book-a"),
            blockedIds = emptySet(),
        )

        assertEquals(
            expected.copy(phase = CaptureSyncPhase.COMPLETE),
            terminalCaptureSyncRecord(expected, expected, CaptureSyncPhase.COMPLETE),
        )
        assertEquals(
            null,
            terminalCaptureSyncRecord(
                current = expected.copy(targetIds = setOf("book-a", "book-b")),
                expected = expected,
                phase = CaptureSyncPhase.COMPLETE,
            ),
        )
    }

    @Test
    fun terminalRecordsRejectLateWorkerMutations() {
        val active = CaptureSyncRecord(
            requestId = "request-1",
            phase = CaptureSyncPhase.RUNNING,
            targetIds = setOf("book-a"),
            syncedIds = emptySet(),
            blockedIds = emptySet(),
        )
        val terminal = active.copy(phase = CaptureSyncPhase.COMPLETE)

        assertEquals(active, activeCaptureSyncRecordForRequest(active, "request-1"))
        assertEquals(null, activeCaptureSyncRecordForRequest(terminal, "request-1"))
        assertEquals(null, activeCaptureSyncRecordForRequest(active, "request-2"))
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
        val membershipSync = upload.indexOf(
            "syncInspectMembershipAfterCaptureInsert(ctx, client, dir, uploadOwner)",
            cloudDelivery,
        )
        val localCommit = upload.indexOf(
            "markUploaded(ctx, dir, delivery, syncRequestId, uploadOwner)",
            cloudDelivery,
        )
        val reviewSync = upload.indexOf(
            "CaptureMetadataSyncWorker.enqueueExplicitSync(ctx)",
            localCommit,
        )

        assertTrue(cloudDelivery >= 0)
        assertTrue(membershipSync > cloudDelivery)
        assertTrue(localCommit > membershipSync)
        assertTrue(reviewSync > localCommit)
        assertTrue(upload.contains("var membership = stored.memberships[dir.name] ?: return"))
        assertTrue(upload.contains("InspectBookMemberships.compareAndSet("))
        assertTrue(upload.contains("membership.copy(cloudOwnerId = owner)"))
        assertFalse(upload.contains("val owner = Prefs.userId(ctx)"))
        assertTrue(upload.contains("client.mutateCaptureCollection("))
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
        assertTrue(upload.contains("cloudOwnerId: String,"))
        assertTrue(upload.contains("cloudOwnerId = cloudOwnerId,"))
        assertTrue(upload.contains("markDelivered(ctx, dir, delivery, \"imported\", syncRequestId, \"lan\")"))
        assertTrue(metadataWorker.contains("entry.deliveryTransport == \"lan\""))
        assertTrue(metadataWorker.contains("entry.deliveryTransport == \"cloud\""))
    }

    @Test
    fun cloudDeliveryStampRequiresTheFrozenOwnerToMatchTheCaptureCreator() {
        val ownerA = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        val manifest = JSONObject()
            .put("creator", JSONObject()
                .put("kind", Prefs.CREATOR_ACCOUNT)
                .put("id", ownerA.uppercase()))

        val stamped = stampDeliveryManifest(
            manifest = manifest,
            uploadedAt = 123L,
            cloudStatus = "pending",
            syncRequestId = "request-1",
            deliveryTransport = "cloud",
            cloudOwnerId = ownerA,
        )

        assertEquals(ownerA, stamped.getString(CLOUD_OWNER_MANIFEST_KEY))
        assertEquals(ownerA, cloudOwnerIdFromDeliveryManifest(stamped))
        assertThrows(IllegalArgumentException::class.java) {
            stampDeliveryManifest(
                manifest = JSONObject()
                    .put("creator", JSONObject()
                        .put("kind", Prefs.CREATOR_ACCOUNT)
                        .put("id", ownerA)),
                uploadedAt = 124L,
                cloudStatus = "pending",
                syncRequestId = "request-2",
                deliveryTransport = "cloud",
                cloudOwnerId = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            )
        }
    }

    @Test
    fun lanDeliveryStampCannotRetainACloudOwner() {
        val manifest = JSONObject()
            .put(CLOUD_OWNER_MANIFEST_KEY, "stale-owner")
            .put("creator", JSONObject()
                .put("kind", Prefs.CREATOR_ACCOUNT)
                .put("id", "stale-owner"))

        val stamped = stampDeliveryManifest(
            manifest = manifest,
            uploadedAt = 123L,
            cloudStatus = "imported",
            syncRequestId = "request-1",
            deliveryTransport = "lan",
        )

        assertFalse(stamped.has(CLOUD_OWNER_MANIFEST_KEY))
        assertEquals("", cloudOwnerIdFromDeliveryManifest(stamped))
    }

    @Test
    fun archiveConfirmationIsPersistedBeforeImportedStatusOrLanMove() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val delivered = upload.substringAfter("private fun markDelivered(")
            .substringBefore("// --- LAN transport")
        val associationWrite = delivered.indexOf("CaptureLibAssociationStore.apply(")
        val manifestWrite = delivered.indexOf("Entries.atomicWrite(manifestFile")
        val move = delivered.indexOf("dir.renameTo(target)")
        assertTrue(associationWrite >= 0)
        assertTrue(manifestWrite > associationWrite)
        assertTrue(move > manifestWrite)

        val poll = upload.substringAfter("private suspend fun pollImports(")
            .substringBefore("private suspend fun syncCloudPhotoJob(")
        assertTrue(poll.contains("client.captureImportStates(waitingForImport.map { it.id })"))
        assertTrue(poll.contains("applyCaptureImportState(latest.dir, remote)"))

        val shared = File(
            "src/main/java/org/whl/bookcapture/CaptureLibAssociation.kt",
        ).readText().substringAfter("internal fun applyCaptureImportState(")
            .substringBefore("internal fun captureLibAssociationFromJson(")
        val sharedAssociationWrite = shared.indexOf("CaptureLibAssociationStore.apply(")
        val sharedStatusWrite = shared.indexOf("Entries.atomicWrite(")
        assertTrue(sharedAssociationWrite >= 0)
        assertTrue(sharedStatusWrite > sharedAssociationWrite)
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
        val recovery = upload.substringAfter("private suspend fun recoverDeliveredAccounting(")
            .substringBefore("private suspend fun finishUploadChain(")
        assertTrue(recovery.contains("Entries.recent(ctx)"))
        assertTrue(recovery.indexOf("EntryOperationLocks.withLock(targetId)") <
            recovery.indexOf("Entries.find(ctx, targetId)"))
        val finish = upload.substringAfter("private suspend fun finishUploadChain(")
            .substringBefore("override suspend fun doWork(): Result")
        assertTrue(finish.contains("captureSyncTerminalReconciliationIds("))
        assertTrue(finish.contains("exactTargetIds = uncheckedTerminal"))
        assertTrue(finish.contains("Prefs.completeCaptureSyncIfUnchanged("))
        assertTrue(finish.contains("syncResultData(ctx, \"continuing\")"))
    }

    @Test
    fun blockedAndSyncedTargetsAreNotSelectedAgainOnADeferredRound() {
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val selection = upload.substringAfter("private fun nextPendingCapture(")
            .substringBefore("private suspend fun setUploadProgress(")

        assertTrue(selection.contains(
            "record.targetIds - record.syncedIds - record.blockedIds",
        ))
    }
}
