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
    existing?.takeIf { it.phase.active }?.let {
        return CaptureSyncStart(it, created = false)
    }
    val requestId = newRequestId.trim()
    require(requestId.isNotEmpty()) { "Capture sync request id is required" }
    val targets = normalizedCaptureSyncIds(targetIds)
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
    val pending = normalizedCaptureSyncIds(pendingIds).intersect(targets - synced)
    val skipped = targets - synced - blocked - pending
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
