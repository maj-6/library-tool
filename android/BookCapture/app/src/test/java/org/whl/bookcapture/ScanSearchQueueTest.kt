package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class ScanSearchQueueTest {
    private val queueId = "11111111-1111-4111-8111-111111111111"
    private val collectionId = "22222222-2222-4222-8222-222222222222"
    private val captureId = "33333333-3333-4333-8333-333333333333"
    private val ownerId = "44444444-4444-4444-8444-444444444444"
    private val otherOwnerId = "55555555-5555-4555-8555-555555555555"
    private val secondQueueId = "66666666-6666-4666-8666-666666666666"

    @Test
    fun strictVersionedQueueRoundTrips() {
        val item = ScanSearchQueueItem(
            id = queueId,
            scanCollectionId = collectionId,
            photoRole = ScanSearchPhotoRole.TITLE_PAGE,
            ocrText = "A New Herbal\nJohn Gerard",
            createdAt = "2026-08-21T12:00:00Z",
        )
        val store = ScanSearchQueueStore(listOf(item))
        val encoded = scanSearchQueueStoreToJson(store)
        assertEquals(3, JSONObject(encoded).getInt("version"))
        assertEquals(store, scanSearchQueueStoreFromJson(encoded))
    }

    @Test
    fun processingPlaceholderRoundTripsAndCompletesToASyncableObservation() {
        val processing = item(ownerId = ownerId).copy(
            scanCollectionId = "",
            photoRole = ScanSearchPhotoRole.TITLE_PAGE,
            ocrText = "",
            dirty = false,
            processing = true,
        )
        val encoded = scanSearchQueueStoreToJson(ScanSearchQueueStore(listOf(processing)))
        assertEquals(processing, scanSearchQueueStoreFromJson(encoded).items.single())
        assertNull(normalizedScanSearchQueueItem(processing.copy(dirty = true)))
        assertNull(normalizedScanSearchQueueItem(processing.copy(ocrText = "premature")))

        val completed = completeScanSearchProcessingItem(
            processing,
            "  A New Herbal\u0000  ",
            "",
            "2026-08-21T12:01:00Z",
        )
        assertEquals("A New Herbal", completed?.ocrText)
        assertFalse(checkNotNull(completed).processing)
        assertTrue(completed.dirty)
        assertEquals(ScanSearchStatus.PENDING, completed.status)
        assertEquals("", completed.errorMessage)
        assertEquals(completed, completeScanSearchProcessingItem(
            completed,
            "A New Herbal",
            "",
            "2026-08-21T12:02:00Z",
        ))
    }

    @Test
    fun processingFailureIsBoundedLocalAndSurvivesAnEmptyCloudSnapshot() {
        val processing = item(ownerId = ownerId).copy(
            scanCollectionId = "",
            ocrText = "",
            dirty = false,
            processing = true,
        )
        val failed = failScanSearchProcessingItem(
            processing,
            "  Mistral\nOCR failed ${"🙂".repeat(1_000)}  ",
            "2026-08-21T12:01:00Z",
        )
        assertEquals(ScanSearchStatus.FAILED, failed?.status)
        assertFalse(checkNotNull(failed).processing)
        assertFalse(failed.dirty)
        assertTrue(failed.errorMessage.startsWith("Mistral OCR failed"))
        assertTrue(failed.errorMessage.length <= ScanSearchQueue.MAX_ERROR_CHARS)
        assertTrue(
            failed.errorMessage.toByteArray(Charsets.UTF_8).size <=
                ScanSearchQueue.MAX_ERROR_BYTES,
        )
        val parsed = scanSearchQueueStoreFromJson(
            scanSearchQueueStoreToJson(ScanSearchQueueStore(listOf(failed))),
        )
        assertEquals(failed, parsed.items.single())
        assertEquals(
            listOf(failed),
            mergeScanSearchQueueStore(parsed, ownerId, emptyList())?.items,
        )
    }

    @Test
    fun routingAProcessingPlaceholderPublishesItBeforeOcrCompletes() {
        val processing = item(ownerId = ownerId).copy(
            scanCollectionId = "",
            ocrText = "",
            dirty = false,
            processing = true,
        )
        val routed = routeScanSearchSessionStore(
            ScanSearchQueueStore(listOf(processing)),
            ownerId,
            queueId,
            collectionId,
            "2026-08-21T12:01:00Z",
        )?.items?.single()
        assertEquals(collectionId, routed?.scanCollectionId)
        assertTrue(checkNotNull(routed).processing)
        assertTrue(routed.dirty)
    }

    @Test
    fun cloudPlaceholderAckAndOcrCompletionAreRaceSafe() {
        val local = item(ownerId = ownerId).copy(
            scanCollectionId = collectionId,
            ocrText = "",
            dirty = true,
            processing = true,
        )
        val cloudPlaceholder = local.copy(revision = 1, dirty = false)
        val acknowledged = checkNotNull(acknowledgeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(local)),
            ownerId,
            local,
            cloudPlaceholder,
        )).items.single()
        assertEquals(cloudPlaceholder, acknowledged)

        val completedAfterAck = checkNotNull(completeScanSearchProcessingItem(
            acknowledged,
            "A New Herbal",
            "",
            "2026-08-21T12:02:00Z",
        ))
        assertEquals(1L, completedAfterAck.revision)
        assertFalse(completedAfterAck.processing)
        assertTrue(completedAfterAck.dirty)

        val completedBeforeAck = checkNotNull(completeScanSearchProcessingItem(
            local,
            "A New Herbal",
            "",
            "2026-08-21T12:01:00Z",
        ))
        assertEquals(
            completedBeforeAck,
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(completedBeforeAck)),
                ownerId,
                local,
                cloudPlaceholder,
            )?.items?.single(),
        )
        assertEquals(
            cloudPlaceholder,
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(),
                ownerId,
                listOf(cloudPlaceholder),
            )?.items?.single(),
        )
        assertNull(acknowledgeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(completedBeforeAck)),
            ownerId,
            completedBeforeAck,
            cloudPlaceholder,
        ))
        assertEquals(
            completedBeforeAck,
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(completedBeforeAck)),
                ownerId,
                listOf(cloudPlaceholder),
            )?.items?.single(),
        )
    }

    @Test
    fun routedOcrFailureKeepsLocalDetailUntilRemotePlaceholderIsCancelled() {
        val processing = item(ownerId = ownerId).copy(
            scanCollectionId = collectionId,
            ocrText = "",
            dirty = true,
            processing = true,
        )
        val failed = checkNotNull(failScanSearchProcessingItem(
            processing,
            "Mistral OCR failed",
            "2026-08-21T12:01:00Z",
        ))
        assertTrue(failed.dirty)
        assertEquals(ScanSearchStatus.FAILED, failed.status)
        assertEquals(
            listOf(failed),
            dismissLocalScanSearchFailures(
                ScanSearchQueueStore(listOf(failed)),
                ownerId,
                queueId,
            )?.items,
        )

        val cleaned = checkNotNull(acknowledgeScanSearchFailureCleanupStore(
            ScanSearchQueueStore(listOf(failed)),
            ownerId,
            failed,
        )).items.single()
        assertFalse(cleaned.dirty)
        assertEquals("Mistral OCR failed", cleaned.errorMessage)
        assertTrue(dismissLocalScanSearchFailures(
            ScanSearchQueueStore(listOf(cleaned)),
            ownerId,
            queueId,
        )?.items?.isEmpty() == true)
        assertEquals(
            listOf(cleaned),
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(cleaned)),
                ownerId,
                emptyList(),
            )?.items,
        )
    }

    @Test
    fun matchedItemsRequireACanonicalCaptureAndPendingItemsCannotCarryOne() {
        val base = """{"version":1,"items":[{
            "id":"$queueId","owner_id":"","scan_collection_id":"$collectionId",
            "photo_role":"cover","ocr_text":"Herbal","status":"pending",
            "matched_capture_id":"$captureId","revision":0,
            "created_at":"2026-08-21T12:00:00Z","updated_at":"2026-08-21T12:00:00Z",
            "dirty":true}]}""".trimIndent()
        assertFalse(scanSearchQueueStoreFromJson(base).valid)
        val migrated = scanSearchQueueStoreFromJson(
            base.replace("\"pending\"", "\"matched\""),
        )
        assertTrue(migrated.valid)
        assertEquals(captureId, migrated.items.single().candidateCaptureId)
        assertEquals(1.0, migrated.items.single().matchConfidence!!, 0.0)
        assertTrue(migrated.items.single().matchEvidence.isNotEmpty())
    }

    @Test
    fun corruptExistingQueueIsNeverOverwritten() {
        val target = File(Files.createTempDirectory("scan-search-queue").toFile(), "queue.json")
        target.writeText("not json")
        val store = readScanSearchQueueStore(target)
        assertFalse(store.valid)
        assertFalse(saveScanSearchQueueStore(target, store.copy(items = emptyList())))
        assertEquals("not json", target.readText())
    }

    @Test
    fun localStatusesExactlyMatchTheCloudContract() {
        assertEquals(ScanSearchStatus.PENDING, ScanSearchStatus.fromWire("pending"))
        assertEquals(ScanSearchStatus.PROPOSED, ScanSearchStatus.fromWire("proposed"))
        assertEquals(ScanSearchStatus.MATCHED, ScanSearchStatus.fromWire("matched"))
        assertEquals(ScanSearchStatus.REJECTED, ScanSearchStatus.fromWire("rejected"))
        assertEquals(ScanSearchStatus.FAILED, ScanSearchStatus.fromWire("failed"))
        assertNull(ScanSearchStatus.fromWire("review"))
    }

    @Test
    fun normalizationRejectsOversizedTextAndLocallyDirtyFailure() {
        val base = item(ownerId = ownerId)
        assertNull(
            normalizedScanSearchQueueItem(
                base.copy(ocrText = "x".repeat(ScanSearchQueue.MAX_OCR_CHARS + 1)),
            ),
        )
        assertNull(
            normalizedScanSearchQueueItem(
                base.copy(status = ScanSearchStatus.FAILED, dirty = true),
            ),
        )
        assertEquals(
            ScanSearchStatus.FAILED,
            normalizedScanSearchQueueItem(
                base.copy(status = ScanSearchStatus.FAILED, dirty = false),
            )?.status,
        )
    }

    @Test
    fun ownerScopedReadNeverReturnsAnotherAccountsRows() {
        val mine = item(ownerId = ownerId)
        val other = item(id = captureId, ownerId = otherOwnerId)
        assertEquals(
            listOf(mine),
            ownerScopedScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine, other)),
                ownerId,
            ).items,
        )
        assertFalse(
            ownerScopedScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine)),
                "not-a-uuid",
            ).valid,
        )
    }

    @Test
    fun cloudMergePreservesForeignRowsAndNewerDirtyLocalIntent() {
        val mine = item(ownerId = ownerId, revision = 1).copy(dirty = false)
        val other = item(id = captureId, ownerId = otherOwnerId)
        val cloud = mine.copy(
            status = ScanSearchStatus.FAILED,
            revision = 2,
            dirty = false,
        )
        val dirty = mine.copy(
            status = ScanSearchStatus.MATCHED,
            candidateCaptureId = captureId,
            matchConfidence = 1.0,
            matchEvidence = legacyEvidence(),
            matchedCaptureId = captureId,
            dirty = true,
        )

        assertEquals(
            setOf(other, cloud),
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine, other)),
                ownerId,
                listOf(cloud),
            )?.items?.toSet(),
        )
        assertEquals(
            normalizedScanSearchQueueItem(dirty),
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(dirty, other)),
                ownerId,
                listOf(cloud),
            )?.items?.first { it.ownerId == ownerId },
        )
        assertNull(
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine)),
                ownerId,
                listOf(cloud.copy(ownerId = otherOwnerId)),
            ),
        )
    }

    @Test
    fun acknowledgementIsIdempotentAndCannotRewriteIdentityOrCommittedMatch() {
        val pending = item(ownerId = ownerId)
        val accepted = pending.copy(revision = 1, dirty = false)
        assertEquals(
            accepted,
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(pending)),
                ownerId,
                pending,
                accepted,
            )?.items?.single(),
        )
        assertNull(
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(pending)),
                ownerId,
                pending,
                accepted.copy(id = captureId),
            ),
        )

        val matched = pending.copy(
            status = ScanSearchStatus.MATCHED,
            candidateCaptureId = captureId,
            matchConfidence = 1.0,
            matchEvidence = legacyEvidence(),
            matchedCaptureId = captureId,
            revision = 1,
        )
        assertNull(
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(matched)),
                ownerId,
                matched,
                accepted.copy(revision = 2),
            ),
        )
        val concurrent = pending.copy(ocrText = "new local text")
        assertEquals(
            ScanSearchQueueStore(listOf(concurrent)),
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(concurrent)),
                ownerId,
                pending,
                accepted,
            ),
        )
    }

    @Test
    fun successiveDraftCapturesShareSessionAndRouteAtomically() {
        val cover = item(ownerId = ownerId).copy(
            scanCollectionId = "",
            sessionId = queueId,
            visualSignature = validSignature(),
        )
        val title = item(id = captureId, ownerId = ownerId).copy(
            scanCollectionId = "",
            sessionId = queueId,
            photoRole = ScanSearchPhotoRole.TITLE_PAGE,
            ocrText = "A New Herbal",
        )
        val routed = routeScanSearchSessionStore(
            ScanSearchQueueStore(listOf(cover, title)),
            ownerId,
            queueId,
            collectionId,
            "2026-08-21T12:05:00Z",
        )

        assertEquals(2, routed?.items?.size)
        assertTrue(routed!!.items.all { it.sessionId == queueId })
        assertTrue(routed.items.all { it.scanCollectionId == collectionId && it.dirty })
        assertNull(routeScanSearchSessionStore(
            routed,
            ownerId,
            queueId,
            otherOwnerId,
            "2026-08-21T12:06:00Z",
        ))
    }

    @Test
    fun routingARecaptureLeavesLocalFailureForExplicitDismissal() {
        val failed = checkNotNull(failScanSearchProcessingItem(
            item(ownerId = ownerId).copy(
                scanCollectionId = "",
                sessionId = queueId,
                ocrText = "",
                dirty = false,
                processing = true,
            ),
            "Mistral OCR failed",
            "2026-08-21T12:01:00Z",
        ))
        val recapture = item(id = secondQueueId, ownerId = ownerId).copy(
            scanCollectionId = "",
            sessionId = queueId,
            createdAt = "2026-08-21T12:02:00Z",
            updatedAt = "2026-08-21T12:02:00Z",
        )

        val routed = checkNotNull(routeScanSearchSessionStore(
            ScanSearchQueueStore(listOf(failed, recapture)),
            ownerId,
            queueId,
            collectionId,
            "2026-08-21T12:03:00Z",
        ))

        assertEquals(failed, routed.items.first { it.id == failed.id })
        assertEquals(
            collectionId,
            routed.items.first { it.id == recapture.id }.scanCollectionId,
        )
        assertTrue(routed.items.first { it.id == recapture.id }.dirty)

        val dismissed = checkNotNull(dismissLocalScanSearchFailures(
            routed,
            ownerId,
            queueId,
        ))
        assertEquals(listOf(recapture.id), dismissed.items.map { it.id })
    }

    @Test
    fun dismissingLocalFailuresNeverDeletesCloudOrAnotherOwnersFailure() {
        val local = checkNotNull(failScanSearchProcessingItem(
            item(ownerId = ownerId).copy(
                scanCollectionId = "",
                sessionId = queueId,
                ocrText = "",
                dirty = false,
                processing = true,
            ),
            "staging failed",
            "2026-08-21T12:01:00Z",
        ))
        val cloud = item(id = secondQueueId, ownerId = ownerId).copy(
            sessionId = queueId,
            status = ScanSearchStatus.FAILED,
            dirty = false,
        )
        val foreign = checkNotNull(failScanSearchProcessingItem(
            item(id = captureId, ownerId = otherOwnerId).copy(
                scanCollectionId = "",
                sessionId = queueId,
                ocrText = "",
                dirty = false,
                processing = true,
            ),
            "foreign failure",
            "2026-08-21T12:01:00Z",
        ))

        val dismissed = checkNotNull(dismissLocalScanSearchFailures(
            ScanSearchQueueStore(listOf(local, cloud, foreign)),
            ownerId,
            queueId,
        ))

        assertEquals(setOf(cloud.id, foreign.id), dismissed.items.mapTo(mutableSetOf()) { it.id })
        assertNull(dismissLocalScanSearchFailures(
            ScanSearchQueueStore(listOf(local)),
            "invalid-owner",
            queueId,
        ))
    }

    @Test
    fun coverCanQueueWithoutOcrOnlyWhenVisualSignatureIsValid() {
        val cover = item(ownerId = ownerId).copy(
            scanCollectionId = "",
            ocrText = "",
            visualSignature = validSignature(),
        )
        assertEquals(cover, normalizedScanSearchQueueItem(cover))
        assertNull(normalizedScanSearchQueueItem(cover.copy(visualSignature = "")))
        assertNull(normalizedScanSearchQueueItem(
            cover.copy(photoRole = ScanSearchPhotoRole.TITLE_PAGE),
        ))
    }

    @Test
    fun confidenceRatedProposalRoundTripsAndRejectDecisionIsProtected() {
        val proposed = item(ownerId = ownerId).copy(
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = captureId,
            matchConfidence = .91,
            matchEvidence = """{"version":1,"components":{"text":0.9}}""",
            revision = 2,
            dirty = false,
        )
        val encoded = scanSearchQueueStoreToJson(ScanSearchQueueStore(listOf(proposed)))
        val parsed = scanSearchQueueStoreFromJson(encoded)
        assertTrue("failed to decode $encoded", parsed.valid)
        val decoded = parsed.items.single()
        assertEquals(.91, decoded.matchConfidence!!, 0.0)
        assertEquals(captureId, decoded.candidateCaptureId)

        val rejected = proposed.copy(status = ScanSearchStatus.REJECTED, dirty = true)
        assertNull(acknowledgeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(rejected)),
            ownerId,
            rejected,
            proposed.copy(revision = 3),
        ))
    }

    @Test
    fun proposalAndTerminalShapesExactlyMatchSqlConstraints() {
        val base = item(ownerId = ownerId)
        val evidence = legacyEvidence()
        assertNull(normalizedScanSearchQueueItem(base.copy(
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = captureId,
        )))
        assertNull(normalizedScanSearchQueueItem(base.copy(
            status = ScanSearchStatus.REJECTED,
            candidateCaptureId = captureId,
            matchConfidence = .8,
        )))
        assertNull(normalizedScanSearchQueueItem(base.copy(
            status = ScanSearchStatus.MATCHED,
            candidateCaptureId = secondQueueId,
            matchedCaptureId = captureId,
            matchConfidence = .8,
            matchEvidence = evidence,
        )))
        assertNull(normalizedScanSearchQueueItem(base.copy(
            status = ScanSearchStatus.FAILED,
            candidateCaptureId = captureId,
            matchConfidence = .8,
            matchEvidence = evidence,
            dirty = false,
        )))
        assertEquals(
            ScanSearchStatus.MATCHED,
            normalizedScanSearchQueueItem(base.copy(
                status = ScanSearchStatus.MATCHED,
                candidateCaptureId = captureId,
                matchedCaptureId = captureId,
                matchConfidence = .8,
                matchEvidence = evidence,
            ))?.status,
        )
    }

    @Test
    fun evidenceLimitIsMeasuredAsUtf8Bytes() {
        val unicodeEvidence = """{"note":"${"🙂".repeat(2_100)}"}"""
        assertTrue(unicodeEvidence.length < ScanSearchQueue.MAX_MATCH_EVIDENCE_BYTES)
        assertTrue(
            unicodeEvidence.toByteArray(Charsets.UTF_8).size >
                ScanSearchQueue.MAX_MATCH_EVIDENCE_BYTES,
        )
        assertNull(normalizedScanSearchQueueItem(item(ownerId = ownerId).copy(
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = captureId,
            matchConfidence = .8,
            matchEvidence = unicodeEvidence,
            dirty = false,
        )))
    }

    @Test
    fun liveCloudMergePrunesCleanAcknowledgedDecisionsButRetainsDirtyIntent() {
        val proposal = item(ownerId = ownerId).copy(
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = captureId,
            matchConfidence = .8,
            matchEvidence = legacyEvidence(),
            revision = 2,
            dirty = false,
        )
        val cleanMatched = proposal.copy(
            status = ScanSearchStatus.MATCHED,
            matchedCaptureId = captureId,
            revision = 3,
        )
        val cleanRejected = proposal.copy(
            id = secondQueueId,
            status = ScanSearchStatus.REJECTED,
            revision = 3,
        )
        val live = item(id = otherOwnerId, ownerId = ownerId, revision = 4)
            .copy(dirty = false)

        val pruned = mergeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(cleanMatched, cleanRejected, live)),
            ownerId,
            listOf(live),
        )
        assertEquals(listOf(live), pruned?.items)

        val dirtyMatched = cleanMatched.copy(dirty = true)
        val retained = mergeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(dirtyMatched, live)),
            ownerId,
            listOf(live),
        )
        assertEquals(
            setOf(normalizedScanSearchQueueItem(dirtyMatched), live),
            retained?.items?.toSet(),
        )
        assertNull(mergeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(live)),
            ownerId,
            listOf(cleanMatched),
        ))

        val siblingProposal = proposal.copy(id = secondQueueId, sessionId = queueId)
        val refreshedProposal = proposal.copy(revision = proposal.revision + 1)
        val desktopTerminalizedSibling = mergeScanSearchQueueStore(
            ScanSearchQueueStore(listOf(proposal, siblingProposal)),
            ownerId,
            listOf(refreshedProposal),
        )
        assertEquals(listOf(normalizedScanSearchQueueItem(refreshedProposal)),
            desktopTerminalizedSibling?.items)
    }

    @Test
    fun versionOneQueueMigratesEachLegacyRowToItsOwnSession() {
        val legacy = """{"version":1,"items":[{
            "id":"$queueId","owner_id":"$ownerId","scan_collection_id":"$collectionId",
            "photo_role":"cover","ocr_text":"Herbal","status":"pending",
            "matched_capture_id":"","revision":0,
            "created_at":"2026-08-21T12:00:00Z","updated_at":"2026-08-21T12:00:00Z",
            "dirty":true}]}""".trimIndent()
        val parsed = scanSearchQueueStoreFromJson(legacy)
        assertTrue(parsed.valid)
        assertEquals(queueId, parsed.items.single().sessionId)
    }

    private fun item(
        id: String = queueId,
        ownerId: String,
        revision: Long = 0,
    ) = ScanSearchQueueItem(
        id = id,
        ownerId = ownerId,
        scanCollectionId = collectionId,
        photoRole = ScanSearchPhotoRole.COVER,
        ocrText = "Herbal",
        revision = revision,
        createdAt = "2026-08-21T12:00:00Z",
    )

    private fun validSignature(): String = checkNotNull(coverVisualSignature(
        48,
        64,
        IntArray(48 * 64) { 0xff336699.toInt() },
    ))

    private fun legacyEvidence(): String =
        """{"version":1,"components":{"legacy":1.0}}"""
}
