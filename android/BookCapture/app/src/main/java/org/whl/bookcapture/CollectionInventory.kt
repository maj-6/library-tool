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
    /** How a finalized capture left this device: "cloud", "lan", or empty. */
    val deliveryTransport: String = "",
    /** Verified cloud account at delivery; empty means LAN or legacy-unknown. */
    val cloudOwnerId: String = "",
    /** `null` means the retained row predates or lacks a curator classification. */
    val digitizationCandidateClassification: Boolean? = null,
    /** Present only for an explicitly classified candidate; valid values are 1..5. */
    val scanPriorityRank: Int? = null,
    /** Canonical catalog scan assessment; null can mean Unassessed or legacy-unknown. */
    val scanPriorityAssessment: ScanPriorityAssessment? = null,
    /** Distinguishes an authoritative Unassessed value from an old cache row. */
    val scanPriorityAssessmentKnown: Boolean = false,
    /** Desktop metadata revision that supplied the assessment. */
    val scanPriorityRevision: Long = 0L,
    /** Server timestamp paired with [scanPriorityRevision]; blank for legacy/unknown rows. */
    val scanPriorityUpdatedAt: String = "",
    /** Effective collection role. Legacy rows are ordinary capture records. */
    val collectionType: CollectionType = CollectionType.CAPTURE,
    /** Active physical set-aside for digitization, independent of OCR/photo capture. */
    val scanMarked: Boolean = false,
    /** Historical capture collection retained when [scanMarked] moves the book. */
    val scanSourceCollectionId: String = "",
    /** Active scan-type destination, retained as audit data after later changes. */
    val scanDestinationCollectionId: String = "",
    /** Server-monotonic revision of scan state; zero means legacy/unknown. */
    val scanRevision: Long = 0L,
) {
    val digitizationCandidate: Boolean
        get() = digitizationCandidateClassification == true || scanMarked
}

/** One Inspect row. Only a still-current entry can carry live photo access. */
internal data class CollectionInventoryItem(
    val summary: CollectionInventorySummary,
    val current: Entries.Entry?,
    /**
     * True when this row came from the cloud rather than this handset, so the UI
     * can say "not on this phone" instead of claiming local media was cleared.
     * Both cases have a null [current]; only the reason differs.
     */
    val remote: Boolean = false,
)

internal data class CollectionInventoryStore(
    val summaries: Map<String, CollectionInventorySummary> = emptyMap(),
    /** False means an existing source was unreadable and must not be replaced. */
    val valid: Boolean = true,
    internal val sourceVersion: Int = COLLECTION_INVENTORY_VERSION,
)

internal const val COLLECTION_INVENTORY_FILE = "collection_inventory.json"
internal const val COLLECTION_INVENTORY_VERSION = 7

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

internal fun collectionInventorySummary(entry: Entries.Entry): CollectionInventorySummary {
    val desktop = entry.desktopBook
    val scanMark = CaptureScanMarkStore.read(entry.dir)
    return CollectionInventorySummary(
        entryId = entry.id,
        collectionId = entry.provenance?.collectionId.orEmpty(),
        collectionName = entry.provenance?.collectionName.orEmpty(),
        title = entry.title,
        author = entry.author,
        year = entry.year,
        photoCount = entry.photoCount,
        createdAt = entry.createdAt,
        deliveryTransport = entry.deliveryTransport,
        cloudOwnerId = entry.cloudOwnerId.trim().lowercase(),
        digitizationCandidateClassification = desktop?.digitizationCandidateClassification,
        scanPriorityRank = desktop?.scanPriorityRank,
        scanPriorityAssessment = desktop?.scanPriorityAssessment,
        scanPriorityAssessmentKnown = desktop?.scanPriorityAssessmentKnown == true,
        scanPriorityRevision = if (desktop?.scanPriorityAssessmentKnown == true) {
            desktop.revision
        } else {
            0L
        },
        scanPriorityUpdatedAt = if (desktop?.scanPriorityAssessmentKnown == true) {
            desktop.updatedAt
        } else {
            ""
        },
        collectionType = if (scanMark == null) {
            CollectionType.CAPTURE
        } else {
            CollectionType.SCAN
        },
        scanMarked = scanMark != null,
        scanSourceCollectionId = scanMark?.sourceCollectionId.orEmpty(),
        scanDestinationCollectionId = scanMark?.scanCollectionId.orEmpty(),
        scanRevision = if (scanMark == null) 0L else 1L,
    )
}

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
 * Reading it in memory is safe; the next successful record writes the current
 * version. Version 2 adds delivery transport. Version 3 adds the verified
 * cloud owner so a photo-free row can never be submitted under another
 * account merely because that account is signed in later. Version 4 retains
 * the desktop's tri-state scan-candidate classification and bounded priority
 * rank. Current writers use `scan_priority_rank`; readers retain a numeric-only
 * fallback for older rows written with the former `scan_priority` key.
 * Version 5 retains collection role plus the independent physical scan mark.
 * Version 6 retains the canonical textual scan assessment, its authoritative
 * presence flag, and the desktop revision that supplied it.
 * Version 7 adds the paired server timestamp so a recreated revision ledger
 * can supersede an older, numerically higher revision.
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
            summaries[entryId] = summaryFromJson(entryId, row, version.toInt())
        }
    } else {
        val entries = root.optJSONObject("entries")
            ?: throw IllegalArgumentException("entries must be an object")
        entries.keys().asSequence().toList().sorted().forEach { entryId ->
            require(entryId.isNotEmpty()) { "invalid entry id" }
            val row = entries.optJSONObject(entryId)
                ?: throw IllegalArgumentException("entry must be an object")
            summaries[entryId] = summaryFromJson(entryId, row, version.toInt())
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

private fun summaryToJson(summary: CollectionInventorySummary): JSONObject {
    require(summary.deliveryTransport in setOf("", "cloud", "lan")) {
        "invalid delivery transport"
    }
    val cloudOwnerId = normalizedInventoryCloudOwner(summary.cloudOwnerId)
    require(summary.deliveryTransport == "cloud" || cloudOwnerId.isEmpty()) {
        "non-cloud delivery cannot have a cloud owner"
    }
    require(
        summary.scanPriorityRank == null ||
            (summary.digitizationCandidateClassification == true &&
                summary.scanPriorityRank in 1..5),
    ) { "scan priority rank requires an explicit candidate and must be in 1..5" }
    require(summary.scanPriorityAssessment == null || summary.scanPriorityAssessmentKnown) {
        "scan priority assessment requires an authoritative projection"
    }
    require(summary.scanPriorityRevision >= 0L) { "scan priority revision must not be negative" }
    require(summary.scanPriorityAssessmentKnown || summary.scanPriorityRevision == 0L) {
        "unknown scan priority cannot carry a revision"
    }
    require(summary.scanPriorityUpdatedAt.length <= 80) {
        "scan priority timestamp is too long"
    }
    require(summary.scanPriorityAssessmentKnown || summary.scanPriorityUpdatedAt.isEmpty()) {
        "unknown scan priority cannot carry a timestamp"
    }
    require(summary.scanRevision >= 0L) { "scan revision must not be negative" }
    require(!summary.scanMarked ||
        (summary.collectionType == CollectionType.SCAN &&
            summary.scanSourceCollectionId.isNotBlank() &&
            summary.scanDestinationCollectionId.isNotBlank())
    ) { "an active scan mark requires scan collection provenance" }
    return JSONObject()
        .put("collection_id", summary.collectionId)
        .put("collection_name", summary.collectionName)
        .put("title", summary.title)
        .put("author", summary.author)
        .put("year", summary.year)
        .put("photo_count", summary.photoCount)
        .put("created_at", summary.createdAt)
        .put("delivery_transport", summary.deliveryTransport)
        .put("cloud_owner_id", cloudOwnerId)
        .put(
            "digitization_candidate",
            summary.digitizationCandidateClassification ?: JSONObject.NULL,
        )
        .put("scan_priority_rank", summary.scanPriorityRank ?: JSONObject.NULL)
        .put(
            "scan_priority",
            summary.scanPriorityAssessment?.wireValue ?: JSONObject.NULL,
        )
        .put("scan_priority_known", summary.scanPriorityAssessmentKnown)
        .put("scan_priority_revision", summary.scanPriorityRevision)
        .put("scan_priority_updated_at", summary.scanPriorityUpdatedAt)
        .put("collection_type", summary.collectionType.wireValue)
        .put("scan_marked", summary.scanMarked)
        .put("scan_source_collection_id", summary.scanSourceCollectionId)
        .put("scan_destination_collection_id", summary.scanDestinationCollectionId)
        .put("scan_revision", summary.scanRevision)
}

private fun summaryFromJson(
    entryId: String,
    row: JSONObject,
    version: Int,
): CollectionInventorySummary {
    val photoCount = requiredWholeNumber(row, "photo_count")
    require(photoCount in 0..Int.MAX_VALUE.toLong()) { "invalid photo count" }
    val createdAt = requiredWholeNumber(row, "created_at")
    require(createdAt >= 0L) { "invalid creation time" }
    val deliveryTransport = if (version < 2) "" else requiredString(row, "delivery_transport")
    require(deliveryTransport in setOf("", "cloud", "lan")) {
        "invalid delivery transport"
    }
    val rawCloudOwnerId = if (version < 3) "" else requiredString(row, "cloud_owner_id")
    require(rawCloudOwnerId == rawCloudOwnerId.trim()) {
        "invalid cloud owner"
    }
    val cloudOwnerId = rawCloudOwnerId.lowercase()
    require(cloudOwnerId.isEmpty() || SAFE_CAPTURE_SYNC_ID.matches(cloudOwnerId)) {
        "invalid cloud owner"
    }
    require(deliveryTransport == "cloud" || cloudOwnerId.isEmpty()) {
        "non-cloud delivery cannot have a cloud owner"
    }
    val digitizationCandidateClassification = if (version < 4) null else {
        optionalInventoryBoolean(row, "digitization_candidate")
    }
    val scanPriorityRank = if (version < 4) null else {
        optionalInventoryPriorityRank(row, digitizationCandidateClassification)
    }
    val scanPriorityAssessmentKnown = if (version < 6) false else {
        optionalInventoryBoolean(row, "scan_priority_known")
            ?: throw IllegalArgumentException("scan_priority_known must be a boolean")
    }
    val scanPriorityAssessment = if (version < 6 || !scanPriorityAssessmentKnown) null else {
        optionalInventoryPriorityAssessment(row)
    }
    val scanPriorityRevision = if (version < 6) 0L else {
        requiredWholeNumber(row, "scan_priority_revision")
    }
    require(scanPriorityRevision >= 0L) { "invalid scan priority revision" }
    require(scanPriorityAssessmentKnown || scanPriorityRevision == 0L) {
        "unknown scan priority cannot carry a revision"
    }
    val scanPriorityUpdatedAt = if (version < 7) "" else {
        requiredString(row, "scan_priority_updated_at")
    }
    require(scanPriorityUpdatedAt.length <= 80) { "invalid scan priority timestamp" }
    require(scanPriorityAssessmentKnown || scanPriorityUpdatedAt.isEmpty()) {
        "unknown scan priority cannot carry a timestamp"
    }
    val collectionType = if (version < 5) CollectionType.CAPTURE else {
        CollectionType.fromWire(requiredString(row, "collection_type"))
            ?: throw IllegalArgumentException("invalid collection type")
    }
    val scanMarked = if (version < 5) false else {
        optionalInventoryBoolean(row, "scan_marked")
            ?: throw IllegalArgumentException("scan_marked must be a boolean")
    }
    val scanSourceCollectionId = if (version < 5) "" else {
        requiredString(row, "scan_source_collection_id")
    }
    val scanDestinationCollectionId = if (version < 5) "" else {
        requiredString(row, "scan_destination_collection_id")
    }
    val scanRevision = if (version < 5) 0L else {
        requiredWholeNumber(row, "scan_revision")
    }
    require(scanRevision >= 0L) { "invalid scan revision" }
    require(!scanMarked ||
        (collectionType == CollectionType.SCAN &&
            scanSourceCollectionId.isNotBlank() &&
            scanDestinationCollectionId.isNotBlank())
    ) { "invalid active scan state" }
    return CollectionInventorySummary(
        entryId = entryId,
        collectionId = requiredString(row, "collection_id"),
        collectionName = requiredString(row, "collection_name"),
        title = requiredString(row, "title"),
        author = requiredString(row, "author"),
        year = requiredString(row, "year"),
        photoCount = photoCount.toInt(),
        createdAt = createdAt,
        deliveryTransport = deliveryTransport,
        cloudOwnerId = cloudOwnerId,
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

private fun optionalInventoryBoolean(source: JSONObject, name: String): Boolean? =
    when (val raw = source.opt(name)) {
        null, JSONObject.NULL -> null
        is Boolean -> raw
        else -> throw IllegalArgumentException("$name must be a boolean or null")
    }

private fun optionalInventoryPriorityAssessment(source: JSONObject): ScanPriorityAssessment? {
    val raw = source.opt("scan_priority")
    if (raw == null || raw === JSONObject.NULL) return null
    val value = raw as? String
        ?: throw IllegalArgumentException("scan_priority must be a string or null")
    return ScanPriorityAssessment.parse(value)
        ?: throw IllegalArgumentException("invalid scan_priority")
}

private fun optionalInventoryPriorityRank(
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

private fun normalizedInventoryCloudOwner(value: String): String {
    require(value == value.trim()) { "invalid cloud owner" }
    val normalized = value.lowercase()
    require(normalized.isEmpty() || SAFE_CAPTURE_SYNC_ID.matches(normalized)) {
        "invalid cloud owner"
    }
    return normalized
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
