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
    MATCHED("matched"),
    FAILED("failed");

    companion object {
        fun fromWire(value: String): ScanSearchStatus? = entries.firstOrNull {
            it.wireValue == value.trim().lowercase()
        }
    }
}

/**
 * A privacy-conscious search request. The temporary camera image is sent to
 * Mistral OCR 4.1 and deleted as before; only the bounded recognized text and
 * the declared cover/title-page role enter the durable queue.
 */
internal data class ScanSearchQueueItem(
    val id: String,
    val ownerId: String = "",
    val scanCollectionId: String,
    val photoRole: ScanSearchPhotoRole,
    val ocrText: String,
    val status: ScanSearchStatus = ScanSearchStatus.PENDING,
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
    const val VERSION = 1
    const val MAX_OCR_CHARS = 16_000
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
        id: String = UUID.randomUUID().toString(),
        now: Instant = Instant.now(),
    ): ScanSearchQueueItem? = synchronized(lock) {
        val owner = currentScanSearchOwner(ctx) ?: return@synchronized null
        val target = file(ctx)
        val store = readScanSearchQueueStore(target)
        if (!store.valid) return@synchronized null
        val item = normalizedScanSearchQueueItem(
            ScanSearchQueueItem(
                id = id,
                ownerId = owner,
                scanCollectionId = scanCollectionId,
                photoRole = photoRole,
                ocrText = ocrText,
                createdAt = now.toString(),
            ),
        ) ?: return@synchronized null
        store.items.firstOrNull { it.id == item.id }?.let { existing ->
            return@synchronized existing.takeIf {
                it.ownerId == item.ownerId &&
                    it.scanCollectionId == item.scanCollectionId &&
                    it.photoRole == item.photoRole &&
                    it.ocrText == item.ocrText
            }
        }
        if (store.items.size >= MAX_ITEMS) return@synchronized null
        val next = (store.items + item)
            .sortedWith(compareBy(ScanSearchQueueItem::createdAt, ScanSearchQueueItem::id))
        if (currentScanSearchOwner(ctx) != owner ||
            !saveScanSearchQueueStore(target, ScanSearchQueueStore(next))
        ) null else item
    }

    fun match(ctx: Context, id: String, captureId: String): Boolean {
        val capture = captureId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(capture)) return false
        return update(ctx, id) { item ->
            when {
                item.status == ScanSearchStatus.PENDING -> item.copy(
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
        normalized.revision < expected.revision ||
        (expected.status == ScanSearchStatus.MATCHED &&
            (normalized.status != ScanSearchStatus.MATCHED ||
                normalized.matchedCaptureId != expected.matchedCaptureId))
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
                .put("scan_collection_id", item.scanCollectionId)
                .put("photo_role", item.photoRole.wireValue)
                .put("ocr_text", item.ocrText)
                .put("status", item.status.wireValue)
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
    require(strictQueueInteger(root.opt("version")) == ScanSearchQueue.VERSION.toLong())
    val rows = root.opt("items") as? JSONArray
        ?: throw IllegalArgumentException("items must be an array")
    require(rows.length() <= ScanSearchQueue.MAX_ITEMS)
    val seen = mutableSetOf<String>()
    val items = buildList {
        for (index in 0 until rows.length()) {
            val row = rows.opt(index) as? JSONObject
                ?: throw IllegalArgumentException("queue item must be an object")
            val item = normalizedScanSearchQueueItem(
                ScanSearchQueueItem(
                    id = row.opt("id") as? String ?: throw IllegalArgumentException(),
                    ownerId = row.opt("owner_id") as? String ?: throw IllegalArgumentException(),
                    scanCollectionId = row.opt("scan_collection_id") as? String
                        ?: throw IllegalArgumentException(),
                    photoRole = ScanSearchPhotoRole.fromWire(
                        row.opt("photo_role") as? String ?: throw IllegalArgumentException(),
                    ) ?: throw IllegalArgumentException(),
                    ocrText = row.opt("ocr_text") as? String ?: throw IllegalArgumentException(),
                    status = ScanSearchStatus.fromWire(
                        row.opt("status") as? String ?: throw IllegalArgumentException(),
                    ) ?: throw IllegalArgumentException(),
                    matchedCaptureId = row.opt("matched_capture_id") as? String
                        ?: throw IllegalArgumentException(),
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
    val collection = raw.scanCollectionId.trim().lowercase()
    val matched = raw.matchedCaptureId.trim().lowercase()
    val ocr = raw.ocrText.replace('\u0000', ' ').trim()
    val createdAt = raw.createdAt.trim()
    val updatedAt = raw.updatedAt.trim()
    if (!SAFE_CAPTURE_SYNC_ID.matches(id) ||
        (owner.isNotEmpty() && !SAFE_CAPTURE_SYNC_ID.matches(owner)) ||
        !SAFE_CAPTURE_SYNC_ID.matches(collection) ||
        ocr.isBlank() || ocr.length > ScanSearchQueue.MAX_OCR_CHARS || raw.revision < 0 ||
        runCatching { Instant.parse(createdAt) }.isFailure ||
        runCatching { Instant.parse(updatedAt) }.isFailure ||
        (raw.status == ScanSearchStatus.FAILED && raw.dirty) ||
        (raw.status == ScanSearchStatus.MATCHED && !SAFE_CAPTURE_SYNC_ID.matches(matched)) ||
        (raw.status != ScanSearchStatus.MATCHED && matched.isNotEmpty())
    ) return null
    return raw.copy(
        id = id,
        ownerId = owner,
        scanCollectionId = collection,
        ocrText = ocr,
        matchedCaptureId = matched,
        createdAt = createdAt,
        updatedAt = updatedAt,
    )
}

private fun strictQueueInteger(value: Any?): Long? = when (value) {
    is Byte -> value.toLong()
    is Short -> value.toLong()
    is Int -> value.toLong()
    is Long -> value
    else -> null
}
