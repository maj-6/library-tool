package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.math.BigDecimal
import java.time.OffsetDateTime

/**
 * Books a box holds according to the CLOUD, not according to this handset.
 *
 * `collection_inventory.json` only ever knows what this phone captured and has
 * not pruned, so a reinstall or a second handset shows an empty box. The cloud
 * keeps every `captures` row forever — desktop import only PATCHes `status`, and
 * `delete_remote` removes storage objects rather than the row — so the row plus
 * its frozen `meta` provenance is the durable record of what is in a crate.
 *
 * This store caches that answer per collection so a box listed once stays
 * readable offline. It is a cache, never a source of truth: a local entry always
 * wins over its remote twin, and a failed fetch must leave the last good copy in
 * place rather than emptying the list.
 */
internal data class RemoteCollectionBook(
    /** Equal to the local entry id: `insertCapture(id = entryId, ...)`. */
    val captureId: String,
    val collectionId: String,
    val collectionName: String,
    val title: String,
    val author: String,
    val year: String,
    val photoCount: Int,
    val createdAt: Long,
)

internal data class RemoteCollectionBooksStore(
    /** collection id -> the books the cloud reported for it. */
    val byCollection: Map<String, List<RemoteCollectionBook>> = emptyMap(),
    /** False means an existing source was unreadable and must not be replaced. */
    val valid: Boolean = true,
    /** The account whose RLS scope produced these rows. */
    val owner: String = "",
)

internal const val REMOTE_COLLECTION_BOOKS_FILE = "remote_collection_books.json"
internal const val REMOTE_COLLECTION_BOOKS_VERSION = 1

/** Per-request page size. PostgREST may return fewer rows; an empty page, not a
 * short one, is the only end-of-snapshot signal. */
internal const val REMOTE_COLLECTION_BOOKS_PAGE_SIZE = 500

/** Refuse an implausibly large or non-terminating snapshot explicitly instead
 * of silently caching a prefix forever. */
internal const val REMOTE_COLLECTION_BOOKS_MAX_ROWS = 50_000

internal data class RemoteCollectionFetchTicket(
    val owner: String,
    val collectionId: String,
    val generation: Long,
)

/**
 * Tracks owner-scoped cloud box snapshots across Activity refreshes.
 *
 * A rearm invalidates both completed and in-flight work. Cache writers check
 * [isCurrent] after entering [RemoteCollectionBooks]' serialized commit
 * boundary, so the generation monitor itself never spans disk I/O.
 */
internal class RemoteCollectionFetchTracker {
    private data class Key(val owner: String, val collectionId: String)

    private val generations = linkedMapOf<Key, Long>()
    private var nextGeneration = 0L

    @Synchronized
    fun begin(owner: String, collectionId: String): RemoteCollectionFetchTicket? {
        if (owner.isEmpty() || collectionId.isEmpty()) return null
        val key = Key(owner, collectionId)
        if (key in generations) return null
        nextGeneration += 1
        val generation = nextGeneration
        generations[key] = generation
        return RemoteCollectionFetchTicket(owner, collectionId, generation)
    }

    @Synchronized
    fun rearm(owner: String) {
        generations.keys.removeAll { it.owner == owner }
    }

    @Synchronized
    fun isCurrent(ticket: RemoteCollectionFetchTicket): Boolean =
        generations[Key(ticket.owner, ticket.collectionId)] == ticket.generation

    @Synchronized
    fun finish(ticket: RemoteCollectionFetchTicket, landed: Boolean) {
        val key = Key(ticket.owner, ticket.collectionId)
        if (!landed && generations[key] == ticket.generation) {
            generations.remove(key)
        }
    }
}

internal object RemoteCollectionBooks {

    /**
     * The cached listing for [owner] only.
     *
     * A listing is the product of one account's RLS scope, so a store written by a
     * different account reads as EMPTY rather than as that account's books. Every
     * other account-scoped path in the app fails closed the same way — see
     * `Prefs.setSession`, `SupabaseClient.open`'s `AccountChanged`, and
     * `UploadWorker`'s capture-sync owner check.
     */
    fun read(ctx: Context, owner: String = Prefs.userId(ctx)): RemoteCollectionBooksStore =
        readRemoteCollectionBooksStore(File(ctx.filesDir, REMOTE_COLLECTION_BOOKS_FILE), owner)

    /**
     * Replace one collection's cached listing. [discardCollectionIds] contains
     * only explicit tombstones/merge losers from the authoritative collection
     * log, so concurrent fetches never infer that a newly created box is dead.
     * A store owned by another account is replaced wholesale rather than merged.
     */
    fun record(
        ctx: Context,
        collectionId: String,
        books: List<RemoteCollectionBook>,
        owner: String = Prefs.userId(ctx),
        discardCollectionIds: Set<String> = emptySet(),
        commitIf: () -> Boolean = { true },
    ): Boolean = record(
        File(ctx.filesDir, REMOTE_COLLECTION_BOOKS_FILE),
        owner,
        collectionId,
        books,
        discardCollectionIds,
        commitIf,
    )

    internal fun record(
        target: File,
        owner: String,
        collectionId: String,
        books: List<RemoteCollectionBook>,
        discardCollectionIds: Set<String> = emptySet(),
        commitIf: () -> Boolean = { true },
    ): Boolean = synchronized(this) {
        // Check only after entering the cache's write order. If an older fetch
        // was already writing when a refresh re-armed the tracker, every newer
        // accepted write queues behind it and therefore remains the final one.
        if (!commitIf()) return@synchronized false
        if (collectionId.isEmpty() || owner.isEmpty()) return@synchronized false
        val stored = readRemoteCollectionBooksStore(target, owner)
        if (!stored.valid) return@synchronized false
        val updated = LinkedHashMap(stored.byCollection)
        discardCollectionIds.forEach(updated::remove)
        updated[collectionId] = books
        if (target.isFile && stored.owner == owner &&
            updated == stored.byCollection
        ) return@synchronized true
        saveRemoteCollectionBooksStore(target, stored.copy(byCollection = updated, owner = owner))
    }

    /** Drop cached boxes that can no longer be rendered, so the file cannot grow
     * without bound. [keepCollectionIds] must be the ids Inspect can actually
     * resolve — a tombstoned box is never displayed, so its listing is dead. */
    fun prune(
        ctx: Context,
        keepCollectionIds: Set<String>,
        owner: String = Prefs.userId(ctx),
    ): Boolean = prune(
        File(ctx.filesDir, REMOTE_COLLECTION_BOOKS_FILE), owner, keepCollectionIds,
    )

    internal fun prune(
        target: File,
        owner: String,
        keepCollectionIds: Set<String>,
    ): Boolean = synchronized(this) {
        val stored = readRemoteCollectionBooksStore(target, owner)
        if (!stored.valid) return@synchronized false
        val updated = stored.byCollection.filterKeys { it in keepCollectionIds }
        if (stored.owner == owner && updated.size == stored.byCollection.size) {
            return@synchronized true
        }
        saveRemoteCollectionBooksStore(target, stored.copy(byCollection = updated, owner = owner))
    }
}

/**
 * Consume a complete owner/collection-scoped capture snapshot.
 *
 * The broad capture RLS policy also lets curator accounts ingest assigned
 * contributors. Android's personal box cache is narrower, so every returned
 * row is checked again against [expectedOwnerId] and [expectedCollectionIds].
 * Any malformed, foreign, duplicate, out-of-order, or oversized snapshot fails
 * before callers replace the last good cache.
 */
internal fun collectRemoteCollectionBookPages(
    expectedOwnerId: String,
    expectedCollectionIds: Set<String>,
    maximumRows: Int = REMOTE_COLLECTION_BOOKS_MAX_ROWS,
    fetchPage: (afterId: String?) -> JSONArray,
): List<RemoteCollectionBook> {
    require(SAFE_CAPTURE_SYNC_ID.matches(expectedOwnerId)) { "invalid capture owner" }
    require(maximumRows > 0) { "maximum rows must be positive" }
    val collectionIds = expectedCollectionIds.asSequence()
        .map { it.trim().lowercase() }
        .filter(SAFE_COLLECTION_FILTER_ID::matches)
        .toSet()
    if (collectionIds.isEmpty()) return emptyList()

    val books = linkedMapOf<String, RemoteCollectionBook>()
    var afterId: String? = null
    var rowCount = 0
    while (true) {
        val rows = fetchPage(afterId)
        if (rows.length() == 0) return books.values.toList()
        if (rows.length() > maximumRows - rowCount) {
            throw IOException("cloud collection capture snapshot is too large")
        }
        rowCount += rows.length()

        var previousId = afterId
        for (index in 0 until rows.length()) {
            val row = rows.optJSONObject(index)
                ?: throw IOException("invalid cloud collection capture row")
            val captureId = captureRowString(row, "id").lowercase()
            if (!SAFE_CAPTURE_SYNC_ID.matches(captureId) ||
                (previousId != null && captureId <= previousId)
            ) {
                throw IOException("cloud collection capture pagination did not advance")
            }
            previousId = captureId
            val parsed = remoteCollectionBookFromCaptureJson(row, expectedOwnerId)
                ?: throw IOException("invalid owner-scoped cloud collection capture row")
            if (parsed.collectionId.lowercase() !in collectionIds ||
                books.putIfAbsent(parsed.captureId, parsed) != null
            ) {
                throw IOException("duplicate or out-of-scope cloud collection capture row")
            }
        }
        afterId = previousId
            ?: throw IOException("cloud collection capture pagination did not advance")
    }
}

/**
 * Fill blank titles from the desktop's curated bibliography.
 *
 * A capture's own `meta` holds a title only when the CAPTURING phone had an
 * extraction API key, so for most rows the desktop's projection is the only
 * source of a real title. The capture's own snapshot still wins when it has one:
 * it is what the contributor saw at capture time.
 *
 * Pure so the precedence is testable without a network or a database.
 */
internal fun enrichRemoteCollectionBooks(
    books: List<RemoteCollectionBook>,
    desktop: Map<String, DesktopBookMetadata>,
): List<RemoteCollectionBook> = books.map { book ->
    val projected = desktop[book.captureId]?.bibliography ?: DesktopBibliography.EMPTY
    if (projected.isEmpty) return@map book
    book.copy(
        title = book.title.ifEmpty { projected.title },
        author = book.author.ifEmpty { projected.author },
        year = book.year.ifEmpty { projected.year },
    )
}

/** Render a cloud row through the same Inspect path as a pruned local summary:
 * both are photo-less rows with a null `current`. Only [CollectionInventoryItem.remote]
 * distinguishes them, so the UI can explain the right reason. */
internal fun RemoteCollectionBook.toInventoryItem(): CollectionInventoryItem =
    CollectionInventoryItem(
        summary = CollectionInventorySummary(
            entryId = captureId,
            collectionId = collectionId,
            collectionName = collectionName,
            title = title,
            author = author,
            year = year,
            photoCount = photoCount,
            createdAt = createdAt,
        ),
        current = null,
        remote = true,
    )

/**
 * Collapse one box's local and remote rows onto the shared capture/entry id.
 *
 * A LOCAL ROW ALWAYS WINS: it can open its photos and carries live upload status,
 * where its cloud twin is a summary. Callers pass local rows first, but the
 * preference is explicit here so ordering cannot silently invert it.
 */
internal fun mergeCollectionBookItems(
    items: List<CollectionInventoryItem>,
): List<CollectionInventoryItem> {
    val byId = LinkedHashMap<String, CollectionInventoryItem>()
    items.forEach { item ->
        val id = item.summary.entryId
        if (id.isEmpty()) return@forEach
        val existing = byId[id]
        if (existing == null || (existing.remote && !item.remote)) byId[id] = item
    }
    return byId.values.toList()
}

/** Aliases the box-listing query projects `meta->>...` into. Named here so the
 * request and this parser cannot drift apart. */
internal const val CAPTURE_COLLECTION_ID_FIELD = "collection_id"
internal const val CAPTURE_COLLECTION_NAME_FIELD = "collection_name"

/**
 * Read one projected `captures` row into a book summary.
 *
 * The row is FLAT, not nested: the query aliases `meta->>scan_collection_id` to
 * [CAPTURE_COLLECTION_ID_FIELD] and so on, so the whole `meta` blob never crosses
 * the wire. `scan_collection_id` / `scan_collection` remain the wire contract
 * shared with `PHONE_PROVENANCE_KEYS` in tools/whl_explorer/server.py — the
 * `scan_` prefix is load-bearing on both ends.
 *
 * Title/author/year are extraction output and are simply absent when the
 * capturing phone had no extraction API key; such a row must still list, or the
 * box looks empty.
 */
internal fun remoteCollectionBookFromCaptureJson(
    row: JSONObject,
    expectedOwnerId: String? = null,
): RemoteCollectionBook? {
    if (expectedOwnerId != null &&
        captureRowString(row, "created_by").lowercase() != expectedOwnerId.trim().lowercase()
    ) return null
    val captureId = captureRowString(row, "id")
    if (captureId.isEmpty()) return null
    val collectionId = captureRowString(row, CAPTURE_COLLECTION_ID_FIELD)
    if (collectionId.isEmpty()) return null
    return RemoteCollectionBook(
        captureId = captureId,
        collectionId = collectionId,
        collectionName = normalizeCollectionField(
            captureRowString(row, CAPTURE_COLLECTION_NAME_FIELD),
        ),
        title = normalizeCollectionField(captureRowString(row, "title")),
        author = normalizeCollectionField(captureRowString(row, "author")),
        year = normalizeCollectionField(captureRowString(row, "year")),
        // The count stays meaningful after import even though the objects are
        // gone: `delete_remote` never resets `photos` to [], so these paths must
        // never be treated as fetchable thumbnails.
        photoCount = row.optJSONArray("photos")?.length() ?: 0,
        createdAt = parseCaptureCreatedAt(captureRowString(row, "created_at")),
    )
}

/** `meta->>missing` projects SQL NULL, which org.json surfaces as
 * [JSONObject.NULL] — and `optString` renders that as the literal "null". Read
 * every projected column through here so an absent title never becomes "null". */
private fun captureRowString(row: JSONObject, name: String): String {
    val value = row.opt(name)
    if (value == null || value === JSONObject.NULL) return ""
    return (value as? String ?: value.toString()).trim()
}

/** PostgREST renders timestamptz with an offset ("+00:00"), which
 * [java.time.Instant.parse] rejects. An unreadable stamp sorts last rather than
 * dropping the book. */
internal fun parseCaptureCreatedAt(raw: String): Long {
    val text = raw.trim()
    if (text.isEmpty()) return 0L
    return try {
        OffsetDateTime.parse(text).toInstant().toEpochMilli()
    } catch (_: Exception) {
        0L
    }
}

internal fun remoteCollectionBooksStoreToJson(store: RemoteCollectionBooksStore): String {
    val collections = JSONObject()
    store.byCollection.toSortedMap().forEach { (collectionId, books) ->
        val array = JSONArray()
        books.sortedBy { it.captureId }.forEach { array.put(remoteBookToJson(it)) }
        collections.put(collectionId, array)
    }
    return JSONObject()
        .put("version", REMOTE_COLLECTION_BOOKS_VERSION)
        .put("owner", store.owner)
        .put("collections", collections)
        .toString()
}

internal fun remoteCollectionBooksStoreFromJson(text: String): RemoteCollectionBooksStore = try {
    val root = JSONObject(text)
    val version = requiredRemoteWholeNumber(root, "version")
    require(version in 1L..REMOTE_COLLECTION_BOOKS_VERSION.toLong()) {
        "unsupported remote collection books version"
    }
    val owner = requiredRemoteString(root, "owner")
    val collections = root.optJSONObject("collections")
        ?: throw IllegalArgumentException("collections must be an object")
    val byCollection = LinkedHashMap<String, List<RemoteCollectionBook>>()
    collections.keys().asSequence().toList().sorted().forEach { collectionId ->
        require(collectionId.isNotEmpty()) { "invalid collection id" }
        val array = collections.optJSONArray(collectionId)
            ?: throw IllegalArgumentException("collection must hold an array")
        val books = mutableListOf<RemoteCollectionBook>()
        for (index in 0 until array.length()) {
            val row = array.optJSONObject(index)
                ?: throw IllegalArgumentException("book must be an object")
            books += remoteBookFromJson(collectionId, row)
        }
        byCollection[collectionId] = books
    }
    RemoteCollectionBooksStore(byCollection, owner = owner)
} catch (_: Exception) {
    RemoteCollectionBooksStore(valid = false)
}

/**
 * Read the listing cache as [owner]. A store another account wrote is reported as
 * an empty — but VALID — store, so the next [RemoteCollectionBooks.record]
 * replaces it wholesale instead of merging one account's books into another's.
 */
internal fun readRemoteCollectionBooksStore(
    target: File,
    owner: String,
): RemoteCollectionBooksStore {
    if (!target.exists()) return RemoteCollectionBooksStore(owner = owner)
    if (!target.isFile) return RemoteCollectionBooksStore(valid = false)
    val stored = try {
        remoteCollectionBooksStoreFromJson(target.readText())
    } catch (_: Exception) {
        RemoteCollectionBooksStore(valid = false)
    }
    if (!stored.valid) return stored
    if (stored.owner != owner) return RemoteCollectionBooksStore(owner = owner)
    return stored
}

internal fun saveRemoteCollectionBooksStore(
    target: File,
    store: RemoteCollectionBooksStore,
): Boolean {
    if (!store.valid) return false
    return try {
        target.parentFile?.mkdirs()
        Entries.atomicWrite(target, remoteCollectionBooksStoreToJson(store))
        true
    } catch (_: Exception) {
        false
    }
}

private fun remoteBookToJson(book: RemoteCollectionBook): JSONObject =
    JSONObject()
        .put("capture_id", book.captureId)
        .put("collection_name", book.collectionName)
        .put("title", book.title)
        .put("author", book.author)
        .put("year", book.year)
        .put("photo_count", book.photoCount)
        .put("created_at", book.createdAt)

private fun remoteBookFromJson(collectionId: String, row: JSONObject): RemoteCollectionBook {
    val captureId = requiredRemoteString(row, "capture_id").trim()
    require(captureId.isNotEmpty()) { "invalid capture id" }
    val photoCount = requiredRemoteWholeNumber(row, "photo_count")
    require(photoCount in 0..Int.MAX_VALUE.toLong()) { "invalid photo count" }
    val createdAt = requiredRemoteWholeNumber(row, "created_at")
    require(createdAt >= 0L) { "invalid creation time" }
    return RemoteCollectionBook(
        captureId = captureId,
        collectionId = collectionId,
        collectionName = requiredRemoteString(row, "collection_name"),
        title = requiredRemoteString(row, "title"),
        author = requiredRemoteString(row, "author"),
        year = requiredRemoteString(row, "year"),
        photoCount = photoCount.toInt(),
        createdAt = createdAt,
    )
}

private fun requiredRemoteString(source: JSONObject, name: String): String =
    source.opt(name) as? String
        ?: throw IllegalArgumentException("$name must be a string")

private fun requiredRemoteWholeNumber(source: JSONObject, name: String): Long {
    val raw = source.opt(name) as? Number
        ?: throw IllegalArgumentException("$name must be a number")
    return try {
        BigDecimal(raw.toString()).longValueExact()
    } catch (_: ArithmeticException) {
        throw IllegalArgumentException("$name must be a whole 64-bit number")
    } catch (_: NumberFormatException) {
        throw IllegalArgumentException("$name must be a whole 64-bit number")
    }
}
