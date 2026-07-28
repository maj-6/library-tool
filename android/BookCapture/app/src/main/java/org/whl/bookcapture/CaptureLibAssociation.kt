package org.whl.bookcapture

import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.OffsetDateTime

internal const val CAPTURE_LIB_ASSOCIATION_FILE = "lib_association.json"
internal const val CAPTURE_LIB_ASSOCIATION_SCHEMA = "org.whl.capture-lib-association"
internal const val CAPTURE_LIB_CONFIRMATION_SCHEMA = "org.whl.capture-lib-confirmation"
internal const val CAPTURE_LIB_MAX_ARCHIVE_BYTES = 250L * 1024L * 1024L
private const val CAPTURE_LIB_MAX_DOCUMENT_BYTES = 8 * 1024
private const val CAPTURE_LIB_MAX_SIDECAR_BYTES = 16 * 1024

private val CAPTURE_LIB_ASSOCIATION_KEYS = setOf(
    "schema",
    "version",
    "capture_id",
    "book_id",
    "archive_sha256",
    "archive_bytes",
    "format_version",
    "state",
    "generated_at",
    "source_revision",
    "source_fingerprint",
)
private val CAPTURE_LIB_CONFIRMATION_KEYS = setOf(
    "schema",
    "version",
    "capture_id",
    "stream_id",
    "revision",
    "updated_at",
    "association",
)
private val CAPTURE_IMPORT_ROW_KEYS = setOf(
    "id",
    "status",
    "created_by",
    "lib_association",
    "lib_association_revision",
    "lib_association_updated_at",
)
private val CAPTURE_LIB_BOOK_ID = Regex("b-[0-9a-f]{32}")
private val CAPTURE_LIB_SHA256 = Regex("[0-9a-f]{64}")
private val CAPTURE_LIB_UUID = Regex(
    "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)
private val CAPTURE_LIB_OFFSET_TIMESTAMP = Regex(
    "[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T" +
        "(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]" +
        "(?:\\.[0-9]{1,9})?" +
        "(?:Z|[+-](?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00))",
)

enum class CaptureLibAssociationState {
    CURRENT,
    STALE,
}

data class CaptureLibAssociation(
    val captureId: String,
    val bookId: String,
    val archiveSha256: String,
    val archiveBytes: Long,
    val formatVersion: String,
    val state: CaptureLibAssociationState,
    val generatedAt: String,
    val sourceRevision: String,
    val sourceFingerprint: String,
) {
    val confirmed: Boolean get() = state == CaptureLibAssociationState.CURRENT

    fun toJson(): JSONObject = JSONObject()
        .put("schema", CAPTURE_LIB_ASSOCIATION_SCHEMA)
        .put("version", 1)
        .put("capture_id", captureId)
        .put("book_id", bookId)
        .put("archive_sha256", archiveSha256)
        .put("archive_bytes", archiveBytes)
        .put("format_version", formatVersion)
        .put("state", state.name.lowercase())
        .put("generated_at", generatedAt)
        .put("source_revision", sourceRevision)
        .put("source_fingerprint", sourceFingerprint)
}

data class CaptureLibConfirmation(
    val captureId: String,
    val streamId: String,
    val revision: Long,
    val updatedAt: String,
    val association: CaptureLibAssociation,
) {
    val confirmed: Boolean get() = association.confirmed

    fun toJson(): JSONObject = JSONObject()
        .put("schema", CAPTURE_LIB_CONFIRMATION_SCHEMA)
        .put("version", 1)
        .put("capture_id", captureId)
        .put("stream_id", streamId)
        .put("revision", revision)
        .put("updated_at", updatedAt)
        .put("association", association.toJson())
}

internal data class CaptureImportState(
    val captureId: String,
    val status: String,
    val confirmation: CaptureLibConfirmation?,
)

internal enum class CaptureLibApplyResult {
    APPLIED,
    UNCHANGED,
    STALE,
    CONFLICT,
}

internal fun captureLibAssociationFromJson(
    value: JSONObject,
    expectedCaptureId: String? = null,
): CaptureLibAssociation? {
    if (value.toString().toByteArray(Charsets.UTF_8).size > CAPTURE_LIB_MAX_DOCUMENT_BYTES ||
        value.keys().asSequence().toSet() != CAPTURE_LIB_ASSOCIATION_KEYS ||
        value.strictInteger("version") != 1L ||
        value.strictText("schema", 80) != CAPTURE_LIB_ASSOCIATION_SCHEMA
    ) return null
    val captureId = value.strictText("capture_id", 160)
        ?.takeIf(CAPTURE_LIB_UUID::matches) ?: return null
    if (expectedCaptureId != null && captureId != expectedCaptureId) return null
    val bookId = value.strictText("book_id", 40)
        ?.takeIf(CAPTURE_LIB_BOOK_ID::matches) ?: return null
    val archiveSha256 = value.strictText("archive_sha256", 64)
        ?.takeIf(CAPTURE_LIB_SHA256::matches) ?: return null
    val archiveBytes = value.strictInteger("archive_bytes")
        ?.takeIf { it in 1..CAPTURE_LIB_MAX_ARCHIVE_BYTES } ?: return null
    val formatVersion = value.strictText("format_version", 16)
        ?.takeIf { it == "3.0" } ?: return null
    val state = when (value.strictText("state", 16)) {
        "current" -> CaptureLibAssociationState.CURRENT
        "stale" -> CaptureLibAssociationState.STALE
        else -> return null
    }
    val generatedAt = value.strictText("generated_at", 80)
        ?.takeIf(::isOffsetTimestamp) ?: return null
    val sourceRevision = value.strictText("source_revision", 512)
        ?.takeIf {
            it.isNotEmpty() && it == it.trim() &&
                it.none(Char::isWhitespace) &&
                '"' !in it && '/' !in it && '\\' !in it
        } ?: return null
    val sourceFingerprint = value.strictText("source_fingerprint", 64)
        ?.takeIf(CAPTURE_LIB_SHA256::matches) ?: return null
    return CaptureLibAssociation(
        captureId = captureId,
        bookId = bookId,
        archiveSha256 = archiveSha256,
        archiveBytes = archiveBytes,
        formatVersion = formatVersion,
        state = state,
        generatedAt = generatedAt,
        sourceRevision = sourceRevision,
        sourceFingerprint = sourceFingerprint,
    )
}

internal fun captureLibConfirmationFromJson(
    value: JSONObject,
    expectedCaptureId: String? = null,
): CaptureLibConfirmation? {
    if (value.keys().asSequence().toSet() != CAPTURE_LIB_CONFIRMATION_KEYS ||
        value.strictInteger("version") != 1L ||
        value.strictText("schema", 80) != CAPTURE_LIB_CONFIRMATION_SCHEMA
    ) return null
    val captureId = value.strictText("capture_id", 160)
        ?.takeIf(CAPTURE_LIB_UUID::matches) ?: return null
    if (expectedCaptureId != null && captureId != expectedCaptureId) return null
    val streamId = value.strictText("stream_id", 160)
        ?.takeIf(CAPTURE_LIB_UUID::matches) ?: return null
    val revision = value.strictInteger("revision")?.takeIf { it > 0 } ?: return null
    val updatedAt = value.strictText("updated_at", 80)
        ?.takeIf(::isOffsetTimestamp) ?: return null
    val association = value.optJSONObject("association")
        ?.let { captureLibAssociationFromJson(it, captureId) } ?: return null
    return CaptureLibConfirmation(
        captureId = captureId,
        streamId = streamId,
        revision = revision,
        updatedAt = updatedAt,
        association = association,
    )
}

internal fun captureImportStateFromJson(
    row: JSONObject,
    expectedOwnerId: String,
): CaptureImportState? {
    if (row.keys().asSequence().toSet() != CAPTURE_IMPORT_ROW_KEYS) return null
    val captureId = row.strictText("id", 160)
        ?.takeIf(CAPTURE_LIB_UUID::matches) ?: return null
    val status = row.strictText("status", 40) ?: return null
    val ownerId = row.strictText("created_by", 160)
        ?.takeIf(CAPTURE_LIB_UUID::matches) ?: return null
    if (ownerId != expectedOwnerId) return null
    val revision = row.strictInteger("lib_association_revision") ?: return null
    val rawAssociation = row.opt("lib_association")
    val rawUpdatedAt = row.opt("lib_association_updated_at")
    val confirmation = when (rawAssociation) {
        null, JSONObject.NULL -> {
            if (revision != 0L || (rawUpdatedAt != null && rawUpdatedAt !== JSONObject.NULL)) {
                return null
            }
            null
        }
        is JSONObject -> {
            val updatedAt = (rawUpdatedAt as? String)
                ?.takeIf { it.length <= 80 && isOffsetTimestamp(it) } ?: return null
            val association = captureLibAssociationFromJson(rawAssociation, captureId)
                ?: return null
            if (revision <= 0L) return null
            CaptureLibConfirmation(
                captureId = captureId,
                streamId = ownerId,
                revision = revision,
                updatedAt = updatedAt,
                association = association,
            )
        }
        else -> return null
    }
    return CaptureImportState(captureId, status, confirmation)
}

internal object CaptureLibAssociationStore {
    private val lock = Any()

    fun read(dir: File): CaptureLibConfirmation? = synchronized(lock) {
        readFile(File(dir, CAPTURE_LIB_ASSOCIATION_FILE))
    }

    fun apply(
        dir: File,
        incoming: CaptureLibConfirmation,
    ): CaptureLibApplyResult = synchronized(lock) {
        if (incoming.captureId != dir.name) return@synchronized CaptureLibApplyResult.CONFLICT
        val validated = captureLibConfirmationFromJson(incoming.toJson(), dir.name)
            ?.takeIf { it == incoming }
            ?: return@synchronized CaptureLibApplyResult.CONFLICT
        val file = File(dir, CAPTURE_LIB_ASSOCIATION_FILE)
        val local = readFile(file)
        val result = captureLibMergeResult(local, validated)
        if (result == CaptureLibApplyResult.APPLIED) {
            Entries.atomicWrite(file, validated.toJson().toString())
        }
        result
    }

    private fun readFile(file: File): CaptureLibConfirmation? = try {
        if (!file.isFile || file.length() !in 1..CAPTURE_LIB_MAX_SIDECAR_BYTES.toLong()) {
            null
        } else {
            captureLibConfirmationFromJson(JSONObject(file.readText()), file.parentFile?.name)
        }
    } catch (_: Exception) {
        null
    }
}

internal fun captureLibMergeResult(
    local: CaptureLibConfirmation?,
    incoming: CaptureLibConfirmation,
): CaptureLibApplyResult {
    if (local == null) return CaptureLibApplyResult.APPLIED
    if (local.captureId != incoming.captureId) return CaptureLibApplyResult.CONFLICT
    if (local.streamId != incoming.streamId) {
        // A desktop rotates its stream before restarting the revision ledger.
        // The server timestamp is the only ordering signal that survives that
        // reset: accepting every changed stream would let a delayed response
        // from the retired stream overwrite the newer confirmation.
        return when {
            timestampAfter(incoming.updatedAt, local.updatedAt) ->
                CaptureLibApplyResult.APPLIED
            timestampBefore(incoming.updatedAt, local.updatedAt) ->
                CaptureLibApplyResult.STALE
            else -> CaptureLibApplyResult.CONFLICT
        }
    }
    if (incoming.revision < local.revision) {
        // A deleted/recreated cloud row may restart at revision one. Its newer
        // server timestamp is the bounded reset signal; ordinary delayed rows
        // retain their older timestamp and are ignored.
        return if (incoming.revision == 1L &&
            timestampAfter(incoming.updatedAt, local.updatedAt)
        ) CaptureLibApplyResult.APPLIED else CaptureLibApplyResult.STALE
    }
    if (incoming.revision == local.revision) {
        return when {
            incoming == local -> CaptureLibApplyResult.UNCHANGED
            timestampBefore(incoming.updatedAt, local.updatedAt) ->
                CaptureLibApplyResult.STALE
            else -> CaptureLibApplyResult.CONFLICT
        }
    }
    return CaptureLibApplyResult.APPLIED
}

private fun JSONObject.strictText(key: String, maximum: Int): String? =
    (opt(key) as? String)?.takeIf { it.length <= maximum }

private fun JSONObject.strictInteger(key: String): Long? = when (val value = opt(key)) {
    is Byte -> value.toLong()
    is Short -> value.toLong()
    is Int -> value.toLong()
    is Long -> value
    else -> null
}

private fun isOffsetTimestamp(value: String): Boolean =
    CAPTURE_LIB_OFFSET_TIMESTAMP.matches(value) && parseTimestamp(value) != null

private fun timestampAfter(candidate: String, baseline: String): Boolean {
    val left = parseTimestamp(candidate) ?: return false
    val right = parseTimestamp(baseline) ?: return true
    return left > right
}

private fun timestampBefore(candidate: String, baseline: String): Boolean {
    val left = parseTimestamp(candidate) ?: return false
    val right = parseTimestamp(baseline) ?: return false
    return left < right
}

private fun parseTimestamp(value: String): Instant? =
    runCatching { Instant.parse(value) }.getOrElse {
        runCatching { OffsetDateTime.parse(value).toInstant() }.getOrNull()
    }
