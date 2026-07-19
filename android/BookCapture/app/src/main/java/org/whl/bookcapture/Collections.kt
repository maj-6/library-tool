package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/**
 * A collection is the batch a book was scanned into — a shelf, a crate, a room.
 *
 * [from] is where that batch physically came from ("Storage", "Christopher
 * Office"). Every book scanned into the collection inherits it, and a single
 * book can override it afterwards, because a crate occasionally arrives with a
 * stray from somewhere else.
 */
data class BookCollection(val id: String, val name: String, val from: String)

/** Longest name/from we will store. Long enough for "Christopher Office, back
 *  shelf", short enough that a row stays one line on a phone. */
internal const val COLLECTION_FIELD_MAX = 80

/** Collapse whitespace and clip. Names are compared case-insensitively for
 *  duplicates but stored as typed, so "Storage" doesn't become "storage". */
internal fun normalizeCollectionField(value: String): String =
    value.trim().replace(Regex("\\s+"), " ").take(COLLECTION_FIELD_MAX)

internal fun collectionsToJson(collections: List<BookCollection>): String {
    val array = JSONArray()
    for (c in collections) {
        array.put(
            JSONObject()
                .put("id", c.id)
                .put("name", c.name)
                .put("from", c.from),
        )
    }
    return JSONObject().put("version", 1).put("collections", array).toString()
}

/**
 * Parse the stored list, dropping anything unusable rather than throwing — a
 * truncated or hand-edited file must not brick the Collections tab. Entries
 * without an id or name are skipped; duplicate ids keep the first.
 */
internal fun collectionsFromJson(text: String): List<BookCollection> = try {
    val array = JSONObject(text).optJSONArray("collections") ?: JSONArray()
    val seen = mutableSetOf<String>()
    val out = mutableListOf<BookCollection>()
    for (i in 0 until array.length()) {
        val item = array.optJSONObject(i) ?: continue
        val id = item.optString("id").trim()
        val name = normalizeCollectionField(item.optString("name"))
        if (id.isEmpty() || name.isEmpty() || !seen.add(id)) continue
        out += BookCollection(id, name, normalizeCollectionField(item.optString("from")))
    }
    out
} catch (_: Exception) {
    emptyList()
}

/** Case-insensitive name clash, ignoring [exceptId] so renaming a collection to
 *  its own name (or a case variant of it) is allowed. */
internal fun collectionNameTaken(
    collections: List<BookCollection>,
    name: String,
    exceptId: String? = null,
): Boolean = collections.any {
    it.id != exceptId && it.name.equals(normalizeCollectionField(name), ignoreCase = true)
}

/** Result of an edit: [collections] is the list to persist, [error] is a string
 *  resource to show instead when the edit was rejected. */
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
    if (collections.none { it.id == id }) {
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
    collections.firstOrNull { it.id == selectedId }?.let { return it }
    return collections.singleOrNull()
}

/**
 * Disk-backed store for the collection list.
 *
 * Local to the phone by design for now: the collection name and its "from" ride
 * along inside each capture's metadata, so the desktop sees provenance without
 * a schema change. Promoting collections to shared cloud rows is a later step.
 */
object Collections {
    private const val FILE = "collections.json"
    private val lock = Any()

    private fun file(ctx: Context): File = File(ctx.filesDir, FILE)

    fun all(ctx: Context): List<BookCollection> = synchronized(lock) {
        val f = file(ctx)
        if (!f.isFile) emptyList() else collectionsFromJson(runCatching { f.readText() }.getOrDefault(""))
    }

    /** Persist atomically; a torn collections.json would read as empty and
     *  silently strand every book's provenance. */
    private fun save(ctx: Context, collections: List<BookCollection>): Boolean = try {
        Entries.atomicWrite(file(ctx), collectionsToJson(collections))
        true
    } catch (_: Exception) {
        false
    }

    /** Apply an edit under the store lock so two rapid taps can't interleave a
     *  read-modify-write and drop one of them. Returns the error resource, or
     *  null on success. */
    internal fun mutate(ctx: Context, edit: (List<BookCollection>) -> CollectionEdit): Int? =
        synchronized(lock) {
            val f = file(ctx)
            val current = if (!f.isFile) emptyList()
            else collectionsFromJson(runCatching { f.readText() }.getOrDefault(""))
            val result = edit(current)
            val next = result.collections ?: return result.error
            if (!save(ctx, next)) return R.string.collections_error_save
            null
        }

    fun delete(ctx: Context, id: String): Boolean = synchronized(lock) {
        val f = file(ctx)
        val current = if (!f.isFile) emptyList()
        else collectionsFromJson(runCatching { f.readText() }.getOrDefault(""))
        val next = removeCollection(current, id)
        if (next.size == current.size) return true
        if (!save(ctx, next)) return false
        if (Prefs.currentCollectionId(ctx) == id) Prefs.setCurrentCollectionId(ctx, null)
        true
    }

    /** The collection a new book would be scanned into, or null if the user
     *  still has to pick one. */
    fun current(ctx: Context): BookCollection? =
        resolveCurrentCollection(all(ctx), Prefs.currentCollectionId(ctx))

    fun byId(ctx: Context, id: String): BookCollection? = all(ctx).firstOrNull { it.id == id }
}
