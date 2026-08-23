package org.whl.bookcapture

import kotlin.math.roundToInt

/**
 * Session-level state shown by the physical scan queue inspector.
 *
 * [ScanSearchQueueItem] is one identifying photo, while an operator thinks in
 * physical books. The inspector therefore collapses every cover/title
 * observation sharing a session id into one row and does not expose a proposal
 * until every newer observation in that session has been matched.
 */
internal enum class ScanQueueInspectorState {
    DRAFT,
    QUEUED,
    MATCHING,
    READY,
    FAILED,
    SAVING_APPROVAL,
    SAVING_REJECTION,
    APPROVED,
    REJECTED,
}

internal data class ScanQueueSessionPresentation(
    val sessionId: String,
    val items: List<ScanSearchQueueItem>,
    val representative: ScanSearchQueueItem,
    val state: ScanQueueInspectorState,
    val destinationCollectionId: String,
    val candidateCaptureId: String,
    val matchConfidence: Double?,
    val errorMessage: String,
    val staleProposal: Boolean,
    val proposalConflict: Boolean,
    val destinationConflict: Boolean,
) {
    val captureCount: Int get() = items.size
    val confidencePercent: Int? get() = matchConfidence?.times(100)?.roundToInt()
    val reviewable: Boolean
        get() = state == ScanQueueInspectorState.READY && candidateCaptureId.isNotEmpty()
}

/**
 * Builds stable, oldest-first queue rows without Android framework work.
 * Callers receive the original validated items so rendering can show a bounded
 * OCR excerpt or visual-only fallback without rereading the queue.
 */
internal fun scanQueueSessionPresentations(
    items: List<ScanSearchQueueItem>,
): List<ScanQueueSessionPresentation> = items
    .groupBy(ScanSearchQueueItem::sessionId)
    .values
    .map(::scanQueueSessionPresentation)
    .sortedWith(compareBy(
        { presentation -> presentation.items.minOf(ScanSearchQueueItem::createdAt) },
        ScanQueueSessionPresentation::sessionId,
    ))

private fun scanQueueSessionPresentation(
    rawItems: List<ScanSearchQueueItem>,
): ScanQueueSessionPresentation {
    require(rawItems.isNotEmpty())
    val items = rawItems.sortedWith(
        compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id),
    )
    val sessionId = items.first().sessionId
    require(sessionId.isNotEmpty() && items.all { it.sessionId == sessionId })

    val proposals = items.filter {
        it.status == ScanSearchStatus.PROPOSED && it.candidateCaptureId.isNotEmpty()
    }
    val candidateIds = proposals.mapTo(linkedSetOf(), ScanSearchQueueItem::candidateCaptureId)
    val destinations = items.mapNotNullTo(linkedSetOf()) {
        it.scanCollectionId.takeIf(String::isNotEmpty)
    }
    val hasPending = items.any { it.status == ScanSearchStatus.PENDING }
    val staleProposal = hasPending && proposals.isNotEmpty()
    val proposalConflict = candidateIds.size > 1
    val destinationConflict = destinations.size > 1
    val errorMessage = items.asSequence()
        .map(ScanSearchQueueItem::errorMessage)
        .map(String::trim)
        .filter(String::isNotEmpty)
        .distinct()
        .joinToString(" \u00b7 ")
        .take(ScanSearchQueue.MAX_ERROR_CHARS)

    val representative = when {
        hasPending -> items.first { it.status == ScanSearchStatus.PENDING }
        proposals.isNotEmpty() -> proposals.first()
        else -> items.first()
    }
    val state = when {
        proposalConflict || destinationConflict -> ScanQueueInspectorState.FAILED
        errorMessage.isNotEmpty() || items.any { it.status == ScanSearchStatus.FAILED } ->
            ScanQueueInspectorState.FAILED
        hasPending && items.any {
            it.status == ScanSearchStatus.PENDING && it.scanCollectionId.isEmpty()
        } -> ScanQueueInspectorState.DRAFT
        items.any(ScanSearchQueueItem::processing) -> ScanQueueInspectorState.MATCHING
        hasPending && items.any { it.status == ScanSearchStatus.PENDING && it.dirty } ->
            ScanQueueInspectorState.QUEUED
        hasPending -> ScanQueueInspectorState.MATCHING
        proposals.isNotEmpty() -> ScanQueueInspectorState.READY
        items.any { it.status == ScanSearchStatus.MATCHED && it.dirty } ->
            ScanQueueInspectorState.SAVING_APPROVAL
        items.any { it.status == ScanSearchStatus.REJECTED && it.dirty } ->
            ScanQueueInspectorState.SAVING_REJECTION
        items.any { it.status == ScanSearchStatus.MATCHED } -> ScanQueueInspectorState.APPROVED
        else -> ScanQueueInspectorState.REJECTED
    }
    val exposeProposal = state == ScanQueueInspectorState.READY && candidateIds.size == 1
    return ScanQueueSessionPresentation(
        sessionId = sessionId,
        items = items,
        representative = representative,
        state = state,
        destinationCollectionId = destinations.singleOrNull().orEmpty(),
        candidateCaptureId = candidateIds.singleOrNull().takeIf { exposeProposal }.orEmpty(),
        matchConfidence = proposals.firstOrNull()
            ?.matchConfidence
            ?.takeIf { exposeProposal },
        errorMessage = errorMessage,
        staleProposal = staleProposal,
        proposalConflict = proposalConflict,
        destinationConflict = destinationConflict,
    )
}
