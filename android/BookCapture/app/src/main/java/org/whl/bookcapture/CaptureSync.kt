package org.whl.bookcapture

/** Persisted phases for an explicitly requested capture-sync batch. */
internal enum class CaptureSyncPhase(
    val storedValue: String,
    val active: Boolean,
) {
    IDLE("idle", false),
    QUEUED("queued", true),
    RUNNING("running", true),
    WAITING_FOR_PROCESSING("waiting_for_processing", true),
    RETRYING("retrying", true),
    COMPLETE("complete", false),
    COMPLETE_WITH_ERRORS("complete_with_errors", false),
    FAILED("failed", false),
    ;

    companion object {
        fun fromStoredValue(value: String?): CaptureSyncPhase =
            entries.firstOrNull { it.storedValue == value?.trim() } ?: IDLE
    }
}

/** Durable authorization and accounting for one user-requested sync batch. */
internal data class CaptureSyncRecord(
    val requestId: String,
    val phase: CaptureSyncPhase,
    val targetIds: Set<String>,
    val syncedIds: Set<String>,
    val blockedIds: Set<String>,
    val transportMode: String = "cloud",
    val lanHost: String = "",
    val cloudOwner: String = "",
    val resolvedTransport: String = if (transportMode == "auto") "" else transportMode,
)

/** Aggregate state suitable for a button label, status row, or progress view. */
internal data class CaptureSyncState(
    val phase: CaptureSyncPhase,
    val eligibleCount: Int,
    val requestedCount: Int,
    val syncedCount: Int,
    val blockedCount: Int,
    val remainingCount: Int,
    val skippedCount: Int,
) {
    val active: Boolean get() = phase.active
}

internal enum class CaptureSyncFinishDecision {
    WAIT,
    COMPLETE,
    COMPLETE_WITH_ERRORS,
}

internal fun captureSyncFinishDecision(
    state: CaptureSyncState,
    sawDeferred: Boolean,
    hadError: Boolean,
): CaptureSyncFinishDecision = when {
    sawDeferred || state.remainingCount > 0 -> CaptureSyncFinishDecision.WAIT
    hadError || state.blockedCount > 0 || state.skippedCount > 0 ||
        state.syncedCount != state.requestedCount ->
        CaptureSyncFinishDecision.COMPLETE_WITH_ERRORS
    else -> CaptureSyncFinishDecision.COMPLETE
}

internal fun captureSyncSkippedTargetIds(
    record: CaptureSyncRecord,
    pendingIds: Collection<String>,
): Set<String> {
    val targets = normalizedCaptureSyncIds(record.targetIds)
    val synced = normalizedCaptureSyncIds(record.syncedIds).intersect(targets)
    val blocked = normalizedCaptureSyncIds(record.blockedIds).intersect(targets - synced)
    val pending = normalizedCaptureSyncIds(pendingIds).intersect(targets - synced - blocked)
    return targets - synced - blocked - pending
}

internal fun captureSyncTerminalReconciliationIds(
    record: CaptureSyncRecord,
    pendingIds: Collection<String>,
): Set<String> {
    val targets = normalizedCaptureSyncIds(record.targetIds)
    val synced = normalizedCaptureSyncIds(record.syncedIds).intersect(targets)
    val blocked = normalizedCaptureSyncIds(record.blockedIds).intersect(targets - synced)
    return captureSyncSkippedTargetIds(record, pendingIds) + blocked
}

internal fun activeCaptureSyncRecordForRequest(
    current: CaptureSyncRecord?,
    requestId: String,
): CaptureSyncRecord? = current?.takeIf {
    it.requestId == requestId && it.phase.active
}

/** Build a terminal update only when no button press or worker changed the
 * durable batch after its completion decision was calculated. */
internal fun terminalCaptureSyncRecord(
    current: CaptureSyncRecord?,
    expected: CaptureSyncRecord,
    phase: CaptureSyncPhase,
): CaptureSyncRecord? {
    require(!phase.active) { "Capture sync terminal phase is required" }
    return current?.takeIf { it == expected && it.phase.active }?.copy(phase = phase)
}

internal data class CaptureSyncStart(
    val record: CaptureSyncRecord,
    val created: Boolean,
)

/** Folder ids are never accepted from UI text, intents, or WorkManager input. */
internal fun normalizedCaptureSyncIds(ids: Collection<String>): Set<String> =
    ids.asSequence()
        .map(String::trim)
        .filter { id ->
            id.isNotEmpty() && id != "." && id != ".." &&
                id.matches(Regex("[A-Za-z0-9._-]+"))
        }
        .toSortedSet()

internal fun beginCaptureSyncRecord(
    existing: CaptureSyncRecord?,
    targetIds: Collection<String>,
    newRequestId: String,
    transportMode: String = "cloud",
    lanHost: String = "",
    cloudOwner: String = "",
): CaptureSyncStart {
    val targets = normalizedCaptureSyncIds(targetIds)
    existing?.takeIf { it.phase.active }?.let {
        val existingTargets = normalizedCaptureSyncIds(it.targetIds)
        val existingSynced = normalizedCaptureSyncIds(it.syncedIds).intersect(existingTargets)
        // Keep the request identity while work may still be in flight. Replacing
        // it would let an old worker deliver queue/ -> sent/ with a receipt the
        // new generation cannot account for. Reconcile the queue snapshot in
        // place instead. Targets remain monotonic until the batch reaches a
        // terminal phase: an in-flight worker may already have moved a folder
        // out of queue/ but not yet recorded it as synced.
        val reconciledTargets = (existingTargets + targets).toSortedSet()
        val reconciled = it.copy(
            targetIds = reconciledTargets,
            syncedIds = existingSynced.intersect(reconciledTargets),
            blockedIds = normalizedCaptureSyncIds(it.blockedIds)
                .intersect(reconciledTargets - existingSynced),
        )
        return CaptureSyncStart(
            record = if (reconciled == it) it else reconciled,
            created = false,
        )
    }
    val requestId = newRequestId.trim()
    require(requestId.isNotEmpty()) { "Capture sync request id is required" }
    val mode = transportMode.takeIf { it in setOf("cloud", "lan", "auto") }
        ?: "cloud"
    return CaptureSyncStart(
        CaptureSyncRecord(
            requestId = requestId,
            phase = if (targets.isEmpty()) CaptureSyncPhase.COMPLETE else CaptureSyncPhase.QUEUED,
            targetIds = targets,
            syncedIds = emptySet(),
            blockedIds = emptySet(),
            transportMode = mode,
            lanHost = lanHost.trim(),
            cloudOwner = cloudOwner.trim(),
            resolvedTransport = if (mode == "auto") "" else mode,
        ),
        created = true,
    )
}

/** A confirmation may complete while the old worker is still finishing its
 * local ownership rejection. Keep that request identity for delivery-receipt
 * recovery, but reopen every newly claimable target and replace its chain. */
internal fun reopenCaptureSyncAfterCloudClaim(
    record: CaptureSyncRecord?,
    currentOwner: String,
    claimedIds: Collection<String>,
): CaptureSyncRecord? {
    val current = record?.takeIf { it.phase.active } ?: return null
    val owner = currentOwner.trim()
    if (owner.isEmpty() || current.cloudOwner.isNotEmpty() && current.cloudOwner != owner) {
        return null
    }
    if (current.transportMode == "lan" || current.resolvedTransport == "lan") return null
    val claimed = normalizedCaptureSyncIds(claimedIds).intersect(current.targetIds)
    if (claimed.isEmpty()) return null
    return current.copy(
        phase = CaptureSyncPhase.RETRYING,
        blockedIds = current.blockedIds - claimed,
    )
}

/** Why a sync request had nothing to send. "No captures ready to sync" is
 * true but unhelpful next to a list of rows reading "failed": those rows are
 * reporting this phone's OCR, and their photos are already delivered. Naming
 * the actual situation is what separates the two. */
internal enum class CaptureSyncEmptyReason {
    REVIEW_QUEUED,
    LIVE_CAPTURE_ONLY,
    DELIVERED_WITH_PROCESSING_ISSUES,
    ALL_DELIVERED,
    NOTHING,
}

internal fun captureSyncEmptyReason(
    requestedCount: Int,
    pendingReviewChanges: Boolean,
    liveCaptureOpen: Boolean,
    deliveredCount: Int,
    deliveredNeedingAttention: Int,
): CaptureSyncEmptyReason = when {
    requestedCount > 0 -> CaptureSyncEmptyReason.NOTHING
    // Matches the pre-existing precedence: a queued review edit is the one
    // thing an "empty" sync press still actually did.
    pendingReviewChanges -> CaptureSyncEmptyReason.REVIEW_QUEUED
    liveCaptureOpen && deliveredCount == 0 -> CaptureSyncEmptyReason.LIVE_CAPTURE_ONLY
    deliveredNeedingAttention > 0 -> CaptureSyncEmptyReason.DELIVERED_WITH_PROCESSING_ISSUES
    deliveredCount > 0 -> CaptureSyncEmptyReason.ALL_DELIVERED
    else -> CaptureSyncEmptyReason.NOTHING
}

internal fun aggregateCaptureSyncState(
    record: CaptureSyncRecord?,
    eligibleIds: Collection<String>,
    pendingIds: Collection<String>,
): CaptureSyncState {
    val eligible = normalizedCaptureSyncIds(eligibleIds)
    if (record == null || record.requestId.isBlank()) {
        return CaptureSyncState(
            phase = CaptureSyncPhase.IDLE,
            eligibleCount = eligible.size,
            requestedCount = 0,
            syncedCount = 0,
            blockedCount = 0,
            remainingCount = 0,
            skippedCount = 0,
        )
    }

    val targets = normalizedCaptureSyncIds(record.targetIds)
    val synced = normalizedCaptureSyncIds(record.syncedIds).intersect(targets)
    val blocked = normalizedCaptureSyncIds(record.blockedIds).intersect(targets - synced)
    val pending = normalizedCaptureSyncIds(pendingIds).intersect(targets - synced - blocked)
    val skipped = captureSyncSkippedTargetIds(record, pendingIds)
    return CaptureSyncState(
        phase = record.phase,
        eligibleCount = eligible.size,
        requestedCount = targets.size,
        syncedCount = synced.size,
        blockedCount = blocked.size,
        remainingCount = pending.size,
        skippedCount = skipped.size,
    )
}
