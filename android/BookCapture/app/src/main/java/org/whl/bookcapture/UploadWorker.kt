package org.whl.bookcapture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.time.Instant
import java.util.concurrent.TimeUnit

internal val IMPORT_POLL_DELAYS_MS = listOf(
    TimeUnit.MINUTES.toMillis(1),
    TimeUnit.MINUTES.toMillis(5),
    TimeUnit.MINUTES.toMillis(30),
    TimeUnit.HOURS.toMillis(2),
    TimeUnit.HOURS.toMillis(8),
    TimeUnit.HOURS.toMillis(24),
)

/** Cloud capture status is intentionally free-form for forward compatibility,
 * but these states are known to be final. Unknown states keep polling so a new
 * server-side in-progress state cannot strand a capture on an older phone. */
private val REMOTE_IMPORT_TERMINAL_STATUSES = setOf(
    "imported",
    "error",
    "failed",
    "void",
    "cancelled",
    "canceled",
)

internal fun normalizeRemoteImportStatus(status: String): String =
    status.trim().lowercase()

internal fun isRemoteImportPending(status: String): Boolean =
    normalizeRemoteImportStatus(status) !in REMOTE_IMPORT_TERMINAL_STATUSES

internal fun sentEntryNeedsLocalRetention(
    cloudStatus: String,
    hasPendingCloudPhotoWork: Boolean,
): Boolean = isRemoteImportPending(cloudStatus) || hasPendingCloudPhotoWork

/** User-facing label for a final cloud import outcome, or null while pending. */
internal fun remoteImportTerminalLabel(status: String): String? =
    when (normalizeRemoteImportStatus(status)) {
        "imported" -> "imported"
        "error" -> "import error"
        "failed" -> "import failed"
        "void" -> "void"
        "cancelled", "canceled" -> "import cancelled"
        else -> null
    }

/** Stable position in the sealed-capture queue. Capture timestamps are the
 * primary key so a newly sealed capture naturally lands after work already in
 * progress; the id makes captures sealed in the same millisecond deterministic. */
internal data class UploadQueueKey(val createdAt: Long, val entryId: String) :
    Comparable<UploadQueueKey> {
    override fun compareTo(other: UploadQueueKey): Int =
        compareValuesBy(this, other, UploadQueueKey::createdAt, UploadQueueKey::entryId)
}

/** Returns one and only one item after [cursor]. Repeating this operation with
 * the returned key walks an arbitrarily large backlog incrementally without
 * ever making one worker responsible for the entire queue. */
internal fun nextUploadQueueKey(
    pending: Collection<UploadQueueKey>,
    cursor: UploadQueueKey?,
): UploadQueueKey? = pending.asSequence()
    .distinct()
    .filter { cursor == null || it > cursor }
    .minOrNull()

internal const val UPLOAD_PROGRESS_ENTRY_ID = "upload-entry-id"
internal const val UPLOAD_PROGRESS_STAGE = "upload-stage"
internal const val UPLOAD_PROGRESS_TOTAL = "upload-total"
internal const val UPLOAD_PROGRESS_SYNCED = "upload-synced"
internal const val UPLOAD_PROGRESS_BLOCKED = "upload-blocked"
internal const val UPLOAD_PROGRESS_REMAINING = "upload-remaining"

internal fun deferredUploadRecheckDelayMs(round: Int): Long {
    val exponent = round.coerceIn(0, 2)
    return TimeUnit.SECONDS.toMillis(30L shl exponent)
}

internal class UploadEntryProblem(
    message: String,
    val retryable: Boolean = false,
    val reason: UploadEntryProblemReason = UploadEntryProblemReason.OTHER,
    cause: Throwable? = null,
) : IOException(message, cause)

internal enum class UploadEntryProblemReason { OTHER, NEEDS_CLOUD_CLAIM }

internal data class ValidatedPhoto(val name: String, val file: File)

internal data class ConfirmedDelivery(
    val entryId: String,
    val photoCount: Int,
    val remotePaths: List<String>,
    val captureLibConfirmation: CaptureLibConfirmation? = null,
)

/**
 * Stamp the local delivery receipt without allowing account provenance to be
 * inferred from mutable session preferences. The caller supplies the owner
 * frozen in the sync request; cloud delivery accepts it only when it matches
 * the account creator already sealed into the capture manifest.
 */
internal fun stampDeliveryManifest(
    manifest: JSONObject,
    uploadedAt: Long,
    cloudStatus: String,
    syncRequestId: String,
    deliveryTransport: String,
    cloudOwnerId: String = "",
): JSONObject {
    require(uploadedAt > 0L) { "delivery time is required" }
    require(cloudStatus in setOf("pending", "imported")) { "invalid cloud status" }
    require(syncRequestId.isNotBlank()) { "sync request id is required" }
    require(deliveryTransport in setOf("cloud", "lan")) { "invalid delivery transport" }
    val owner = cloudOwnerId.trim().lowercase()
    manifest.put("delivery_transport", deliveryTransport)
    if (deliveryTransport == "cloud") {
        require(SAFE_CAPTURE_SYNC_ID.matches(owner)) { "cloud owner is required" }
        manifest.put(CLOUD_OWNER_MANIFEST_KEY, owner)
        require(cloudOwnerIdFromDeliveryManifest(manifest) == owner) {
            "cloud owner does not match capture creator"
        }
    } else {
        require(owner.isEmpty()) { "LAN delivery cannot have a cloud owner" }
        manifest.remove(CLOUD_OWNER_MANIFEST_KEY)
    }
    return manifest
        .put("uploaded_at", uploadedAt)
        .put("cloud_status", cloudStatus)
        .put("sync_request_id", syncRequestId)
}

/** Cloud work must be visible to WorkManager as network-bound. LAN and an
 * unresolved Auto request may need to run on unvalidated, local-only Wi-Fi. */
internal fun captureUploadRequiresConnectedNetwork(record: CaptureSyncRecord?): Boolean =
    record?.resolvedTransport == "cloud" || record?.transportMode == "cloud"

/** An older or unresolved Auto WorkSpec cannot gain a network constraint in
 * place. Hand it off after the frozen request has resolved to cloud. */
internal fun captureUploadNeedsConnectedHandoff(
    resolvedTransport: String,
    scheduledForConnectedNetwork: Boolean,
): Boolean = resolvedTransport == "cloud" && !scheduledForConnectedNetwork

internal const val MAX_CAPTURE_UPLOAD_RETRIES = 3

/** Reprocessing may improve the metadata shipped with a capture, but it must
 * never pin the photos in queue/ forever. A fresh explicit retry gets the same
 * grace window as automatic processing; after that, delivery proceeds and the
 * reprocess worker can safely finish against the retained sent/ copy. */
internal const val REPROCESS_UPLOAD_HOLD_MS = 10 * 60 * 1000L

internal fun shouldWaitForReprocessBeforeUpload(
    markerLastModifiedMs: Long,
    nowMs: Long,
    holdMs: Long = REPROCESS_UPLOAD_HOLD_MS,
): Boolean {
    require(holdMs >= 0)
    if (markerLastModifiedMs <= 0) return false
    // A small clock rollback should not defeat a newly-created hold, but a
    // corrupt/far-future timestamp must not recreate an indefinite pin.
    if (markerLastModifiedMs >= nowMs) return markerLastModifiedMs - nowMs < holdMs
    return nowMs - markerLastModifiedMs < holdMs
}

internal fun reprocessUploadHoldIsActive(
    dir: File,
    nowMs: Long = System.currentTimeMillis(),
): Boolean {
    val marker = File(dir, Entries.REPROCESS_PENDING)
    return marker.isFile && shouldWaitForReprocessBeforeUpload(
        marker.lastModified(),
        nowMs,
    )
}

internal enum class CaptureTokenFailureAction { RETRY, WAIT_FOR_SIGN_IN, ACCOUNT_CHANGED }

/** accessToken() returns null for both a temporary refresh failure and a real
 * sign-out. Preserve the batch in both cases; only an account switch is a
 * terminal ownership mismatch. */
internal fun captureTokenFailureAction(
    signedIn: Boolean,
    currentOwner: String,
    expectedOwner: String,
): CaptureTokenFailureAction = when {
    !signedIn || currentOwner.isBlank() -> CaptureTokenFailureAction.WAIT_FOR_SIGN_IN
    currentOwner.trim() == expectedOwner.trim() -> CaptureTokenFailureAction.RETRY
    else -> CaptureTokenFailureAction.ACCOUNT_CHANGED
}

internal fun isCaptureSessionRejection(code: Int): Boolean = code == 401

/** A healthy live WorkSpec must never be canceled by a repeated button press.
 * KEEP still recreates unique work whose prior chain is already terminal. */
internal fun captureSyncEnqueuePolicy(start: CaptureSyncStart): ExistingWorkPolicy = when {
    start.created -> ExistingWorkPolicy.REPLACE
    start.record.phase == CaptureSyncPhase.WAITING_FOR_PROCESSING -> ExistingWorkPolicy.REPLACE
    start.record.phase == CaptureSyncPhase.RETRYING -> ExistingWorkPolicy.REPLACE
    else -> ExistingWorkPolicy.KEEP
}

internal fun shouldRetryCaptureUpload(runAttemptCount: Int): Boolean =
    runAttemptCount < MAX_CAPTURE_UPLOAD_RETRIES

internal fun captureUploadFailureMessage(entryId: String, error: Throwable): String {
    val detail = error.message.orEmpty().replace(Regex("\\s+"), " ").trim().take(120)
        .ifEmpty { error.javaClass.simpleName.ifEmpty { "unknown error" } }
    return "Entry ${entryId.take(8).ifEmpty { "unknown" }} could not upload: $detail"
}

/** Validate the whole manifest photo set before starting any network writes.
 * Silently skipping one missing page would turn a partial upload into a
 * successful one, so one bad member keeps the entire entry recoverable. */
internal fun validateUploadPhotos(dir: File, names: List<String>): List<ValidatedPhoto> {
    val label = dir.name.take(8).ifEmpty { "unknown" }
    if (names.isEmpty()) {
        throw UploadEntryProblem(
            "Entry $label has no photos to upload; it was kept pending. " +
                "Open Recent to discard it and recapture.")
    }

    val seen = mutableSetOf<String>()
    val problems = mutableListOf<String>()
    val photos = mutableListOf<ValidatedPhoto>()
    for (name in names) {
        when {
            !name.matches(PHOTO_NAME) -> problems += "$name (invalid name)"
            !seen.add(name) -> problems += "$name (listed twice)"
            else -> {
                val file = File(dir, name)
                if (!file.isFile) problems += "$name (missing)"
                else if (!looksLikeCompleteJpeg(file)) problems += "$name (corrupt)"
                else photos += ValidatedPhoto(name, file)
            }
        }
    }
    val unlisted = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
        ?.map { it.name }
        ?.filterNot(seen::contains)
        .orEmpty()
    problems += unlisted.map { "$it (not listed)" }
    if (problems.isNotEmpty()) {
        val summary = problems.take(3).joinToString(", ") +
            if (problems.size > 3) ", and ${problems.size - 3} more" else ""
        throw UploadEntryProblem(
            "Entry $label has missing or corrupt photos: $summary. " +
                "It was kept pending; restore them or discard and recapture.")
    }
    return photos
}

/**
 * Keep the established transport names (`photo_N.jpg`) but send the immutable
 * camera source behind each name. Existing cloud and LAN importers already
 * treat those parts as capture originals and generate their own display
 * derivatives, so this closes the raw-retention gap without duplicating pages
 * or requiring a new protocol field.
 */
internal fun selectTransportOriginals(
    dir: File,
    displayPhotos: List<ValidatedPhoto>,
): List<ValidatedPhoto> {
    val label = dir.name.take(8).ifEmpty { "unknown" }
    val contract = PhotoAssetStore.read(dir)
    val byCaptureFile = contract.assets.associateBy { it.captureFile }
    return displayPhotos.map { display ->
        val asset = byCaptureFile[display.name]
        if (asset == null) {
            if (contract.legacyFallback) return@map display
            throw UploadEntryProblem(
                "Entry $label has no photo-asset record for ${display.name}; " +
                    "it was kept pending to preserve its camera original.",
            )
        }
        val original = File(dir, asset.original.reference)
        val sameAsDisplay = runCatching {
            original.canonicalFile == display.file.canonicalFile
        }.getOrDefault(original.absolutePath == display.file.absolutePath)
        if (!contract.legacyFallback && sameAsDisplay) {
            throw UploadEntryProblem(
                "Entry $label has no separate camera original for ${display.name}; " +
                    "it was kept pending.",
            )
        }
        if (!original.isFile || !looksLikeCompleteJpeg(original)) {
            throw UploadEntryProblem(
                "Entry $label has a missing or corrupt camera original for ${display.name}; " +
                    "it was kept pending.",
            )
        }
        val expected = asset.original.sha256.lowercase()
        if (!contract.legacyFallback && expected.isEmpty()) {
            throw UploadEntryProblem(
                "Entry $label has an unverified camera original for ${display.name}; " +
                    "it was kept pending.",
            )
        }
        if (expected.isNotEmpty() && sha256Hex(original) != expected) {
            throw UploadEntryProblem(
                "Entry $label has a changed camera original for ${display.name}; " +
                    "it was kept pending.",
            )
        }
        ValidatedPhoto(display.name, original)
    }
}

private fun sha256Hex(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { input ->
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            digest.update(buffer, 0, read)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}

internal fun originalTransportPayload(photoAssets: JSONObject): JSONObject =
    JSONObject(photoAssets.toString()).put(
        "transport",
        JSONObject().put("representation", "original").put("version", 1),
    )

/** The note document is transport metadata, not model-extracted bibliography.
 * Replace a same-named value from meta.json with the authoritative sidecar
 * snapshot and omit the key entirely when the capture has no notes. */
internal fun attachCaptureNotes(
    meta: JSONObject,
    notes: JSONObject?,
): JSONObject = meta.apply {
    remove(CAPTURE_NOTES_META_KEY)
    if (notes != null && CaptureNotes.hasNotes(notes)) {
        put(CAPTURE_NOTES_META_KEY, JSONObject(notes.toString()))
    }
}

/** Cheap structural guard for the camera JPEGs: readable, bounded correctly,
 * and containing both a non-empty image frame and a scan. This catches empty,
 * truncated, and obvious garbage files without decoding a full-resolution
 * page into memory. */
internal fun looksLikeCompleteJpeg(file: File): Boolean = try {
    RandomAccessFile(file, "r").use { input ->
        val fileLength = input.length()
        if (fileLength < 4 ||
            input.readUnsignedByte() != 0xff || input.readUnsignedByte() != 0xd8)
            return@use false
        input.seek(fileLength - 2)
        if (input.readUnsignedByte() != 0xff || input.readUnsignedByte() != 0xd9)
            return@use false

        var hasFrame = false
        var hasScan = false
        input.seek(2)
        while (input.filePointer < fileLength - 2 && !hasScan) {
            if (input.readUnsignedByte() != 0xff) return@use false
            var marker = input.readUnsignedByte()
            while (marker == 0xff) marker = input.readUnsignedByte() // legal fill bytes
            if (marker == 0x00 || marker == 0xd8 || marker == 0xd9) return@use false
            if (marker == 0x01 || marker in 0xd0..0xd7) continue     // no payload
            if (input.filePointer + 2 > fileLength) return@use false

            val segmentLength = input.readUnsignedShort()
            if (segmentLength < 2) return@use false
            val segmentEnd = input.filePointer + segmentLength - 2
            if (segmentEnd > fileLength - 2) return@use false
            when {
                marker in JPEG_FRAME_MARKERS -> {
                    if (segmentLength < 8) return@use false
                    input.readUnsignedByte()                         // sample precision
                    val height = input.readUnsignedShort()
                    val width = input.readUnsignedShort()
                    if (height == 0 || width == 0) return@use false
                    hasFrame = true
                }
                marker == 0xda -> {
                    if (!hasFrame || segmentLength < 6 || segmentEnd >= fileLength - 2)
                        return@use false
                    hasScan = true
                }
            }
            input.seek(segmentEnd)
        }
        hasFrame && hasScan
    }
} catch (_: Exception) {
    false
}

private val JPEG_FRAME_MARKERS = setOf(
    0xc0, 0xc1, 0xc2, 0xc3,
    0xc5, 0xc6, 0xc7,
    0xc9, 0xca, 0xcb,
    0xcd, 0xce, 0xcf,
)

/** A receipt exists only after every photo write and the capture-row write
 * have returned successfully. Stable object paths plus server-side upserts
 * make replay after a partial attempt safe. */
internal fun deliverValidatedCapture(
    entryId: String,
    deviceFolder: String,
    photos: List<ValidatedPhoto>,
    uploadPhoto: (String, File) -> Unit,
    insertRecord: (List<String>) -> Unit,
): ConfirmedDelivery {
    require(photos.isNotEmpty()) { "validated delivery requires at least one photo" }
    val remote = photos.map { photo ->
        val path = "$deviceFolder/$entryId/${photo.name}"
        uploadPhoto(path, photo.file)
        path
    }
    insertRecord(remote)
    return ConfirmedDelivery(entryId, photos.size, remote)
}

/**
 * After a user-created batch authorizes its frozen ids, uploads one sealed
 * capture per WorkManager invocation and persists a serial cursor continuation
 * for the next capture. Photos go to the `captures`
 * bucket ("<device>/<entryId>/photo_N.jpg"), then
 * one `captures` table row carrying the contributor and whatever OCR/meta the
 * background pipeline has produced. Uploads run as the signed-in user; the
 * folder moves to sent/ (the recent list's history) only after both steps.
 *
 * A freshly sealed entry gets a grace period for the pipeline to finish, so
 * the row usually ships WITH its extraction; an entry that cannot process
 * (no keys, hard API error) ships anyway once it ages past the window —
 * photos are the cargo, metadata is the bonus.
 *
 * Errors split two ways: transient (network, 5xx) retries this capture with
 * backoff; permanent errors are recorded while later captures keep moving.
 */
class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        private const val PROCESS_GRACE_MS = REPROCESS_UPLOAD_HOLD_MS
        const val EXPLICIT_SYNC_WORK_NAME = "capture-upload"
        private const val IMPORT_POLL_WORK = "capture-import-poll"
        private const val POLL_ONLY = "poll-only"
        private const val SYNC_REQUEST_ID = "explicit-sync-request-id"
        private const val HAS_CURSOR = "upload-has-cursor"
        private const val CURSOR_CREATED_AT = "upload-cursor-created-at"
        private const val CURSOR_ENTRY_ID = "upload-cursor-entry-id"
        private const val CHAIN_SAW_DEFERRED = "upload-chain-saw-deferred"
        private const val CHAIN_HAD_ERROR = "upload-chain-had-error"
        private const val DEFERRED_ROUND = "upload-deferred-round"
        private const val SCHEDULED_FOR_CONNECTED_NETWORK =
            "upload-scheduled-for-connected-network"

        /**
         * The sole capture-delivery entry point. The eligible folder ids are
         * frozen before WorkManager is touched, so captures sealed after this
         * button press remain local for the next explicit sync.
         */
        internal fun enqueueExplicitSync(ctx: Context): CaptureSyncState {
            val session = CaptureSession(ctx)
            val targets = session.manualSyncCandidates().map { it.name }
            val start = Prefs.beginCaptureSync(ctx, targets)
            if (start.record.targetIds.isNotEmpty()) {
                WorkManager.getInstance(ctx).enqueueUniqueWork(
                    EXPLICIT_SYNC_WORK_NAME,
                    captureSyncEnqueuePolicy(start),
                    request(ctx, start.record.requestId),
                )
            }
            return captureSyncState(ctx)
        }

        /** A filesystem-backed aggregate; no WorkManager query is required. */
        internal fun captureSyncState(ctx: Context): CaptureSyncState {
            val session = CaptureSession(ctx)
            val candidates = session.manualSyncCandidates().map { it.name }
            return aggregateCaptureSyncState(
                record = Prefs.captureSyncRecord(ctx),
                eligibleIds = candidates,
                pendingIds = candidates,
            )
        }

        /** Existing lifecycle calls may resume, but never authorize, a batch. */
        @Deprecated("Capture uploads require enqueueExplicitSync")
        fun enqueue(ctx: Context) {
            resumeExplicitSync(ctx)
        }

        @Deprecated("Capture uploads require enqueueExplicitSync")
        fun kick(ctx: Context) {
            resumeExplicitSync(ctx)
        }

        private fun resumeExplicitSync(ctx: Context) {
            val active = Prefs.activeCaptureSyncRecord(ctx) ?: return
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork(
                    EXPLICIT_SYNC_WORK_NAME,
                    ExistingWorkPolicy.KEEP,
                    request(ctx, active.requestId),
                )
        }

        /** A settings repair may replace backoff work, but it cannot authorize
         * a new batch. Before the first successful delivery only, refresh the
         * frozen destination from the newly saved settings. */
        internal fun restartExplicitSyncAfterSettingsChange(ctx: Context) {
            val active = Prefs.refreshUndeliveredCaptureSyncDestination(ctx) ?: return
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                EXPLICIT_SYNC_WORK_NAME,
                ExistingWorkPolicy.REPLACE,
                request(ctx, active.requestId),
            )
        }

        private fun request(
            ctx: Context,
            syncRequestId: String,
            cursor: UploadQueueKey? = null,
            sawDeferred: Boolean = false,
            hadError: Boolean = false,
            deferredRound: Int = 0,
            delayMs: Long = 0,
        ): OneTimeWorkRequest {
            val scheduledForConnectedNetwork = captureUploadRequiresConnectedNetwork(
                Prefs.captureSyncRecord(ctx)?.takeIf { it.requestId == syncRequestId },
            )
            val builder = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(
                    SYNC_REQUEST_ID to syncRequestId,
                    HAS_CURSOR to (cursor != null),
                    CURSOR_CREATED_AT to (cursor?.createdAt ?: 0L),
                    CURSOR_ENTRY_ID to cursor?.entryId.orEmpty(),
                    CHAIN_SAW_DEFERRED to sawDeferred,
                    CHAIN_HAD_ERROR to hadError,
                    DEFERRED_ROUND to deferredRound,
                    SCHEDULED_FOR_CONNECTED_NETWORK to scheduledForConnectedNetwork,
                ))
                .setConstraints(
                    // LAN Wi-Fi may have no validated Internet capability, but
                    // cloud work must use JobScheduler's network lifecycle on
                    // Android 15+ rather than an unconstrained greedy worker.
                    Constraints.Builder().setRequiredNetworkType(
                        if (scheduledForConnectedNetwork) NetworkType.CONNECTED
                        else NetworkType.NOT_REQUIRED,
                    ).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            if (delayMs > 0) builder.setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
            return builder.build()
        }

        private fun continueUploadChain(
            ctx: Context,
            syncRequestId: String,
            cursor: UploadQueueKey?,
            sawDeferred: Boolean,
            hadError: Boolean,
            deferredRound: Int,
            delayMs: Long = 0,
        ): Boolean {
            // A newer explicit press may supersede this worker while a blocking
            // upload call is returning. Never append stale work behind the new
            // generation's unique chain.
            if (Prefs.activeCaptureSyncRecord(ctx)?.requestId != syncRequestId) return true
            return try {
                WorkManager.getInstance(ctx).enqueueUniqueWork(
                    EXPLICIT_SYNC_WORK_NAME,
                    ExistingWorkPolicy.APPEND_OR_REPLACE,
                    request(
                        ctx,
                        syncRequestId,
                        cursor,
                        sawDeferred,
                        hadError,
                        deferredRound,
                        delayMs,
                    ),
                ).result.get()
                true
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                throw CancellationException("upload continuation interrupted").also {
                    it.initCause(e)
                }
            } catch (_: Exception) {
                false
            }
        }

        private fun pollRequest(delayMs: Long) = OneTimeWorkRequestBuilder<UploadWorker>()
            .setInputData(workDataOf(POLL_ONLY to true))
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
            .build()

        /** A finite persisted chain keeps import state moving even when no new
         * capture happens to start UploadWorker. Replacing an older chain on a
         * fresh upload restarts the bounded lifecycle for the new row. */
        private fun scheduleImportPolling(ctx: Context) {
            val requests = IMPORT_POLL_DELAYS_MS.map(::pollRequest)
            var continuation = WorkManager.getInstance(ctx).beginUniqueWork(
                IMPORT_POLL_WORK,
                ExistingWorkPolicy.REPLACE,
                requests.first(),
            )
            for (request in requests.drop(1)) continuation = continuation.then(request)
            continuation.enqueue()
        }

        // 4xx is permanent — except the two that are really the network's or
        // the server's mood: timeout and rate limit.
        private fun permanent(code: Int) =
            code in 400..499 && code != 408 && code != 425 && code != 429
    }

    private data class PendingCapture(val key: UploadQueueKey, val dir: File)

    private fun inputCursor(): UploadQueueKey? =
        if (!inputData.getBoolean(HAS_CURSOR, false)) null
        else UploadQueueKey(
            inputData.getLong(CURSOR_CREATED_AT, 0L),
            inputData.getString(CURSOR_ENTRY_ID).orEmpty(),
        )

    private fun inputSyncRequestId(): String =
        inputData.getString(SYNC_REQUEST_ID).orEmpty().trim()

    private fun authorizedSyncRecord(ctx: Context): CaptureSyncRecord? {
        val inputId = inputSyncRequestId()
        if (inputId.isEmpty()) return null
        return Prefs.activeCaptureSyncRecord(ctx)?.takeIf { it.requestId == inputId }
    }

    private fun queueKey(dir: File): UploadQueueKey {
        val manifest = File(dir, "manifest.json")
        val createdAt = try {
            JSONObject(manifest.readText()).optLong("created_at", manifest.lastModified())
        } catch (_: Exception) {
            manifest.lastModified()
        }
        return UploadQueueKey(createdAt, dir.name)
    }

    private fun nextPendingCapture(
        session: CaptureSession,
        cursor: UploadQueueKey?,
    ): PendingCapture? {
        val record = authorizedSyncRecord(applicationContext) ?: return null
        val targets = record.targetIds - record.syncedIds - record.blockedIds
        val byKey = session.pendingUploads()
            .filter { it.name in targets }
            .associateBy(::queueKey)
        val next = nextUploadQueueKey(byKey.keys, cursor) ?: return null
        return PendingCapture(next, checkNotNull(byKey[next]))
    }

    private suspend fun setUploadProgress(entryId: String, stage: String) {
        val state = captureSyncState(applicationContext)
        setProgress(workDataOf(
            UPLOAD_PROGRESS_ENTRY_ID to entryId,
            UPLOAD_PROGRESS_STAGE to stage,
            UPLOAD_PROGRESS_TOTAL to state.requestedCount,
            UPLOAD_PROGRESS_SYNCED to state.syncedCount,
            UPLOAD_PROGRESS_BLOCKED to state.blockedCount,
            UPLOAD_PROGRESS_REMAINING to state.remainingCount,
        ))
    }

    private fun syncResultData(ctx: Context, stage: String, entryId: String = "") =
        captureSyncState(ctx).let { state ->
            workDataOf(
                UPLOAD_PROGRESS_ENTRY_ID to entryId,
                UPLOAD_PROGRESS_STAGE to stage,
                UPLOAD_PROGRESS_TOTAL to state.requestedCount,
                UPLOAD_PROGRESS_SYNCED to state.syncedCount,
                UPLOAD_PROGRESS_BLOCKED to state.blockedCount,
                UPLOAD_PROGRESS_REMAINING to state.remainingCount,
            )
        }

    private suspend fun waitForSignIn(
        ctx: Context,
        syncRequestId: String,
        entryId: String,
    ): Result {
        val message = "Sign in again to continue capture sync"
        Prefs.setLastUploadError(ctx, message)
        Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
        setUploadProgress(entryId, "waiting-for-sign-in")
        return Result.success(syncResultData(ctx, "waiting-for-sign-in", entryId))
    }

    private suspend fun failForAccountChange(
        ctx: Context,
        syncRequestId: String,
        entryId: String,
    ): Result {
        val message = "Capture sync belongs to a different account"
        Prefs.setLastUploadError(ctx, message)
        Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.FAILED)
        setUploadProgress(entryId, "account-changed")
        return Result.failure(syncResultData(ctx, "account-changed", entryId))
    }

    private suspend fun recoverDeliveredAccounting(
        ctx: Context,
        record: CaptureSyncRecord,
        exactTargetIds: Set<String>? = null,
    ): Boolean {
        var schedulingSucceeded = true
        val unresolved = record.targetIds - record.syncedIds
        val recoveryIds = exactTargetIds?.intersect(unresolved) ?: Entries.recent(ctx)
            .asSequence()
            .filter { it.uploaded && it.id in unresolved }
            .map { it.id }
            .toSet()
        for (targetId in recoveryIds) {
            EntryOperationLocks.withLock(targetId) {
                // The sent snapshot is only a cheap prefilter. Resolve after
                // taking the same lock used by delivery before accounting it.
                val entry = Entries.find(ctx, targetId)
                    ?.takeIf { it.uploaded } ?: return@withLock
                val receipt = try {
                    JSONObject(File(entry.dir, "manifest.json").readText())
                } catch (_: Exception) {
                    return@withLock
                }
                if (receipt.optString("sync_request_id") != record.requestId) return@withLock
                // A crash can occur after queue -> sent but before the normal
                // post-delivery enqueue. Persist metadata work first; only then
                // close the upload accounting window. Repeating either action
                // is safe and the delivery marker routes cloud and LAN rows.
                if (CaptureMetadataStore.hasPendingReviewSync(entry.dir)) {
                    val persisted = try {
                        CaptureMetadataSyncWorker.enqueueExplicitSyncDurably(ctx)
                    } catch (_: Exception) {
                        false
                    }
                    if (!persisted) {
                        schedulingSucceeded = false
                        return@withLock
                    }
                }
                Prefs.markCaptureSynced(ctx, record.requestId, entry.id)
            }
        }
        return schedulingSucceeded
    }

    private suspend fun finishUploadChain(ctx: Context, syncRequestId: String): Result {
        var finishRecord = Prefs.activeCaptureSyncRecord(ctx)?.takeIf {
            it.requestId == syncRequestId
        } ?: run {
            return Result.success(workDataOf(UPLOAD_PROGRESS_STAGE to "superseded"))
        }
        val checkedSkipped = mutableSetOf<String>()
        var pendingIds = CaptureSession(ctx).manualSyncCandidates().map { it.name }
        var state = aggregateCaptureSyncState(finishRecord, pendingIds, pendingIds)
        while (true) {
            // A missing target may be between queue/ and sent/ in a replaced
            // worker. Lock only those missing targets, not the whole backlog:
            // OCR/model work holds the same per-entry lock for much longer.
            val uncheckedTerminal = captureSyncTerminalReconciliationIds(
                finishRecord,
                pendingIds,
            ) - checkedSkipped
            if (uncheckedTerminal.isEmpty()) break
            if (!recoverDeliveredAccounting(
                    ctx,
                    finishRecord,
                    exactTargetIds = uncheckedTerminal,
                )
            ) {
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                return Result.retry()
            }
            checkedSkipped += uncheckedTerminal
            finishRecord = Prefs.activeCaptureSyncRecord(ctx)?.takeIf {
                it.requestId == syncRequestId
            } ?: return Result.success(
                workDataOf(UPLOAD_PROGRESS_STAGE to "superseded"),
            )
            pendingIds = CaptureSession(ctx).manualSyncCandidates().map { it.name }
            state = aggregateCaptureSyncState(finishRecord, pendingIds, pendingIds)
        }
        val hadError = inputData.getBoolean(CHAIN_HAD_ERROR, false)
        val sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false)
        val decision = captureSyncFinishDecision(state, sawDeferred, hadError)
        if (decision == CaptureSyncFinishDecision.WAIT) {
            Prefs.setCaptureSyncPhase(
                ctx,
                syncRequestId,
                if (sawDeferred) CaptureSyncPhase.WAITING_FOR_PROCESSING
                else CaptureSyncPhase.RETRYING,
            )
            val round = inputData.getInt(DEFERRED_ROUND, 0)
            val persisted = continueUploadChain(
                ctx = ctx,
                syncRequestId = syncRequestId,
                cursor = null,
                sawDeferred = false,
                hadError = hadError,
                deferredRound = round + 1,
                delayMs = deferredUploadRecheckDelayMs(round),
            )
            if (!persisted) {
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                return Result.retry()
            }
        } else {
            val terminalPhase = if (
                decision == CaptureSyncFinishDecision.COMPLETE_WITH_ERRORS
            ) {
                CaptureSyncPhase.COMPLETE_WITH_ERRORS
            } else CaptureSyncPhase.COMPLETE
            if (!Prefs.completeCaptureSyncIfUnchanged(ctx, finishRecord, terminalPhase)) {
                // A second press expanded the same active request after the
                // finish decision. Its KEEP enqueue may have found this worker
                // unfinished, so this worker owns persisting the next round.
                val stillActive = Prefs.activeCaptureSyncRecord(ctx)
                    ?.takeIf { it.requestId == syncRequestId }
                    ?: return Result.success(
                        workDataOf(UPLOAD_PROGRESS_STAGE to "superseded"),
                    )
                val persisted = continueUploadChain(
                    ctx = ctx,
                    syncRequestId = stillActive.requestId,
                    cursor = null,
                    sawDeferred = false,
                    hadError = hadError,
                    deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
                )
                if (!persisted) return Result.retry()
                return Result.success(syncResultData(ctx, "continuing"))
            }
            if (decision == CaptureSyncFinishDecision.COMPLETE) {
                Prefs.setLastUploadError(ctx, null)
            }
            if (hasPendingImports(ctx)) scheduleImportPolling(ctx)
            Entries.pruneSent(ctx, ::retainSentEntryLocally)
        }
        val stage = when {
            decision == CaptureSyncFinishDecision.WAIT && sawDeferred -> "waiting-for-processing"
            decision == CaptureSyncFinishDecision.WAIT -> "retrying"
            decision == CaptureSyncFinishDecision.COMPLETE_WITH_ERRORS -> "complete-with-errors"
            else -> "complete"
        }
        return Result.success(syncResultData(ctx, stage))
    }

    private suspend fun retryOrBlockCandidate(
        ctx: Context,
        syncRequestId: String,
        candidate: PendingCapture,
        error: String,
    ): Result {
        if (authorizedSyncRecord(ctx) == null) {
            return Result.success(
                workDataOf(
                    UPLOAD_PROGRESS_STAGE to "superseded",
                    UPLOAD_PROGRESS_ENTRY_ID to candidate.key.entryId,
                ),
            )
        }
        Prefs.setLastUploadError(ctx, error)
        if (shouldRetryCaptureUpload(runAttemptCount)) {
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            setUploadProgress(candidate.key.entryId, "retrying")
            return Result.retry()
        }

        val terminalError = "$error after ${runAttemptCount + 1} attempts"
        Prefs.setLastUploadError(ctx, terminalError)
        Prefs.markCaptureSyncBlocked(ctx, syncRequestId, candidate.key.entryId)
        setUploadProgress(candidate.key.entryId, "blocked")
        val persisted = continueUploadChain(
            ctx = ctx,
            syncRequestId = syncRequestId,
            cursor = candidate.key,
            sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false),
            hadError = true,
            deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
        )
        if (!persisted) {
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            return Result.retry()
        }
        return Result.success(syncResultData(ctx, "blocked", candidate.key.entryId))
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        if (inputData.getBoolean(POLL_ONLY, false)) {
            return@withContext pollImportsOnly(ctx)
        }

        val syncRecord = authorizedSyncRecord(ctx)
            ?: return@withContext Result.success(
                workDataOf(UPLOAD_PROGRESS_STAGE to "manual-sync-required"),
            )
        val syncRequestId = syncRecord.requestId
        Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RUNNING)

        val session = CaptureSession(ctx)
        val cursor = inputCursor()
        if (cursor == null) {
            // Only folders frozen into this user-requested batch are rescued.
            session.recoverOrphans(syncRecord.targetIds)
        }
        // A continuation can deliver and move an entry to sent/ before its
        // metadata work is durably scheduled or its batch accounting closes.
        // Retried continuations retain their cursor, so reconcile sent/
        // before every pending-queue lookup rather than only at chain start.
        if (!recoverDeliveredAccounting(ctx, syncRecord)) {
            Prefs.setCaptureSyncPhase(
                ctx,
                syncRequestId,
                CaptureSyncPhase.RETRYING,
            )
            return@withContext Result.retry()
        }
        val candidate = nextPendingCapture(session, cursor)
            ?: return@withContext finishUploadChain(ctx, syncRequestId)
        setUploadProgress(candidate.key.entryId, "preparing")

        // The batch freezes its transport/destination. Auto resolves once on
        // the first attempt, then later captures cannot silently fall through
        // to a different destination when connectivity changes.
        var resolved = syncRecord.resolvedTransport
        val lan = if (syncRecord.transportMode != "cloud" &&
            syncRecord.lanHost.isNotEmpty()) {
            try { LanClient(ctx, syncRecord.lanHost) } catch (_: Exception) { null }
        } else null
        val readyLan = lan?.takeIf { it.ping() }
        if (resolved.isEmpty() && syncRecord.transportMode == "auto") {
            resolved = if (readyLan != null) "lan" else "cloud"
            resolved = Prefs.resolveCaptureSyncTransport(
                ctx, syncRequestId, resolved,
            ) ?: return@withContext Result.retry()
        }
        if (captureUploadNeedsConnectedHandoff(
                resolved,
                inputData.getBoolean(SCHEDULED_FOR_CONNECTED_NETWORK, false),
            )) {
            // The current WorkSpec was created while Auto was unresolved (or
            // by an older app version), so it cannot be upgraded in place.
            // Re-select this same candidate from a CONNECTED continuation.
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            setUploadProgress(candidate.key.entryId, "waiting-for-network")
            val persisted = continueUploadChain(
                ctx = ctx,
                syncRequestId = syncRequestId,
                cursor = cursor,
                sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false),
                hadError = inputData.getBoolean(CHAIN_HAD_ERROR, false),
                deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
            )
            if (!persisted) {
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                return@withContext Result.retry()
            }
            return@withContext Result.success(
                syncResultData(ctx, "waiting-for-network", candidate.key.entryId),
            )
        }
        if (resolved == "lan") {
            if (readyLan != null) {
                return@withContext uploadOneViaLan(ctx, candidate, readyLan)
            }
            return@withContext retryOrBlockCandidate(
                ctx,
                syncRequestId,
                candidate,
                captureUploadFailureMessage(
                    candidate.key.entryId,
                    IOException("paired desktop could not be authenticated"),
                ),
            )
        }

        if (!Prefs.configured(ctx)) {
            Prefs.setLastUploadError(ctx, "Capture sync is not configured")
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.FAILED)
            return@withContext Result.failure()
        }
        if (!Auth.signedIn(ctx)) {
            return@withContext waitForSignIn(ctx, syncRequestId, candidate.key.entryId)
        }
        val uploadOwner = syncRecord.cloudOwner.ifEmpty { Prefs.userId(ctx) }
        if (uploadOwner != Prefs.userId(ctx)) {
            return@withContext failForAccountChange(
                ctx,
                syncRequestId,
                candidate.key.entryId,
            )
        }
        val client = SupabaseClient(ctx, uploadOwner)
        var transient = false
        var deferredForProcessing = false
        var permanentError: String? = null
        var retryableError: String? = null
        var delivered = false
        var ownershipClaimRejected = false
        candidate.dir.let { dir ->
            try {
                EntryOperationLocks.withLock(dir.name) {
                    if (!dir.isDirectory) return@withLock
                    val entry = Entries.find(ctx, dir.name)
                    val now = System.currentTimeMillis()
                    if (reprocessUploadHoldIsActive(dir, now)) {
                        deferredForProcessing = true
                        return@withLock
                    }
                // Validate before the processing grace period: a damaged,
                // sealed entry needs an actionable error now, not ten minutes
                // later. OCR/meta sidecars may still arrive during the grace.
                val prepared = prepareCapture(ctx, dir)
                when (cloudUploadOwnership(prepared.creator, uploadOwner)) {
                    CloudUploadOwnership.ALLOWED -> Unit
                    CloudUploadOwnership.NEEDS_CLAIM -> throw UploadEntryProblem(
                        "This local book scan is not claimed by an account. " +
                            "Open its details and choose Claim for cloud upload.",
                        reason = UploadEntryProblemReason.NEEDS_CLOUD_CLAIM,
                    )
                    CloudUploadOwnership.DIFFERENT_ACCOUNT -> throw UploadEntryProblem(
                        "This book scan belongs to a different account and was kept on this phone.")
                }
                val canProcess = Prefs.mistralKey(ctx).isNotEmpty()
                val processingCanImprove = entry != null && (
                    entry.meta == null ||
                        (entry.processing.status != Entries.ProcessingStatus.COMPLETE &&
                            entry.processing.retryable)
                    )
                if (entry != null && canProcess && processingCanImprove &&
                    now - entry.createdAt < PROCESS_GRACE_MS) {
                    deferredForProcessing = true
                    return@withLock
                }
                val pendingReviewSync = CaptureMetadataStore.hasPendingReviewSync(dir)
                val delivery = uploadEntry(client, dir, prepared)
                syncInspectMembershipAfterCaptureInsert(ctx, client, dir, uploadOwner)
                markUploaded(ctx, dir, delivery, syncRequestId, uploadOwner)
                // The capture row now exists, so an attention/review edit made
                // before this explicit sync can be pushed instead of racing
                // the pre-upload metadata pass started by the same button.
                if (pendingReviewSync) {
                    val persisted = try {
                        CaptureMetadataSyncWorker.enqueueExplicitSyncDurably(ctx)
                    } catch (_: Exception) {
                        false
                    }
                    if (!persisted) throw UploadEntryProblem(
                        "Capture was delivered, but its review sync could not be scheduled.",
                        retryable = true,
                    )
                } else {
                    CaptureMetadataSyncWorker.enqueueExplicitSync(ctx)
                }
                delivered = true
                }
            } catch (e: UploadEntryProblem) {
                if (e.retryable) {
                    transient = true
                    retryableError = retryableError ?: e.message
                } else {
                    ownershipClaimRejected =
                        e.reason == UploadEntryProblemReason.NEEDS_CLOUD_CLAIM
                    permanentError = permanentError ?: e.message
                }
            } catch (e: SupabaseClient.SignedOut) {
                when (captureTokenFailureAction(
                    Auth.signedIn(ctx),
                    Prefs.userId(ctx),
                    uploadOwner,
                )) {
                    CaptureTokenFailureAction.RETRY -> {
                        val message = "Session refresh was interrupted; capture sync will retry"
                        Prefs.setLastUploadError(ctx, message)
                        Prefs.setCaptureSyncPhase(
                            ctx,
                            syncRequestId,
                            CaptureSyncPhase.RETRYING,
                        )
                        setUploadProgress(candidate.key.entryId, "retrying-session")
                        return@withContext Result.retry()
                    }
                    CaptureTokenFailureAction.WAIT_FOR_SIGN_IN ->
                        return@withContext waitForSignIn(
                            ctx,
                            syncRequestId,
                            candidate.key.entryId,
                        )
                    CaptureTokenFailureAction.ACCOUNT_CHANGED ->
                        return@withContext failForAccountChange(
                            ctx,
                            syncRequestId,
                            candidate.key.entryId,
                        )
                }
            } catch (e: SupabaseClient.HttpException) {
                if (isCaptureSessionRejection(e.code)) {
                    when (captureTokenFailureAction(
                        Auth.signedIn(ctx),
                        Prefs.userId(ctx),
                        uploadOwner,
                    )) {
                        CaptureTokenFailureAction.RETRY -> {
                            Prefs.expireAccessToken(ctx, uploadOwner)
                            val message = "Session was rejected; refreshing before retry"
                            Prefs.setLastUploadError(ctx, message)
                            Prefs.setCaptureSyncPhase(
                                ctx,
                                syncRequestId,
                                CaptureSyncPhase.RETRYING,
                            )
                            setUploadProgress(candidate.key.entryId, "refreshing-session")
                            return@withContext Result.retry()
                        }
                        CaptureTokenFailureAction.WAIT_FOR_SIGN_IN ->
                            return@withContext waitForSignIn(
                                ctx,
                                syncRequestId,
                                candidate.key.entryId,
                            )
                        CaptureTokenFailureAction.ACCOUNT_CHANGED ->
                            return@withContext failForAccountChange(
                                ctx,
                                syncRequestId,
                                candidate.key.entryId,
                            )
                    }
                } else if (permanent(e.code))
                    permanentError = permanentError ?: (e.message?.take(120) ?: "HTTP ${e.code}")
                else {
                    transient = true
                    retryableError = retryableError ?:
                        captureUploadFailureMessage(candidate.key.entryId, e)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                transient = true      // network et al: keep the folder; retry later
                retryableError = retryableError ?:
                    captureUploadFailureMessage(candidate.key.entryId, e)
            }
        }

        if (authorizedSyncRecord(ctx) == null) {
            if (delivered) scheduleImportPolling(ctx)
            return@withContext Result.success(
                workDataOf(
                    UPLOAD_PROGRESS_STAGE to "superseded",
                    UPLOAD_PROGRESS_ENTRY_ID to candidate.key.entryId,
                ),
            )
        }

        if (transient) {
            val error = retryableError
                ?: "Entry ${candidate.key.entryId.take(8)} could not upload"
            Prefs.setLastUploadError(ctx, error)
            if (shouldRetryCaptureUpload(runAttemptCount)) {
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                setUploadProgress(candidate.key.entryId, "retrying")
                return@withContext Result.retry()
            }
            permanentError = "$error after ${runAttemptCount + 1} attempts"
        }

        var ownershipBlockHandled = false
        if (permanentError != null && ownershipClaimRejected) {
            var claimedWhileWorkerWasRunning = false
            EntryOperationLocks.withLock(candidate.key.entryId) {
                claimedWhileWorkerWasRunning = cloudUploadOwnership(
                    readCaptureCreator(ctx, candidate.dir),
                    uploadOwner,
                ) == CloudUploadOwnership.ALLOWED
                if (!claimedWhileWorkerWasRunning) {
                    Prefs.markCaptureSyncBlocked(
                        ctx,
                        syncRequestId,
                        candidate.key.entryId,
                    )
                    ownershipBlockHandled = true
                }
            }
            if (claimedWhileWorkerWasRunning) {
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                setUploadProgress(candidate.key.entryId, "retrying-claimed-capture")
                return@withContext Result.retry()
            }
        }

        val hadError = inputData.getBoolean(CHAIN_HAD_ERROR, false) ||
            permanentError != null
        if (permanentError != null) Prefs.setLastUploadError(ctx, permanentError)
        if (delivered) scheduleImportPolling(ctx)
        val stage = when {
            permanentError != null -> "blocked"
            deferredForProcessing -> "waiting-for-processing"
            delivered -> "delivered"
            else -> "skipped"
        }
        when {
            delivered -> Prefs.markCaptureSynced(ctx, syncRequestId, candidate.key.entryId)
            permanentError != null && !ownershipBlockHandled ->
                Prefs.markCaptureSyncBlocked(ctx, syncRequestId, candidate.key.entryId)
            deferredForProcessing -> Prefs.setCaptureSyncPhase(
                ctx,
                syncRequestId,
                CaptureSyncPhase.WAITING_FOR_PROCESSING,
            )
        }
        setUploadProgress(candidate.key.entryId, stage)
        val persisted = continueUploadChain(
            ctx = ctx,
            syncRequestId = syncRequestId,
            cursor = candidate.key,
            sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false) ||
                deferredForProcessing,
            hadError = hadError,
            deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
        )
        if (!persisted) {
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            return@withContext Result.retry()
        }
        Result.success(syncResultData(ctx, stage, candidate.key.entryId))
    }

    private data class PreparedCapture(
        val manifest: JSONObject,
        val id: String,
        val creator: CaptureCreator,
        val photoAssets: JSONObject,
        val captureNotes: JSONObject?,
        val photos: List<ValidatedPhoto>,
    )

    private fun prepareCapture(ctx: Context, dir: File): PreparedCapture {
        val manifestFile = File(dir, "manifest.json")
        val manifest = try {
            if (!manifestFile.isFile) {
                throw UploadEntryProblem(
                    "Entry ${dir.name.take(8)} is missing its upload information; " +
                        "it was kept pending. Open Recent to discard and recapture.")
            }
            JSONObject(manifestFile.readText())
        } catch (e: UploadEntryProblem) {
            throw e
        } catch (e: JSONException) {
            throw UploadEntryProblem(
                "Entry ${dir.name.take(8)} has damaged upload information; its photos were " +
                    "kept pending. Open Recent to discard and recapture.",
                cause = e,
            )
        } catch (e: IOException) {
            throw UploadEntryProblem(
                "Entry ${dir.name.take(8)} could not be read locally; it remains pending " +
                    "and will retry.",
                retryable = true,
                cause = e,
            )
        }

        val id: String
        val names: List<String>
        try {
            id = manifest.getString("id")
            val array = manifest.getJSONArray("photos")
            names = (0 until array.length()).map { index -> array.getString(index) }
        } catch (e: JSONException) {
            throw UploadEntryProblem(
                "Entry ${dir.name.take(8)} has damaged upload information; its photos were " +
                    "kept pending. Open Recent to discard and recapture.",
                cause = e,
            )
        }
        if (id != dir.name || !id.matches(Regex("[A-Za-z0-9._-]+")) || id == "." || id == "..") {
            throw UploadEntryProblem(
                "Entry ${dir.name.take(8)} has inconsistent upload information; it was kept " +
                    "pending. Open Recent to discard and recapture.")
        }
        val displayPhotos = validateUploadPhotos(dir, names)
        // Upload is the final local boundary before another system sees the
        // capture. Complete any provisional checksums/dimensions and refresh
        // the embedded manifest snapshot without changing legacy photo paths.
        PhotoAssetStore.completeForUpload(dir, displayPhotos.map { it.file })
        val photoAssets = PhotoAssetStore.payload(dir, manifest)
        manifest.put(PHOTO_ASSETS_MANIFEST_KEY, photoAssets)
        val transportPhotos = selectTransportOriginals(dir, displayPhotos)
        val outboundPhotoAssets = originalTransportPayload(photoAssets)
        val captureNotes = CaptureNotes.payload(dir, manifest)
            .takeIf(CaptureNotes::hasNotes)
        return PreparedCapture(
            manifest,
            id,
            captureCreatorFromManifest(manifest, Prefs.anonymousCreatorId(ctx)),
            outboundPhotoAssets,
            captureNotes,
            transportPhotos,
        )
    }

    private fun uploadEntry(
        client: SupabaseClient,
        dir: File,
        prepared: PreparedCapture,
    ): ConfirmedDelivery {
        val manifest = prepared.manifest
        val id = prepared.id
        val device = manifest.optString("device", "phone")
        val deviceSafe = device.replace(Regex("[^A-Za-z0-9._-]"), "_")
            .trim('.').ifEmpty { "phone" }        // "." / ".." would bend the URL path
        val ocr = JSONObject()
        for (photo in prepared.photos) {
            File(dir, "${photo.name}.txt").takeIf { it.isFile }
                ?.let { ocr.put(photo.name, it.readText().take(20_000)) }
        }
        val createdMs = manifest.optLong("created_at", 0L)
        val createdAt = if (createdMs > 0) Instant.ofEpochMilli(createdMs).toString() else ""
        val meta = attachCaptureNotes(withProvenance(File(dir, "meta.json").takeIf { it.isFile }
            ?.let { try { JSONObject(it.readText()) } catch (_: Exception) { null } }
            ?: JSONObject(), dir), prepared.captureNotes)
            .put(PHOTO_ASSETS_META_KEY, prepared.photoAssets)
        return deliverValidatedCapture(
            entryId = id,
            deviceFolder = deviceSafe,
            photos = prepared.photos,
            uploadPhoto = client::uploadPhoto,
            insertRecord = { remote ->
                client.insertCapture(
                    id, device, remote, manifest.optString("note", ""),
                    createdAt, ocr, meta)
            },
        )
    }

    /**
     * A move in Inspect changes current organization, never the immutable
     * capture-time provenance embedded in the upload payload. If that move was
     * made while the capture was still local-only, apply its durable overlay as
     * soon as the capture row exists. A failed RPC leaves the queue entry in
     * place; the next idempotent upload retry can finish the membership update.
     */
    private fun syncInspectMembershipAfterCaptureInsert(
        ctx: Context,
        client: SupabaseClient,
        dir: File,
        uploadOwner: String,
    ) {
        val owner = uploadOwner.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) {
            throw IOException("capture collection owner is invalid")
        }
        repeat(8) {
            val stored = InspectBookMemberships.read(ctx)
            if (!stored.valid) {
                throw IOException("capture collection outbox could not be read")
            }
            var membership = stored.memberships[dir.name] ?: return
            if (membership.cloudOwnerId.isNotEmpty() &&
                !membership.cloudOwnerId.equals(owner, ignoreCase = true)
            ) {
                throw IOException("capture collection outbox belongs to another account")
            }
            val requestedCollectionId = membership.collectionId.ifEmpty {
                readProvenance(dir)?.collectionId.orEmpty()
            }
            val targetCollectionId = resolvedLiveCollectionId(
                requestedCollectionId,
                Collections.allRecords(ctx),
            )
            if (targetCollectionId == null) {
                if (membership.removed) {
                    // The destination is immaterial for a tombstone, but the
                    // server RPC requires a live one. Retain the intent until a
                    // merge/restoration makes it resolvable; clearing it would
                    // let immutable capture provenance resurrect the book.
                    throw IOException("deleted capture has no live collection")
                }
                when (InspectBookMemberships.compareAndSet(
                    ctx,
                    dir.name,
                    membership,
                    replacement = null,
                )) {
                    InspectMembershipCompareResult.UPDATED -> return
                    InspectMembershipCompareResult.CHANGED -> return@repeat
                    InspectMembershipCompareResult.FAILED ->
                        throw IOException(
                            "obsolete capture collection membership could not be cleared",
                        )
                }
            }

            if (membership.collectionId != targetCollectionId) {
                val resolved = membership.copy(collectionId = targetCollectionId)
                when (InspectBookMemberships.compareAndSet(
                    ctx,
                    dir.name,
                    membership,
                    resolved,
                )) {
                    InspectMembershipCompareResult.UPDATED -> membership = resolved
                    InspectMembershipCompareResult.CHANGED -> return@repeat
                    InspectMembershipCompareResult.FAILED ->
                        throw IOException("merged capture collection membership could not be saved")
                }
            }

            val owned = membership.copy(cloudOwnerId = owner)
            if (owned != membership) {
                when (InspectBookMemberships.compareAndSet(
                    ctx,
                    dir.name,
                    membership,
                    owned,
                )) {
                    InspectMembershipCompareResult.UPDATED -> membership = owned
                    InspectMembershipCompareResult.CHANGED -> return@repeat
                    InspectMembershipCompareResult.FAILED ->
                        throw IOException("capture collection outbox owner could not be saved")
                }
            }

            val accepted = client.mutateCaptureCollection(
                captureIds = setOf(dir.name),
                collectionId = targetCollectionId,
                removed = membership.removed,
            )
            if (accepted != setOf(dir.name)) {
                throw IOException("capture collection membership was not accepted")
            }
            val latest = InspectBookMemberships.read(ctx)
            if (!latest.valid) {
                throw IOException("capture collection outbox could not be verified")
            }
            val latestMembership = latest.memberships[dir.name]
            if (latestMembership == null || latestMembership == membership) return
            // A newer UI action won the compare-and-set race. Apply that intent
            // before the upload can commit locally and leave it stranded.
        }
        throw IOException("capture collection membership changed too often")
    }

    /** queue/<id> -> sent/<id>, stamped; the recent list's "uploaded". */
    private fun markUploaded(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
        syncRequestId: String,
        cloudOwnerId: String,
    ) {
        markDelivered(
            ctx,
            dir,
            delivery,
            "pending",
            syncRequestId,
            "cloud",
            cloudOwnerId,
        )
    }

    private fun markDelivered(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
        cloudStatus: String,
        syncRequestId: String,
        deliveryTransport: String,
        cloudOwnerId: String = "",
    ) {
        check(delivery.entryId == dir.name && delivery.photoCount > 0) {
            "delivery receipt does not match local entry"
        }
        try {
            delivery.captureLibConfirmation?.let { confirmation ->
                when (CaptureLibAssociationStore.apply(dir, confirmation)) {
                    CaptureLibApplyResult.APPLIED, CaptureLibApplyResult.UNCHANGED -> Unit
                    CaptureLibApplyResult.STALE, CaptureLibApplyResult.CONFLICT ->
                        throw IOException("archive confirmation conflicts with local state")
                }
            }
            val manifestFile = File(dir, "manifest.json")
            val manifest = stampDeliveryManifest(
                manifest = JSONObject(manifestFile.readText()),
                uploadedAt = System.currentTimeMillis(),
                cloudStatus = cloudStatus,
                syncRequestId = syncRequestId,
                deliveryTransport = deliveryTransport,
                cloudOwnerId = cloudOwnerId,
            )
            Entries.atomicWrite(manifestFile, manifest.toString())
            val target = File(Entries.sentRoot(ctx), dir.name)
            if (!dir.renameTo(target)) {
                throw IOException("could not move entry into sent history")
            }
        } catch (e: Exception) {
            throw UploadEntryProblem(
                "Entry ${dir.name.take(8)} was accepted, but its local status could not be " +
                    "saved. It remains pending and will retry safely.",
                retryable = true,
                cause = e,
            )
        }
    }

    // --- LAN transport ----------------------------------------------------------

    /** POST each queued entry to the paired desktop, which imports synchronously
     *  — a 200 IS "imported", so there is nothing to poll afterwards. No signed-in
     *  account or grace wait is needed: the desktop does its own OCR on ingest. */
    private suspend fun uploadOneViaLan(
        ctx: Context,
        candidate: PendingCapture,
        client: LanClient,
    ): Result {
        val syncRequestId = inputSyncRequestId()
        var transient = false
        var deferredForProcessing = false
        var permanentError: String? = null
        var retryableError: String? = null
        var delivered = false
        candidate.dir.let { dir ->
            try {
                EntryOperationLocks.withLock(dir.name) {
                    if (!dir.isDirectory) return@withLock
                    if (reprocessUploadHoldIsActive(dir)) {
                        deferredForProcessing = true
                        return@withLock
                    }
                    setUploadProgress(candidate.key.entryId, "uploading")
                    val pendingReviewSync = CaptureMetadataStore.hasPendingReviewSync(dir)
                    val delivery = uploadEntryLan(client, dir, prepareCapture(ctx, dir))
                    markSentImported(ctx, dir, delivery, syncRequestId)
                    // The initial multipart carried the review snapshot, but
                    // the paired desktop's canonical revision is returned by
                    // /lan/metadata. Queue this only after queue -> sent so the
                    // worker can find and acknowledge the durable sidecar.
                    if (pendingReviewSync) {
                        val persisted = try {
                            CaptureMetadataSyncWorker.enqueueExplicitSyncDurably(ctx)
                        } catch (_: Exception) {
                            false
                        }
                        if (!persisted) throw UploadEntryProblem(
                            "Capture was delivered, but its review sync could not be scheduled.",
                            retryable = true,
                        )
                    } else {
                        CaptureMetadataSyncWorker.enqueueExplicitSync(ctx)
                    }
                    delivered = true
                }
            } catch (e: UploadEntryProblem) {
                if (e.retryable) {
                    transient = true
                    retryableError = retryableError ?: e.message
                } else {
                    permanentError = permanentError ?: e.message
                }
            } catch (e: LanClient.HttpException) {
                if (permanent(e.code))
                    permanentError = permanentError ?: (e.message?.take(120) ?: "HTTP ${e.code}")
                else {
                    transient = true
                    retryableError = retryableError ?:
                        captureUploadFailureMessage(candidate.key.entryId, e)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                transient = true                              // desktop unreachable: retry
                retryableError = retryableError ?:
                    captureUploadFailureMessage(candidate.key.entryId, e)
            }
        }

        if (authorizedSyncRecord(ctx) == null) {
            return Result.success(
                workDataOf(
                    UPLOAD_PROGRESS_STAGE to "superseded",
                    UPLOAD_PROGRESS_ENTRY_ID to candidate.key.entryId,
                ),
            )
        }

        if (transient) {
            val error = retryableError
                ?: "Entry ${candidate.key.entryId.take(8)} could not upload"
            Prefs.setLastUploadError(ctx, error)
            if (shouldRetryCaptureUpload(runAttemptCount)) {
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                setUploadProgress(candidate.key.entryId, "retrying")
                return Result.retry()
            }
            permanentError = "$error after ${runAttemptCount + 1} attempts"
        }

        val hadError = inputData.getBoolean(CHAIN_HAD_ERROR, false) ||
            permanentError != null
        if (permanentError != null) Prefs.setLastUploadError(ctx, permanentError)
        val stage = when {
            permanentError != null -> "blocked"
            deferredForProcessing -> "waiting-for-processing"
            delivered -> "delivered"
            else -> "skipped"
        }
        when {
            delivered -> Prefs.markCaptureSynced(ctx, syncRequestId, candidate.key.entryId)
            permanentError != null ->
                Prefs.markCaptureSyncBlocked(ctx, syncRequestId, candidate.key.entryId)
            deferredForProcessing -> Prefs.setCaptureSyncPhase(
                ctx,
                syncRequestId,
                CaptureSyncPhase.WAITING_FOR_PROCESSING,
            )
        }
        setUploadProgress(candidate.key.entryId, stage)
        val persisted = continueUploadChain(
            ctx = ctx,
            syncRequestId = syncRequestId,
            cursor = candidate.key,
            sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false) ||
                deferredForProcessing,
            hadError = hadError,
            deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
        )
        if (!persisted) {
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            return Result.retry()
        }
        return Result.success(syncResultData(ctx, stage, candidate.key.entryId))
    }

    private fun uploadEntryLan(
        client: LanClient,
        dir: File,
        prepared: PreparedCapture,
    ): ConfirmedDelivery {
        val manifest = prepared.manifest
        val id = prepared.id
        val device = manifest.optString("device", "phone")
        val photos = prepared.photos.map { it.name to it.file }
        val ocr = JSONObject()
        for (photo in prepared.photos) {
            File(dir, "${photo.name}.txt").takeIf { it.isFile }
                ?.let { ocr.put(photo.name, it.readText().take(20_000)) }
        }
        val createdMs = manifest.optLong("created_at", 0L)
        val createdAt = if (createdMs > 0) Instant.ofEpochMilli(createdMs).toString() else ""
        val meta = attachCaptureNotes(withProvenance(File(dir, "meta.json").takeIf { it.isFile }
            ?.let { try { JSONObject(it.readText()) } catch (_: Exception) { null } }
            ?: JSONObject(), dir), prepared.captureNotes)
        val captureReview = CaptureMetadataStore.readReview(dir)?.current
            ?.let(::captureReviewLanBody)
        val confirmation = client.uploadCapture(
            id,
            device,
            manifest.optString("note", ""),
            createdAt,
            ocr,
            meta,
            prepared.photoAssets,
            captureReview,
            photos,
        )
        return ConfirmedDelivery(
            id,
            photos.size,
            photos.map { it.first },
            confirmation,
        )
    }

    /** Read the entry's frozen provenance and fold it into the outgoing meta —
     *  from the sidecar, which an override keeps current, not from meta.json,
     *  which reprocessing rewrites. */
    private fun withProvenance(meta: JSONObject, dir: File): JSONObject =
        applyProvenanceToPayload(meta, readProvenance(dir))

    /** queue/<id> -> sent/<id>, marked imported (LAN import is synchronous). */
    private fun markSentImported(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
        syncRequestId: String,
    ) {
        markDelivered(ctx, dir, delivery, "imported", syncRequestId, "lan")
    }

    private fun hasPendingImportOrPhotoWork(entry: Entries.Entry): Boolean = runCatching {
        sentEntryNeedsLocalRetention(
            entry.cloudStatus,
            cloudPhotoWorkPending(PhotoAssetStore.read(entry.dir)),
        )
    }.getOrDefault(true)

    /** A user-requested reprocess holds the browsing copy in place until it
     * finishes. Delivery already defers to this marker; retention must too, or
     * an explicit retry can have the directory archived out from under it
     * between the request and the worker run. */
    private fun retainSentEntryLocally(entry: Entries.Entry): Boolean = runCatching {
        entry.reprocessPending() ||
            CaptureMetadataStore.hasPendingReviewSync(entry.dir) ||
            hasPendingImportOrPhotoWork(entry)
    }.getOrDefault(true)

    private fun hasPendingImports(ctx: Context): Boolean =
        Entries.recent(ctx).any { it.uploaded && hasPendingImportOrPhotoWork(it) }

    /** A delayed poll never drains uploads or changes upload-error state. Its
     * only job is to synchronize already-sent cloud rows. The persisted chain
     * provides later attempts, so a failed cosmetic poll may finish normally. */
    private suspend fun pollImportsOnly(ctx: Context): Result {
        if (Prefs.configured(ctx) && Auth.signedIn(ctx)) {
            try {
                pollImports(ctx, SupabaseClient(ctx))
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                // Known-terminal entries are still safe to prune below. Any
                // unknown status remains protected for the next bounded poll.
            }
        }
        Entries.pruneSent(ctx, ::retainSentEntryLocally)
        return Result.success()
    }

    /** Ask the cloud whether the desktop has imported what we sent. Returns
     * true while at least one local sent row still needs a later poll. */
    private suspend fun pollImports(ctx: Context, client: SupabaseClient): Boolean {
        val sent = Entries.recent(ctx).filter { it.uploaded }
        if (sent.isEmpty()) return false
        val ownerId = Prefs.userId(ctx)
        var cloudQueryFailed = false
        val jobs = try {
            client.photoProcessingJobs(sent.map { it.id })
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            cloudQueryFailed = true
            emptyList()
        }
        val entriesById = sent.associateBy { it.id }
        for (job in jobs) {
            if (job.ownerId != ownerId || Prefs.userId(ctx) != ownerId) continue
            val entry = entriesById[job.captureId] ?: continue
            syncCloudPhotoJob(ctx, client, entry, job, ownerId)
        }

        val waitingForImport = sent.filter { isRemoteImportPending(it.cloudStatus) }
        var importQueryFailed = false
        val importStates = if (waitingForImport.isEmpty()) emptyMap() else try {
            client.captureImportStates(waitingForImport.map { it.id })
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            importQueryFailed = true
            emptyMap()
        }
        for (entry in waitingForImport) {
            val remote = importStates[entry.id] ?: continue
            val status = normalizeRemoteImportStatus(remote.status)
            if (status == normalizeRemoteImportStatus(entry.cloudStatus) &&
                remote.confirmation == null
            ) continue
            try {
                EntryOperationLocks.withLock(entry.id) {
                    val latest = Entries.find(ctx, entry.id) ?: return@withLock
                    applyCaptureImportState(latest.dir, remote)
                }
            } catch (_: Exception) { }
        }
        return cloudQueryFailed || importQueryFailed || hasPendingImports(ctx)
    }

    private suspend fun syncCloudPhotoJob(
        ctx: Context,
        client: SupabaseClient,
        entry: Entries.Entry,
        job: CloudPhotoProcessingJob,
        ownerId: String,
    ) {
        if (job.state != "completed") {
            EntryOperationLocks.withLock(entry.id) {
                if (entry.dir.isDirectory) PhotoAssetStore.recordCloudJobState(entry.dir, job)
            }
            return
        }

        val decision = EntryOperationLocks.withLock(entry.id) {
            if (!entry.dir.isDirectory) CloudResultDecision.NotApplicable
            else validateCloudPhotoResult(PhotoAssetStore.read(entry.dir), job, ownerId)
        }
        when (decision) {
            CloudResultDecision.NotApplicable,
            CloudResultDecision.Superseded -> return
            is CloudResultDecision.Rejected -> {
                recordCloudResultFailure(
                    entry,
                    job,
                    "Cloud result failed verification: ${decision.reason}",
                )
                return
            }
            is CloudResultDecision.Ready -> {
                val alreadyInstalled = EntryOperationLocks.withLock(entry.id) {
                    entry.dir.isDirectory && PhotoAssetStore.hasVerifiedCloudDisplay(
                        entry.dir,
                        decision.plan,
                    )
                }
                if (!alreadyInstalled) downloadAndInstallCloudDisplay(
                    ctx,
                    client,
                    entry,
                    decision.plan,
                        ownerId,
                    )
                // Nonlinear page-curvature correction cannot transform old
                // polygons with a homography. Schedule OCR against only the
                // verified display derivative; its durable marker survives a
                // missing key, process death, and exhausted transient retries.
                CloudDisplayReocrWorker.enqueuePending(ctx, entry.id)
            }
        }
    }

    private suspend fun downloadAndInstallCloudDisplay(
        ctx: Context,
        client: SupabaseClient,
        entry: Entries.Entry,
        plan: CloudDisplayInstallPlan,
        ownerId: String,
    ) {
        val temporary = try {
            EntryOperationLocks.withLock(entry.id) {
                if (!entry.dir.isDirectory) null else File.createTempFile(
                    ".cloud-${plan.job.id.take(12)}-",
                    ".part",
                    entry.dir,
                )
            }
        } catch (_: Exception) {
            recordCloudInstallRetry(entry, plan.job, "Cloud display could not be staged")
            return
        } ?: return
        try {
            val receipt = client.downloadPrivateObject(
                plan.artifact.bucket,
                plan.artifact.path,
                temporary,
                plan.artifact.bytes.coerceAtMost(MAX_CLOUD_DERIVATIVE_BYTES),
            )
            val invalid = verifyCloudDisplayDownload(
                temporary,
                plan.artifact,
                receipt.contentType,
                receipt.bytes,
            )
            if (invalid != null) {
                recordCloudResultFailure(
                    entry,
                    plan.job,
                    "Cloud display failed verification: $invalid",
                )
                return
            }
            if (Prefs.userId(ctx) != ownerId) throw SupabaseClient.AccountChanged()
            val installed = EntryOperationLocks.withLock(entry.id) {
                entry.dir.isDirectory && PhotoAssetStore.installCloudDisplayDerivative(
                    entry.dir,
                    plan,
                    temporary,
                    receipt,
                )
            }
            if (!installed) {
                recordCloudInstallRetry(entry, plan.job, "Cloud display could not be saved")
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: SupabaseClient.ObjectTooLarge) {
            recordCloudResultFailure(
                entry,
                plan.job,
                "Cloud display failed verification: artifact size",
            )
        } catch (e: SupabaseClient.SignedOut) {
            throw e
        } catch (e: SupabaseClient.AccountChanged) {
            throw e
        } catch (e: SupabaseClient.HttpException) {
            if (permanent(e.code)) {
                recordCloudResultFailure(
                    entry,
                    plan.job,
                    "Cloud display download was rejected (HTTP ${e.code})",
                )
            } else {
                recordCloudInstallRetry(entry, plan.job)
            }
        } catch (_: Exception) {
            // HTTP/network/storage availability can recover; the completed job
            // remains queryable and the bounded poll chain will try again.
            recordCloudInstallRetry(entry, plan.job)
        } finally {
            temporary.delete()
        }
    }

    private suspend fun recordCloudInstallRetry(
        entry: Entries.Entry,
        job: CloudPhotoProcessingJob,
        error: String = "Cloud display download will retry",
    ) {
        EntryOperationLocks.withLock(entry.id) {
            if (entry.dir.isDirectory) {
                PhotoAssetStore.recordCloudInstallRetry(entry.dir, job, error)
            }
        }
    }

    private suspend fun recordCloudResultFailure(
        entry: Entries.Entry,
        job: CloudPhotoProcessingJob,
        error: String,
    ) {
        EntryOperationLocks.withLock(entry.id) {
            if (entry.dir.isDirectory) {
                PhotoAssetStore.recordCloudResultFailure(entry.dir, job, error)
            }
        }
    }
}
