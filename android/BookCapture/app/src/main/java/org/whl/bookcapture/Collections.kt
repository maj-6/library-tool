package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.util.UUID

/**
 * A collection is the batch a book was scanned into — a shelf, a crate, a room.
 *
 * [from] is where that batch physically came from ("Storage", "Christopher
 * Office"). [updatedAt] and [deleted] are synchronization state. Tombstones are
 * retained on disk but hidden from [Collections.all], so a signed-out delete
 * cannot be resurrected by the next cloud pull.
 */
data class BookCollection(
    val id: String,
    val name: String,
    val from: String,
    val updatedAt: String = "",
    val deleted: Boolean = false,
    /** Non-null only for an irreversible, human-confirmed identity merge. */
    val mergedInto: String? = null,
)

private fun BookCollection.isLive(): Boolean = !deleted && mergedInto == null

/** The last cloud representation reconciled on this device. Content identity,
 * not the phone clock, tells sync whether each side changed since that point. */
internal data class CollectionSyncShadow(val hash: String, val updatedAt: String)

internal data class CollectionStore(
    val collections: List<BookCollection>,
    val shadow: Map<String, CollectionSyncShadow> = emptyMap(),
    /** IDs with local intent that has not yet been acknowledged by the cloud.
     * This distinguishes a real A -> B -> A edit from an unchanged row whose
     * untrusted device timestamp merely differs from the cloud baseline. */
    val dirty: Set<String> = emptySet(),
    /** False means the on-disk source was unreadable/corrupt. Such a store is
     * safe to display as empty, but must never be persisted over the source. */
    val valid: Boolean = true,
)

/** A conditional cloud write. A null [expectedCloudUpdatedAt] means insert only;
 * otherwise the PATCH must still see exactly that remote revision. */
internal data class CollectionCloudWrite(
    val row: BookCollection,
    val expectedCloudUpdatedAt: String?,
)

internal data class CollectionMerge(
    val collections: List<BookCollection>,
    val shadow: Map<String, CollectionSyncShadow>,
    val dirty: Set<String>,
    val writes: List<CollectionCloudWrite>,
)

/** Longest name/from we will store. Long enough for "Christopher Office, back
 * shelf", short enough that a row stays one line on a phone. */
internal const val COLLECTION_FIELD_MAX = 80

/** Collapse whitespace and clip. Names are compared case-insensitively for
 * duplicates but stored as typed, so "Storage" doesn't become "storage". */
internal fun normalizeCollectionField(value: String): String =
    value.trim().replace(Regex("\\s+"), " ").take(COLLECTION_FIELD_MAX)

private fun collectionToJson(collection: BookCollection): JSONObject =
    JSONObject()
        .put("id", collection.id)
        .put("name", collection.name)
        .put("from", collection.from)
        .apply {
            if (collection.updatedAt.isNotEmpty()) put("updated_at", collection.updatedAt)
            if (collection.deleted) put("deleted", true)
            collection.mergedInto?.let { put("merged_into", it) }
        }

internal fun collectionStoreToJson(store: CollectionStore): String {
    val array = JSONArray()
    store.collections.forEach { array.put(collectionToJson(it)) }
    val shadow = JSONObject()
    store.shadow.toSortedMap().forEach { (id, baseline) ->
        shadow.put(
            id,
            JSONObject()
                .put("hash", baseline.hash)
                .put("updated_at", baseline.updatedAt),
        )
    }
    val dirty = JSONArray()
    store.dirty.toSortedSet().forEach { dirty.put(it) }
    return JSONObject()
        .put("version", 2)
        .put("collections", array)
        .put("sync_shadow", shadow)
        .put("sync_dirty", dirty)
        .toString()
}

internal fun collectionsToJson(collections: List<BookCollection>): String =
    collectionStoreToJson(CollectionStore(collections))

/**
 * Parse the stored list, dropping anything unusable rather than throwing — a
 * truncated or hand-edited file must not brick the Collections tab. Version 1
 * rows simply have no revision yet and are claimed on their first sync.
 */
internal fun collectionStoreFromJson(text: String): CollectionStore = try {
    val root = JSONObject(text)
    val rawVersion = root.opt("version")
    require(rawVersion is Number && rawVersion.toDouble().let {
        it.isFinite() && it % 1.0 == 0.0
    }) { "version must be an integer" }
    val version = rawVersion.toInt()
    require(version == 1 || version == 2) { "unsupported collections version" }
    val array = root.optJSONArray("collections")
        ?: throw IllegalArgumentException("collections must be an array")
    if (version == 2 && root.has("sync_shadow") && root.optJSONObject("sync_shadow") == null) {
        throw IllegalArgumentException("sync_shadow must be an object")
    }
    if (version == 2 && root.has("sync_dirty") && root.optJSONArray("sync_dirty") == null) {
        throw IllegalArgumentException("sync_dirty must be an array")
    }
    val seen = mutableSetOf<String>()
    val out = mutableListOf<BookCollection>()
    for (i in 0 until array.length()) {
        val item = array.optJSONObject(i) ?: continue
        val id = item.optString("id").trim()
        val name = normalizeCollectionField(item.optString("name"))
        if (id.isEmpty() || name.isEmpty() || !seen.add(id)) continue
        val mergedInto = if (item.isNull("merged_into")) null
        else item.optString("merged_into").trim().ifEmpty { null }
        out += BookCollection(
            id = id,
            name = name,
            from = normalizeCollectionField(item.optString("from")),
            updatedAt = item.optString("updated_at").trim(),
            deleted = item.optBoolean("deleted", false),
            mergedInto = mergedInto,
        )
    }
    val shadowObject = root.optJSONObject("sync_shadow") ?: JSONObject()
    val shadow = mutableMapOf<String, CollectionSyncShadow>()
    for (id in shadowObject.keys()) {
        val item = shadowObject.optJSONObject(id)
            ?: throw IllegalArgumentException("sync_shadow entries must be objects")
        val rawHash = item.opt("hash")
        val rawUpdatedAt = item.opt("updated_at")
        require(rawHash is String && rawUpdatedAt is String) {
            "sync_shadow fields must be strings"
        }
        val hash = rawHash.trim()
        val updatedAt = rawUpdatedAt.trim()
        require(hash.isNotEmpty() && updatedAt.isNotEmpty()) {
            "sync_shadow fields must not be empty"
        }
        shadow[id] = CollectionSyncShadow(hash, updatedAt)
    }
    val dirtyArray = root.optJSONArray("sync_dirty") ?: JSONArray()
    val dirty = buildSet {
        for (i in 0 until dirtyArray.length()) {
            val rawId = dirtyArray.opt(i)
            require(rawId is String) { "sync_dirty entries must be strings" }
            val id = rawId.trim()
            require(id.isNotEmpty() && id in seen) { "sync_dirty id has no collection" }
            add(id)
        }
    }
    CollectionStore(out, shadow, dirty)
} catch (_: Exception) {
    CollectionStore(emptyList(), valid = false)
}

internal fun collectionsFromJson(text: String): List<BookCollection> =
    collectionStoreFromJson(text).collections

/** Case-insensitive name clash, ignoring [exceptId] so renaming a collection to
 * its own name (or a case variant of it) is allowed. */
internal fun collectionNameTaken(
    collections: List<BookCollection>,
    name: String,
    exceptId: String? = null,
): Boolean = collections.any {
    it.isLive() && it.id != exceptId &&
        it.name.equals(normalizeCollectionField(name), ignoreCase = true)
}

/** Result of an edit: [collections] is the list to persist, [error] is a string
 * resource to show instead when the edit was rejected. */
internal data class CollectionEdit(val collections: List<BookCollection>?, val error: Int?)

internal fun addCollection(
    collections: List<BookCollection>,
    name: String,
    from: String,
    id: String = UUID.randomUUID().toString(),
): CollectionEdit {
    val clean = normalizeCollectionField(name)
    if (clean.isEmpty()) return CollectionEdit(null, R.string.collections_error_name_required)
    if (collectionNameTaken(collections, clean)) {
        return CollectionEdit(null, R.string.collections_error_name_taken)
    }
    return CollectionEdit(collections + BookCollection(id, clean, normalizeCollectionField(from)), null)
}

internal fun updateCollection(
    collections: List<BookCollection>,
    id: String,
    name: String,
    from: String,
): CollectionEdit {
    val clean = normalizeCollectionField(name)
    if (clean.isEmpty()) return CollectionEdit(null, R.string.collections_error_name_required)
    if (collectionNameTaken(collections, clean, exceptId = id)) {
        return CollectionEdit(null, R.string.collections_error_name_taken)
    }
    if (collections.none { it.id == id && !it.deleted }) {
        return CollectionEdit(null, R.string.collections_error_missing)
    }
    return CollectionEdit(
        collections.map {
            if (it.id == id) it.copy(name = clean, from = normalizeCollectionField(from)) else it
        },
        null,
    )
}

internal fun removeCollection(collections: List<BookCollection>, id: String): List<BookCollection> =
    collections.filterNot { it.id == id }

/**
 * Which collection a new book should go into, given what is stored and what was
 * last selected. Returns null when the user must choose — that is what makes
 * selection mandatory. A single collection selects itself, because forcing a
 * choice between one option is just a tax.
 */
internal fun resolveCurrentCollection(
    collections: List<BookCollection>,
    selectedId: String?,
): BookCollection? {
    val live = collections.filter { it.isLive() }
    live.firstOrNull { it.id == selectedId }?.let { return it }
    return live.singleOrNull()
}

private fun parseCollectionTimestamp(value: String): Instant =
    runCatching { Instant.parse(value.trim()) }.getOrElse {
        runCatching { OffsetDateTime.parse(value.trim()).toInstant() }
            .getOrDefault(Instant.EPOCH)
    }

internal fun nextCollectionTimestamp(
    previous: String,
    now: Instant = Instant.now(),
): String {
    val old = parseCollectionTimestamp(previous)
    return (if (now.isAfter(old)) now else old.plusMillis(1)).atOffset(ZoneOffset.UTC).toString()
}

/** A successful CAS is a new cloud revision even when this phone's raw clock
 * trails the row it matched. Never move the shared updated_at backwards. */
internal fun collectionPatchTimestamp(expected: String): String {
    val remote = parseCollectionTimestamp(expected)
    // The phone timestamp has already served its only safe purpose: as the
    // tiebreak when both sides changed since the sync shadow. Once the exact
    // remote revision is won by CAS, advance from that trusted baseline.
    return remote.plusMillis(1)
        .atOffset(ZoneOffset.UTC)
        .toString()
}

private fun compareCollectionTimestamps(left: String, right: String): Int =
    parseCollectionTimestamp(left).compareTo(parseCollectionTimestamp(right))

/** Stable semantic identity; timestamps are revision metadata, not content. */
internal fun collectionContentHash(collection: BookCollection): String {
    val canonical = listOf(
        collection.id,
        collection.name,
        collection.from,
        collection.deleted.toString(),
        collection.mergedInto.orEmpty(),
    ).joinToString("\u0000")
    val digest = MessageDigest.getInstance("SHA-256").digest(canonical.toByteArray())
    return buildString(digest.size * 2) {
        digest.forEach { byte ->
            val value = byte.toInt() and 0xff
            if (value < 16) append('0')
            append(value.toString(16))
        }
    }
}

private fun shadowOf(row: BookCollection) =
    CollectionSyncShadow(collectionContentHash(row), row.updatedAt)

/**
 * Pure three-way merge. The shadow makes a one-sided local edit push even when
 * this phone's clock is behind the cloud revision. `updated_at` arbitrates only
 * when both contents moved since the shared baseline. Ties follow the existing
 * desktop sync convention and stay local.
 */
internal fun mergeCollections(
    local: List<BookCollection>,
    cloud: List<BookCollection>,
    shadow: Map<String, CollectionSyncShadow>,
    dirty: Set<String> = emptySet(),
): CollectionMerge {
    val localById = local.associateBy { it.id }
    val cloudById = cloud.associateBy { it.id }
    val order = linkedSetOf<String>().apply {
        addAll(local.map { it.id })
        addAll(cloud.map { it.id })
        addAll(shadow.keys.sorted())
        addAll(dirty.sorted())
    }
    val merged = mutableListOf<BookCollection>()
    val nextShadow = shadow.toMutableMap()
    val nextDirty = dirty.toMutableSet()
    val writes = mutableListOf<CollectionCloudWrite>()

    for (id in order) {
        val localRow = localById[id]
        val cloudRow = cloudById[id]
        val baseline = shadow[id]
        when {
            localRow == null && cloudRow == null -> {
                nextShadow.remove(id)
                nextDirty.remove(id)
            }
            localRow == null -> {
                val incoming = checkNotNull(cloudRow)
                merged += incoming
                nextShadow[id] = shadowOf(incoming)
                nextDirty.remove(id)
            }
            cloudRow == null -> {
                merged += localRow
                if (localRow.mergedInto == null) {
                    nextDirty += id
                    writes += CollectionCloudWrite(localRow, expectedCloudUpdatedAt = null)
                } else {
                    // A merge marker can only be created by the transactional
                    // server RPC. Never recreate or clear it through ordinary
                    // authenticated row writes.
                    nextDirty.remove(id)
                }
            }
            else -> {
                val localHash = collectionContentHash(localRow)
                val cloudHash = collectionContentHash(cloudRow)
                if (cloudRow.mergedInto != null) {
                    // A human-confirmed identity merge is not an ordinary LWW
                    // delete. A stale phone must never resurrect the old id.
                    merged += cloudRow
                    nextShadow[id] = shadowOf(cloudRow)
                    nextDirty.remove(id)
                    continue
                }
                if (localRow.mergedInto != null) {
                    // The database contract makes clearing merged_into
                    // impossible for normal clients. Preserve the last durable
                    // marker if a transient/inconsistent response omits it.
                    merged += localRow
                    nextDirty.remove(id)
                    continue
                }
                if (localHash == cloudHash) {
                    // A dirty A -> B -> A edit has the same content as an
                    // unchanged cloud row, but still carries a causal revision.
                    // Push exactly once only while cloud is the exact baseline.
                    // A newer equal-content cloud revision means either our
                    // previous PATCH succeeded before a crash or another device
                    // already established the same state; its clock is irrelevant.
                    val cloudIsBaseline = baseline != null &&
                        cloudHash == baseline.hash &&
                        compareCollectionTimestamps(
                            cloudRow.updatedAt,
                            baseline.updatedAt,
                        ) == 0
                    if (id in dirty && cloudIsBaseline) {
                        merged += localRow
                        nextDirty += id
                        writes += CollectionCloudWrite(localRow, cloudRow.updatedAt)
                    } else {
                        // Hydrate Postgres' normalized timestamp after a write.
                        merged += cloudRow
                        nextShadow[id] = shadowOf(cloudRow)
                        nextDirty.remove(id)
                    }
                    continue
                }
                // Dirty is explicit because a clean row with a skewed device
                // timestamp and an A -> B -> A local edit otherwise have the
                // same observable content/revision shape. Hash mismatch is the
                // upgrade fallback for an edit saved by an older v2 build.
                val localChanged = baseline == null ||
                    id in dirty ||
                    localHash != baseline.hash
                val cloudChanged = baseline == null ||
                    cloudHash != baseline.hash ||
                    compareCollectionTimestamps(
                        cloudRow.updatedAt,
                        baseline.updatedAt,
                    ) > 0
                val cloudWins = when {
                    !localChanged && cloudChanged -> true
                    localChanged && !cloudChanged -> false
                    else -> compareCollectionTimestamps(
                        cloudRow.updatedAt,
                        localRow.updatedAt,
                    ) > 0
                }
                if (cloudWins) {
                    merged += cloudRow
                    nextShadow[id] = shadowOf(cloudRow)
                    nextDirty.remove(id)
                } else {
                    merged += localRow
                    nextDirty += id
                    writes += CollectionCloudWrite(localRow, cloudRow.updatedAt)
                }
            }
        }
    }
    return CollectionMerge(merged, nextShadow, nextDirty, writes)
}

/**
 * Record a successful conditional write without trampling a newer local edit
 * that landed while HTTP was in flight. The acknowledged cloud row becomes the
 * baseline either way; only an unchanged local row is replaced to hydrate the
 * server's normalized timestamp.
 */
internal fun acknowledgeCollectionWrite(
    store: CollectionStore,
    sent: BookCollection,
    cloud: BookCollection,
): CollectionStore {
    if (sent.id != cloud.id) return store
    val next = store.collections.toMutableList()
    val nextDirty = store.dirty.toMutableSet()
    val index = next.indexOfFirst { it.id == sent.id }
    if (index >= 0) {
        next[index] = if (next[index] == sent) {
            nextDirty.remove(sent.id)
            cloud
        } else {
            // This edit is known to follow the just-accepted content. Rebase it
            // one millisecond after the cloud revision instead of carrying a
            // wildly skewed phone clock into the next PATCH.
            next[index].copy(
                updatedAt = nextCollectionTimestamp(cloud.updatedAt, Instant.EPOCH),
            ).also { nextDirty += sent.id }
        }
    } else {
        nextDirty.remove(sent.id)
    }
    return store.copy(
        collections = next,
        shadow = store.shadow + (cloud.id to shadowOf(cloud)),
        dirty = nextDirty,
    )
}

/** Disk-backed, local-first collection store. Cloud sync is additive: every UI
 * mutation commits here first, then merely schedules a best-effort worker. */
object Collections {
    private const val FILE = "collections.json"
    private val lock = Any()

    private fun file(ctx: Context): File = File(ctx.filesDir, FILE)

    private fun read(ctx: Context): CollectionStore = readCollectionStore(file(ctx))

    fun all(ctx: Context): List<BookCollection> = synchronized(lock) {
        read(ctx).collections.filter { it.isLive() }
    }

    internal fun allRecords(ctx: Context): List<BookCollection> = synchronized(lock) {
        read(ctx).collections
    }

    /** Persist atomically; a torn collections.json would read as empty and
     * silently strand every book's provenance. */
    private fun save(ctx: Context, store: CollectionStore): Boolean =
        saveCollectionStore(file(ctx), store)

    /** Apply an edit under the store lock so two rapid taps cannot interleave a
     * read-modify-write and drop one. Deleted rows remain as sync tombstones. */
    internal fun mutate(ctx: Context, edit: (List<BookCollection>) -> CollectionEdit): Int? {
        val result = synchronized(lock) {
            val store = read(ctx)
            if (!store.valid) return@synchronized R.string.collections_error_save
            val currentLive = store.collections.filter { it.isLive() }
            val edited = edit(currentLive)
            val nextLive = edited.collections ?: return@synchronized edited.error
            val remaining = nextLive.associateBy { it.id }.toMutableMap()
            val now = Instant.now()
            val nextRecords = mutableListOf<BookCollection>()
            val nextDirty = store.dirty.toMutableSet()
            for (old in store.collections) {
                if (!old.isLive()) {
                    nextRecords += old
                    continue
                }
                val replacement = remaining.remove(old.id)
                if (replacement == null) {
                    nextRecords += old.copy(
                        updatedAt = nextCollectionTimestamp(old.updatedAt, now),
                        deleted = true,
                    )
                    nextDirty += old.id
                } else {
                    val changed = replacement.name != old.name || replacement.from != old.from
                    nextRecords += replacement.copy(
                        updatedAt = if (changed) nextCollectionTimestamp(old.updatedAt, now)
                        else old.updatedAt,
                        deleted = false,
                    )
                    if (changed) nextDirty += old.id
                }
            }
            remaining.values.forEach { added ->
                nextRecords += added.copy(
                    updatedAt = nextCollectionTimestamp(added.updatedAt, now),
                    deleted = false,
                    mergedInto = null,
                )
                nextDirty += added.id
            }
            if (!save(ctx, store.copy(collections = nextRecords, dirty = nextDirty))) {
                R.string.collections_error_save
            } else null
        }
        if (result == null) CollectionSyncWorker.enqueue(ctx)
        return result
    }

    fun delete(ctx: Context, id: String): Boolean {
        val deleted = synchronized(lock) {
            val store = read(ctx)
            if (!store.valid) return@synchronized false
            val index = store.collections.indexOfFirst { it.id == id }
            if (index < 0 || !store.collections[index].isLive()) return@synchronized true
            val next = store.collections.toMutableList()
            val old = next[index]
            next[index] = old.copy(
                updatedAt = nextCollectionTimestamp(old.updatedAt),
                deleted = true,
            )
            if (!save(ctx, store.copy(
                    collections = next,
                    dirty = store.dirty + id,
                ))
            ) return@synchronized false
            if (Prefs.currentCollectionId(ctx) == id) Prefs.setCurrentCollectionId(ctx, null)
            true
        }
        if (deleted) CollectionSyncWorker.enqueue(ctx)
        return deleted
    }

    /** Merge one freshly fetched cloud snapshot into the latest local state.
     * The merge and its baseline are committed together before network writes. */
    @Throws(IOException::class)
    internal fun planSync(
        ctx: Context,
        cloud: List<BookCollection>,
    ): List<CollectionCloudWrite> = synchronized(lock) {
        val store = read(ctx)
        if (!store.valid) throw IOException("collections.json is unreadable")
        val merge = mergeCollections(store.collections, cloud, store.shadow, store.dirty)
        if ((merge.collections != store.collections || merge.shadow != store.shadow ||
                merge.dirty != store.dirty) &&
            !save(ctx, store.copy(
                collections = merge.collections,
                shadow = merge.shadow,
                dirty = merge.dirty,
            ))
        ) {
            throw IOException("could not persist collection sync")
        }
        Prefs.currentCollectionId(ctx)?.let { selected ->
            if (merge.collections.none { it.id == selected && it.isLive() }) {
                Prefs.setCurrentCollectionId(ctx, null)
            }
        }
        merge.writes
    }

    @Throws(IOException::class)
    internal fun acknowledgeWrite(
        ctx: Context,
        sent: BookCollection,
        cloud: BookCollection,
    ) = synchronized(lock) {
        val store = read(ctx)
        if (!store.valid) throw IOException("collections.json is unreadable")
        val acknowledged = acknowledgeCollectionWrite(store, sent, cloud)
        if (acknowledged != store && !save(ctx, acknowledged)) {
            throw IOException("could not persist collection sync acknowledgement")
        }
    }

    /** The collection a new book would be scanned into, or null if the user
     * still has to pick one. */
    fun current(ctx: Context): BookCollection? =
        resolveCurrentCollection(all(ctx), Prefs.currentCollectionId(ctx))

    fun byId(ctx: Context, id: String): BookCollection? =
        all(ctx).firstOrNull { it.id == id }
}

/** Read separately from Android context so preservation of corrupt local-first
 * state is testable. A missing file is a legitimate new store; an existing
 * unreadable or malformed file is not. */
internal fun readCollectionStore(file: File): CollectionStore = try {
    if (!file.isFile) CollectionStore(emptyList())
    else collectionStoreFromJson(file.readText())
} catch (_: Exception) {
    CollectionStore(emptyList(), valid = false)
}

internal fun saveCollectionStore(file: File, store: CollectionStore): Boolean {
    if (!store.valid) return false
    return try {
        Entries.atomicWrite(file, collectionStoreToJson(store))
        true
    } catch (_: Exception) {
        false
    }
}
