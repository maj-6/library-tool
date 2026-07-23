package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.text.Normalizer
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.util.UUID

/**
 * A collection is the batch a book was scanned into — a shelf, a crate, a room.
 *
 * [from] is where that batch physically came from ("Storage", "Christopher
 * Office"). [tagId] is the editable, human-facing identifier printed as a QR
 * code on its physical box; it is deliberately separate from the durable UUID
 * [id]. [parentId] is a durable collection-to-collection hierarchy edge;
 * physical provenance is never used as identity. [updatedAt] and [deleted] are
 * synchronization state. Tombstones are retained on disk but hidden from
 * [Collections.all], so a signed-out delete cannot be resurrected by the next
 * cloud pull.
 */
data class BookCollection(
    val id: String,
    val name: String,
    val from: String,
    val updatedAt: String = "",
    val deleted: Boolean = false,
    /** Non-null only for an irreversible, human-confirmed identity merge. */
    val mergedInto: String? = null,
    val parentId: String? = null,
    val tagId: String = defaultCollectionTagId(name),
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

/** Compact enough for a box label while leaving room for a numeric suffix. */
internal const val COLLECTION_TAG_ID_MAX = 32

// Keep this explicitly bounded list in sync with migration 018. Using Unicode
// categories or locale-sensitive uppercase independently in two runtimes would
// let the same legacy name receive two different printed labels.
private val COLLECTION_TAG_MARKS = Regex(
    "[\u0300-\u036F\u1AB0-\u1AFF\u1DC0-\u1DFF\u20D0-\u20FF\uFE20-\uFE2F]+",
)
private val COLLECTION_TAG_SEPARATORS = Regex("[^A-Z0-9]+")
private val COLLECTION_TAG_SEQUENCE = Regex("^(.*)_([1-9][0-9]*)$")

private fun uppercaseAscii(value: String): String = buildString(value.length) {
    value.forEach { char ->
        append(
            if (char in 'a'..'z') {
                (char.code - 'a'.code + 'A'.code).toChar()
            } else {
                char
            },
        )
    }
}

/**
 * Canonical form shared by editing, persistence, sync, and QR lookup. Accents
 * are folded where Unicode provides a decomposition; punctuation and runs of
 * whitespace become one underscore. An empty result remains empty so callers
 * can choose whether to suggest a value or reject it.
 */
private fun canonicalCollectionTagIdCharacters(value: String): String =
    uppercaseAscii(
        Normalizer.normalize(value.trim(), Normalizer.Form.NFKD)
            .replace(COLLECTION_TAG_MARKS, ""),
    )
        .replace(COLLECTION_TAG_SEPARATORS, "_")
        .trim('_')

internal fun normalizeCollectionTagId(value: String): String =
    canonicalCollectionTagIdCharacters(value)
        .take(COLLECTION_TAG_ID_MAX)
        .trimEnd('_')

private fun sequencedCollectionTagId(stem: String, sequence: Int): String {
    val suffix = "_${sequence.coerceAtLeast(1)}"
    val fallback = "COLLECTION"
    val available = (COLLECTION_TAG_ID_MAX - suffix.length).coerceAtLeast(1)
    val clipped = stem.take(available).trimEnd('_')
        .ifEmpty { fallback.take(available) }
    return clipped + suffix
}

/** The first label suggested for a name; notably, Fungi becomes FUNGI_1. */
internal fun defaultCollectionTagId(name: String): String {
    val stem = normalizeCollectionTagId(name).ifEmpty { "COLLECTION" }
    return sequencedCollectionTagId(stem, 1)
}

/** Effective canonical tag for legacy/directly constructed model instances. */
internal fun canonicalCollectionTagId(collection: BookCollection): String =
    normalizeCollectionTagId(collection.tagId).ifEmpty {
        defaultCollectionTagId(collection.name)
    }

/**
 * Tags stay reserved even on tombstones. Reusing one would make an old QR label
 * silently open a different physical box after a delete and later recreation.
 */
internal fun collectionTagIdTaken(
    collections: List<BookCollection>,
    tagId: String,
    exceptId: String? = null,
): Boolean {
    val canonical = normalizeCollectionTagId(tagId)
    return canonical.isNotEmpty() && collections.any {
        it.id != exceptId && canonicalCollectionTagId(it) == canonical
    }
}

internal fun collectionTagIdsAreUnique(collections: List<BookCollection>): Boolean {
    val seen = mutableSetOf<String>()
    return collections.all { seen.add(canonicalCollectionTagId(it)) }
}

/** A duplicate matters to the UI only while at least one matching box is live. */
internal fun conflictingLiveCollectionTagId(
    collections: List<BookCollection>,
): String? = collections
    .groupBy(::canonicalCollectionTagId)
    .entries
    .firstOrNull { (_, rows) -> rows.size > 1 && rows.any { it.isLive() } }
    ?.key

/**
 * Resolve a suggestion or migrated value without changing UUID identity. An
 * existing numeric suffix is incremented (FUNGI_1 -> FUNGI_2); otherwise the
 * first collision candidate is suffixed with _1.
 */
internal fun resolveCollectionTagId(
    name: String,
    collections: List<BookCollection>,
    preferredTagId: String? = null,
    exceptId: String? = null,
): String {
    val preferred = preferredTagId?.let(::normalizeCollectionTagId)
        ?.takeIf { it.isNotEmpty() }
        ?: defaultCollectionTagId(name)
    if (!collectionTagIdTaken(collections, preferred, exceptId)) return preferred

    val match = COLLECTION_TAG_SEQUENCE.matchEntire(preferred)
    val stem = match?.groupValues?.get(1)?.ifEmpty { "COLLECTION" } ?: preferred
    var sequence = match?.groupValues?.get(2)?.toIntOrNull()
        ?.takeIf { it < Int.MAX_VALUE }
        ?.plus(1)
        ?: 1
    while (true) {
        val candidate = sequencedCollectionTagId(stem, sequence)
        if (!collectionTagIdTaken(collections, candidate, exceptId)) return candidate
        sequence = if (sequence == Int.MAX_VALUE) 1 else sequence + 1
    }
}

internal fun suggestCollectionTagId(
    name: String,
    collections: List<BookCollection>,
    exceptId: String? = null,
): String = resolveCollectionTagId(name, collections, exceptId = exceptId)

/**
 * Pure QR result. A unique tag on a live row returns that row. A tag retained
 * by a human-confirmed merge loser follows the authoritative merge chain to its
 * live survivor, keeping old printed labels useful. Ordinary deletes, missing
 * targets, cycles, malformed tags, and duplicate tags resolve to no box.
 */
internal fun findCollectionByTagId(
    collections: List<BookCollection>,
    scannedTagId: String,
): BookCollection? {
    // Editing may truncate a suggestion, but scanning must never turn an
    // overlong payload into a valid tag by matching only its first 32 chars.
    val canonical = canonicalCollectionTagIdCharacters(scannedTagId)
    if (canonical.isEmpty() || canonical.length > COLLECTION_TAG_ID_MAX) return null
    var cursor = collections.singleOrNull { canonicalCollectionTagId(it) == canonical }
        ?: return null
    val byId = collections.associateBy { it.id }
    val visited = mutableSetOf<String>()
    while (visited.add(cursor.id)) {
        if (cursor.isLive()) return cursor
        val survivorId = cursor.mergedInto ?: return null
        cursor = byId[survivorId] ?: return null
    }
    return null
}

/** Collapse whitespace and clip. Names are compared case-insensitively for
 * duplicates but stored as typed, so "Storage" doesn't become "storage". */
internal fun normalizeCollectionField(value: String): String =
    value.trim().replace(Regex("\\s+"), " ").take(COLLECTION_FIELD_MAX)

private fun collectionToJson(collection: BookCollection): JSONObject =
    JSONObject()
        .put("id", collection.id)
        .put("name", collection.name)
        .put("from", collection.from)
        .put("tag_id", canonicalCollectionTagId(collection))
        .apply {
            if (collection.updatedAt.isNotEmpty()) put("updated_at", collection.updatedAt)
            if (collection.deleted) put("deleted", true)
            collection.mergedInto?.let { put("merged_into", it) }
            collection.parentId?.let { put("parent_id", it) }
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
        .put("version", 4)
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
    require(version in 1..4) { "unsupported collections version" }
    val array = root.optJSONArray("collections")
        ?: throw IllegalArgumentException("collections must be an array")
    if (version >= 2 && root.has("sync_shadow") && root.optJSONObject("sync_shadow") == null) {
        throw IllegalArgumentException("sync_shadow must be an object")
    }
    if (version >= 2 && root.has("sync_dirty") && root.optJSONArray("sync_dirty") == null) {
        throw IllegalArgumentException("sync_dirty must be an array")
    }
    val seen = mutableSetOf<String>()
    val out = mutableListOf<BookCollection>()
    for (i in 0 until array.length()) {
        val item = array.optJSONObject(i) ?: continue
        val id = item.optString("id").trim()
        val name = normalizeCollectionField(item.optString("name"))
        if (id.isEmpty() || name.isEmpty() || !seen.add(id)) continue
        val rawTagId = when (val rawTag = item.opt("tag_id")) {
            null, JSONObject.NULL -> null
            is String -> rawTag
            else -> continue
        }
        // A v4 duplicate can be a real cross-device conflict awaiting the
        // database's permanent-reservation verdict. Preserve it so no printed
        // label is silently reassigned; QR lookup fails closed until a human
        // explicitly retags one row. Legacy rows are allocated together below
        // in UUID order, matching migration 018 rather than depending on this
        // device's JSON insertion order.
        val tagId = if (version >= 4) {
            normalizeCollectionTagId(rawTagId.orEmpty())
                .ifEmpty { defaultCollectionTagId(name) }
        } else {
            defaultCollectionTagId(name)
        }
        val mergedInto = if (item.isNull("merged_into")) null
        else item.optString("merged_into").trim().ifEmpty { null }
        val parentId = when (val rawParent = item.opt("parent_id")) {
            null, JSONObject.NULL -> null
            is String -> rawParent.trim().ifEmpty { null }
            else -> continue
        }
        out += BookCollection(
            id = id,
            name = name,
            from = normalizeCollectionField(item.optString("from")),
            updatedAt = item.optString("updated_at").trim(),
            deleted = item.optBoolean("deleted", false),
            mergedInto = mergedInto,
            parentId = parentId,
            tagId = tagId,
        )
    }
    val collections = if (version >= 4) out else {
        val allocated = mutableListOf<BookCollection>()
        val tagsById = mutableMapOf<String, String>()
        out.sortedBy { it.id.lowercase() }.forEach { row ->
            val migrated = row.copy(
                tagId = resolveCollectionTagId(row.name, allocated),
            )
            allocated += migrated
            tagsById[row.id] = migrated.tagId
        }
        out.map { it.copy(tagId = tagsById.getValue(it.id)) }
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
    val migratedShadow = if (version >= 4) shadow else shadow.mapValues { (id, baseline) ->
        val row = collections.firstOrNull { it.id == id }
        // Adding a synthesized tag is a format migration, not local intent. If
        // every pre-tag semantic field still matches the saved baseline, rebase
        // that baseline so a differing server backfill wins as a one-sided
        // cloud change. A genuinely edited legacy row keeps its old hash (and
        // usually its dirty marker), preserving the normal three-way merge.
        if (row != null && baseline.hash == collectionContentHashBeforeTags(row)) {
            baseline.copy(hash = collectionContentHash(row))
        } else {
            baseline
        }
    }
    CollectionStore(collections, migratedShadow, dirty)
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

private fun normalizeCollectionParentId(value: String?): String? =
    value?.trim()?.ifEmpty { null }

/**
 * True when [parentId] is not a live collection, points to this collection, or
 * reaches this collection (or any pre-existing cycle) through parent links.
 * An unchanged dangling parent can be allowed so an unrelated edit never
 * silently destroys hierarchy received before its parent was deleted locally.
 */
internal fun collectionParentIsInvalid(
    collections: List<BookCollection>,
    collectionId: String,
    parentId: String?,
    allowedMissingParentId: String? = null,
): Boolean {
    val cleanParentId = normalizeCollectionParentId(parentId) ?: return false
    if (cleanParentId == collectionId) return true
    val liveById = collections.filter { it.isLive() }.associateBy { it.id }
    var cursor = liveById[cleanParentId]
        ?: return cleanParentId != normalizeCollectionParentId(allowedMissingParentId)
    val visited = mutableSetOf(collectionId)
    while (true) {
        if (!visited.add(cursor.id)) return true
        val nextId = normalizeCollectionParentId(cursor.parentId) ?: return false
        cursor = liveById[nextId] ?: return false
    }
}

/** Valid live choices for the collection editor, excluding self, descendants,
 * and branches that are already cyclic. */
internal fun collectionParentCandidates(
    collections: List<BookCollection>,
    collectionId: String,
): List<BookCollection> = collections.filter {
    it.isLive() &&
        !collectionParentIsInvalid(collections, collectionId, it.id)
}

/** Result of an edit: [collections] is the list to persist, [error] is a string
 * resource to show instead when the edit was rejected. */
internal data class CollectionEdit(val collections: List<BookCollection>?, val error: Int?)

internal fun addCollection(
    collections: List<BookCollection>,
    name: String,
    from: String,
    id: String = UUID.randomUUID().toString(),
    parentId: String? = null,
    tagId: String? = null,
): CollectionEdit {
    val clean = normalizeCollectionField(name)
    if (clean.isEmpty()) return CollectionEdit(null, R.string.collections_error_name_required)
    if (collectionNameTaken(collections, clean)) {
        return CollectionEdit(null, R.string.collections_error_name_taken)
    }
    val requestedTagId = tagId?.let(::normalizeCollectionTagId)
    val cleanTagId = if (requestedTagId.isNullOrEmpty()) {
        suggestCollectionTagId(clean, collections)
    } else {
        if (collectionTagIdTaken(collections, requestedTagId)) {
            return CollectionEdit(null, R.string.collections_error_tag_id_taken)
        }
        requestedTagId
    }
    val cleanParentId = normalizeCollectionParentId(parentId)
    if (collectionParentIsInvalid(collections, id, cleanParentId)) {
        return CollectionEdit(null, R.string.collections_error_parent_invalid)
    }
    return CollectionEdit(
        collections + BookCollection(
            id = id,
            name = clean,
            from = normalizeCollectionField(from),
            parentId = cleanParentId,
            tagId = cleanTagId,
        ),
        null,
    )
}

internal fun updateCollection(
    collections: List<BookCollection>,
    id: String,
    name: String,
    from: String,
    parentId: String? = null,
    tagId: String? = null,
): CollectionEdit {
    val clean = normalizeCollectionField(name)
    if (clean.isEmpty()) return CollectionEdit(null, R.string.collections_error_name_required)
    if (collectionNameTaken(collections, clean, exceptId = id)) {
        return CollectionEdit(null, R.string.collections_error_name_taken)
    }
    val existing = collections.firstOrNull { it.id == id && it.isLive() }
    if (existing == null) {
        return CollectionEdit(null, R.string.collections_error_missing)
    }
    val requestedTagId = tagId?.let(::normalizeCollectionTagId)
    val cleanTagId = when {
        tagId == null -> existing.tagId
        requestedTagId.isNullOrEmpty() -> suggestCollectionTagId(clean, collections, id)
        collectionTagIdTaken(collections, requestedTagId, id) -> {
            return CollectionEdit(null, R.string.collections_error_tag_id_taken)
        }
        else -> requestedTagId
    }
    val cleanParentId = normalizeCollectionParentId(parentId)
    val allowedMissing = existing.parentId?.takeIf { it == cleanParentId }
    if (collectionParentIsInvalid(collections, id, cleanParentId, allowedMissing)) {
        return CollectionEdit(null, R.string.collections_error_parent_invalid)
    }
    return CollectionEdit(
        collections.map {
            if (it.id == id) it.copy(
                name = clean,
                from = normalizeCollectionField(from),
                parentId = cleanParentId,
                tagId = cleanTagId,
            ) else it
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

private fun hashCollectionFields(fields: List<String>): String {
    val canonical = fields.joinToString("\u0000")
    val digest = MessageDigest.getInstance("SHA-256").digest(canonical.toByteArray())
    return buildString(digest.size * 2) {
        digest.forEach { byte ->
            val value = byte.toInt() and 0xff
            if (value < 16) append('0')
            append(value.toString(16))
        }
    }
}

/** Hash emitted by the parent-aware v3 store immediately before tags existed. */
private fun collectionContentHashBeforeTags(collection: BookCollection): String {
    val fields = mutableListOf(
        collection.id,
        collection.name,
        collection.from,
        collection.deleted.toString(),
        collection.mergedInto.orEmpty(),
    )
    // Preserve every v2 hash when no parent exists, so upgrading does not make
    // all clean rows look locally edited against their stored sync shadow.
    collection.parentId?.let { fields += "parent:$it" }
    return hashCollectionFields(fields)
}

/** Stable semantic identity; timestamps are revision metadata, not content. */
internal fun collectionContentHash(collection: BookCollection): String {
    val fields = mutableListOf(
        collection.id,
        collection.name,
        collection.from,
        collection.deleted.toString(),
        collection.mergedInto.orEmpty(),
        "tag:${canonicalCollectionTagId(collection)}",
    )
    collection.parentId?.let { fields += "parent:$it" }
    return hashCollectionFields(fields)
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

internal data class RetiredCollectionRetag(
    val store: CollectionStore?,
    val error: Int?,
)

/** Pure retired-box edit: change only the printed tag and keep the tombstone. */
internal fun retagRetiredCollection(
    store: CollectionStore,
    id: String,
    tagId: String,
    now: Instant = Instant.now(),
): RetiredCollectionRetag {
    if (!store.valid) {
        return RetiredCollectionRetag(null, R.string.collections_error_save)
    }
    val index = store.collections.indexOfFirst {
        it.id == id && it.deleted && it.mergedInto == null
    }
    if (index < 0) {
        return RetiredCollectionRetag(null, R.string.collections_error_missing)
    }
    val canonical = normalizeCollectionTagId(tagId)
    val old = store.collections[index]
    if (canonical.isEmpty() || canonical == canonicalCollectionTagId(old)) {
        return RetiredCollectionRetag(null, R.string.collections_error_retag_required)
    }
    if (collectionTagIdTaken(store.collections, canonical, exceptId = id)) {
        return RetiredCollectionRetag(null, R.string.collections_error_tag_id_taken)
    }
    val next = store.collections.toMutableList()
    next[index] = old.copy(
        tagId = canonical,
        updatedAt = nextCollectionTimestamp(old.updatedAt, now),
    )
    return RetiredCollectionRetag(
        store.copy(collections = next, dirty = store.dirty + id),
        null,
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

    internal fun conflictingLiveTagId(ctx: Context): String? = synchronized(lock) {
        conflictingLiveCollectionTagId(read(ctx).collections)
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
            val retired = store.collections.filterNot { it.isLive() }
            // The editor intentionally receives live rows only, but an old QR
            // label must never be rebound to a different box. Recheck against
            // tombstones under the store lock before anything is persisted.
            if (!collectionTagIdsAreUnique(nextLive + retired)) {
                return@synchronized R.string.collections_error_tag_id_taken
            }
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
                    val changed = replacement.name != old.name ||
                        replacement.from != old.from ||
                        replacement.parentId != old.parentId ||
                        replacement.tagId != old.tagId
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

    /** Resolve a cloud reservation conflict for a locally retired box without
     * resurrecting it. This is the only ordinary edit allowed on a tombstone. */
    internal fun retagRetired(ctx: Context, id: String, tagId: String): Int? {
        val result = synchronized(lock) {
            val store = read(ctx)
            val edit = retagRetiredCollection(store, id, tagId)
            val next = edit.store ?: return@synchronized edit.error
            if (!save(ctx, next)) R.string.collections_error_save else null
        }
        if (result == null) {
            Prefs.setCollectionTagConflict(ctx, null)
            CollectionSyncWorker.enqueue(ctx)
        }
        return result
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

    fun byTagId(ctx: Context, tagId: String): BookCollection? =
        findCollectionByTagId(allRecords(ctx), tagId)
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
