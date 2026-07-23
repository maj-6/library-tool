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
    cause: Throwable? = null,
) : IOException(message, cause)

internal data class ValidatedPhoto(val name: String, val file: File)

internal data class ConfirmedDelivery(
    val entryId: String,
    val photoCount: Int,
    val remotePaths: List<String>,
)

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
        private const val PROCESS_GRACE_MS = 10 * 60 * 1000L
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
                    if (start.created) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
                    request(start.record.requestId),
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
                    request(active.requestId),
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
                request(active.requestId),
            )
        }

        private fun request(
            syncRequestId: String,
            cursor: UploadQueueKey? = null,
            sawDeferred: Boolean = false,
            hadError: Boolean = false,
            deferredRound: Int = 0,
            delayMs: Long = 0,
        ): OneTimeWorkRequest {
            val builder = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(
                    SYNC_REQUEST_ID to syncRequestId,
                    HAS_CURSOR to (cursor != null),
                    CURSOR_CREATED_AT to (cursor?.createdAt ?: 0L),
                    CURSOR_ENTRY_ID to cursor?.entryId.orEmpty(),
                    CHAIN_SAW_DEFERRED to sawDeferred,
                    CHAIN_HAD_ERROR to hadError,
                    DEFERRED_ROUND to deferredRound,
                ))
                .setConstraints(
                    // LAN Wi-Fi may have no validated Internet capability.
                    // Transport probes below provide retry semantics for both
                    // local and cloud destinations.
                    Constraints.Builder().setRequiredNetworkType(
                        NetworkType.NOT_REQUIRED,
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
        ): Boolean = try {
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                EXPLICIT_SYNC_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request(
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
        val targets = authorizedSyncRecord(applicationContext)?.targetIds.orEmpty()
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

    private suspend fun recoverDeliveredAccounting(
        ctx: Context,
        record: CaptureSyncRecord,
    ): Boolean {
        var schedulingSucceeded = true
        for (snapshot in Entries.recent(ctx)) {
            if (!snapshot.uploaded || snapshot.id !in record.targetIds ||
                snapshot.id in record.syncedIds) continue
            EntryOperationLocks.withLock(snapshot.id) {
                val entry = Entries.find(ctx, snapshot.id)
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
        val hadError = inputData.getBoolean(CHAIN_HAD_ERROR, false)
        val sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false)
        if (sawDeferred) {
            Prefs.setCaptureSyncPhase(
                ctx,
                syncRequestId,
                CaptureSyncPhase.WAITING_FOR_PROCESSING,
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
            if (!hadError) Prefs.setLastUploadError(ctx, null)
            if (hasPendingImports(ctx)) scheduleImportPolling(ctx) else Entries.pruneSent(ctx)
            Prefs.setCaptureSyncPhase(
                ctx,
                syncRequestId,
                if (hadError) CaptureSyncPhase.COMPLETE_WITH_ERRORS
                else CaptureSyncPhase.COMPLETE,
            )
        }
        val stage = if (sawDeferred) "waiting-for-processing" else "complete"
        return Result.success(syncResultData(ctx, stage))
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
        val lanReady = lan?.ping() == true
        if (resolved.isEmpty() && syncRecord.transportMode == "auto") {
            resolved = if (lanReady) "lan" else "cloud"
            resolved = Prefs.resolveCaptureSyncTransport(
                ctx, syncRequestId, resolved,
            ) ?: return@withContext Result.retry()
        }
        if (resolved == "lan") {
            if (lanReady && lan != null) {
                return@withContext uploadOneViaLan(ctx, candidate, lan)
            }
            run {
                Prefs.setLastUploadError(ctx, "paired desktop could not be authenticated")
                Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
                setUploadProgress(candidate.key.entryId, "retrying")
                return@withContext Result.retry()
            }
        }

        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) {
            Prefs.setLastUploadError(ctx, if (Auth.signedIn(ctx)) null else "signed out")
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.FAILED)
            return@withContext Result.failure()
        }
        val uploadOwner = syncRecord.cloudOwner.ifEmpty { Prefs.userId(ctx) }
        if (uploadOwner != Prefs.userId(ctx)) {
            Prefs.setLastUploadError(ctx, "capture sync belongs to a different account")
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.FAILED)
            return@withContext Result.failure()
        }
        val client = SupabaseClient(ctx, uploadOwner)
        var transient = false
        var deferredForProcessing = false
        var permanentError: String? = null
        var retryableError: String? = null
        var delivered = false
        val now = System.currentTimeMillis()

        candidate.dir.let { dir ->
            try {
                EntryOperationLocks.withLock(dir.name) {
                    if (!dir.isDirectory) return@withLock
                    val entry = Entries.find(ctx, dir.name)
                    if (File(dir, Entries.REPROCESS_PENDING).isFile) {
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
                            "Open its details and choose Claim for cloud upload.")
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
                markUploaded(ctx, dir, delivery, syncRequestId)
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
                    permanentError = permanentError ?: e.message
                }
            } catch (e: SupabaseClient.SignedOut) {
                permanentError = permanentError ?: "signed out"
            } catch (e: SupabaseClient.HttpException) {
                if (permanent(e.code))
                    permanentError = permanentError ?: (e.message?.take(120) ?: "HTTP ${e.code}")
                else transient = true
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                transient = true      // network et al: keep the folder; retry later
            }
        }

        if (transient) {
            retryableError?.let { Prefs.setLastUploadError(ctx, it) }
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            setUploadProgress(candidate.key.entryId, "retrying")
            return@withContext Result.retry()
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

    /** queue/<id> -> sent/<id>, stamped; the recent list's "uploaded". */
    private fun markUploaded(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
        syncRequestId: String,
    ) {
        markDelivered(ctx, dir, delivery, "pending", syncRequestId, "cloud")
    }

    private fun markDelivered(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
        cloudStatus: String,
        syncRequestId: String,
        deliveryTransport: String,
    ) {
        check(delivery.entryId == dir.name && delivery.photoCount > 0) {
            "delivery receipt does not match local entry"
        }
        try {
            val manifestFile = File(dir, "manifest.json")
            val manifest = JSONObject(manifestFile.readText())
                .put("uploaded_at", System.currentTimeMillis())
                .put("cloud_status", cloudStatus)
                .put("sync_request_id", syncRequestId)
                .put("delivery_transport", deliveryTransport)
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
                    if (File(dir, Entries.REPROCESS_PENDING).isFile) {
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
                else transient = true
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                transient = true                              // desktop unreachable: retry
            }
        }

        if (transient) {
            retryableError?.let { Prefs.setLastUploadError(ctx, it) }
            Prefs.setCaptureSyncPhase(ctx, syncRequestId, CaptureSyncPhase.RETRYING)
            setUploadProgress(candidate.key.entryId, "retrying")
            return Result.retry()
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
        client.uploadCapture(id, device, manifest.optString("note", ""),
                             createdAt, ocr, meta,
                             prepared.photoAssets, captureReview, photos)
        return ConfirmedDelivery(id, photos.size, photos.map { it.first })
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

    private fun hasPendingImports(ctx: Context): Boolean = Entries.recent(ctx).any { entry ->
        entry.uploaded && (
            isRemoteImportPending(entry.cloudStatus) ||
                cloudPhotoWorkPending(PhotoAssetStore.read(entry.dir))
            )
    }

    /** A delayed poll never drains uploads or changes upload-error state. Its
     * only job is to synchronize already-sent cloud rows. The persisted chain
     * provides later attempts, so a failed cosmetic poll may finish normally. */
    private suspend fun pollImportsOnly(ctx: Context): Result {
        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) return Result.success()
        val waiting = try {
            pollImports(ctx, SupabaseClient(ctx))
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            true
        }
        if (!waiting) Entries.pruneSent(ctx)
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
        val statuses = if (waitingForImport.isEmpty()) emptyMap() else try {
            client.captureStatuses(waitingForImport.map { it.id })
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            importQueryFailed = true
            emptyMap()
        }
        for (entry in waitingForImport) {
            val status = statuses[entry.id]?.let(::normalizeRemoteImportStatus) ?: continue
            if (status == normalizeRemoteImportStatus(entry.cloudStatus)) continue
            try {
                EntryOperationLocks.withLock(entry.id) {
                    val manifestFile = File(entry.dir, "manifest.json")
                    if (!manifestFile.isFile) return@withLock
                    Entries.atomicWrite(
                        manifestFile,
                        JSONObject(manifestFile.readText())
                            .put("cloud_status", status)
                            .toString(),
                    )
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
