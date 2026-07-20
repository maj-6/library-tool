package org.whl.bookcapture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit

internal const val MAX_AUTOMATIC_PROCESS_RETRIES = 3

/**
 * Automatic captures share one serial WorkManager chain. A permanently partial
 * or invalid response must eventually release that chain so later books can
 * run. Explicit reprocess work is isolated per entry and may keep retrying.
 */
internal fun shouldRetryProcessingWork(
    retryRequested: Boolean,
    forceReprocess: Boolean,
    runAttemptCount: Int,
): Boolean = retryRequested &&
    (forceReprocess || runAttemptCount < MAX_AUTOMATIC_PROCESS_RETRIES)

/**
 * Background processing, kicked after every photo and every seal: standardize
 * new photos, OCR them (Mistral), and once an entry is sealed with text,
 * extract the bibliography (DeepSeek default, Mistral fallback). Results land
 * next to the photos (photo_N.jpg.txt, meta.json) so the recent list can show
 * a book record instead of "Processing…", and the upload carries them.
 *
 * Photos are processed while the entry is still OPEN — OCR runs during the
 * seconds the user is flipping to the next page, so by "done" most of the
 * work is already behind us.
 */
class ProcessWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        private const val KEY_ENTRY_ID = "entry_id"
        private const val KEY_FORCE_REPROCESS = "force_reprocess"
        private const val ACTIVE_CAPTURE_IDLE_SECONDS = 2L
        const val UNIQUE_WORK_NAME = "capture-process"
        const val BACKLOG_WORK_NAME = "capture-process-backlog"
        private const val EXPLICIT_WORK_PREFIX = "capture-reprocess-"

        fun workNameForEntry(entryId: String): String = EXPLICIT_WORK_PREFIX + entryId

        /** A newly sealed capture is appended exactly once to the bounded
         * backlog even when a broader scan is already running. */
        fun enqueuePending(ctx: Context, entryId: String) {
            if (entryId.isBlank()) return
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                BACKLOG_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                processingRequest(entryId, forceReprocess = false),
            )
        }

        fun enqueue(ctx: Context, entryId: String? = null) {
            val explicitId = entryId?.takeIf { it.isNotBlank() }
            val activeId = if (explicitId == null) Prefs.currentEntryId(ctx) else null
            val requestedId = explicitId ?: activeId
            val builder = OneTimeWorkRequestBuilder<ProcessWorker>()
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            if (activeId != null) builder.setInitialDelay(ACTIVE_CAPTURE_IDLE_SECONDS, TimeUnit.SECONDS)
            if (requestedId != null) builder.setInputData(workDataOf(
                KEY_ENTRY_ID to requestedId,
                KEY_FORCE_REPROCESS to (explicitId != null),
            ))
            val req = builder.build()

            // Shutter calls do not need a special call site: while a capture is
            // active they replace this delayed request and keep moving its start
            // two seconds out. Done clears currentEntryId, so its enqueue replaces
            // the delay with an immediate, persisted background run.
            activeId?.let { id ->
                File(Entries.queueRoot(ctx), id).takeIf { it.isDirectory }
                    ?.let { Entries.markWaiting(it) }
            }
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork(
                    explicitId?.let(::workNameForEntry) ?: UNIQUE_WORK_NAME,
                    ExistingWorkPolicy.REPLACE,
                    req,
                )
        }

        /** Partition a backlog scan into a serial WorkManager chain. Each
         * request owns one capture, so a slow OCR call cannot turn one worker
         * into an unbounded drain of every book on the phone. Appending is
         * safe because every stage is file-idempotent. */
        private fun processingRequest(entryId: String, forceReprocess: Boolean) =
            OneTimeWorkRequestBuilder<ProcessWorker>()
                .setInputData(workDataOf(
                    KEY_ENTRY_ID to entryId,
                    KEY_FORCE_REPROCESS to forceReprocess,
                ))
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .build()

        private fun enqueueBacklog(ctx: Context, entryIds: List<String>) {
            val requests = entryIds.distinct().sorted().map { entryId ->
                processingRequest(entryId, forceReprocess = false)
            }
            if (requests.isEmpty()) return
            var continuation = WorkManager.getInstance(ctx).beginUniqueWork(
                BACKLOG_WORK_NAME,
                ExistingWorkPolicy.KEEP,
                requests.first(),
            )
            for (request in requests.drop(1)) continuation = continuation.then(request)
            continuation.enqueue()
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val mistral = Prefs.mistralKey(ctx)
        val deepseek = Prefs.deepseekKey(ctx)
        val requestedId = inputData.getString(KEY_ENTRY_ID)?.takeIf { it.isNotBlank() }
        val forceReprocess = inputData.getBoolean(KEY_FORCE_REPROCESS, false)
        var transient = false
        var permanent: String? = null
        var lastFailure: String? = null

        val dirs = requestedId?.let { id -> Entries.find(ctx, id)?.let { listOf(it.dir) } ?: emptyList() }
            ?: (Entries.queueRoot(ctx).listFiles { f: File -> f.isDirectory }?.toList() ?: emptyList())
        if (requestedId == null) {
            enqueueBacklog(ctx, dirs.map { it.name })
            return@withContext Result.success()
        }
        for (dir in dirs) {
            currentCoroutineContext().ensureActive()
            val outcome = EntryOperationLocks.withLock(dir.name) {
                if (!dir.isDirectory) DirectoryOutcome()
                else processDirectory(
                    ctx,
                    dir,
                    mistral,
                    deepseek,
                    forced = forceReprocess && requestedId == dir.name,
                    workerRetry = runAttemptCount > 0,
                )
            }
            transient = transient || outcome.retry
            if (permanent == null) permanent = outcome.permanentError
            if (lastFailure == null) lastFailure = outcome.lastError
        }

        // Kept for the existing top-level warning; the authoritative details
        // now live beside each entry in processing.json.
        Prefs.setLastProcError(ctx, permanent ?: lastFailure)
        // freshly processed entries may be ready to ship
        UploadWorker.kick(ctx)
        when {
            shouldRetryProcessingWork(
                transient,
                forceReprocess,
                runAttemptCount,
            ) -> Result.retry()
            // A terminal failure belongs to this capture, not the serial
            // backlog chain. Marking an automatic unit failed would prevent
            // every dependent book from running. An explicit user-requested
            // reprocess may still expose WorkInfo.failure to its own observer.
            permanent != null && forceReprocess -> Result.failure()
            else -> Result.success()
        }
    }

    private data class DirectoryOutcome(
        val retry: Boolean = false,
        val permanentError: String? = null,
        val lastError: String? = permanentError,
    )

    private suspend fun processDirectory(
        ctx: Context,
        dir: File,
        mistral: String,
        deepseek: String,
        forced: Boolean,
        workerRetry: Boolean,
    ): DirectoryOutcome {
        var entry = Entries.find(ctx, dir.name) ?: return DirectoryOutcome()
        if (workerRetry && !forced && entry.processing.status == Entries.ProcessingStatus.FAILED &&
            !entry.processing.retryable) {
            return DirectoryOutcome(permanentError = entry.processing.lastError)
        }

        val photos = entry.photos()
        if (photos.isEmpty()) return DirectoryOutcome()
        Entries.markProcessing(dir, Entries.ProcessingStage.STANDARDIZING)

        var waitingForJpeg = false
        for (photo in photos) {
            currentCoroutineContext().ensureActive()
            val sidecar = File(dir, photo.name + ".txt")
            if (sidecar.isFile) continue                 // already standardized and OCR'd
            // A photo CameraX is still stream-copying is a truncated JPEG.
            if (!isCompleteJpeg(photo)) {
                waitingForJpeg = true
                continue
            }
            // Never destructively standardize the only copy. New captures
            // normally registered a hard-linked original in the camera
            // callback; legacy/recovered captures get one last IO-worker
            // preservation attempt here.
            val originalPreserved = PhotoAssetStore.prepareForProcessing(dir, photo)
            try {
                if (originalPreserved) Pipeline.standardizeInPlace(photo)
            } catch (_: Exception) {
                // The original remains usable for OCR/upload.
            }
            PhotoAssetStore.recordDisplayVersion(
                dir,
                photo,
                recipe = "android-standardize",
                recipeVersion = "1",
            )
            if (mistral.isEmpty()) continue

            Entries.markProcessing(dir, Entries.ProcessingStage.OCR)
            try {
                val result = Pipeline.ocrResult(photo, mistral)
                // Geometry lands before the Markdown sidecar, which is the
                // per-photo commit marker checked at the top of this loop. A
                // process death can therefore cause a harmless repeated OCR,
                // but can never leave text permanently suppressing geometry.
                val geometryStored = PhotoAssetStore.mergeGeometry(dir, photo, result.geometry)
                if (result.geometry != null && !geometryStored) {
                    throw IOException("OCR geometry could not be persisted")
                }
                // Retain the exact successful provider result before the
                // Markdown commit marker. A crash can repeat OCR, but cannot
                // leave a completed-looking photo without its diagnostics.
                if (result.providerResponse.isNotBlank()) {
                    Entries.atomicWrite(
                        File(dir, photo.name + Entries.MISTRAL_RESPONSE_SUFFIX),
                        result.providerResponse,
                    )
                }
                Entries.atomicWrite(sidecar, result.markdown)
                currentCoroutineContext().ensureActive()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Pipeline.PermanentError) {
                val message = failureMessage("OCR", e)
                entry.finishReprocess(message)
                Entries.markFailed(dir, Entries.ProcessingStage.OCR, message, retryable = false)
                return DirectoryOutcome(permanentError = message)
            } catch (e: Exception) {
                val message = failureMessage("OCR", e)
                Entries.markFailed(dir, Entries.ProcessingStage.OCR, message, retryable = true)
                return DirectoryOutcome(retry = true, lastError = message)
            }
        }

        entry = Entries.find(ctx, dir.name) ?: return DirectoryOutcome()
        val missingOcr = photos.any { !File(dir, it.name + ".txt").isFile }
        if (missingOcr && mistral.isEmpty()) {
            val message = "OCR: Mistral API key is missing"
            Entries.markFailed(dir, Entries.ProcessingStage.OCR, message, retryable = false)
            entry.finishReprocess(message)
            return DirectoryOutcome(permanentError = message)
        }
        if (missingOcr || waitingForJpeg) {
            Entries.markWaiting(dir, Entries.ProcessingStage.OCR)
            return DirectoryOutcome(retry = true)
        }

        // Extraction waits for Done so it cannot lock in a half-captured title page.
        if (!entry.sealed) {
            Entries.markWaiting(dir, Entries.ProcessingStage.EXTRACTION)
            return DirectoryOutcome()
        }
        if (!forced && entry.meta != null &&
            entry.processing.status == Entries.ProcessingStatus.COMPLETE) {
            Entries.markComplete(dir)
            return DirectoryOutcome()
        }
        if (forced && deepseek.isEmpty()) {
            val message = "Extraction: DeepSeek API key is missing"
            entry.finishReprocess(message)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            return DirectoryOutcome(permanentError = message)
        }
        if (deepseek.isEmpty() && mistral.isEmpty()) {
            val message = "Extraction API key is missing"
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            entry.finishReprocess(message)
            return DirectoryOutcome(permanentError = message)
        }

        val text = entry.ocrText()
        if (text.isEmpty()) {
            val message = "Extraction: OCR returned no text"
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            entry.finishReprocess(message)
            return DirectoryOutcome(permanentError = message)
        }

        Entries.markProcessing(dir, Entries.ProcessingStage.EXTRACTION)
        return try {
            val instructions = entry.customInstructions().ifEmpty {
                Prefs.extractionInstructions(applicationContext)
            }
            val extraction = Pipeline.extract(text, deepseek, mistral, instructions)
            val mistralExtraction = File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE)
            if (extraction.provider == "mistral" && !extraction.providerResponse.isNullOrBlank()) {
                Entries.atomicWrite(mistralExtraction, extraction.providerResponse)
            } else {
                // Do not present an obsolete Mistral extraction as the source of
                // metadata that a later successful DeepSeek run superseded.
                mistralExtraction.delete()
            }
            val merged = Pipeline.mergeExtraction(
                existing = entry.meta,
                incoming = extraction.metadata,
                replaceExisting = forced,
            )
            // Only an explicit user-requested reprocess owns the hold marker.
            // Automatic extraction may return useful-but-partial metadata; it
            // can keep retrying during the upload grace period, but must not
            // pin the capture locally forever when a model consistently omits
            // an optional field.
            if (forced && !extraction.complete) Entries.holdForProcessing(dir)
            Entries.atomicWrite(File(dir, "meta.json"), merged.toString())
            PhotoAssetStore.applyBibliographicSuggestions(dir, merged)
            requestPostProcessing(ctx, dir, merged)
            if (extraction.complete) {
                Entries.markComplete(dir)
                entry.finishReprocess()
                DirectoryOutcome()
            } else {
                val warning = extraction.warning ?: "Partial extraction response"
                Entries.markPartial(dir, warning)
                DirectoryOutcome(retry = true, lastError = warning)
            }
        } catch (e: Pipeline.PermanentError) {
            val message = failureMessage("Extraction", e)
            entry.finishReprocess(message)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            DirectoryOutcome(permanentError = message)
        } catch (e: Pipeline.InvalidExtractionError) {
            val message = failureMessage("Extraction", e)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = true)
            DirectoryOutcome(retry = true, lastError = message)
        } catch (e: Exception) {
            val message = failureMessage("Extraction", e)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = true)
            DirectoryOutcome(retry = true, lastError = message)
        }
    }

    /** Freeze post-processing preferences only after extraction supplied the
     * catalog year and role heuristics have run. This records intent; it does
     * not enqueue a nonexistent service or alter processing completion. */
    private fun requestPostProcessing(ctx: Context, dir: File, metadata: JSONObject) {
        val publicationYear = metadata.optString("year").trim().toIntOrNull()
            ?.takeIf { it in 1..9999 }
        val profile = Prefs.postProcessingProfile(ctx, publicationYear)
        PhotoAssetStore.read(dir).orderedAssets()
            .filter { asset -> isPostProcessingRole(asset.role.effectiveRole) }
            .forEach { asset ->
                if (!PhotoAssetStore.requestProcessing(dir, asset.assetId, profile)) {
                    throw IOException(
                        "Post-processing request could not be persisted for ${asset.captureFile}",
                    )
                }
            }
    }

    private fun failureMessage(stage: String, error: Exception): String {
        val detail = error.message?.trim()?.replace(Regex("\\s+"), " ")?.take(300)
            .takeUnless { it.isNullOrEmpty() }
            ?: error.javaClass.simpleName
        return "$stage: $detail"
    }

    /** A complete JPEG ends in the EOI marker FF D9; a file mid-write does not.
     *  Cheap two-byte tail read, so a half-copied capture is never processed. */
    private fun isCompleteJpeg(f: File): Boolean = try {
        val len = f.length()
        len >= 4 && RandomAccessFile(f, "r").use { raf ->
            raf.seek(len - 2)
            raf.read() == 0xFF && raf.read() == 0xD9
        }
    } catch (_: Exception) { false }
}
