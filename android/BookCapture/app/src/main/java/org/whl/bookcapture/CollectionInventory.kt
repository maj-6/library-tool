package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.math.BigDecimal

/**
 * Small, photo-free records that survive removal of old sent-entry folders.
 *
 * The collection fields are capture-time snapshots. [collectionId] remains the
 * durable link when a collection is renamed, while [collectionName] preserves
 * what was printed/selected when the book was captured.
 */
internal data class CollectionInventorySummary(
    val entryId: String,
    val collectionId: String,
    val collectionName: String,
    val title: String,
    val author: String,
    val year: String,
    val photoCount: Int,
    val createdAt: Long,
)

/** One Inspect row. Only a still-current entry can carry live photo access. */
internal data class CollectionInventoryItem(
    val summary: CollectionInventorySummary,
    val current: Entries.Entry?,
)

internal data class CollectionInventoryStore(
    val summaries: Map<String, CollectionInventorySummary> = emptyMap(),
    /** False means an existing source was unreadable and must not be replaced. */
    val valid: Boolean = true,
    internal val sourceVersion: Int = COLLECTION_INVENTORY_VERSION,
)

internal const val COLLECTION_INVENTORY_FILE = "collection_inventory.json"
internal const val COLLECTION_INVENTORY_VERSION = 1

internal object CollectionInventory {

    fun read(ctx: Context): CollectionInventoryStore =
        readCollectionInventoryStore(File(ctx.filesDir, COLLECTION_INVENTORY_FILE))

    /**
     * Capture every uploaded entry before Entries removes any browsing media.
     * A failed/unsafe persistence attempt returns false so pruning can abort.
     */
    fun recordFinalized(ctx: Context, entries: Collection<Entries.Entry>): Boolean =
        recordFinalized(File(ctx.filesDir, COLLECTION_INVENTORY_FILE), entries)

    internal fun recordFinalized(
        target: File,
        entries: Collection<Entries.Entry>,
    ): Boolean = synchronized(this) {
        val stored = readCollectionInventoryStore(target)
        if (!stored.valid) return@synchronized false

        val updated = LinkedHashMap(stored.summaries)
        entries.asSequence()
            .filter { it.uploaded }
            .forEach { entry ->
                val summary = collectionInventorySummary(entry)
                updated[summary.entryId] = summary
            }

        if (target.isFile && stored.sourceVersion == COLLECTION_INVENTORY_VERSION &&
            updated == stored.summaries
        ) return@synchronized true

        saveCollectionInventoryStore(target, stored.copy(summaries = updated))
    }

    /** Current queue/sent data replaces the durable snapshot with the same id. */
    fun items(ctx: Context): List<CollectionInventoryItem> =
        mergeCollectionInventory(read(ctx).summaries.values, Entries.recent(ctx))
}

internal fun collectionInventorySummary(entry: Entries.Entry): CollectionInventorySummary =
    CollectionInventorySummary(
        entryId = entry.id,
        collectionId = entry.provenance?.collectionId.orEmpty(),
        collectionName = entry.provenance?.collectionName.orEmpty(),
        title = entry.title,
        author = entry.author,
        year = entry.year,
        photoCount = entry.photoCount,
        createdAt = entry.createdAt,
    )

/**
 * Pure union used by Inspect presentation code. Durable duplicates collapse by
 * id, and a current Entry always wins because it is applied last.
 */
internal fun mergeCollectionInventory(
    durable: Collection<CollectionInventorySummary>,
    current: List<Entries.Entry>,
): List<CollectionInventoryItem> {
    val byId = LinkedHashMap<String, CollectionInventoryItem>()
    durable.forEach { summary ->
        if (summary.entryId.isNotEmpty()) {
            byId.putIfAbsent(summary.entryId, CollectionInventoryItem(summary, null))
        }
    }
    current.forEach { entry ->
        byId[entry.id] = CollectionInventoryItem(collectionInventorySummary(entry), entry)
    }
    return byId.values.sortedWith(
        compareByDescending<CollectionInventoryItem> { it.summary.createdAt }
            .thenBy { it.summary.entryId },
    )
}

internal fun collectionInventoryStoreToJson(store: CollectionInventoryStore): String {
    val entries = JSONObject()
    store.summaries.toSortedMap().forEach { (entryId, summary) ->
        entries.put(entryId, summaryToJson(summary))
    }
    return JSONObject()
        .put("version", COLLECTION_INVENTORY_VERSION)
        .put("entries", entries)
        .toString()
}

/**
 * Version 0 was the pre-keyed prototype shape (an array with an `id` field).
 * Reading it in memory is safe; the next successful record writes version 1.
 */
internal fun collectionInventoryStoreFromJson(text: String): CollectionInventoryStore = try {
    val root = JSONObject(text)
    val version = requiredWholeNumber(root, "version")
    require(version in 0L..COLLECTION_INVENTORY_VERSION.toLong()) {
        "unsupported collection inventory version"
    }

    val summaries = LinkedHashMap<String, CollectionInventorySummary>()
    if (version == 0L) {
        val entries = root.optJSONArray("entries")
            ?: throw IllegalArgumentException("entries must be an array")
        for (index in 0 until entries.length()) {
            val row = entries.optJSONObject(index)
                ?: throw IllegalArgumentException("entry must be an object")
            val entryId = requiredString(row, "id").trim()
            require(entryId.isNotEmpty() && entryId !in summaries) { "invalid entry id" }
            summaries[entryId] = summaryFromJson(entryId, row)
        }
    } else {
        val entries = root.optJSONObject("entries")
            ?: throw IllegalArgumentException("entries must be an object")
        entries.keys().asSequence().toList().sorted().forEach { entryId ->
            require(entryId.isNotEmpty()) { "invalid entry id" }
            val row = entries.optJSONObject(entryId)
                ?: throw IllegalArgumentException("entry must be an object")
            summaries[entryId] = summaryFromJson(entryId, row)
        }
    }
    CollectionInventoryStore(summaries, sourceVersion = version.toInt())
} catch (_: Exception) {
    CollectionInventoryStore(valid = false)
}

internal fun readCollectionInventoryStore(target: File): CollectionInventoryStore {
    if (!target.exists()) return CollectionInventoryStore()
    if (!target.isFile) return CollectionInventoryStore(valid = false)
    return try {
        collectionInventoryStoreFromJson(target.readText())
    } catch (_: Exception) {
        CollectionInventoryStore(valid = false)
    }
}

internal fun saveCollectionInventoryStore(
    target: File,
    store: CollectionInventoryStore,
): Boolean {
    if (!store.valid) return false
    return try {
        target.parentFile?.mkdirs()
        Entries.atomicWrite(target, collectionInventoryStoreToJson(store))
        true
    } catch (_: Exception) {
        false
    }
}

private fun summaryToJson(summary: CollectionInventorySummary): JSONObject =
    JSONObject()
        .put("collection_id", summary.collectionId)
        .put("collection_name", summary.collectionName)
        .put("title", summary.title)
        .put("author", summary.author)
        .put("year", summary.year)
        .put("photo_count", summary.photoCount)
        .put("created_at", summary.createdAt)

private fun summaryFromJson(
    entryId: String,
    row: JSONObject,
): CollectionInventorySummary {
    val photoCount = requiredWholeNumber(row, "photo_count")
    require(photoCount in 0..Int.MAX_VALUE.toLong()) { "invalid photo count" }
    val createdAt = requiredWholeNumber(row, "created_at")
    require(createdAt >= 0L) { "invalid creation time" }
    return CollectionInventorySummary(
        entryId = entryId,
        collectionId = requiredString(row, "collection_id"),
        collectionName = requiredString(row, "collection_name"),
        title = requiredString(row, "title"),
        author = requiredString(row, "author"),
        year = requiredString(row, "year"),
        photoCount = photoCount.toInt(),
        createdAt = createdAt,
    )
}

private fun requiredString(source: JSONObject, name: String): String =
    source.opt(name) as? String
        ?: throw IllegalArgumentException("$name must be a string")

private fun requiredWholeNumber(source: JSONObject, name: String): Long {
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
