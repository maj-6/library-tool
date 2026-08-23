package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.util.UUID

internal enum class ScanSearchPhotoRole(val wireValue: String) {
    COVER("cover"),
    TITLE_PAGE("title_page");

    companion object {
        fun fromWire(value: String): ScanSearchPhotoRole? = entries.firstOrNull {
            it.wireValue == value.trim().lowercase()
        }
    }
}

internal enum class ScanSearchStatus(val wireValue: String) {
    PENDING("pending"),
    PROPOSED("proposed"),
    MATCHED("matched"),
    REJECTED("rejected"),
    FAILED("failed");

    companion object {
        fun fromWire(value: String): ScanSearchStatus? = entries.firstOrNull {
            it.wireValue == value.trim().lowercase()
        }
    }
}

internal fun ScanSearchStatus.isLiveCloudQueueStatus(): Boolean =
    this == ScanSearchStatus.PENDING ||
        this == ScanSearchStatus.PROPOSED ||
        this == ScanSearchStatus.FAILED

/** A server reservation that contains no evidence and is safe to CAS-cancel. */
internal fun ScanSearchQueueItem.isBlankCloudReservation(): Boolean =
    !processing && !dirty && errorMessage.isEmpty() && revision > 0L &&
        scanCollectionId.isNotEmpty() &&
        status in setOf(ScanSearchStatus.PENDING, ScanSearchStatus.FAILED) &&
        ocrText.isEmpty() && visualSignature.isEmpty() &&
        candidateCaptureId.isEmpty() && matchedCaptureId.isEmpty() &&
        matchConfidence == null && matchEvidence.isEmpty()

/**
 * A privacy-conscious search request. The temporary camera image is sent to
 * Mistral OCR 4.1 and deleted as before; only the bounded recognized text and
 * a bounded non-reversible visual descriptor and recognized text enter the
 * durable queue. Successive rows share [sessionId] and are reviewed as one
 * physical book; no source pixels are retained here.
 */
internal data class ScanSearchQueueItem(
    val id: String,
    val ownerId: String = "",
    val sessionId: String = id,
    val scanCollectionId: String,
    val photoRole: ScanSearchPhotoRole,
    val ocrText: String,
    /** Canonical JSON for whl-cover-v1, or empty for a title-page-only row. */
    val visualSignature: String = "",
    val status: ScanSearchStatus = ScanSearchStatus.PENDING,
    val candidateCaptureId: String = "",
    val matchConfidence: Double? = null,
    /** Bounded versioned JSON supplied by the matcher; never arbitrary UI text. */
    val matchEvidence: String = "",
    val matchedCaptureId: String = "",
    val revision: Long = 0,
    val createdAt: String,
    val updatedAt: String = createdAt,
    val dirty: Boolean = true,
    /** Mistral work in progress; routed placeholders sync without source pixels. */
    val processing: Boolean = false,
    /** Bounded local failure detail; cloud rows always leave this empty. */
    val errorMessage: String = "",
)

internal data class ScanSearchQueueStore(
    val items: List<ScanSearchQueueItem> = emptyList(),
    val valid: Boolean = true,
)

internal object ScanSearchQueue {
    const val FILE_NAME = "scan_search_queue.json"
    const val VERSION = 3
    const val MAX_OCR_CHARS = 16_000
    const val MAX_OCR_BYTES = 65_536
    const val MAX_VISUAL_SIGNATURE_BYTES = 4_096
    const val MAX_MATCH_EVIDENCE_BYTES = 8_192
    const val MAX_ERROR_CHARS = 500
    const val MAX_ERROR_BYTES = 2_048
    const val MAX_ITEMS = 500
    private val lock = Any()

    /** Only the signed-in owner's rows are visible through the app-facing read. */
    fun read(ctx: Context): ScanSearchQueueStore {
        val owner = currentScanSearchOwner(ctx) ?: return ScanSearchQueueStore()
        val store = ownerScopedScanSearchQueueStore(read(file(ctx)), owner)
        return if (currentScanSearchOwner(ctx) == owner) store else ScanSearchQueueStore()
    }

    internal fun read(target: File): ScanSearchQueueStore = synchronized(lock) {
        readScanSearchQueueStore(target)
    }

    fun enqueue(
        ctx: Context,
        scanCollectionId: String,
        photoRole: ScanSearchPhotoRole,
        ocrText: String,
        sessionId: String = "",
        visualSignature: String = "",
        id: String = UUID.randomUUID().toString(),
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = enqueueInternal(
        ctx = ctx,
        scanCollectionId = scanCollectionId,
        photoRole = photoRole,
        ocrText = ocrText,
        sessionId = sessionId.ifBlank { id },
        visualSignature = visualSignature,
        id = id,
        now = now,
    )

    /**
     * Persist camera acceptance before network OCR starts. The placeholder is
     * metadata-only until [completeProcessing] adds Mistral/visual evidence.
     * A routed placeholder is an immediate Supabase outbox mutation; a draft
     * remains local until the operator chooses A/B/C.
     */
    fun enqueueProcessing(
        ctx: Context,
        ownerId: String,
        sessionId: String,
        scanCollectionId: String,
        photoRole: ScanSearchPhotoRole,
        id: String = UUID.randomUUID().toString(),
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = synchronized(lock) {
        val owner = ownerId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner) || currentScanSearchOwner(ctx) != owner) {
            return@synchronized null
        }
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val item = normalizedScanSearchQueueItem(
            ScanSearchQueueItem(
                id = id,
                ownerId = owner,
                sessionId = sessionId,
                scanCollectionId = scanCollectionId,
                photoRole = photoRole,
                ocrText = "",
                status = ScanSearchStatus.PENDING,
                createdAt = now.toString(),
                dirty = scanCollectionId.isNotBlank(),
                processing = true,
            ),
        ) ?: return@synchronized null
        store.items.firstOrNull { it.id == item.id }?.let { existing ->
            return@synchronized existing.takeIf { it == item }
        }
        if (store.items.size >= MAX_ITEMS) return@synchronized null
        val next = (store.items + item)
            .sortedWith(compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id))
        if (currentScanSearchOwner(ctx) != owner ||
            !saveScanSearchQueueStore(target, ScanSearchQueueStore(next))
        ) null else item
    }

    /** Replace one exact local placeholder with evidence ready for cloud sync. */
    fun completeProcessing(
        ctx: Context,
        id: String,
        ocrText: String,
        visualSignature: String = "",
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = updateItem(ctx, id) { item ->
        completeScanSearchProcessingItem(
            item,
            boundedScanSearchOcrText(ocrText),
            visualSignature,
            now.toString(),
        )
    }

    /** Keep a failed photo visible; routed failures cancel any cloud placeholder. */
    fun failProcessing(
        ctx: Context,
        id: String,
        errorMessage: String,
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = updateItem(ctx, id) { item ->
        failScanSearchProcessingItem(
            item,
            boundedScanSearchErrorMessage(errorMessage),
            now.toString(),
        )
    }

    /**
     * Worker-only failure reconciliation that remains valid after sign-out or
     * an account switch. Both the row id and its original owner must match, so
     * another account can never mutate the abandoned observation.
     */
    internal fun failProcessingForWorker(
        ctx: Context,
        id: String,
        ownerId: String,
        errorMessage: String,
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = synchronized(lock) {
        val normalizedId = id.trim().lowercase()
        val owner = ownerId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(normalizedId) ||
            !SAFE_CAPTURE_SYNC_ID.matches(owner)
        ) return@synchronized null
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val index = store.items.indexOfFirst {
            it.id == normalizedId && it.ownerId == owner
        }
        if (index < 0) return@synchronized null
        val replacement = failScanSearchProcessingItem(
            store.items[index],
            boundedScanSearchErrorMessage(errorMessage),
            now.toString(),
        ) ?: return@synchronized null
        if (replacement == store.items[index]) return@synchronized replacement
        val next = store.items.toMutableList().apply { this[index] = replacement }
        if (saveScanSearchQueueStore(target, ScanSearchQueueStore(next))) replacement else null
    }

    /** Unscoped lookup reserved for a durable worker reconciling account changes. */
    internal fun processingItemForWorker(ctx: Context, id: String): ScanSearchQueueItem? =
        synchronized(lock) {
            val normalizedId = id.trim().lowercase()
            if (!SAFE_CAPTURE_SYNC_ID.matches(normalizedId)) return@synchronized null
            val store = readScanSearchQueueStore(file(ctx))
            if (!store.valid) return@synchronized null
            store.items.firstOrNull { it.id == normalizedId && it.processing }
        }

    /** Unscoped owner lookup used only to purge private OCR photos at sign-out. */
    internal fun processingItemsForWorker(
        ctx: Context,
        ownerId: String,
    ): List<ScanSearchQueueItem> = synchronized(lock) {
        val owner = ownerId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return@synchronized emptyList()
        val store = readScanSearchQueueStore(file(ctx))
        if (!store.valid) return@synchronized emptyList()
        store.items.filter { it.ownerId == owner && it.processing }
    }

    /** Persist a capture before the operator chooses A/B/C. Drafts never sync. */
    fun enqueueDraft(
        ctx: Context,
        sessionId: String,
        photoRole: ScanSearchPhotoRole,
        ocrText: String,
        visualSignature: String = "",
        id: String = UUID.randomUUID().toString(),
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = enqueueInternal(
        ctx = ctx,
        scanCollectionId = "",
        photoRole = photoRole,
        ocrText = ocrText,
        sessionId = sessionId,
        visualSignature = visualSignature,
        id = id,
        now = now,
    )

    private fun enqueueInternal(
        ctx: Context,
        scanCollectionId: String,
        photoRole: ScanSearchPhotoRole,
        ocrText: String,
        sessionId: String,
        visualSignature: String,
        id: String,
        now: Instant,
    ): ScanSearchQueueItem? = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized null
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val item = normalizedScanSearchQueueItem(
            ScanSearchQueueItem(
                id = id,
                ownerId = owner,
                sessionId = sessionId,
                scanCollectionId = scanCollectionId,
                photoRole = photoRole,
                ocrText = ocrText,
                visualSignature = visualSignature,
                createdAt = now.toString(),
            ),
        ) ?: return@synchronized null
        store.items.firstOrNull { it.id == item.id }?.let { existing ->
            return@synchronized existing.takeIf {
                    it.ownerId == item.ownerId &&
                    it.sessionId == item.sessionId &&
                    it.scanCollectionId == item.scanCollectionId &&
                    it.photoRole == item.photoRole &&
                    it.ocrText == item.ocrText &&
                    it.visualSignature == item.visualSignature
            }
        }
        if (store.items.size >= MAX_ITEMS) return@synchronized null
        val next = (store.items + item)
            .sortedWith(compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id))
        if (currentScanSearchOwner(ctx) != owner ||
            !saveScanSearchQueueStore(target, ScanSearchQueueStore(next))
        ) null else item
    }

    /** Atomically publish every capture in one book session to an A/B/C destination. */
    fun routeSession(
        ctx: Context,
        sessionId: String,
        scanCollectionId: String,
        now: Instant = Instant.now(),
    ): List<ScanSearchQueueItem>? = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized null
        val session = sessionId.trim().lowercase()
        val destination = scanCollectionId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(session) ||
            !SAFE_CAPTURE_SYNC_ID.matches(destination)
        ) return@synchronized null
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val routed = routeScanSearchSessionStore(
            store,
            owner,
            session,
            destination,
            now.toString(),
        ) ?: return@synchronized null
        if (currentScanSearchOwner(ctx) != owner ||
            !saveScanSearchQueueStore(target, routed)
        ) return@synchronized null
        routed.items.filter {
            it.ownerId == owner && it.sessionId == session &&
                it.status == ScanSearchStatus.PENDING &&
                it.scanCollectionId == destination
        }
    }

    /** Remove only local OCR/staging failures; cloud-authored rows are retained. */
    fun dismissLocalFailures(ctx: Context, sessionId: String): List<String>? = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized null
        val session = sessionId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(session)) return@synchronized null
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val next = dismissLocalScanSearchFailures(store, owner, session)
            ?: return@synchronized null
        val retainedIds = next.items.mapTo(mutableSetOf(), ScanSearchQueueItem::id)
        val removedIds = store.items.map(ScanSearchQueueItem::id).filterNot(retainedIds::contains)
        if (removedIds.isEmpty()) return@synchronized emptyList()
        if (currentScanSearchOwner(ctx) != owner || !saveScanSearchQueueStore(target, next)) {
            return@synchronized null
        }
        removedIds
    }

    fun oldestDraftSession(ctx: Context): String? = read(ctx).items
        .firstOrNull {
            it.status == ScanSearchStatus.PENDING && it.scanCollectionId.isEmpty()
        }
        ?.sessionId

    fun approveProposal(ctx: Context, id: String, captureId: String): Boolean {
        val capture = captureId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(capture)) return false
        return update(ctx, id) { item ->
            when {
                item.status == ScanSearchStatus.PROPOSED &&
                    item.candidateCaptureId == capture -> item.copy(
                    status = ScanSearchStatus.MATCHED,
                    matchedCaptureId = capture,
                    dirty = true,
                    updatedAt = Instant.now().toString(),
                )
                item.status == ScanSearchStatus.MATCHED &&
                    item.matchedCaptureId == capture -> item
                else -> null
            }
        }
    }

    fun rejectProposal(ctx: Context, id: String, captureId: String): Boolean {
        val capture = captureId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(capture)) return false
        return update(ctx, id) { item ->
            when {
                item.status == ScanSearchStatus.PROPOSED &&
                    item.candidateCaptureId == capture -> item.copy(
                    status = ScanSearchStatus.REJECTED,
                    dirty = true,
                    updatedAt = Instant.now().toString(),
                )
                item.status == ScanSearchStatus.REJECTED &&
                    item.candidateCaptureId == capture -> item
                else -> null
            }
        }
    }

    /** Adopt the server's authoritative revision without erasing newer local intent. */
    fun acknowledge(
        ctx: Context,
        expected: ScanSearchQueueItem,
        cloud: ScanSearchQueueItem,
    ): Boolean = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized true
        if (expected.ownerId != owner) return@synchronized true
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized false
        val next = acknowledgeScanSearchQueueStore(store, owner, expected, cloud)
            ?: return@synchronized false
        currentScanSearchOwner(ctx) != owner || next == store ||
            saveScanSearchQueueStore(target, next)
    }

    /** Mark an exact local error clean after its remote placeholder was removed. */
    fun acknowledgeFailureCleanup(
        ctx: Context,
        expected: ScanSearchQueueItem,
    ): Boolean = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized true
        if (expected.ownerId != owner) return@synchronized true
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized false
        val next = acknowledgeScanSearchFailureCleanupStore(store, owner, expected)
            ?: return@synchronized false
        currentScanSearchOwner(ctx) != owner || next == store ||
            saveScanSearchQueueStore(target, next)
    }

    /** Merge a cloud snapshot; a local dirty row wins until its RPC is acknowledged. */
    fun mergeCloud(
        ctx: Context,
        ownerId: String,
        cloudItems: List<ScanSearchQueueItem>,
    ): Boolean = synchronized(lock) {
        val owner = ownerId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return@synchronized false
        if (currentScanSearchOwner(ctx) != owner) return@synchronized true
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized false
        val merged = mergeScanSearchQueueStore(store, owner, cloudItems)
            ?: return@synchronized false
        if (currentScanSearchOwner(ctx) != owner || merged == store) true
        else saveScanSearchQueueStore(target, merged)
    }

    /**
     * Drop one exact stale outbox intent and merge the snapshot that proved it
     * stale. This is atomic locally, so a dirty decision/failure cannot mask the
     * authoritative row (or its authoritative absence).
     */
    fun mergeCloudAfterStaleMutation(
        ctx: Context,
        ownerId: String,
        expected: ScanSearchQueueItem,
        cloudItems: List<ScanSearchQueueItem>,
    ): Boolean = synchronized(lock) {
        val owner = ownerId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return@synchronized false
        if (currentScanSearchOwner(ctx) != owner) return@synchronized true
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized false
        val merged = mergeScanSearchQueueStoreAfterStaleMutation(
            store,
            owner,
            expected,
            cloudItems,
        ) ?: return@synchronized false
        if (currentScanSearchOwner(ctx) != owner || merged == store) true
        else saveScanSearchQueueStore(target, merged)
    }

    private fun update(
        ctx: Context,
        id: String,
        transform: (ScanSearchQueueItem) -> ScanSearchQueueItem?,
    ): Boolean = updateItem(ctx, id, transform) != null

    private fun updateItem(
        ctx: Context,
        id: String,
        transform: (ScanSearchQueueItem) -> ScanSearchQueueItem?,
    ): ScanSearchQueueItem? = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized null
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val normalizedId = id.trim().lowercase()
        val index = store.items.indexOfFirst {
            it.id == normalizedId && it.ownerId == owner
        }
        if (index < 0) return@synchronized null
        val transformed = transform(store.items[index]) ?: return@synchronized null
        val replacement = normalizedScanSearchQueueItem(transformed)
            ?: return@synchronized null
        if (replacement.id != normalizedId || replacement.ownerId != owner) {
            return@synchronized null
        }
        if (replacement == store.items[index]) return@synchronized replacement
        val next = store.items.toMutableList().apply { this[index] = replacement }
        if (currentScanSearchOwner(ctx) == owner &&
            saveScanSearchQueueStore(target, ScanSearchQueueStore(next))
        ) replacement else null
    }

    private fun file(ctx: Context): File = File(ctx.filesDir, FILE_NAME)

    private fun currentScanSearchOwner(ctx: Context): String? =
        Prefs.userId(ctx).trim().lowercase().takeIf(SAFE_CAPTURE_SYNC_ID::matches)
}

internal fun ownerScopedScanSearchQueueStore(
    store: ScanSearchQueueStore,
    ownerId: String,
): ScanSearchQueueStore {
    if (!store.valid) return store
    val owner = ownerId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return ScanSearchQueueStore(valid = false)
    return store.copy(items = store.items.filter { it.ownerId == owner })
}

internal fun routeScanSearchSessionStore(
    store: ScanSearchQueueStore,
    ownerId: String,
    sessionId: String,
    scanCollectionId: String,
    updatedAt: String,
): ScanSearchQueueStore? {
    if (!store.valid) return null
    val owner = ownerId.trim().lowercase()
    val session = sessionId.trim().lowercase()
    val destination = scanCollectionId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner) ||
        !SAFE_CAPTURE_SYNC_ID.matches(session) ||
        !SAFE_CAPTURE_SYNC_ID.matches(destination) ||
        runCatching { Instant.parse(updatedAt) }.isFailure
    ) return null
    val normalized = store.items.map { normalizedScanSearchQueueItem(it) ?: return null }
    val sessionIndices = normalized.indices.filter { index ->
        val item = normalized[index]
        item.ownerId == owner && item.sessionId == session
    }
    val pendingIndices = sessionIndices.filter { normalized[it].status == ScanSearchStatus.PENDING }
    if (sessionIndices.isEmpty() || pendingIndices.isEmpty() ||
        sessionIndices.any { index ->
            val item = normalized[index]
            item.status != ScanSearchStatus.PENDING &&
                !(item.status == ScanSearchStatus.FAILED && item.errorMessage.isNotEmpty())
        } ||
        pendingIndices.any { index ->
            normalized[index].scanCollectionId.let {
                it.isNotEmpty() && it != destination
            }
        }
    ) return null
    val next = normalized.toMutableList()
    pendingIndices.forEach { index ->
        next[index] = requireNotNull(normalizedScanSearchQueueItem(
            next[index].copy(
                scanCollectionId = destination,
                dirty = true,
                updatedAt = updatedAt,
            ),
        ))
    }
    return ScanSearchQueueStore(next)
}

internal fun dismissLocalScanSearchFailures(
    store: ScanSearchQueueStore,
    ownerId: String,
    sessionId: String,
): ScanSearchQueueStore? {
    if (!store.valid) return null
    val owner = ownerId.trim().lowercase()
    val session = sessionId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner) || !SAFE_CAPTURE_SYNC_ID.matches(session)) {
        return null
    }
    val normalized = store.items.map { normalizedScanSearchQueueItem(it) ?: return null }
    return ScanSearchQueueStore(normalized.filterNot { item ->
        item.ownerId == owner && item.sessionId == session &&
            item.status == ScanSearchStatus.FAILED &&
            item.errorMessage.isNotEmpty() && !item.processing && !item.dirty
    })
}

/** Apply one exact RPC response without erasing a concurrent local decision. */
internal fun acknowledgeScanSearchQueueStore(
    store: ScanSearchQueueStore,
    ownerId: String,
    expected: ScanSearchQueueItem,
    cloud: ScanSearchQueueItem,
): ScanSearchQueueStore? {
    if (!store.valid) return null
    val owner = ownerId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner) || expected.ownerId != owner) return null
    val index = store.items.indexOfFirst {
        it.id == expected.id && it.ownerId == owner
    }
    if (index < 0 || store.items[index] != expected) return store
    if (cloud.errorMessage.isNotEmpty()) return null
    val remote = normalizedScanSearchQueueItem(
        cloud.copy(dirty = false, processing = false),
    ) ?: return null
    val normalized = if (expected.processing && remote.status == ScanSearchStatus.PENDING &&
        remote.isBlankCloudReservation() &&
        remote.id == expected.id && remote.ownerId == owner &&
        remote.sessionId == expected.sessionId &&
        remote.scanCollectionId == expected.scanCollectionId &&
        remote.photoRole == expected.photoRole
    ) {
        normalizedScanSearchQueueItem(remote.copy(processing = true)) ?: return null
    } else {
        remote
    }
    if (normalized.id != expected.id || normalized.ownerId != owner ||
        normalized.sessionId != expected.sessionId ||
        normalized.scanCollectionId != expected.scanCollectionId ||
        normalized.photoRole != expected.photoRole ||
        normalized.revision < expected.revision ||
        (!expected.processing && expected.status == ScanSearchStatus.PENDING &&
            (normalized.processing || normalized.ocrText != expected.ocrText ||
                normalized.visualSignature != expected.visualSignature)) ||
        (expected.status == ScanSearchStatus.MATCHED &&
            (normalized.status != ScanSearchStatus.MATCHED ||
                normalized.matchedCaptureId != expected.matchedCaptureId)) ||
        (expected.status == ScanSearchStatus.REJECTED &&
            (normalized.status != ScanSearchStatus.REJECTED ||
                normalized.candidateCaptureId != expected.candidateCaptureId))
    ) return null
    val next = store.items.toMutableList().apply { this[index] = normalized }
    return ScanSearchQueueStore(next)
}

internal fun acknowledgeScanSearchFailureCleanupStore(
    store: ScanSearchQueueStore,
    ownerId: String,
    expected: ScanSearchQueueItem,
): ScanSearchQueueStore? {
    if (!store.valid) return null
    val owner = ownerId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner) || expected.ownerId != owner ||
        expected.status != ScanSearchStatus.FAILED || expected.errorMessage.isEmpty() ||
        !expected.dirty
    ) return null
    val index = store.items.indexOfFirst {
        it.id == expected.id && it.ownerId == owner
    }
    if (index < 0 || store.items[index] != expected) return store
    val replacement = normalizedScanSearchQueueItem(expected.copy(dirty = false)) ?: return null
    val next = store.items.toMutableList().apply { this[index] = replacement }
    return ScanSearchQueueStore(next)
}

/** Merge one owner's authoritative snapshot while preserving other accounts. */
internal fun mergeScanSearchQueueStore(
    store: ScanSearchQueueStore,
    ownerId: String,
    cloudItems: List<ScanSearchQueueItem>,
): ScanSearchQueueStore? {
    if (!store.valid) return null
    val owner = ownerId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return null

    val localItems = store.items.map { normalizedScanSearchQueueItem(it) ?: return null }
    if (localItems.map(ScanSearchQueueItem::id).toSet().size != localItems.size) return null
    val normalizedCloud = cloudItems.map {
        if (it.errorMessage.isNotEmpty()) return null
        normalizedScanSearchQueueItem(it.copy(dirty = false, processing = false)) ?: return null
    }
    if (normalizedCloud.any { it.ownerId != owner } ||
        normalizedCloud.any { !it.status.isLiveCloudQueueStatus() } ||
        normalizedCloud.map(ScanSearchQueueItem::id).toSet().size != normalizedCloud.size
    ) return null

    val foreign = localItems.filter { it.ownerId != owner }
    if (normalizedCloud.any { cloud -> foreign.any { it.id == cloud.id } }) return null
    val local = localItems.filter { it.ownerId == owner }.associateBy { it.id }
    val cloud = normalizedCloud.associateBy { it.id }
    val mergedOwner = (local.keys + cloud.keys).sorted().mapNotNull { id ->
        val left = local[id]
        val right = cloud[id]
        when {
            left?.dirty == true -> left
            right == null && left != null &&
                (left.processing || left.errorMessage.isNotEmpty()) -> left
            right == null && left?.dirty == false -> null
            right == null -> left
            left?.processing == true && right.status == ScanSearchStatus.PENDING &&
                right.revision >= left.revision &&
                right.isBlankCloudReservation() &&
                right.ownerId == left.ownerId && right.sessionId == left.sessionId &&
                right.scanCollectionId == left.scanCollectionId &&
                right.photoRole == left.photoRole ->
                normalizedScanSearchQueueItem(right.copy(processing = true)) ?: return null
            left == null || right.revision >= left.revision -> right
            else -> left
        }
    }
    val merged = (foreign + mergedOwner)
        .sortedWith(compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id))
    if (merged.size > ScanSearchQueue.MAX_ITEMS) return null
    return ScanSearchQueueStore(merged)
}

/** Merge a pull only after proving that the exact local mutation became stale. */
internal fun mergeScanSearchQueueStoreAfterStaleMutation(
    store: ScanSearchQueueStore,
    ownerId: String,
    expected: ScanSearchQueueItem,
    cloudItems: List<ScanSearchQueueItem>,
): ScanSearchQueueStore? {
    if (!store.valid) return null
    val owner = ownerId.trim().lowercase()
    if (!SAFE_CAPTURE_SYNC_ID.matches(owner) || expected.ownerId != owner ||
        !expected.dirty || expected.status !in setOf(
            ScanSearchStatus.FAILED,
            ScanSearchStatus.MATCHED,
            ScanSearchStatus.REJECTED,
        )
    ) return null
    val index = store.items.indexOfFirst { it.id == expected.id && it.ownerId == owner }
    if (index < 0 || store.items[index] != expected) return store
    val withoutExpected = ScanSearchQueueStore(
        store.items.toMutableList().apply { removeAt(index) },
    )
    return mergeScanSearchQueueStore(withoutExpected, owner, cloudItems)
}

internal fun scanSearchQueueStoreToJson(store: ScanSearchQueueStore): String {
    require(store.valid) { "cannot encode invalid scan search queue" }
    val rows = JSONArray()
    store.items.sortedWith(compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id))
        .forEach { raw ->
            val item = requireNotNull(normalizedScanSearchQueueItem(raw))
            rows.put(JSONObject()
                .put("id", item.id)
                .put("owner_id", item.ownerId)
                .put("session_id", item.sessionId)
                .put("scan_collection_id", item.scanCollectionId)
                .put("photo_role", item.photoRole.wireValue)
                .put("ocr_text", item.ocrText)
                .put("visual_signature", item.visualSignature)
                .put("status", item.status.wireValue)
                .put("candidate_capture_id", item.candidateCaptureId)
                .put("match_confidence", item.matchConfidence ?: JSONObject.NULL)
                .put("match_evidence", item.matchEvidence)
                .put("matched_capture_id", item.matchedCaptureId)
                .put("revision", item.revision)
                .put("created_at", item.createdAt)
                .put("updated_at", item.updatedAt)
                .put("dirty", item.dirty)
                .put("processing", item.processing)
                .put("error_message", item.errorMessage))
        }
    return JSONObject().put("version", ScanSearchQueue.VERSION).put("items", rows).toString()
}

internal fun scanSearchQueueStoreFromJson(text: String): ScanSearchQueueStore = try {
    val root = JSONObject(text)
    val version = strictQueueInteger(root.opt("version"))
        ?: throw IllegalArgumentException("version must be an integer")
    require(version in 1L..ScanSearchQueue.VERSION.toLong())
    val rows = root.opt("items") as? JSONArray
        ?: throw IllegalArgumentException("items must be an array")
    require(rows.length() <= ScanSearchQueue.MAX_ITEMS)
    val seen = mutableSetOf<String>()
    val items = buildList {
        for (index in 0 until rows.length()) {
            val row = rows.opt(index) as? JSONObject
                ?: throw IllegalArgumentException("queue item must be an object")
            val status = ScanSearchStatus.fromWire(
                row.opt("status") as? String ?: throw IllegalArgumentException(),
            ) ?: throw IllegalArgumentException()
            val matchedCaptureId = row.opt("matched_capture_id") as? String
                ?: throw IllegalArgumentException()
            val legacyMatched = version == 1L && status == ScanSearchStatus.MATCHED
            val item = normalizedScanSearchQueueItem(
                ScanSearchQueueItem(
                    id = row.opt("id") as? String ?: throw IllegalArgumentException(),
                    ownerId = row.opt("owner_id") as? String ?: throw IllegalArgumentException(),
                    sessionId = if (version == 1L) {
                        row.opt("id") as? String ?: throw IllegalArgumentException()
                    } else {
                        row.opt("session_id") as? String ?: throw IllegalArgumentException()
                    },
                    scanCollectionId = row.opt("scan_collection_id") as? String
                        ?: throw IllegalArgumentException(),
                    photoRole = ScanSearchPhotoRole.fromWire(
                        row.opt("photo_role") as? String ?: throw IllegalArgumentException(),
                    ) ?: throw IllegalArgumentException(),
                    ocrText = row.opt("ocr_text") as? String ?: throw IllegalArgumentException(),
                    visualSignature = if (version == 1L) "" else
                        row.opt("visual_signature") as? String ?: throw IllegalArgumentException(),
                    status = status,
                    candidateCaptureId = if (legacyMatched) matchedCaptureId
                    else if (version == 1L) "" else
                        row.opt("candidate_capture_id") as? String
                            ?: throw IllegalArgumentException(),
                    matchConfidence = if (legacyMatched) 1.0 else if (version == 1L ||
                        row.opt("match_confidence") == JSONObject.NULL
                    ) null else strictQueueNumber(row.opt("match_confidence"))
                        ?: throw IllegalArgumentException(),
                    matchEvidence = if (legacyMatched) LEGACY_MATCH_EVIDENCE
                    else if (version == 1L) "" else
                        row.opt("match_evidence") as? String ?: throw IllegalArgumentException(),
                    matchedCaptureId = matchedCaptureId,
                    revision = strictQueueInteger(row.opt("revision"))
                        ?: throw IllegalArgumentException(),
                    createdAt = row.opt("created_at") as? String ?: throw IllegalArgumentException(),
                    updatedAt = row.opt("updated_at") as? String ?: throw IllegalArgumentException(),
                    dirty = row.opt("dirty") as? Boolean ?: throw IllegalArgumentException(),
                    processing = if (version < 3L) false else
                        row.opt("processing") as? Boolean ?: throw IllegalArgumentException(),
                    errorMessage = if (version < 3L) "" else
                        row.opt("error_message") as? String ?: throw IllegalArgumentException(),
                ),
            ) ?: throw IllegalArgumentException("invalid queue item")
            require(seen.add(item.id))
            add(item)
        }
    }
    ScanSearchQueueStore(items)
} catch (_: Exception) {
    ScanSearchQueueStore(valid = false)
}

internal fun readScanSearchQueueStore(target: File): ScanSearchQueueStore = try {
    if (!target.exists()) ScanSearchQueueStore()
    else if (!target.isFile) ScanSearchQueueStore(valid = false)
    else scanSearchQueueStoreFromJson(target.readText())
} catch (_: Exception) {
    ScanSearchQueueStore(valid = false)
}

internal fun saveScanSearchQueueStore(target: File, store: ScanSearchQueueStore): Boolean {
    if (!store.valid) return false
    return try {
        target.parentFile?.mkdirs()
        Entries.atomicWrite(target, scanSearchQueueStoreToJson(store))
        true
    } catch (_: Exception) {
        false
    }
}

internal fun completeScanSearchProcessingItem(
    raw: ScanSearchQueueItem,
    ocrText: String,
    visualSignature: String,
    updatedAt: String,
): ScanSearchQueueItem? {
    val item = normalizedScanSearchQueueItem(raw) ?: return null
    val ocr = boundedScanSearchOcrText(ocrText)
    val visual = visualSignature.trim()
    if (!item.processing) {
        return item.takeIf {
            it.status == ScanSearchStatus.PENDING && it.errorMessage.isEmpty() &&
                it.ocrText == ocr && it.visualSignature == visual && it.dirty
        }
    }
    if (item.status != ScanSearchStatus.PENDING) return null
    return normalizedScanSearchQueueItem(
        item.copy(
            ocrText = ocr,
            visualSignature = visual,
            processing = false,
            errorMessage = "",
            dirty = true,
            updatedAt = updatedAt,
        ),
    )
}

internal fun failScanSearchProcessingItem(
    raw: ScanSearchQueueItem,
    errorMessage: String,
    updatedAt: String,
): ScanSearchQueueItem? {
    val item = normalizedScanSearchQueueItem(raw) ?: return null
    val error = boundedScanSearchErrorMessage(errorMessage)
    if (!item.processing) {
        return item.takeIf {
            it.status == ScanSearchStatus.FAILED && it.errorMessage == error
        }
    }
    if (item.status != ScanSearchStatus.PENDING) return null
    return normalizedScanSearchQueueItem(
        item.copy(
            ocrText = "",
            visualSignature = "",
            status = ScanSearchStatus.FAILED,
            candidateCaptureId = "",
            matchConfidence = null,
            matchEvidence = "",
            matchedCaptureId = "",
            processing = false,
            errorMessage = error,
            dirty = item.scanCollectionId.isNotEmpty(),
            updatedAt = updatedAt,
        ),
    )
}

internal fun boundedScanSearchOcrText(value: String): String = boundedScanSearchText(
    value.replace('\u0000', ' ').replace("\r\n", "\n").replace('\r', '\n').trim(),
    ScanSearchQueue.MAX_OCR_CHARS,
    ScanSearchQueue.MAX_OCR_BYTES,
)

internal fun boundedScanSearchErrorMessage(value: String): String {
    val normalized = value.replace('\u0000', ' ').replace(Regex("\\s+"), " ").trim()
        .ifEmpty { "Mistral OCR 4.1 failed." }
    return boundedScanSearchText(
        normalized,
        ScanSearchQueue.MAX_ERROR_CHARS,
        ScanSearchQueue.MAX_ERROR_BYTES,
    )
}

private fun boundedScanSearchText(value: String, maximumChars: Int, maximumBytes: Int): String {
    val limited = value.take(maximumChars)
    if (limited.toByteArray(Charsets.UTF_8).size <= maximumBytes) return limited
    var low = 0
    var high = limited.length
    while (low < high) {
        val middle = (low + high + 1) / 2
        if (limited.substring(0, middle).toByteArray(Charsets.UTF_8).size <= maximumBytes) {
            low = middle
        } else {
            high = middle - 1
        }
    }
    var end = low
    if (end > 0 && end < limited.length && limited[end - 1].isHighSurrogate() &&
        limited[end].isLowSurrogate()
    ) {
        end -= 1
    }
    return limited.substring(0, end).trimEnd()
}

internal fun normalizedScanSearchQueueItem(raw: ScanSearchQueueItem): ScanSearchQueueItem? {
    val id = raw.id.trim().lowercase()
    val owner = raw.ownerId.trim().lowercase()
    val session = raw.sessionId.trim().lowercase().ifEmpty { id }
    val collection = raw.scanCollectionId.trim().lowercase()
    val candidate = raw.candidateCaptureId.trim().lowercase()
    val matched = raw.matchedCaptureId.trim().lowercase()
    val ocr = raw.ocrText.replace('\u0000', ' ').trim()
    val visual = normalizedBoundedJsonObject(
        raw.visualSignature,
        ScanSearchQueue.MAX_VISUAL_SIGNATURE_BYTES,
    ) ?: return null
    if (visual.isNotEmpty() && !validCoverVisualSignature(visual)) return null
    val evidence = normalizedBoundedJsonObject(
        raw.matchEvidence,
        ScanSearchQueue.MAX_MATCH_EVIDENCE_BYTES,
    ) ?: return null
    val error = raw.errorMessage.replace('\u0000', ' ').trim()
    val createdAt = raw.createdAt.trim()
    val updatedAt = raw.updatedAt.trim()
    val localFailure = raw.status == ScanSearchStatus.FAILED && error.isNotEmpty()
    val blankCloudReservation = !raw.processing && !raw.dirty && error.isEmpty() &&
        raw.revision > 0L && collection.isNotEmpty() &&
        raw.status in setOf(ScanSearchStatus.PENDING, ScanSearchStatus.FAILED) &&
        ocr.isEmpty() && visual.isEmpty() && candidate.isEmpty() && matched.isEmpty() &&
        raw.matchConfidence == null && evidence.isEmpty()
    val statusShapeValid = when {
        raw.processing ->
            raw.status == ScanSearchStatus.PENDING && error.isEmpty() &&
                ocr.isEmpty() && visual.isEmpty() &&
                candidate.isEmpty() && matched.isEmpty() &&
                raw.matchConfidence == null && evidence.isEmpty() &&
                (collection.isNotEmpty() || (!raw.dirty && raw.revision == 0L))
        localFailure ->
            !raw.processing && ocr.isEmpty() && visual.isEmpty() &&
                candidate.isEmpty() && matched.isEmpty() &&
                raw.matchConfidence == null && evidence.isEmpty() &&
                (!raw.dirty || collection.isNotEmpty())
        raw.status == ScanSearchStatus.PENDING || raw.status == ScanSearchStatus.FAILED ->
            !raw.processing && error.isEmpty() &&
            candidate.isEmpty() && matched.isEmpty() &&
                raw.matchConfidence == null && evidence.isEmpty()
        raw.status == ScanSearchStatus.PROPOSED || raw.status == ScanSearchStatus.REJECTED ->
            !raw.processing && error.isEmpty() &&
            candidate.isNotEmpty() && matched.isEmpty() &&
                raw.matchConfidence != null && evidence.isNotEmpty()
        raw.status == ScanSearchStatus.MATCHED ->
            !raw.processing && error.isEmpty() &&
            candidate.isNotEmpty() && matched == candidate &&
                raw.matchConfidence != null && evidence.isNotEmpty()
        else -> false
    }
    if (!SAFE_CAPTURE_SYNC_ID.matches(id) ||
        (owner.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(owner)) ||
        !SAFE_CAPTURE_SYNC_ID.matches(session) ||
        (collection.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(collection)) ||
        (collection.isEmpty() && raw.status != ScanSearchStatus.PENDING && !localFailure) ||
        (!raw.processing && !localFailure && !blankCloudReservation &&
            raw.status != ScanSearchStatus.FAILED &&
            ocr.isBlank() &&
            (raw.photoRole != ScanSearchPhotoRole.COVER || visual.isEmpty())) ||
        ocr.length > ScanSearchQueue.MAX_OCR_CHARS ||
        ocr.toByteArray(Charsets.UTF_8).size > ScanSearchQueue.MAX_OCR_BYTES ||
        raw.revision < 0 ||
        (candidate.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(candidate)) ||
        (raw.matchConfidence != null &&
            (!raw.matchConfidence.isFinite() || raw.matchConfidence !in 0.0..1.0)) ||
        runCatching { Instant.parse(createdAt) }.isFailure ||
        runCatching { Instant.parse(updatedAt) }.isFailure ||
        error.length > ScanSearchQueue.MAX_ERROR_CHARS ||
        error.toByteArray(Charsets.UTF_8).size > ScanSearchQueue.MAX_ERROR_BYTES ||
        (raw.status == ScanSearchStatus.FAILED && error.isEmpty() && raw.dirty) ||
        !statusShapeValid
    ) return null
    return raw.copy(
        id = id,
        ownerId = owner,
        sessionId = session,
        scanCollectionId = collection,
        ocrText = ocr,
        visualSignature = visual,
        candidateCaptureId = candidate,
        matchEvidence = evidence,
        matchedCaptureId = matched,
        errorMessage = error,
        createdAt = createdAt,
        updatedAt = updatedAt,
    )
}

/** Empty is the wire representation of SQL NULL in the local cache. */
private fun normalizedBoundedJsonObject(value: String, maximumBytes: Int): String? {
    val trimmed = value.trim()
    if (trimmed.isEmpty()) return ""
    if (trimmed.toByteArray(Charsets.UTF_8).size > maximumBytes) return null
    val normalized = try {
        JSONObject(trimmed).toString()
    } catch (_: Exception) {
        return null
    }
    return normalized.takeIf {
        it.toByteArray(Charsets.UTF_8).size <= maximumBytes
    }
}

private const val LEGACY_MATCH_EVIDENCE =
    "{\"version\":1,\"source\":\"legacy_android_queue\",\"components\":{\"legacy\":1.0}}"

private fun strictQueueInteger(value: Any?): Long? = when (value) {
    is Byte -> value.toLong()
    is Short -> value.toLong()
    is Int -> value.toLong()
    is Long -> value
    else -> null
}

private fun strictQueueNumber(value: Any?): Double? =
    (value as? Number)?.toDouble()
