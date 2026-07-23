package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.OffsetDateTime

internal const val CAPTURE_METADATA_MAX_BYTES = 256 * 1024
internal const val CAPTURE_METADATA_BATCH_SIZE = 40
private const val CAPTURE_METADATA_WRAPPER_OVERHEAD = 8 * 1024
internal val SAFE_CAPTURE_SYNC_ID = Regex(
    "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" +
        "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
)

enum class DesktopAvailabilityState { AVAILABLE, UNAVAILABLE, UNKNOWN }

data class DesktopAvailability(
    val state: DesktopAvailabilityState = DesktopAvailabilityState.UNKNOWN,
    val url: String = "",
    val identifier: String = "",
    val detail: String = "",
) {
    val available: Boolean? get() = when (state) {
        DesktopAvailabilityState.AVAILABLE -> true
        DesktopAvailabilityState.UNAVAILABLE -> false
        DesktopAvailabilityState.UNKNOWN -> null
    }
}

data class DesktopCopyrightMetadata(
    val status: String = "",
    val registrationRecords: List<JSONObject> = emptyList(),
    val renewalRecords: List<JSONObject> = emptyList(),
)

/** A bounded, desktop-authored projection for one registered phone capture.
 * [dataCopy] retains unknown future keys without exposing mutable store state. */
class DesktopBookMetadata internal constructor(
    val captureId: String,
    internal val ownerId: String,
    val bookId: String,
    val revision: Long,
    val updatedAt: String,
    private val dataText: String,
) {
    val registered: Boolean get() = bookId.isNotEmpty()

    fun dataCopy(): JSONObject = JSONObject(dataText)

    val copyright: DesktopCopyrightMetadata
        get() = parseDesktopCopyright(dataCopy().optJSONObject("copyright"))

    val whl: DesktopAvailability
        get() = parseDesktopAvailability(
            dataCopy().optJSONObject("availability")?.optJSONObject("whl"),
        )

    val internetArchive: DesktopAvailability
        get() {
            val availability = dataCopy().optJSONObject("availability")
            return parseDesktopAvailability(
                availability?.optJSONObject("internet_archive")
                    ?: availability?.optJSONObject("ia"),
            )
        }

    val scanStatus: String get() = dataCopy().strictString("scan_status", 80).orEmpty()

    val remarks: List<String> get() = parseDesktopRemarks(dataCopy().opt("remarks"))

    internal fun semanticallyEquals(other: DesktopBookMetadata): Boolean =
        captureId == other.captureId && ownerId == other.ownerId &&
            bookId == other.bookId && revision == other.revision && dataText == other.dataText

    internal fun toSidecarJson(): JSONObject = JSONObject()
        .put("schema", DESKTOP_BOOK_SCHEMA)
        .put("version", 1)
        .put("capture_id", captureId)
        .put("owner_id", ownerId)
        .put("book_id", bookId)
        .put("revision", revision)
        .put("updated_at", updatedAt)
        .put("data", JSONObject(dataText))
}

data class CaptureReviewMetadata(
    val captureId: String,
    val revision: Long = 0,
    val updatedAt: String = "",
    val needsAttention: Boolean = false,
    val attentionReason: String = "",
    val needsReview: Boolean = false,
    val reviewId: String = "",
    val status: String = "",
)

internal data class CaptureReviewStore(
    val current: CaptureReviewMetadata,
    val shadow: CaptureReviewMetadata? = null,
    val dirty: Boolean = false,
)

internal data class CaptureReviewCloudWrite(
    val state: CaptureReviewMetadata,
    val expectedCloudRevision: Long?,
)

internal data class CaptureReviewMerge(
    val store: CaptureReviewStore,
    val write: CaptureReviewCloudWrite? = null,
    val conflict: String? = null,
)

internal enum class DesktopMetadataApplyResult { APPLIED, UNCHANGED, STALE, CONFLICT }

internal sealed interface CaptureReviewFileState {
    data object Missing : CaptureReviewFileState
    data class Valid(val store: CaptureReviewStore) : CaptureReviewFileState
    data object Corrupt : CaptureReviewFileState
}

private const val DESKTOP_BOOK_FILE = "desktop_book.json"
private const val CAPTURE_REVIEW_FILE = "capture_review.json"
private const val DESKTOP_BOOK_SCHEMA = "org.whl.bookcapture.desktop-book"
private const val CAPTURE_REVIEW_SCHEMA = "org.whl.bookcapture.capture-review"

internal object CaptureMetadataStore {
    private val fileLock = Any()

    fun readDesktopBook(dir: File): DesktopBookMetadata? = synchronized(fileLock) {
        readDesktopBookFile(File(dir, DESKTOP_BOOK_FILE))
    }

    fun applyDesktopBook(
        dir: File,
        incoming: DesktopBookMetadata,
    ): DesktopMetadataApplyResult = synchronized(fileLock) {
        val file = File(dir, DESKTOP_BOOK_FILE)
        val local = readDesktopBookFile(file)
        when {
            incoming.captureId != dir.name -> DesktopMetadataApplyResult.CONFLICT
            local == null -> {
                Entries.atomicWrite(file, incoming.toSidecarJson().toString())
                DesktopMetadataApplyResult.APPLIED
            }
            incoming.ownerId != local.ownerId ||
                syncTimestampAfter(incoming.updatedAt, local.updatedAt) -> {
                // A LAN desktop rotates ownerId when its revision ledger is
                // lost. A newer server timestamp also lets a compact/pruned or
                // restored ledger recover without stranding revision 1 behind
                // a stale higher local revision.
                Entries.atomicWrite(file, incoming.toSidecarJson().toString())
                DesktopMetadataApplyResult.APPLIED
            }
            incoming.revision < local.revision -> DesktopMetadataApplyResult.STALE
            incoming.revision == local.revision && local.semanticallyEquals(incoming) ->
                DesktopMetadataApplyResult.UNCHANGED
            incoming.revision == local.revision &&
                syncTimestampBefore(incoming.updatedAt, local.updatedAt) ->
                DesktopMetadataApplyResult.STALE
            incoming.revision == local.revision -> DesktopMetadataApplyResult.CONFLICT
            else -> {
                Entries.atomicWrite(file, incoming.toSidecarJson().toString())
                DesktopMetadataApplyResult.APPLIED
            }
        }
    }

    fun reviewState(dir: File): CaptureReviewFileState = synchronized(fileLock) {
        readReviewFile(File(dir, CAPTURE_REVIEW_FILE), dir.name)
    }

    fun readReview(dir: File): CaptureReviewStore? = when (val state = reviewState(dir)) {
            is CaptureReviewFileState.Valid -> state.store
            CaptureReviewFileState.Missing, CaptureReviewFileState.Corrupt -> null
    }

    /** Corruption is pending work too: an explicit sync must surface it rather
     * than silently declaring the batch complete and pruning the only copy. */
    fun hasPendingReviewSync(dir: File): Boolean = when (val state = reviewState(dir)) {
        is CaptureReviewFileState.Valid -> state.store.dirty
        CaptureReviewFileState.Corrupt -> true
        CaptureReviewFileState.Missing -> false
    }

    /** Atomically read, transform, and persist the review sidecar.
     *
     * Review edits, cloud merges, and HTTP acknowledgements are all
     * read-modify-write operations. Keeping the transform under the same
     * monitor as the atomic file replacement prevents one of those operations
     * from silently overwriting another in this process. Callers that can race
     * a queue/sent directory move must additionally hold [EntryOperationLocks]
     * for [File.getName]. A `null` result means there is no state to persist.
     */
    fun mutateReview(
        dir: File,
        transform: (CaptureReviewStore?) -> CaptureReviewStore?,
    ): Boolean = synchronized(fileLock) {
        val file = File(dir, CAPTURE_REVIEW_FILE)
        val current = when (val state = readReviewFile(file, dir.name)) {
            CaptureReviewFileState.Missing -> null
            is CaptureReviewFileState.Valid -> state.store
            CaptureReviewFileState.Corrupt -> return@synchronized false
        }
        val next = transform(current) ?: return@synchronized true
        try {
            Entries.atomicWrite(
                file,
                reviewStoreToJson(next).toString(),
            )
            true
        } catch (_: Exception) {
            false
        }
    }

    /** Delete a sent browsing copy only when it has no outbound local state.
     *
     * Keep the review check and deletion under the same monitor used by
     * [mutateReview]. Otherwise a successful UI edit could land between a dirty
     * check and `deleteRecursively`, report success, and then be erased by
     * retention pruning. A malformed review sidecar is retained conservatively:
     * it may be the only copy of an offline edit. Future per-entry outbound
     * sidecars must be included in this gate before they are introduced.
     */
    fun deleteIfNoUnsyncedLocalMutation(dir: File): Boolean = synchronized(fileLock) {
        if (!dir.exists()) return@synchronized true
        val reviewFile = File(dir, CAPTURE_REVIEW_FILE)
        if (reviewFile.isFile) {
            val state = readReviewFile(reviewFile, dir.name)
            if (state !is CaptureReviewFileState.Valid || state.store.dirty) {
                return@synchronized false
            }
        }
        dir.deleteRecursively() && !dir.exists()
    }

    private fun readDesktopBookFile(file: File): DesktopBookMetadata? {
        return try {
            // The database limit applies to `data`; the local envelope also holds
            // ids, revision, timestamp, and schema fields.
            if (!file.isFile ||
                file.length() > CAPTURE_METADATA_MAX_BYTES + CAPTURE_METADATA_WRAPPER_OVERHEAD) {
                return null
            }
            val wrapper = JSONObject(file.readText())
            if (wrapper.strictString("schema", 80) != DESKTOP_BOOK_SCHEMA ||
                wrapper.strictLong("version") != 1L) return null
            desktopBookMetadataFromJson(wrapper)
        } catch (_: Exception) {
            null
        }
    }

    private fun readReviewFile(
        file: File,
        expectedCaptureId: String,
    ): CaptureReviewFileState {
        if (!file.exists()) return CaptureReviewFileState.Missing
        return try {
            if (!file.isFile || file.length() > 32 * 1024) {
                return CaptureReviewFileState.Corrupt
            }
            val wrapper = JSONObject(file.readText())
            if (wrapper.strictString("schema", 80) != CAPTURE_REVIEW_SCHEMA ||
                wrapper.strictLong("version") != 1L ||
                wrapper.strictString("capture_id", 160) != expectedCaptureId) {
                return CaptureReviewFileState.Corrupt
            }
            val current = captureReviewFromJson(
                wrapper.optJSONObject("current") ?: return CaptureReviewFileState.Corrupt,
                expectedCaptureId = expectedCaptureId,
                allowLocalRevision = true,
            ) ?: return CaptureReviewFileState.Corrupt
            val shadowValue = wrapper.opt("shadow")
            val shadow = when (shadowValue) {
                null, JSONObject.NULL -> null
                is JSONObject -> captureReviewFromJson(
                    shadowValue,
                    expectedCaptureId = expectedCaptureId,
                ) ?: return CaptureReviewFileState.Corrupt
                else -> return CaptureReviewFileState.Corrupt
            }
            val dirty = wrapper.strictBoolean("dirty")
                ?: return CaptureReviewFileState.Corrupt
            CaptureReviewFileState.Valid(CaptureReviewStore(current, shadow, dirty))
        } catch (_: Exception) {
            CaptureReviewFileState.Corrupt
        }
    }
}

internal fun desktopBookMetadataFromJson(row: JSONObject): DesktopBookMetadata? {
    val captureId = row.strictString("capture_id", 160)
        ?.takeIf { SAFE_CAPTURE_SYNC_ID.matches(it) } ?: return null
    val ownerId = row.strictString("owner_id", 160)
        ?.takeIf { SAFE_CAPTURE_SYNC_ID.matches(it) } ?: return null
    val bookId = row.strictString("book_id", 200) ?: return null
    val revision = row.strictLong("revision")?.takeIf { it > 0 } ?: return null
    val updatedAt = row.strictString("updated_at", 80)?.takeIf { it.isNotEmpty() } ?: return null
    val data = row.optJSONObject("data") ?: return null
    val dataText = data.toString()
    if (dataText.toByteArray(Charsets.UTF_8).size > CAPTURE_METADATA_MAX_BYTES) return null
    return DesktopBookMetadata(captureId, ownerId, bookId, revision, updatedAt, dataText)
}

internal fun captureReviewFromJson(
    row: JSONObject,
    expectedCaptureId: String? = null,
    allowLocalRevision: Boolean = false,
): CaptureReviewMetadata? {
    val captureId = row.strictString("capture_id", 160)
        ?.takeIf { SAFE_CAPTURE_SYNC_ID.matches(it) } ?: return null
    if (expectedCaptureId != null && captureId != expectedCaptureId) return null
    val revision = row.strictLong("revision") ?: return null
    if (revision < if (allowLocalRevision) 0 else 1) return null
    val updatedAt = row.strictString("updated_at", 80) ?: return null
    if (!allowLocalRevision && updatedAt.isEmpty()) return null
    val needsAttention = row.strictBoolean("needs_attention") ?: return null
    val needsReview = row.strictBoolean("needs_review") ?: return null
    val reason = row.strictString("attention_reason", 1_000) ?: return null
    val reviewId = row.strictString("review_id", 160) ?: return null
    val status = row.strictString("status", 40) ?: return null
    return CaptureReviewMetadata(
        captureId = captureId,
        revision = revision,
        updatedAt = updatedAt,
        needsAttention = needsAttention || needsReview,
        attentionReason = reason,
        needsReview = needsReview,
        reviewId = reviewId,
        status = status,
    )
}

internal fun editCaptureReview(
    existing: CaptureReviewStore?,
    captureId: String,
    needsAttention: Boolean,
    needsReview: Boolean,
    reason: String,
): CaptureReviewStore {
    val current = existing?.current ?: CaptureReviewMetadata(captureId)
    val effectiveReview = needsReview
    val next = current.copy(
        needsAttention = needsAttention || effectiveReview,
        attentionReason = reason.trim().take(1_000)
            .takeIf { needsAttention || effectiveReview }
            .orEmpty(),
        needsReview = effectiveReview,
    )
    if (reviewWritableEquals(current, next)) return existing ?: CaptureReviewStore(next)
    return CaptureReviewStore(next, existing?.shadow, dirty = true)
}

internal fun mergeCaptureReview(
    local: CaptureReviewStore?,
    cloud: CaptureReviewMetadata?,
): CaptureReviewMerge? {
    if (local == null && cloud == null) return null
    if (local == null) return CaptureReviewMerge(CaptureReviewStore(cloud!!, cloud, false))
    if (cloud == null) {
        val write = local.current.takeIf { local.dirty }?.let {
            CaptureReviewCloudWrite(it, expectedCloudRevision = null)
        }
        return CaptureReviewMerge(local, write)
    }
    if (local.current.captureId != cloud.captureId) return CaptureReviewMerge(local)
    val baseline = local.shadow
    // A service-side delete/recreate can reset the revision sequence. Treat a
    // lower revision as a new baseline and, when the phone is dirty, plan a CAS
    // against that actual row. Returning a clean/no-write merge here would
    // strand the local edit while reporting a successful explicit sync.
    if (baseline != null && cloud.revision < baseline.revision) {
        if (!syncTimestampAfter(cloud.updatedAt, local.current.updatedAt)) {
            return CaptureReviewMerge(local)
        }
        if (!local.dirty) {
            return CaptureReviewMerge(CaptureReviewStore(cloud, cloud, false))
        }
        val recreated = conservativeReviewMerge(local.current, cloud, baseline = null)
        if (recreated.conflict != null) {
            return CaptureReviewMerge(local, conflict = recreated.conflict)
        }
        return CaptureReviewMerge(
            CaptureReviewStore(recreated.state, cloud, dirty = true),
            CaptureReviewCloudWrite(recreated.state, expectedCloudRevision = cloud.revision),
        )
    }
    if (!local.dirty || reviewWritableEquals(local.current, cloud)) {
        return CaptureReviewMerge(CaptureReviewStore(cloud, cloud, false))
    }

    val cloudUnchanged = baseline != null && reviewWritableEquals(cloud, baseline)
    val merged = if (cloudUnchanged) {
        ConservativeReviewMerge(local.current.copy(
            revision = cloud.revision,
            updatedAt = cloud.updatedAt,
            reviewId = cloud.reviewId,
            status = cloud.status,
        ))
    } else {
        conservativeReviewMerge(local.current, cloud, baseline)
    }
    if (merged.conflict != null) {
        return CaptureReviewMerge(local, conflict = merged.conflict)
    }
    val next = merged.state
    return CaptureReviewMerge(
        CaptureReviewStore(next, cloud, dirty = true),
        CaptureReviewCloudWrite(next, expectedCloudRevision = cloud.revision),
    )
}

internal fun acknowledgeCaptureReviewWrite(
    store: CaptureReviewStore,
    sent: CaptureReviewMetadata,
    accepted: CaptureReviewMetadata,
): CaptureReviewStore {
    if (store.current.captureId != accepted.captureId) return store
    return if (reviewWritableEquals(store.current, sent)) {
        CaptureReviewStore(accepted, accepted, dirty = false)
    } else {
        CaptureReviewStore(
            store.current.copy(
                revision = accepted.revision,
                updatedAt = accepted.updatedAt,
                reviewId = accepted.reviewId,
                status = accepted.status,
            ),
            shadow = accepted,
            dirty = true,
        )
    }
}

internal fun captureReviewCloudBody(state: CaptureReviewMetadata): JSONObject = JSONObject()
    .put("capture_id", state.captureId)
    .put("needs_attention", state.needsAttention)
    .put("attention_reason", state.attentionReason)
    .put("needs_review", state.needsReview)

internal fun captureReviewLanBody(state: CaptureReviewMetadata): JSONObject =
    captureReviewToJson(state)
        .put("schema", CAPTURE_REVIEW_SCHEMA)
        .put("version", 1)

private data class ConservativeReviewMerge(
    val state: CaptureReviewMetadata,
    val conflict: String? = null,
)

private fun conservativeReviewMerge(
    local: CaptureReviewMetadata,
    cloud: CaptureReviewMetadata,
    baseline: CaptureReviewMetadata?,
): ConservativeReviewMerge {
    val localReasonChanged = baseline == null || local.attentionReason != baseline.attentionReason
    val cloudReasonChanged = baseline == null || cloud.attentionReason != baseline.attentionReason
    val localReason = local.attentionReason.trim()
    val cloudReason = cloud.attentionReason.trim()
    val reason = when {
        localReasonChanged && cloudReasonChanged -> {
            if (localReason.isNotEmpty() && cloudReason.isNotEmpty() &&
                localReason != cloudReason) {
                combineReviewReasons(cloudReason, localReason)
                    ?: return ConservativeReviewMerge(
                        local,
                        "concurrent review reasons exceed the 1000-character limit",
                    )
            } else {
                localReason.ifEmpty { cloudReason }
            }
        }
        localReasonChanged -> localReason
        cloudReasonChanged -> cloudReason
        cloudReason.isNotEmpty() -> cloudReason
        else -> localReason
    }
    val needsReview = local.needsReview || cloud.needsReview
    val needsAttention = local.needsAttention || cloud.needsAttention || needsReview
    return ConservativeReviewMerge(local.copy(
        revision = cloud.revision,
        updatedAt = cloud.updatedAt,
        needsAttention = needsAttention,
        attentionReason = if (needsAttention) reason else "",
        needsReview = needsReview,
        reviewId = cloud.reviewId,
        status = cloud.status,
    ))
}

private fun combineReviewReasons(desktop: String, phone: String): String? {
    if (desktop == phone) return desktop
    val phoneLine = "Phone: $phone"
    if (desktop.lineSequence().any { it == phoneLine }) return desktop
    val combined = "Desktop: $desktop\n$phoneLine"
    return combined.takeIf { it.length <= 1_000 }
}

private fun syncTimestampAfter(candidate: String, baseline: String): Boolean {
    val left = parseSyncTimestamp(candidate) ?: return false
    val right = parseSyncTimestamp(baseline) ?: return baseline.isBlank()
    return left > right
}

private fun syncTimestampBefore(candidate: String, baseline: String): Boolean {
    val left = parseSyncTimestamp(candidate) ?: return false
    val right = parseSyncTimestamp(baseline) ?: return false
    return left < right
}

private fun parseSyncTimestamp(value: String): Instant? =
    runCatching { Instant.parse(value.trim()) }.getOrElse {
        runCatching { OffsetDateTime.parse(value.trim()).toInstant() }.getOrNull()
    }

internal fun reviewWritableEquals(left: CaptureReviewMetadata, right: CaptureReviewMetadata): Boolean =
    left.captureId == right.captureId &&
        left.needsAttention == right.needsAttention &&
        left.attentionReason == right.attentionReason &&
        left.needsReview == right.needsReview

private fun reviewStoreToJson(store: CaptureReviewStore): JSONObject = JSONObject()
    .put("schema", CAPTURE_REVIEW_SCHEMA)
    .put("version", 1)
    .put("capture_id", store.current.captureId)
    .put("current", captureReviewToJson(store.current))
    .put("shadow", store.shadow?.let(::captureReviewToJson) ?: JSONObject.NULL)
    .put("dirty", store.dirty)

private fun captureReviewToJson(value: CaptureReviewMetadata): JSONObject = JSONObject()
    .put("capture_id", value.captureId)
    .put("revision", value.revision)
    .put("updated_at", value.updatedAt)
    .put("needs_attention", value.needsAttention)
    .put("attention_reason", value.attentionReason)
    .put("needs_review", value.needsReview)
    .put("review_id", value.reviewId)
    .put("status", value.status)

private fun parseDesktopCopyright(value: JSONObject?): DesktopCopyrightMetadata {
    if (value == null) return DesktopCopyrightMetadata()
    return DesktopCopyrightMetadata(
        status = value.strictString("status", 500).orEmpty(),
        registrationRecords = value.objectList("registration_records", 50),
        renewalRecords = value.objectList("renewal_records", 50),
    )
}

private fun parseDesktopAvailability(value: JSONObject?): DesktopAvailability {
    if (value == null) return DesktopAvailability()
    val state = when (value.strictString("state", 40)?.lowercase()) {
        "available", "present", "yes" -> DesktopAvailabilityState.AVAILABLE
        "unavailable", "absent", "no" -> DesktopAvailabilityState.UNAVAILABLE
        else -> when (value.opt("available")) {
            true -> DesktopAvailabilityState.AVAILABLE
            false -> DesktopAvailabilityState.UNAVAILABLE
            else -> DesktopAvailabilityState.UNKNOWN
        }
    }
    return DesktopAvailability(
        state = state,
        url = value.strictString("url", 2_048).orEmpty(),
        identifier = value.strictString("identifier", 300).orEmpty(),
        detail = value.strictString("detail", 1_000).orEmpty(),
    )
}

private fun parseDesktopRemarks(value: Any?): List<String> = when (value) {
    is String -> listOf(value.trim()).filter { it.isNotEmpty() }
    is JSONArray -> (0 until minOf(value.length(), 100)).mapNotNull { index ->
        when (val item = value.opt(index)) {
            is String -> item.trim().take(2_000).takeIf { it.isNotEmpty() }
            is JSONObject -> item.strictString("text", 2_000)?.takeIf { it.isNotEmpty() }
            else -> null
        }
    }
    else -> emptyList()
}

private fun JSONObject.objectList(key: String, limit: Int): List<JSONObject> {
    val array = optJSONArray(key) ?: return emptyList()
    return (0 until minOf(array.length(), limit)).mapNotNull { index ->
        array.optJSONObject(index)?.let { JSONObject(it.toString()) }
    }
}

private fun JSONObject.strictString(key: String, maxLength: Int): String? {
    val value = opt(key)
    if (value === JSONObject.NULL) return ""
    if (value !is String) return null
    val trimmed = value.trim()
    return trimmed.takeIf { it.length <= maxLength }
}

private fun JSONObject.strictLong(key: String): Long? = when (val value = opt(key)) {
    is Byte, is Short, is Int, is Long -> (value as Number).toLong()
    else -> null
}

private fun JSONObject.strictBoolean(key: String): Boolean? = opt(key) as? Boolean
