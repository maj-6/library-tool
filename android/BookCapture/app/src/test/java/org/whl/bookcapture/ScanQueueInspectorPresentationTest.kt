package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanQueueInspectorPresentationTest {

    @Test
    fun observationsAreGroupedIntoOldestFirstPhysicalBookSessions() {
        val newer = item(
            id = secondItemId,
            sessionId = secondSessionId,
            createdAt = "2026-08-22T12:10:00Z",
        )
        val title = item(
            id = secondPhotoId,
            sessionId = sessionId,
            role = ScanSearchPhotoRole.TITLE_PAGE,
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = candidateId,
            confidence = .905,
            createdAt = "2026-08-22T12:01:00Z",
            dirty = false,
        )
        val cover = item(
            id = firstItemId,
            sessionId = sessionId,
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = candidateId,
            confidence = .905,
            createdAt = "2026-08-22T12:00:00Z",
            dirty = false,
        )

        val presentations = scanQueueSessionPresentations(listOf(newer, title, cover))

        assertEquals(listOf(sessionId, secondSessionId), presentations.map { it.sessionId })
        val ready = presentations.first()
        assertEquals(2, ready.captureCount)
        assertEquals(listOf(firstItemId, secondPhotoId), ready.items.map { it.id })
        assertEquals(ScanQueueInspectorState.READY, ready.state)
        assertEquals(firstItemId, ready.representative.id)
        assertEquals(collectionId, ready.destinationCollectionId)
        assertEquals(candidateId, ready.candidateCaptureId)
        assertEquals(91, ready.confidencePercent)
        assertTrue(ready.reviewable)
    }

    @Test
    fun draftAndLocalProcessingStatesStayDistinctFromCloudMatching() {
        val draft = item(
            id = firstItemId,
            sessionId = sessionId,
            collectionId = "",
            processing = true,
        )
        val processing = item(
            id = secondItemId,
            sessionId = secondSessionId,
            processing = true,
        )
        val queued = item(
            id = thirdItemId,
            sessionId = thirdSessionId,
            dirty = true,
        )
        val matching = item(
            id = fourthItemId,
            sessionId = fourthSessionId,
            dirty = false,
        )

        val bySession = scanQueueSessionPresentations(
            listOf(draft, processing, queued, matching),
        ).associateBy(ScanQueueSessionPresentation::sessionId)

        assertEquals(ScanQueueInspectorState.DRAFT, bySession.getValue(sessionId).state)
        assertEquals(
            ScanQueueInspectorState.MATCHING,
            bySession.getValue(secondSessionId).state,
        )
        assertEquals(ScanQueueInspectorState.QUEUED, bySession.getValue(thirdSessionId).state)
        assertEquals(
            ScanQueueInspectorState.MATCHING,
            bySession.getValue(fourthSessionId).state,
        )
    }

    @Test
    fun newerPendingEvidenceSuppressesAnOlderProposalUntilRematched() {
        val proposal = item(
            id = firstItemId,
            sessionId = sessionId,
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = candidateId,
            confidence = .93,
            dirty = false,
        )
        val pending = item(
            id = secondItemId,
            sessionId = sessionId,
            createdAt = "2026-08-22T12:01:00Z",
            dirty = false,
        )

        val presentation = scanQueueSessionPresentations(listOf(proposal, pending)).single()

        assertEquals(ScanQueueInspectorState.MATCHING, presentation.state)
        assertTrue(presentation.staleProposal)
        assertEquals("", presentation.candidateCaptureId)
        assertEquals(null, presentation.confidencePercent)
        assertFalse(presentation.reviewable)
    }

    @Test
    fun errorsAndConflictingProposalsCannotBecomeReviewable() {
        val failed = scanQueueSessionPresentations(listOf(item(
            id = firstItemId,
            sessionId = sessionId,
            errorMessage = "Mistral request failed",
        ))).single()
        assertEquals(ScanQueueInspectorState.FAILED, failed.state)
        assertEquals("Mistral request failed", failed.errorMessage)
        assertFalse(failed.reviewable)

        val firstProposal = item(
            id = secondItemId,
            sessionId = secondSessionId,
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = candidateId,
            confidence = .88,
            dirty = false,
        )
        val secondProposal = item(
            id = thirdItemId,
            sessionId = secondSessionId,
            status = ScanSearchStatus.PROPOSED,
            candidateCaptureId = otherCandidateId,
            confidence = .86,
            createdAt = "2026-08-22T12:01:00Z",
            dirty = false,
        )
        val conflict = scanQueueSessionPresentations(
            listOf(firstProposal, secondProposal),
        ).single()
        assertEquals(ScanQueueInspectorState.FAILED, conflict.state)
        assertTrue(conflict.proposalConflict)
        assertEquals("", conflict.candidateCaptureId)
        assertFalse(conflict.reviewable)
    }

    @Test
    fun failedCaptureCanBeDismissedWithoutHidingItsSuccessfulRecapture() {
        val failed = item(
            id = firstItemId,
            sessionId = sessionId,
            status = ScanSearchStatus.FAILED,
            dirty = false,
            errorMessage = "Mistral request failed",
        ).copy(ocrText = "")
        val recapture = item(
            id = secondItemId,
            sessionId = sessionId,
            createdAt = "2026-08-22T12:01:00Z",
        )

        val blocked = scanQueueSessionPresentations(listOf(failed, recapture)).single()
        assertEquals(ScanQueueInspectorState.FAILED, blocked.state)
        assertEquals("Mistral request failed", blocked.errorMessage)

        val recovered = scanQueueSessionPresentations(listOf(recapture)).single()
        assertEquals(ScanQueueInspectorState.QUEUED, recovered.state)
    }

    @Test
    fun terminalLocalDecisionsExposeTheirSavingState() {
        val approval = item(
            id = firstItemId,
            sessionId = sessionId,
            status = ScanSearchStatus.MATCHED,
            candidateCaptureId = candidateId,
            matchedCaptureId = candidateId,
            confidence = .9,
        )
        val rejection = item(
            id = secondItemId,
            sessionId = secondSessionId,
            status = ScanSearchStatus.REJECTED,
            candidateCaptureId = candidateId,
            confidence = .9,
        )

        val bySession = scanQueueSessionPresentations(listOf(approval, rejection))
            .associateBy(ScanQueueSessionPresentation::sessionId)
        assertEquals(
            ScanQueueInspectorState.SAVING_APPROVAL,
            bySession.getValue(sessionId).state,
        )
        assertEquals(
            ScanQueueInspectorState.SAVING_REJECTION,
            bySession.getValue(secondSessionId).state,
        )
    }

    private fun item(
        id: String,
        sessionId: String,
        collectionId: String = DEFAULT_COLLECTION_ID,
        role: ScanSearchPhotoRole = ScanSearchPhotoRole.COVER,
        status: ScanSearchStatus = ScanSearchStatus.PENDING,
        candidateCaptureId: String = "",
        matchedCaptureId: String = "",
        confidence: Double? = null,
        createdAt: String = "2026-08-22T12:00:00Z",
        dirty: Boolean = true,
        processing: Boolean = false,
        errorMessage: String = "",
    ) = ScanSearchQueueItem(
        id = id,
        ownerId = ownerId,
        sessionId = sessionId,
        scanCollectionId = collectionId,
        photoRole = role,
        ocrText = "A New Herbal",
        status = status,
        candidateCaptureId = candidateCaptureId,
        matchConfidence = confidence,
        matchEvidence = if (candidateCaptureId.isEmpty()) "" else
            """{"version":1,"components":{"text":0.9}}""",
        matchedCaptureId = matchedCaptureId,
        createdAt = createdAt,
        dirty = dirty,
        processing = processing,
        errorMessage = errorMessage,
    )

    private companion object {
        const val ownerId = "11111111-1111-4111-8111-111111111111"
        const val DEFAULT_COLLECTION_ID = "22222222-2222-4222-8222-222222222222"
        const val collectionId = DEFAULT_COLLECTION_ID
        const val sessionId = "33333333-3333-4333-8333-333333333333"
        const val secondSessionId = "44444444-4444-4444-8444-444444444444"
        const val thirdSessionId = "55555555-5555-4555-8555-555555555555"
        const val fourthSessionId = "66666666-6666-4666-8666-666666666666"
        const val firstItemId = "77777777-7777-4777-8777-777777777777"
        const val secondItemId = "88888888-8888-4888-8888-888888888888"
        const val secondPhotoId = "99999999-9999-4999-8999-999999999999"
        const val thirdItemId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        const val fourthItemId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        const val candidateId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        const val otherCandidateId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    }
}
