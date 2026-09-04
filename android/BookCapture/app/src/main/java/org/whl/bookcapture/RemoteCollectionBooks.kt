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
    /** Immutable capture-time provenance from `meta.scan_collection_id`. */
    val originalCollectionId: String,
    /** Current effective box after any server-side move. */
    val collectionId: String,
    /** A tombstone is returned to evict an older cached membership. */
    val removed: Boolean,
    /** Server-monotonic membership revision; zero means legacy/unmodified. */
    val membershipRevision: Long,
    val collectionName: String,
    val title: String,
    val author: String,
    val year: String,
    val photoCount: Int,
    val createdAt: Long,
    /** `null` means this cache row predates or lacks a curator classification. */
    val digitizationCandidateClassification: Boolean? = null,
    /** Present only for an explicitly classified candidate; valid values are 1..5. */
    val scanPriorityRank: Int? = null,
    /** Canonical catalog scan assessment; null can mean Unassessed or legacy-unknown. */
    val scanPriorityAssessment: ScanPriorityAssessment? = null,
    /** Distinguishes an authoritative Unassessed value from an old/cache-miss value. */
    val scanPriorityAssessmentKnown: Boolean = false,
    /** Desktop metadata revision that supplied the assessment. */
    val scanPriorityRevision: Long = 0L,
    /** Server timestamp paired with [scanPriorityRevision]; blank for legacy/unknown rows. */
    val scanPriorityUpdatedAt: String = "",
    /** Effective destination role projected by the inventory view. */
    val collectionType: CollectionType = CollectionType.CAPTURE,
    /** True while this physical book is set aside for digitization. */
    val scanMarked: Boolean = false,
    /** Capture collection the book was removed from when first marked. */
    val scanSourceCollectionId: String = "",
    /** Active scan-type collection, retained for audit after state changes. */
    val scanDestinationCollectionId: String = "",
    /** Independent server-monotonic scan-state revision. */
    val scanRevision: Long = 0L,
) {
    val digitizationCandidate: Boolean
        get() = digitizationCandidateClassification == true || scanMarked
}

internal data class RemoteCollectionBooksStore(
    /** collection id -> the books the cloud reported for it. */
    val byCollection: Map<String, List<RemoteCollectionBook>> = emptyMap(),
    /** False means an existing source was unreadable and must not be replaced. */
    val valid: Boolean = true,
    /** The account whose RLS scope produced these rows. */
    val owner: String = "",
)

internal data class RemoteCollectionRecordResult(
    /** Rows returned or conclusively invalidated by this complete snapshot. */
    val acknowledgedCaptureIds: Set<String>,
)

internal const val REMOTE_COLLECTION_BOOKS_FILE = "remote_collection_books.json"
internal const val REMOTE_COLLECTION_BOOKS_VERSION = 6

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
    ): Boolean = recordSnapshot(
        target = target,
        owner = owner,
        collectionId = collectionId,
        books = books,
        queriedCollectionIds = emptySet(),
        discardCollectionIds = discardCollectionIds,
        commitIf = commitIf,
    ) != null

    /**
     * Commit a complete query and suppress cached rows whose old effective box
     * was queried but which are now absent (for example A -> B -> C when this
     * device asks B). The synthetic tombstone keeps the cached revision, so the
     * next authoritative A/C row at the server's newer revision replaces it.
     */
    fun recordAuthoritative(
        ctx: Context,
        collectionId: String,
        books: List<RemoteCollectionBook>,
        queriedCollectionIds: Set<String>,
        owner: String = Prefs.userId(ctx),
        discardCollectionIds: Set<String> = emptySet(),
        commitIf: () -> Boolean = { true },
    ): RemoteCollectionRecordResult? = recordAuthoritative(
        File(ctx.filesDir, REMOTE_COLLECTION_BOOKS_FILE),
        owner,
        collectionId,
        books,
        queriedCollectionIds,
        discardCollectionIds,
        commitIf,
    )

    internal fun recordAuthoritative(
        target: File,
        owner: String,
        collectionId: String,
        books: List<RemoteCollectionBook>,
        queriedCollectionIds: Set<String>,
        discardCollectionIds: Set<String> = emptySet(),
        commitIf: () -> Boolean = { true },
    ): RemoteCollectionRecordResult? = recordSnapshot(
        target,
        owner,
        collectionId,
        books,
        queriedCollectionIds,
        discardCollectionIds,
        commitIf,
    )

    private fun recordSnapshot(
        target: File,
        owner: String,
        collectionId: String,
        books: List<RemoteCollectionBook>,
        queriedCollectionIds: Set<String>,
        discardCollectionIds: Set<String>,
        commitIf: () -> Boolean,
    ): RemoteCollectionRecordResult? = synchronized(this) {
        // Check only after entering the cache's write order. If an older fetch
        // was already writing when a refresh re-armed the tracker, every newer
        // accepted write queues behind it and therefore remains the final one.
        if (!commitIf()) return@synchronized null
        if (collectionId.isEmpty() || owner.isEmpty()) return@synchronized null
        val stored = readRemoteCollectionBooksStore(target, owner)
        if (!stored.valid) return@synchronized null
        val queried = queriedCollectionIds.asSequence()
            .map { it.trim().lowercase() }
            .filter(String::isNotEmpty)
            .toSet()
        val cachedPriorityById = stored.byCollection.values.asSequence()
            .flatten()
            .filter { it.scanPriorityAssessmentKnown }
            .groupBy { it.captureId.lowercase() }
            .mapValues { (_, copies) ->
                copies.reduce { selected, candidate ->
                    if (scanPrioritySupersedes(candidate, selected)) candidate else selected
                }
            }
        val booksWithRetainedPriority = books.map { incoming ->
            cachedPriorityById[incoming.captureId.lowercase()]?.let { cached ->
                retainNewerScanPriority(incoming, cached)
            } ?: incoming
        }
        val returned = booksWithRetainedPriority
            .mapTo(linkedSetOf()) { it.captureId.lowercase() }
        val stale = if (queried.isEmpty()) emptySet() else {
            stored.byCollection.values.asSequence()
                .flatten()
                .filter { book ->
                    book.collectionId.lowercase() in queried &&
                        book.captureId.lowercase() !in returned
                }
                .mapTo(linkedSetOf()) { it.captureId.lowercase() }
        }
        val staleRepresentatives = stored.byCollection.values.asSequence()
            .flatten()
            .filter { it.captureId.lowercase() in stale }
            .groupBy { it.captureId.lowercase() }
            .mapValues { (_, copies) ->
                copies.maxBy { it.membershipRevision }.copy(removed = true)
            }
        // A locally accepted B -> C move updates the cached row before the
        // follow-up B snapshot. That snapshot legitimately omits a capture whose
        // original box is A and whose new effective box is C; replacing B with
        // the empty response must not discard the only copy of the accepted C
        // state. Carry only rows already known to have left the queried closure.
        val departed = stored.byCollection[collectionId].orEmpty().filter { book ->
            queried.isNotEmpty() &&
                book.captureId.lowercase() !in returned &&
                book.captureId.lowercase() !in stale &&
                book.collectionId.lowercase() !in queried &&
                book.collectionId !in discardCollectionIds
        }
        val updated = LinkedHashMap(
            stored.byCollection.mapValues { (_, cachedBooks) ->
                cachedBooks.map { book ->
                    if (book.captureId.lowercase() in stale) book.copy(removed = true)
                    else book
                }
            },
        )
        discardCollectionIds.forEach(updated::remove)
        // Keep one synthetic representative even when the queried key is being
        // replaced with an empty response; otherwise a local durable summary
        // falls back to frozen provenance and resurrects the moved book.
        updated[collectionId] = booksWithRetainedPriority + staleRepresentatives.values + departed
        val result = RemoteCollectionRecordResult(returned + stale)
        if (target.isFile && stored.owner == owner &&
            updated == stored.byCollection
        ) return@synchronized result
        if (!saveRemoteCollectionBooksStore(
                target,
                stored.copy(byCollection = updated, owner = owner),
            )
        ) return@synchronized null
        result
    }

    /**
     * Apply an accepted RPC mutation to every cached occurrence immediately.
     *
     * A capture can be cached under both its immutable original box and a box
     * whose listing was fetched after a move. Updating every occurrence keeps a
     * stale outer cache key from reviving the old effective membership while the
     * next authoritative listing is still in flight.
     */
    fun applyMembershipMutation(
        ctx: Context,
        ids: Collection<String>,
        collectionId: String,
        removed: Boolean,
        owner: String = Prefs.userId(ctx),
    ): Boolean {
        val normalizedCollectionId = collectionId.trim().lowercase()
        val collection = Collections.allRecords(ctx)
            .firstOrNull { it.id.equals(normalizedCollectionId, ignoreCase = true) }
        return applyMembershipMutation(
            File(ctx.filesDir, REMOTE_COLLECTION_BOOKS_FILE),
            owner,
            ids,
            normalizedCollectionId,
            collection?.name.orEmpty(),
            removed,
            collection?.collectionType ?: CollectionType.CAPTURE,
        )
    }

    internal fun applyMembershipMutation(
        target: File,
        owner: String,
        ids: Collection<String>,
        collectionId: String,
        collectionName: String,
        removed: Boolean,
        collectionType: CollectionType = CollectionType.CAPTURE,
    ): Boolean = synchronized(this) {
        val normalizedOwner = owner.trim().lowercase()
        val normalizedCollectionId = collectionId.trim().lowercase()
        val normalizedIds = ids.asSequence()
            .map { it.trim().lowercase() }
            .distinct()
            .toSet()
        if (normalizedOwner.isEmpty() ||
            !SAFE_COLLECTION_FILTER_ID.matches(normalizedCollectionId) ||
            normalizedIds.any { !SAFE_CAPTURE_SYNC_ID.matches(it) }
        ) return@synchronized false
        if (normalizedIds.isEmpty()) return@synchronized true

        val stored = readRemoteCollectionBooksStore(target, normalizedOwner)
        if (!stored.valid) return@synchronized false
        var changed = false
        val updated = stored.byCollection.mapValues { (_, books) ->
            books.map { book ->
                if (book.captureId.lowercase() !in normalizedIds) return@map book
                val membershipChanged =
                    !book.collectionId.equals(normalizedCollectionId, ignoreCase = true) ||
                        book.removed != removed
                val scanMarked = collectionType == CollectionType.SCAN && !removed
                val scanSourceCollectionId = if (scanMarked) {
                    book.scanSourceCollectionId.ifEmpty {
                        book.collectionId.takeIf {
                            book.collectionType == CollectionType.CAPTURE
                        }.orEmpty().ifEmpty { book.originalCollectionId }
                    }
                } else {
                    book.scanSourceCollectionId
                }
                val scanDestinationCollectionId = if (scanMarked) {
                    normalizedCollectionId
                } else {
                    book.scanDestinationCollectionId
                }
                val scanStateChanged = book.collectionType != collectionType ||
                    book.scanMarked != scanMarked ||
                    book.scanSourceCollectionId != scanSourceCollectionId ||
                    book.scanDestinationCollectionId != scanDestinationCollectionId
                val next = book.copy(
                    collectionId = normalizedCollectionId,
                    removed = removed,
                    membershipRevision = when {
                        !membershipChanged -> book.membershipRevision
                        book.membershipRevision == Long.MAX_VALUE -> Long.MAX_VALUE
                        else -> book.membershipRevision + 1
                    },
                    collectionName = collectionName.ifEmpty { book.collectionName },
                    collectionType = collectionType,
                    scanMarked = scanMarked,
                    scanSourceCollectionId = scanSourceCollectionId,
                    scanDestinationCollectionId = scanDestinationCollectionId,
                    scanRevision = when {
                        !scanStateChanged -> book.scanRevision
                        book.scanRevision == Long.MAX_VALUE -> Long.MAX_VALUE
                        else -> book.scanRevision + 1
                    },
                )
                if (next != book) changed = true
                next
            }
        }
        if (!changed) return@synchronized true
        saveRemoteCollectionBooksStore(
            target,
            stored.copy(byCollection = updated, owner = normalizedOwner),
        )
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
            if ((parsed.originalCollectionId.lowercase() !in collectionIds &&
                    parsed.collectionId.lowercase() !in collectionIds) ||
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
 * Fill blank titles and scan classification from the desktop's curated projection.
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
    val metadata = desktop[book.captureId] ?: return@map book
    val projected = metadata.bibliography
    book.copy(
        title = book.title.ifEmpty { projected.title },
        author = book.author.ifEmpty { projected.author },
        year = book.year.ifEmpty { projected.year },
        digitizationCandidateClassification = metadata.digitizationCandidateClassification,
        scanPriorityRank = metadata.scanPriorityRank,
        scanPriorityAssessment = metadata.scanPriorityAssessment,
        scanPriorityAssessmentKnown = metadata.scanPriorityAssessmentKnown,
        scanPriorityRevision = metadata.revision
            .takeIf { metadata.scanPriorityAssessmentKnown }
            ?: 0L,
        scanPriorityUpdatedAt = metadata.updatedAt
            .takeIf { metadata.scanPriorityAssessmentKnown }
            .orEmpty(),
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
            deliveryTransport = "cloud",
            digitizationCandidateClassification = digitizationCandidateClassification,
            scanPriorityRank = scanPriorityRank,
            scanPriorityAssessment = scanPriorityAssessment,
            scanPriorityAssessmentKnown = scanPriorityAssessmentKnown,
            scanPriorityRevision = scanPriorityRevision,
            scanPriorityUpdatedAt = scanPriorityUpdatedAt,
            collectionType = collectionType,
            scanMarked = scanMarked,
            scanSourceCollectionId = scanSourceCollectionId,
            scanDestinationCollectionId = scanDestinationCollectionId,
            scanRevision = scanRevision,
        ),
        current = null,
        remote = true,
    )

/**
 * Collapse one box's local and remote rows onto the shared capture/entry id.
 *
 * A LOCAL ROW ALWAYS WINS: it can open its photos and carries live upload status,
 * where its cloud twin is a summary. Unknown legacy scan metadata on that local
 * row may be filled from its enriched cloud twin, but an explicit local true or
 * false classification is never overwritten. Callers pass local rows first,
 * but the preference is explicit here so ordering cannot silently invert it.
 */
internal fun mergeCollectionBookItems(
    items: List<CollectionInventoryItem>,
): List<CollectionInventoryItem> {
    val byId = LinkedHashMap<String, CollectionInventoryItem>()
    items.forEach { item ->
        val id = item.summary.entryId
        if (id.isEmpty()) return@forEach
        val existing = byId[id]
        if (existing == null) {
            byId[id] = item
        } else {
            val preferred = if (existing.remote && !item.remote) item else existing
            val fallback = if (preferred === item) existing else item
            byId[id] = fillUnknownScanMetadata(preferred, fallback)
        }
    }
    return byId.values.toList()
}

private fun fillUnknownScanMetadata(
    preferred: CollectionInventoryItem,
    fallback: CollectionInventoryItem,
): CollectionInventoryItem {
    val preferredCandidate = preferred.summary.digitizationCandidateClassification
    val fallbackCandidate = fallback.summary.digitizationCandidateClassification
    val candidate = preferredCandidate ?: fallbackCandidate
    val rank = when {
        candidate != true -> null
        preferredCandidate == true && preferred.summary.scanPriorityRank != null ->
            preferred.summary.scanPriorityRank
        fallbackCandidate == true -> fallback.summary.scanPriorityRank
        else -> null
    }
    val preferredScan = preferred.summary
    val fallbackScan = fallback.summary
    val useFallbackScan = fallbackScan.scanRevision > preferredScan.scanRevision ||
        (fallbackScan.scanRevision == preferredScan.scanRevision &&
            !preferredScan.scanMarked && fallbackScan.scanMarked)
    val selectedScan = if (useFallbackScan) fallbackScan else preferredScan
    val prioritySource = when {
        !preferredScan.scanPriorityAssessmentKnown -> fallbackScan
        fallbackScan.scanPriorityAssessmentKnown && scanPriorityVersionSupersedes(
            candidateRevision = fallbackScan.scanPriorityRevision,
            candidateUpdatedAt = fallbackScan.scanPriorityUpdatedAt,
            baselineRevision = preferredScan.scanPriorityRevision,
            baselineUpdatedAt = preferredScan.scanPriorityUpdatedAt,
        ) -> fallbackScan
        else -> preferredScan
    }
    if (candidate == preferredCandidate && rank == preferred.summary.scanPriorityRank &&
        selectedScan.collectionType == preferredScan.collectionType &&
        selectedScan.scanMarked == preferredScan.scanMarked &&
        selectedScan.scanSourceCollectionId == preferredScan.scanSourceCollectionId &&
        selectedScan.scanDestinationCollectionId == preferredScan.scanDestinationCollectionId &&
        selectedScan.scanRevision == preferredScan.scanRevision &&
        prioritySource.scanPriorityAssessment == preferredScan.scanPriorityAssessment &&
        prioritySource.scanPriorityAssessmentKnown == preferredScan.scanPriorityAssessmentKnown &&
        prioritySource.scanPriorityRevision == preferredScan.scanPriorityRevision &&
        prioritySource.scanPriorityUpdatedAt == preferredScan.scanPriorityUpdatedAt
    ) {
        return preferred
    }
    return preferred.copy(
        summary = preferred.summary.copy(
            digitizationCandidateClassification = candidate,
            scanPriorityRank = rank,
            scanPriorityAssessment = prioritySource.scanPriorityAssessment,
            scanPriorityAssessmentKnown = prioritySource.scanPriorityAssessmentKnown,
            scanPriorityRevision = prioritySource.scanPriorityRevision,
            scanPriorityUpdatedAt = prioritySource.scanPriorityUpdatedAt,
            collectionType = selectedScan.collectionType,
            scanMarked = selectedScan.scanMarked,
            scanSourceCollectionId = selectedScan.scanSourceCollectionId,
            scanDestinationCollectionId = selectedScan.scanDestinationCollectionId,
            scanRevision = selectedScan.scanRevision,
        ),
    )
}

/** Columns projected by the owner-scoped collection inventory view. */
internal const val CAPTURE_ORIGINAL_COLLECTION_ID_FIELD = "original_collection_id"
internal const val CAPTURE_COLLECTION_ID_FIELD = "collection_id"
internal const val CAPTURE_COLLECTION_NAME_FIELD = "collection_name"
internal const val CAPTURE_COLLECTION_REMOVED_FIELD = "removed"
internal const val CAPTURE_COLLECTION_REVISION_FIELD = "membership_revision"
internal const val CAPTURE_COLLECTION_PHOTO_COUNT_FIELD = "photo_count"
internal const val CAPTURE_COLLECTION_TYPE_FIELD = "collection_type"
internal const val CAPTURE_SCAN_MARKED_FIELD = "scan_marked"
internal const val CAPTURE_SCAN_SOURCE_COLLECTION_ID_FIELD = "scan_source_collection_id"
internal const val CAPTURE_SCAN_DESTINATION_COLLECTION_ID_FIELD =
    "scan_destination_collection_id"
internal const val CAPTURE_SCAN_REVISION_FIELD = "scan_revision"

/**
 * Read one projected `captures` row into a book summary.
 *
 * The row is FLAT, not nested: the inventory view keeps the immutable
 * [CAPTURE_ORIGINAL_COLLECTION_ID_FIELD] beside effective
 * [CAPTURE_COLLECTION_ID_FIELD], so neither the whole `meta` blob nor private
 * Storage object paths cross the wire.
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
    val originalCollectionId = captureRowString(row, CAPTURE_ORIGINAL_COLLECTION_ID_FIELD)
    val collectionId = captureRowString(row, CAPTURE_COLLECTION_ID_FIELD)
    if (collectionId.isEmpty()) return null
    val removed = captureRowBoolean(row, CAPTURE_COLLECTION_REMOVED_FIELD) ?: return null
    val membershipRevision = captureRowWholeNumber(
        row,
        CAPTURE_COLLECTION_REVISION_FIELD,
    )?.takeIf { it >= 0L } ?: return null
    val photoCount = captureRowWholeNumber(
        row,
        CAPTURE_COLLECTION_PHOTO_COUNT_FIELD,
    )?.takeIf { it in 0L..Int.MAX_VALUE.toLong() } ?: return null
    val collectionType = if (!row.has(CAPTURE_COLLECTION_TYPE_FIELD) ||
        row.isNull(CAPTURE_COLLECTION_TYPE_FIELD)
    ) {
        CollectionType.CAPTURE
    } else {
        CollectionType.fromWire(captureRowString(row, CAPTURE_COLLECTION_TYPE_FIELD))
            ?: return null
    }
    val scanMarked = if (!row.has(CAPTURE_SCAN_MARKED_FIELD) ||
        row.isNull(CAPTURE_SCAN_MARKED_FIELD)
    ) false else captureRowBoolean(row, CAPTURE_SCAN_MARKED_FIELD) ?: return null
    val scanSourceCollectionId = captureRowString(
        row,
        CAPTURE_SCAN_SOURCE_COLLECTION_ID_FIELD,
    ).lowercase()
    val scanDestinationCollectionId = captureRowString(
        row,
        CAPTURE_SCAN_DESTINATION_COLLECTION_ID_FIELD,
    ).lowercase()
    val scanRevision = if (!row.has(CAPTURE_SCAN_REVISION_FIELD) ||
        row.isNull(CAPTURE_SCAN_REVISION_FIELD)
    ) 0L else captureRowWholeNumber(row, CAPTURE_SCAN_REVISION_FIELD)
        ?.takeIf { it >= 0L } ?: return null
    if ((scanSourceCollectionId.isNotEmpty() &&
            !SAFE_COLLECTION_FILTER_ID.matches(scanSourceCollectionId)) ||
        (scanDestinationCollectionId.isNotEmpty() &&
            !SAFE_COLLECTION_FILTER_ID.matches(scanDestinationCollectionId)) ||
        (scanMarked &&
            (collectionType != CollectionType.SCAN || scanSourceCollectionId.isEmpty() ||
                scanDestinationCollectionId != collectionId.lowercase() ||
                scanSourceCollectionId == scanDestinationCollectionId))
    ) return null
    return RemoteCollectionBook(
        captureId = captureId,
        originalCollectionId = originalCollectionId,
        collectionId = collectionId,
        removed = removed,
        membershipRevision = membershipRevision,
        collectionName = normalizeCollectionField(
            captureRowString(row, CAPTURE_COLLECTION_NAME_FIELD),
        ),
        title = normalizeCollectionField(captureRowString(row, "title")),
        author = normalizeCollectionField(captureRowString(row, "author")),
        year = normalizeCollectionField(captureRowString(row, "year")),
        photoCount = photoCount.toInt(),
        createdAt = parseCaptureCreatedAt(captureRowString(row, "created_at")),
        collectionType = collectionType,
        scanMarked = scanMarked,
        scanSourceCollectionId = scanSourceCollectionId,
        scanDestinationCollectionId = scanDestinationCollectionId,
        scanRevision = scanRevision,
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

private fun captureRowBoolean(row: JSONObject, name: String): Boolean? =
    row.opt(name) as? Boolean

private fun captureRowWholeNumber(row: JSONObject, name: String): Long? {
    val raw = row.opt(name) as? Number ?: return null
    return try {
        BigDecimal(raw.toString()).longValueExact()
    } catch (_: ArithmeticException) {
        null
    } catch (_: NumberFormatException) {
        null
    }
}

/** A failed/legacy metadata lookup cannot erase a previously cached assessment;
 * an authoritative newer projection, including explicit Unassessed, does win. */
private fun retainNewerScanPriority(
    incoming: RemoteCollectionBook,
    cached: RemoteCollectionBook,
): RemoteCollectionBook {
    if (!cached.scanPriorityAssessmentKnown) return incoming
    if (incoming.scanPriorityAssessmentKnown && scanPrioritySupersedes(incoming, cached)) {
        return incoming
    }
    return incoming.copy(
        scanPriorityAssessment = cached.scanPriorityAssessment,
        scanPriorityAssessmentKnown = true,
        scanPriorityRevision = cached.scanPriorityRevision,
        scanPriorityUpdatedAt = cached.scanPriorityUpdatedAt,
    )
}

private fun scanPrioritySupersedes(
    candidate: RemoteCollectionBook,
    baseline: RemoteCollectionBook,
): Boolean = scanPriorityVersionSupersedes(
    candidateRevision = candidate.scanPriorityRevision,
    candidateUpdatedAt = candidate.scanPriorityUpdatedAt,
    baselineRevision = baseline.scanPriorityRevision,
    baselineUpdatedAt = baseline.scanPriorityUpdatedAt,
)

private fun scanPriorityVersionSupersedes(
    candidateRevision: Long,
    candidateUpdatedAt: String,
    baselineRevision: Long,
    baselineUpdatedAt: String,
): Boolean = metadataVersionSupersedes(
    candidateRevision = candidateRevision,
    candidateUpdatedAt = candidateUpdatedAt,
    baselineRevision = baselineRevision,
    baselineUpdatedAt = baselineUpdatedAt,
)

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
            books += remoteBookFromJson(collectionId, row, version.toInt())
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

private fun remoteBookToJson(book: RemoteCollectionBook): JSONObject {
    require(
        book.scanPriorityRank == null ||
            (book.digitizationCandidateClassification == true && book.scanPriorityRank in 1..5),
    ) { "scan priority rank requires an explicit candidate and must be in 1..5" }
    require(book.scanPriorityAssessment == null || book.scanPriorityAssessmentKnown) {
        "scan priority assessment requires an authoritative projection"
    }
    require(book.scanPriorityRevision >= 0L) { "scan priority revision must not be negative" }
    require(book.scanPriorityAssessmentKnown || book.scanPriorityRevision == 0L) {
        "unknown scan priority cannot carry a revision"
    }
    require(book.scanPriorityUpdatedAt.length <= 80) {
        "scan priority timestamp is too long"
    }
    require(book.scanPriorityAssessmentKnown || book.scanPriorityUpdatedAt.isEmpty()) {
        "unknown scan priority cannot carry a timestamp"
    }
    require(book.scanRevision >= 0L) { "scan revision must not be negative" }
    require(!book.scanMarked ||
        (book.collectionType == CollectionType.SCAN &&
            book.scanSourceCollectionId.isNotBlank() &&
            book.scanDestinationCollectionId == book.collectionId &&
            book.scanSourceCollectionId != book.scanDestinationCollectionId)
    ) { "invalid active scan state" }
    return JSONObject()
        .put("capture_id", book.captureId)
        .put("original_collection_id", book.originalCollectionId)
        .put("collection_id", book.collectionId)
        .put("removed", book.removed)
        .put("membership_revision", book.membershipRevision)
        .put("collection_name", book.collectionName)
        .put("title", book.title)
        .put("author", book.author)
        .put("year", book.year)
        .put("photo_count", book.photoCount)
        .put("created_at", book.createdAt)
        .put(
            "digitization_candidate",
            book.digitizationCandidateClassification ?: JSONObject.NULL,
        )
        .put("scan_priority_rank", book.scanPriorityRank ?: JSONObject.NULL)
        .put(
            "scan_priority",
            book.scanPriorityAssessment?.wireValue ?: JSONObject.NULL,
        )
        .put("scan_priority_known", book.scanPriorityAssessmentKnown)
        .put("scan_priority_revision", book.scanPriorityRevision)
        .put("scan_priority_updated_at", book.scanPriorityUpdatedAt)
        .put("collection_type", book.collectionType.wireValue)
        .put("scan_marked", book.scanMarked)
        .put("scan_source_collection_id", book.scanSourceCollectionId)
        .put("scan_destination_collection_id", book.scanDestinationCollectionId)
        .put("scan_revision", book.scanRevision)
}

private fun remoteBookFromJson(
    cachedCollectionId: String,
    row: JSONObject,
    version: Int,
): RemoteCollectionBook {
    val captureId = requiredRemoteString(row, "capture_id").trim()
    require(captureId.isNotEmpty()) { "invalid capture id" }
    val photoCount = requiredRemoteWholeNumber(row, "photo_count")
    require(photoCount in 0..Int.MAX_VALUE.toLong()) { "invalid photo count" }
    val createdAt = requiredRemoteWholeNumber(row, "created_at")
    require(createdAt >= 0L) { "invalid creation time" }
    val originalCollectionId = if (version == 1) cachedCollectionId
    else requiredRemoteString(row, "original_collection_id").trim()
    val collectionId = if (version == 1) cachedCollectionId
    else requiredRemoteString(row, "collection_id").trim()
    require(collectionId.isNotEmpty()) { "invalid effective collection id" }
    val removed = if (version == 1) false else requiredRemoteBoolean(row, "removed")
    val membershipRevision = if (version == 1) 0L
    else requiredRemoteWholeNumber(row, "membership_revision")
    require(membershipRevision >= 0L) { "invalid membership revision" }
    val digitizationCandidateClassification = if (version < 3) null else {
        optionalRemoteBoolean(row, "digitization_candidate")
    }
    val scanPriorityRank = if (version < 3) null else {
        optionalRemotePriorityRank(row, digitizationCandidateClassification)
    }
    val scanPriorityAssessmentKnown = if (version < 5) false else {
        optionalRemoteBoolean(row, "scan_priority_known")
            ?: throw IllegalArgumentException("scan_priority_known must be a boolean")
    }
    val scanPriorityAssessment = if (version < 5 || !scanPriorityAssessmentKnown) null else {
        optionalRemotePriorityAssessment(row)
    }
    val scanPriorityRevision = if (version < 5) 0L else {
        requiredRemoteWholeNumber(row, "scan_priority_revision")
    }
    require(scanPriorityRevision >= 0L) { "invalid scan priority revision" }
    require(scanPriorityAssessmentKnown || scanPriorityRevision == 0L) {
        "unknown scan priority cannot carry a revision"
    }
    val scanPriorityUpdatedAt = if (version < 6) "" else {
        requiredRemoteString(row, "scan_priority_updated_at")
    }
    require(scanPriorityUpdatedAt.length <= 80) { "invalid scan priority timestamp" }
    require(scanPriorityAssessmentKnown || scanPriorityUpdatedAt.isEmpty()) {
        "unknown scan priority cannot carry a timestamp"
    }
    val collectionType = if (version < 4) CollectionType.CAPTURE else {
        CollectionType.fromWire(requiredRemoteString(row, "collection_type"))
            ?: throw IllegalArgumentException("invalid collection type")
    }
    val scanMarked = if (version < 4) false else {
        requiredRemoteBoolean(row, "scan_marked")
    }
    val scanSourceCollectionId = if (version < 4) "" else {
        requiredRemoteString(row, "scan_source_collection_id")
    }
    val scanDestinationCollectionId = if (version < 4) "" else {
        requiredRemoteString(row, "scan_destination_collection_id")
    }
    val scanRevision = if (version < 4) 0L else {
        requiredRemoteWholeNumber(row, "scan_revision")
    }
    require(scanRevision >= 0L) { "invalid scan revision" }
    require(!scanMarked ||
        (collectionType == CollectionType.SCAN &&
            scanSourceCollectionId.isNotBlank() &&
            scanDestinationCollectionId == collectionId &&
            scanSourceCollectionId != scanDestinationCollectionId)
    ) { "invalid active scan state" }
    return RemoteCollectionBook(
        captureId = captureId,
        originalCollectionId = originalCollectionId,
        collectionId = collectionId,
        removed = removed,
        membershipRevision = membershipRevision,
        collectionName = requiredRemoteString(row, "collection_name"),
        title = requiredRemoteString(row, "title"),
        author = requiredRemoteString(row, "author"),
        year = requiredRemoteString(row, "year"),
        photoCount = photoCount.toInt(),
        createdAt = createdAt,
        digitizationCandidateClassification = digitizationCandidateClassification,
        scanPriorityRank = scanPriorityRank,
        scanPriorityAssessment = scanPriorityAssessment,
        scanPriorityAssessmentKnown = scanPriorityAssessmentKnown,
        scanPriorityRevision = scanPriorityRevision,
        scanPriorityUpdatedAt = scanPriorityUpdatedAt,
        collectionType = collectionType,
        scanMarked = scanMarked,
        scanSourceCollectionId = scanSourceCollectionId,
        scanDestinationCollectionId = scanDestinationCollectionId,
        scanRevision = scanRevision,
    )
}

private fun optionalRemoteBoolean(source: JSONObject, name: String): Boolean? =
    when (val raw = source.opt(name)) {
        null, JSONObject.NULL -> null
        is Boolean -> raw
        else -> throw IllegalArgumentException("$name must be a boolean or null")
    }

private fun optionalRemotePriorityAssessment(source: JSONObject): ScanPriorityAssessment? {
    val raw = source.opt("scan_priority")
    if (raw == null || raw === JSONObject.NULL) return null
    val value = raw as? String
        ?: throw IllegalArgumentException("scan_priority must be a string or null")
    return ScanPriorityAssessment.parse(value)
        ?: throw IllegalArgumentException("invalid scan_priority")
}

private fun optionalRemotePriorityRank(
    source: JSONObject,
    candidate: Boolean?,
): Int? {
    val name = if (source.has("scan_priority_rank")) {
        "scan_priority_rank"
    } else {
        "scan_priority"
    }
    val raw = source.opt(name)
    if (raw == null || raw === JSONObject.NULL) return null
    // `scan_priority` now belongs to the textual assessment enum. Only an
    // actual legacy JSON integer can be recovered as an ordinal rank.
    if (name == "scan_priority" && raw is String) return null
    require(candidate == true) { "$name requires an explicit candidate" }
    val value = when (raw) {
        is Byte, is Short, is Int, is Long -> (raw as Number).toLong()
        else -> throw IllegalArgumentException("$name must be an integer")
    }
    require(value in 1L..5L) { "$name must be in 1..5" }
    return value.toInt()
}

private fun requiredRemoteString(source: JSONObject, name: String): String =
    source.opt(name) as? String
        ?: throw IllegalArgumentException("$name must be a string")

private fun requiredRemoteBoolean(source: JSONObject, name: String): Boolean =
    source.opt(name) as? Boolean
        ?: throw IllegalArgumentException("$name must be a boolean")

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
