package org.whl.bookcapture

import android.content.Context
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject
import java.io.File

/**
 * A device-local override for where Inspect renders one capture.
 *
 * Capture provenance remains the historical record of where a book was scanned.
 * This overlay instead records the user's later organizational decision until an
 * authoritative cloud listing acknowledges it. A removed row is a tombstone; it
 * may have no destination when the book was removed before it was ever moved.
 */
internal data class InspectBookMembership(
    val collectionId: String,
    val removed: Boolean,
    /** Account whose cloud capture is known to consume this pending intent. */
    val cloudOwnerId: String = "",
    /** Local queue/sent/archive media still needs a crash-safe deletion pass. */
    val cleanupPending: Boolean = false,
)

internal data class InspectBookMembershipStore(
    val memberships: Map<String, InspectBookMembership> = emptyMap(),
    /** False means an existing source was unreadable and must not be replaced. */
    val valid: Boolean = true,
)

internal enum class InspectMembershipCompareResult {
    UPDATED,
    CHANGED,
    FAILED,
}

internal const val INSPECT_BOOK_MEMBERSHIPS_FILE = "inspect_book_memberships.json"
internal const val INSPECT_BOOK_MEMBERSHIPS_VERSION = 3

/** Serialize UI mutations, listing retries, and deferred destructive cleanup. */
internal val INSPECT_MEMBERSHIP_MUTATION_MUTEX = Mutex()

internal object InspectBookMemberships {
    private val lock = Any()

    fun read(ctx: Context): InspectBookMembershipStore = read(file(ctx))

    internal fun read(target: File): InspectBookMembershipStore = synchronized(lock) {
        readInspectBookMembershipStore(target)
    }

    /** Move every capture to [destinationCollectionId], reviving tombstoned rows. */
    fun move(
        ctx: Context,
        captureIds: Collection<String>,
        destinationCollectionId: String,
    ): Boolean = setMembership(
        file(ctx),
        captureIds,
        destinationCollectionId,
        removed = false,
    )

    internal fun move(
        target: File,
        captureIds: Collection<String>,
        destinationCollectionId: String,
    ): Boolean = setMembership(
        target,
        captureIds,
        destinationCollectionId,
        removed = false,
    )

    /** Set the destination and removal state together in one durable store write. */
    fun setMembership(
        ctx: Context,
        captureIds: Collection<String>,
        collectionId: String,
        removed: Boolean,
        cleanupPending: Boolean = false,
    ): Boolean = setMembership(
        file(ctx),
        captureIds,
        collectionId,
        removed,
        cleanupPending,
    )

    internal fun setMembership(
        target: File,
        captureIds: Collection<String>,
        collectionId: String,
        removed: Boolean,
        cleanupPending: Boolean = false,
    ): Boolean {
        val ids = normalizeCaptureIds(captureIds) ?: return false
        val destination = collectionId.trim()
        if ((destination.isEmpty() && !removed) || (cleanupPending && !removed)) return false
        return mutate(target) { current ->
            val updated = LinkedHashMap(current)
            ids.forEach { captureId ->
                updated[captureId] = InspectBookMembership(
                    destination,
                    removed = removed,
                    cloudOwnerId = updated[captureId]?.cloudOwnerId.orEmpty(),
                    cleanupPending = cleanupPending,
                )
            }
            updated
        }
    }

    /** Hide every capture from Inspect while preserving its last destination. */
    fun remove(ctx: Context, captureIds: Collection<String>): Boolean =
        remove(file(ctx), captureIds)

    internal fun remove(target: File, captureIds: Collection<String>): Boolean {
        val ids = normalizeCaptureIds(captureIds) ?: return false
        return mutate(target) { current ->
            val updated = LinkedHashMap(current)
            ids.forEach { captureId ->
                val previous = updated[captureId]
                updated[captureId] = InspectBookMembership(
                    collectionId = previous?.collectionId.orEmpty(),
                    removed = true,
                    cloudOwnerId = previous?.cloudOwnerId.orEmpty(),
                    cleanupPending = previous?.cleanupPending ?: false,
                )
            }
            updated
        }
    }

    /** Mark successful/missing local media cleanup without changing membership. */
    fun markCleanupComplete(ctx: Context, captureIds: Collection<String>): Boolean =
        markCleanupComplete(file(ctx), captureIds)

    internal fun markCleanupComplete(
        target: File,
        captureIds: Collection<String>,
    ): Boolean {
        val ids = normalizeCaptureIds(captureIds) ?: return false
        return mutate(target) { current ->
            val updated = LinkedHashMap(current)
            ids.forEach { captureId ->
                updated[captureId]?.takeIf { it.cleanupPending }?.let { membership ->
                    updated[captureId] = membership.copy(cleanupPending = false)
                }
            }
            updated
        }
    }

    /** Drop overlays the authoritative source has acknowledged. */
    fun clear(ctx: Context, captureIds: Collection<String>): Boolean =
        clear(file(ctx), captureIds)

    internal fun clear(target: File, captureIds: Collection<String>): Boolean {
        val ids = normalizeCaptureIds(captureIds) ?: return false
        return mutate(target) { current ->
            val updated = LinkedHashMap(current)
            ids.forEach { captureId ->
                if (updated[captureId]?.cleanupPending != true) updated.remove(captureId)
            }
            updated
        }
    }

    /**
     * Replace one intent only if it is still the snapshot the caller acted on.
     * UploadWorker uses this to avoid clearing or rewriting a newer UI action.
     */
    fun compareAndSet(
        ctx: Context,
        captureId: String,
        expected: InspectBookMembership,
        replacement: InspectBookMembership?,
    ): InspectMembershipCompareResult = compareAndSet(
        file(ctx),
        captureId,
        expected,
        replacement,
    )

    internal fun compareAndSet(
        target: File,
        captureId: String,
        expected: InspectBookMembership,
        replacement: InspectBookMembership?,
    ): InspectMembershipCompareResult = synchronized(lock) {
        val id = captureId.trim()
        if (id.isEmpty() || id != captureId ||
            !validInspectBookMembership(expected) ||
            (replacement != null && !validInspectBookMembership(replacement))
        ) return@synchronized InspectMembershipCompareResult.FAILED

        val stored = readInspectBookMembershipStore(target)
        if (!stored.valid) return@synchronized InspectMembershipCompareResult.FAILED
        if (stored.memberships[id] != expected) {
            return@synchronized InspectMembershipCompareResult.CHANGED
        }
        val updated = LinkedHashMap(stored.memberships)
        if (replacement == null) updated.remove(id) else updated[id] = replacement
        if (updated == stored.memberships) {
            return@synchronized InspectMembershipCompareResult.UPDATED
        }
        if (saveInspectBookMembershipStore(target, InspectBookMembershipStore(updated))) {
            InspectMembershipCompareResult.UPDATED
        } else {
            InspectMembershipCompareResult.FAILED
        }
    }

    /** Mark staged intents that can be retried independently of the visible box. */
    fun markCloud(
        ctx: Context,
        captureIds: Collection<String>,
        ownerId: String,
    ): Boolean = markCloud(file(ctx), captureIds, ownerId)

    internal fun markCloud(
        target: File,
        captureIds: Collection<String>,
        ownerId: String,
    ): Boolean = synchronized(lock) {
        val ids = normalizeCaptureIds(captureIds) ?: return false
        val owner = ownerId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return false
        val stored = readInspectBookMembershipStore(target)
        if (!stored.valid || ids.any { it !in stored.memberships }) return@synchronized false
        val updated = LinkedHashMap(stored.memberships)
        ids.forEach { captureId ->
            updated[captureId] = updated.getValue(captureId).copy(cloudOwnerId = owner)
        }
        if (updated == stored.memberships) return@synchronized true
        saveInspectBookMembershipStore(
            target,
            InspectBookMembershipStore(updated),
        )
    }

    private fun mutate(
        target: File,
        transform: (Map<String, InspectBookMembership>) -> Map<String, InspectBookMembership>,
    ): Boolean = synchronized(lock) {
        val stored = readInspectBookMembershipStore(target)
        if (!stored.valid) return@synchronized false
        val updated = transform(stored.memberships)
        if (updated == stored.memberships) return@synchronized true
        saveInspectBookMembershipStore(
            target,
            InspectBookMembershipStore(LinkedHashMap(updated)),
        )
    }

    private fun file(ctx: Context): File = File(ctx.filesDir, INSPECT_BOOK_MEMBERSHIPS_FILE)
}

/** Retry crash/lifecycle-interrupted page cleanup from the durable tombstones. */
internal suspend fun retryPendingInspectBookCleanup(ctx: Context) =
    INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock {
        val pendingIds = InspectBookMemberships.read(ctx)
            .takeIf { it.valid }
            ?.memberships
            ?.filterValues { it.removed && it.cleanupPending }
            ?.keys
            .orEmpty()
        pendingIds.forEach { captureId ->
            // Revalidate inside the same process-wide ordering as a UI revive.
            // A stale cleanup pass must never delete media after a newer move.
            val latest = InspectBookMemberships.read(ctx)
                .takeIf { it.valid }
                ?.memberships
                ?.get(captureId)
            if (latest?.removed != true || !latest.cleanupPending) return@forEach
            when (Entries.deleteLocalSafely(ctx, captureId, allowUploaded = true)) {
                Entries.DeleteResult.DELETED,
                Entries.DeleteResult.MISSING ->
                    InspectBookMemberships.markCleanupComplete(ctx, setOf(captureId))
                Entries.DeleteResult.ACTIVE_CAPTURE,
                Entries.DeleteResult.ALREADY_UPLOADED,
                Entries.DeleteResult.DELETE_FAILED -> Unit
            }
        }
    }

private fun normalizeCaptureIds(captureIds: Collection<String>): List<String>? {
    val normalized = captureIds.map(String::trim)
    if (normalized.any(String::isEmpty)) return null
    return normalized.distinct()
}

private fun validInspectBookMembership(membership: InspectBookMembership): Boolean =
    (membership.removed || membership.collectionId.isNotEmpty()) &&
        membership.collectionId == membership.collectionId.trim() &&
        (membership.removed || !membership.cleanupPending) &&
        (membership.cloudOwnerId.isEmpty() ||
            SAFE_CAPTURE_SYNC_ID.matches(membership.cloudOwnerId))

internal fun inspectBookMembershipStoreToJson(store: InspectBookMembershipStore): String {
    require(store.valid) { "cannot encode an invalid membership store" }
    val memberships = JSONObject()
    store.memberships.toSortedMap().forEach { (captureId, membership) ->
        require(captureId.isNotBlank() && captureId == captureId.trim()) {
            "invalid capture id"
        }
        require(membership.removed || membership.collectionId.isNotBlank()) {
            "an active membership needs a destination collection"
        }
        require(membership.collectionId == membership.collectionId.trim()) {
            "invalid destination collection id"
        }
        require(membership.cloudOwnerId.isEmpty() ||
            SAFE_CAPTURE_SYNC_ID.matches(membership.cloudOwnerId)
        ) { "invalid cloud owner id" }
        require(membership.removed || !membership.cleanupPending) {
            "active membership cannot need cleanup"
        }
        memberships.put(
            captureId,
            JSONObject()
                .put("collection_id", membership.collectionId)
                .put("removed", membership.removed)
                .put("cloud_owner_id", membership.cloudOwnerId)
                .put("cleanup_pending", membership.cleanupPending),
        )
    }
    return JSONObject()
        .put("version", INSPECT_BOOK_MEMBERSHIPS_VERSION)
        .put("memberships", memberships)
        .toString()
}

internal fun inspectBookMembershipStoreFromJson(text: String): InspectBookMembershipStore = try {
    val root = JSONObject(text)
    val version = strictWholeNumber(root, "version")
        ?: throw IllegalArgumentException("version must be an integer")
    require(version in 1L..INSPECT_BOOK_MEMBERSHIPS_VERSION.toLong()) {
        "unsupported membership version"
    }
    val rows = root.opt("memberships") as? JSONObject
        ?: throw IllegalArgumentException("memberships must be an object")
    val memberships = linkedMapOf<String, InspectBookMembership>()
    rows.keys().asSequence().forEach { captureId ->
        require(captureId.isNotBlank() && captureId == captureId.trim()) {
            "invalid capture id"
        }
        val row = rows.opt(captureId) as? JSONObject
            ?: throw IllegalArgumentException("membership must be an object")
        val collectionId = row.opt("collection_id") as? String
            ?: throw IllegalArgumentException("collection_id must be a string")
        val removed = row.opt("removed") as? Boolean
            ?: throw IllegalArgumentException("removed must be a boolean")
        val cloudOwnerId = if (version < 2L) "" else {
            (row.opt("cloud_owner_id") as? String)
                ?: throw IllegalArgumentException("cloud_owner_id must be a string")
        }
        val cleanupPending = if (version < 3L) false else {
            (row.opt("cleanup_pending") as? Boolean)
                ?: throw IllegalArgumentException("cleanup_pending must be a boolean")
        }
        require(collectionId == collectionId.trim()) { "invalid destination collection id" }
        require(removed || collectionId.isNotEmpty()) {
            "an active membership needs a destination collection"
        }
        require(cloudOwnerId.isEmpty() || SAFE_CAPTURE_SYNC_ID.matches(cloudOwnerId)) {
            "invalid cloud owner id"
        }
        require(removed || !cleanupPending) { "active membership cannot need cleanup" }
        memberships[captureId] = InspectBookMembership(
            collectionId,
            removed,
            cloudOwnerId,
            cleanupPending,
        )
    }
    InspectBookMembershipStore(memberships)
} catch (_: Exception) {
    InspectBookMembershipStore(valid = false)
}

internal fun readInspectBookMembershipStore(target: File): InspectBookMembershipStore {
    if (!target.exists()) return InspectBookMembershipStore()
    if (!target.isFile) return InspectBookMembershipStore(valid = false)
    return try {
        inspectBookMembershipStoreFromJson(target.readText())
    } catch (_: Exception) {
        InspectBookMembershipStore(valid = false)
    }
}

internal fun saveInspectBookMembershipStore(
    target: File,
    store: InspectBookMembershipStore,
): Boolean {
    if (!store.valid) return false
    return try {
        target.parentFile?.mkdirs()
        Entries.atomicWrite(target, inspectBookMembershipStoreToJson(store))
        true
    } catch (_: Exception) {
        false
    }
}

private fun strictWholeNumber(source: JSONObject, name: String): Long? =
    when (val value = source.opt(name)) {
        is Byte -> value.toLong()
        is Short -> value.toLong()
        is Int -> value.toLong()
        is Long -> value
        else -> null
    }
