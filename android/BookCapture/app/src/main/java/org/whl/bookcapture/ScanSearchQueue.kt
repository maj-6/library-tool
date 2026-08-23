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
)

internal data class ScanSearchQueueStore(
    val items: List<ScanSearchQueueItem> = emptyList(),
    val valid: Boolean = true,
)

internal object ScanSearchQueue {
    const val FILE_NAME = "scan_search_queue.json"
    const val VERSION = 2
    const val MAX_OCR_CHARS = 16_000
    const val MAX_OCR_BYTES = 65_536
    const val MAX_VISUAL_SIGNATURE_BYTES = 4_096
    const val MAX_MATCH_EVIDENCE_BYTES = 8_192
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
        routed.items.filter { it.ownerId == owner && it.sessionId == session }
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

    private fun update(
        ctx: Context,
        id: String,
        transform: (ScanSearchQueueItem) -> ScanSearchQueueItem?,
    ): Boolean = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized false
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized false
        val normalizedId = id.trim().lowercase()
        val index = store.items.indexOfFirst {
            it.id == normalizedId && it.ownerId == owner
        }
        if (index < 0) return@synchronized false
        val transformed = transform(store.items[index]) ?: return@synchronized false
        val replacement = normalizedScanSearchQueueItem(transformed)
            ?: return@synchronized false
        if (replacement.id != normalizedId || replacement.ownerId != owner) {
            return@synchronized false
        }
        if (replacement == store.items[index]) return@synchronized true
        val next = store.items.toMutableList().apply { this[index] = replacement }
        currentScanSearchOwner(ctx) == owner &&
            saveScanSearchQueueStore(target, ScanSearchQueueStore(next))
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
    val indices = normalized.indices.filter { index ->
        val item = normalized[index]
        item.ownerId == owner && item.sessionId == session
    }
    if (indices.isEmpty() || indices.any { index ->
            val item = normalized[index]
            item.status != ScanSearchStatus.PENDING ||
                (item.scanCollectionId.isNotEmpty() &&
                    item.scanCollectionId != destination)
        }
    ) return null
    val next = normalized.toMutableList()
    indices.forEach { index ->
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
    val normalized = normalizedScanSearchQueueItem(cloud.copy(dirty = false)) ?: return null
    if (normalized.id != expected.id || normalized.ownerId != owner ||
        normalized.sessionId != expected.sessionId ||
        normalized.revision < expected.revision ||
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
        normalizedScanSearchQueueItem(it.copy(dirty = false)) ?: return null
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
            right == null && left?.dirty == false -> null
            right == null -> left
            left == null || right.revision >= left.revision -> right
            else -> left
        }
    }
    val merged = (foreign + mergedOwner)
        .sortedWith(compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id))
    if (merged.size > ScanSearchQueue.MAX_ITEMS) return null
    return ScanSearchQueueStore(merged)
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
                .put("dirty", item.dirty))
        }
    return JSONObject().put("version", ScanSearchQueue.VERSION).put("items", rows).toString()
}

internal fun scanSearchQueueStoreFromJson(text: String): ScanSearchQueueStore = try {
    val root = JSONObject(text)
    val version = strictQueueInteger(root.opt("version"))
    require(version == 1L || version == ScanSearchQueue.VERSION.toLong())
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
    val createdAt = raw.createdAt.trim()
    val updatedAt = raw.updatedAt.trim()
    val statusShapeValid = when (raw.status) {
        ScanSearchStatus.PENDING, ScanSearchStatus.FAILED ->
            candidate.isEmpty() && matched.isEmpty() &&
                raw.matchConfidence == null && evidence.isEmpty()
        ScanSearchStatus.PROPOSED, ScanSearchStatus.REJECTED ->
            candidate.isNotEmpty() && matched.isEmpty() &&
                raw.matchConfidence != null && evidence.isNotEmpty()
        ScanSearchStatus.MATCHED ->
            candidate.isNotEmpty() && matched == candidate &&
                raw.matchConfidence != null && evidence.isNotEmpty()
    }
    if (!SAFE_CAPTURE_SYNC_ID.matches(id) ||
        (owner.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(owner)) ||
        !SAFE_CAPTURE_SYNC_ID.matches(session) ||
        (collection.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(collection)) ||
        (collection.isEmpty() && raw.status != ScanSearchStatus.PENDING) ||
        (ocr.isBlank() &&
            (raw.photoRole != ScanSearchPhotoRole.COVER || visual.isEmpty())) ||
        ocr.length > ScanSearchQueue.MAX_OCR_CHARS ||
        ocr.toByteArray(Charsets.UTF_8).size > ScanSearchQueue.MAX_OCR_BYTES ||
        raw.revision < 0 ||
        (candidate.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(candidate)) ||
        (raw.matchConfidence != null &&
            (!raw.matchConfidence.isFinite() || raw.matchConfidence !in 0.0..1.0)) ||
        runCatching { Instant.parse(createdAt) }.isFailure ||
        runCatching { Instant.parse(updatedAt) }.isFailure ||
        (raw.status == ScanSearchStatus.FAILED && raw.dirty) ||
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
