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
 * Uploads one sealed capture per WorkManager invocation and persists a serial
 * cursor continuation for the next capture. Photos go to the `captures`
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
        private const val UPLOAD_WORK = "capture-upload"
        private const val IMPORT_POLL_WORK = "capture-import-poll"
        private const val POLL_ONLY = "poll-only"
        private const val HAS_CURSOR = "upload-has-cursor"
        private const val CURSOR_CREATED_AT = "upload-cursor-created-at"
        private const val CURSOR_ENTRY_ID = "upload-cursor-entry-id"
        private const val CHAIN_SAW_DEFERRED = "upload-chain-saw-deferred"
        private const val CHAIN_HAD_ERROR = "upload-chain-had-error"
        private const val DEFERRED_ROUND = "upload-deferred-round"

        /** Fresh run now, fresh backoff clock. REPLACE cancels a chain that
         *  may be hours into exponential backoff — right after "done" or a
         *  settings fix, waiting that out is wrong. Safe because every run
         *  resumes from local folders and uploads are upsert-idempotent. */
        fun enqueue(ctx: Context) {
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork(UPLOAD_WORK, ExistingWorkPolicy.REPLACE, request())
        }

        /** Opportunistic nudge (onResume, post-processing): starts a run only
         *  if none is queued, never resets a live chain's backoff. */
        fun kick(ctx: Context) {
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork(UPLOAD_WORK, ExistingWorkPolicy.KEEP, request())
        }

        private fun request(
            cursor: UploadQueueKey? = null,
            sawDeferred: Boolean = false,
            hadError: Boolean = false,
            deferredRound: Int = 0,
            delayMs: Long = 0,
        ): OneTimeWorkRequest {
            val builder = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(
                    HAS_CURSOR to (cursor != null),
                    CURSOR_CREATED_AT to (cursor?.createdAt ?: 0L),
                    CURSOR_ENTRY_ID to cursor?.entryId.orEmpty(),
                    CHAIN_SAW_DEFERRED to sawDeferred,
                    CHAIN_HAD_ERROR to hadError,
                    DEFERRED_ROUND to deferredRound,
                ))
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            if (delayMs > 0) builder.setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
            return builder.build()
        }

        private fun continueUploadChain(
            ctx: Context,
            cursor: UploadQueueKey?,
            sawDeferred: Boolean,
            hadError: Boolean,
            deferredRound: Int,
            delayMs: Long = 0,
        ): Boolean = try {
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                UPLOAD_WORK,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request(cursor, sawDeferred, hadError, deferredRound, delayMs),
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
        private fun permanent(code: Int) = code in 400..499 && code != 408 && code != 429
    }

    private data class PendingCapture(val key: UploadQueueKey, val dir: File)

    private fun inputCursor(): UploadQueueKey? =
        if (!inputData.getBoolean(HAS_CURSOR, false)) null
        else UploadQueueKey(
            inputData.getLong(CURSOR_CREATED_AT, 0L),
            inputData.getString(CURSOR_ENTRY_ID).orEmpty(),
        )

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
        val byKey = session.pendingUploads().associateBy(::queueKey)
        val next = nextUploadQueueKey(byKey.keys, cursor) ?: return null
        return PendingCapture(next, checkNotNull(byKey[next]))
    }

    private suspend fun setUploadProgress(entryId: String, stage: String) {
        setProgress(workDataOf(
            UPLOAD_PROGRESS_ENTRY_ID to entryId,
            UPLOAD_PROGRESS_STAGE to stage,
        ))
    }

    private suspend fun finishUploadChain(ctx: Context): Result {
        val hadError = inputData.getBoolean(CHAIN_HAD_ERROR, false)
        val sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false)
        if (sawDeferred) {
            val round = inputData.getInt(DEFERRED_ROUND, 0)
            val persisted = continueUploadChain(
                ctx = ctx,
                cursor = null,
                sawDeferred = false,
                hadError = hadError,
                deferredRound = round + 1,
                delayMs = deferredUploadRecheckDelayMs(round),
            )
            if (!persisted) return Result.retry()
        } else {
            if (!hadError) Prefs.setLastUploadError(ctx, null)
            if (hasPendingImports(ctx)) scheduleImportPolling(ctx) else Entries.pruneSent(ctx)
        }
        return Result.success(workDataOf(
            UPLOAD_PROGRESS_STAGE to
                if (sawDeferred) "waiting-for-processing" else "complete",
        ))
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        if (inputData.getBoolean(POLL_ONLY, false)) {
            return@withContext pollImportsOnly(ctx)
        }

        val session = CaptureSession(ctx)
        val cursor = inputCursor()
        if (cursor == null) {
            session.recoverOrphans()  // once per chain: crash leftovers become uploads
        }
        val candidate = nextPendingCapture(session, cursor)
            ?: return@withContext finishUploadChain(ctx)
        setUploadProgress(candidate.key.entryId, "preparing")

        // transport: a paired desktop over the LAN (offline) when selected, or
        // in "auto" when it answers; otherwise the cloud path below.
        val mode = Prefs.transport(ctx)
        val lan = if (mode != "cloud" && Prefs.lanHost(ctx).isNotEmpty())
            try { LanClient(ctx) } catch (_: Exception) { null } else null
        if (lan != null) {
            if (lan.ping()) return@withContext uploadOneViaLan(ctx, candidate, lan)
            if (mode == "lan") {
                Prefs.setLastUploadError(ctx, "paired desktop could not be authenticated")
                setUploadProgress(candidate.key.entryId, "retrying")
                return@withContext Result.retry()
            }
        }

        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) {
            Prefs.setLastUploadError(ctx, if (Auth.signedIn(ctx)) null else "signed out")
            return@withContext Result.failure()
        }
        val uploadOwner = Prefs.userId(ctx)
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
                when (cloudUploadOwnership(prepared.creator, Prefs.userId(ctx))) {
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
                val delivery = uploadEntry(client, dir, prepared)
                markUploaded(ctx, dir, delivery)
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
        setUploadProgress(candidate.key.entryId, stage)
        val persisted = continueUploadChain(
            ctx = ctx,
            cursor = candidate.key,
            sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false) ||
                deferredForProcessing,
            hadError = hadError,
            deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
        )
        if (!persisted) return@withContext Result.retry()
        Result.success(workDataOf(
            UPLOAD_PROGRESS_ENTRY_ID to candidate.key.entryId,
            UPLOAD_PROGRESS_STAGE to stage,
        ))
    }

    private data class PreparedCapture(
        val manifest: JSONObject,
        val id: String,
        val creator: CaptureCreator,
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
        return PreparedCapture(
            manifest,
            id,
            captureCreatorFromManifest(manifest, Prefs.anonymousCreatorId(ctx)),
            validateUploadPhotos(dir, names),
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
        val meta = File(dir, "meta.json").takeIf { it.isFile }
            ?.let { try { JSONObject(it.readText()) } catch (_: Exception) { null } }
            ?: JSONObject()
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
    private fun markUploaded(ctx: Context, dir: File, delivery: ConfirmedDelivery) {
        markDelivered(ctx, dir, delivery, "pending")
    }

    private fun markDelivered(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
        cloudStatus: String,
    ) {
        check(delivery.entryId == dir.name && delivery.photoCount > 0) {
            "delivery receipt does not match local entry"
        }
        try {
            val manifestFile = File(dir, "manifest.json")
            val manifest = JSONObject(manifestFile.readText())
                .put("uploaded_at", System.currentTimeMillis())
                .put("cloud_status", cloudStatus)
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
                    val delivery = uploadEntryLan(client, dir, prepareCapture(ctx, dir))
                    markSentImported(ctx, dir, delivery)
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
        setUploadProgress(candidate.key.entryId, stage)
        val persisted = continueUploadChain(
            ctx = ctx,
            cursor = candidate.key,
            sawDeferred = inputData.getBoolean(CHAIN_SAW_DEFERRED, false) ||
                deferredForProcessing,
            hadError = hadError,
            deferredRound = inputData.getInt(DEFERRED_ROUND, 0),
        )
        if (!persisted) return Result.retry()
        return Result.success(workDataOf(
            UPLOAD_PROGRESS_ENTRY_ID to candidate.key.entryId,
            UPLOAD_PROGRESS_STAGE to stage,
        ))
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
        val meta = File(dir, "meta.json").takeIf { it.isFile }
            ?.let { try { JSONObject(it.readText()) } catch (_: Exception) { null } }
            ?: JSONObject()
        client.uploadCapture(id, device, manifest.optString("note", ""),
                             createdAt, ocr, meta, photos)
        return ConfirmedDelivery(id, photos.size, photos.map { it.first })
    }

    /** queue/<id> -> sent/<id>, marked imported (LAN import is synchronous). */
    private fun markSentImported(
        ctx: Context,
        dir: File,
        delivery: ConfirmedDelivery,
    ) {
        markDelivered(ctx, dir, delivery, "imported")
    }

    private fun hasPendingImports(ctx: Context): Boolean =
        Entries.recent(ctx).any { it.uploaded && isRemoteImportPending(it.cloudStatus) }

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
    private fun pollImports(ctx: Context, client: SupabaseClient): Boolean {
        val waiting = Entries.recent(ctx)
            .filter { it.uploaded && isRemoteImportPending(it.cloudStatus) }
        if (waiting.isEmpty()) return false
        val statuses = try {
            client.captureStatuses(waiting.map { it.id })
        } catch (_: Exception) {
            return true                              // later bounded poll tries again
        }
        for (e in waiting) {
            val s = statuses[e.id]?.let(::normalizeRemoteImportStatus) ?: continue
            if (s == normalizeRemoteImportStatus(e.cloudStatus)) continue
            try {
                val mf = File(e.dir, "manifest.json")
                Entries.atomicWrite(
                    mf,
                    JSONObject(mf.readText()).put("cloud_status", s).toString(),
                )
            } catch (_: Exception) { }
        }
        return hasPendingImports(ctx)
    }
}
