package org.whl.bookcapture

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
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
internal const val MAX_FORCED_PROCESS_RETRIES = 3

internal enum class ProcessingWorkDecision {
    RETRY,
    COMPLETE_CHAIN,
}

internal fun isUnfinishedProcessingWork(state: WorkInfo.State): Boolean = when (state) {
    WorkInfo.State.ENQUEUED,
    WorkInfo.State.RUNNING,
    WorkInfo.State.BLOCKED,
    -> true
    WorkInfo.State.SUCCEEDED,
    WorkInfo.State.FAILED,
    WorkInfo.State.CANCELLED,
    -> false
}

/**
 * Processing captures share serial WorkManager chains. A permanently partial,
 * invalid, or repeatedly transient response must eventually release its chain
 * so later books can run.
 */
internal fun shouldRetryProcessingWork(
    retryRequested: Boolean,
    forceReprocess: Boolean,
    runAttemptCount: Int,
): Boolean = retryRequested && runAttemptCount < if (forceReprocess) {
    MAX_FORCED_PROCESS_RETRIES
} else {
    MAX_AUTOMATIC_PROCESS_RETRIES
}

internal fun processingWorkDecision(
    retryRequested: Boolean,
    forceReprocess: Boolean,
    runAttemptCount: Int,
): ProcessingWorkDecision = if (
    shouldRetryProcessingWork(retryRequested, forceReprocess, runAttemptCount)
) {
    ProcessingWorkDecision.RETRY
} else {
    // A terminal result must succeed at the WorkManager level. Both kinds of
    // retry use serial chains, and a failed WorkManager result would cancel
    // every capture behind this one even though the error is entry-local.
    ProcessingWorkDecision.COMPLETE_CHAIN
}

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

        const val RETRY_WORK_NAME = "capture-process-retry"

        /**
         * Re-run the pipeline over captures the user picked out of the
         * attention list, one serial chain so a long backlog cannot open a
         * request per book at once.
         *
         * Forced, for two reasons the automatic paths cannot supply: it
         * bypasses the terminal-failure gate in [processDirectory], which is
         * what pins a capture to "failed" after a permanent error like a
         * rejected API key; and it is the only route that reaches sent/ at all,
         * since [enqueueBacklog] enumerates queue/ alone. Callers must have
         * called [Entries.Entry.requestReprocess] first — that is what clears
         * the stale error and holds the directory against retention.
         */
        fun enqueueForcedRetry(ctx: Context, entryIds: List<String>) {
            val requests = entryIds.asSequence()
                .map(String::trim)
                .filter { it.isNotEmpty() }
                .distinct()
                .sorted()
                .map { processingRequest(it, forceReprocess = true) }
                .toList()
            if (requests.isEmpty()) return
            var continuation = WorkManager.getInstance(ctx).beginUniqueWork(
                RETRY_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                requests.first(),
            )
            for (request in requests.drop(1)) continuation = continuation.then(request)
            continuation.enqueue()
        }

        /**
         * Repair explicit-reprocess holds left by an older failed serial chain.
         * This performs a blocking WorkManager query and must run off the main
         * thread. An unfinished chain already owns every pending marker; only a
         * fully terminal or missing chain is safe to rebuild.
         */
        @Synchronized
        fun resumePendingForcedRetries(ctx: Context): Boolean {
            val pendingIds = Entries.recent(ctx).asSequence()
                .filter { it.reprocessPending() }
                .map { it.id }
                .toList()
            if (pendingIds.isEmpty()) return false

            val workManager = WorkManager.getInstance(ctx)
            val retryWork = try {
                workManager.getWorkInfosForUniqueWork(RETRY_WORK_NAME).get()
            } catch (error: Exception) {
                Log.w("ProcessWorker", "Could not inspect explicit retry work", error)
                return false
            }
            if (retryWork.any { isUnfinishedProcessingWork(it.state) }) return false

            enqueueForcedRetry(ctx, pendingIds)
            return true
        }

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
                val forced = forceReprocess && requestedId == dir.name
                val directoryOutcome = if (!dir.isDirectory) DirectoryOutcome()
                else if (!cleanupCommittedThumbnailDeletes(dir)) {
                    DirectoryOutcome(
                        retry = true,
                        lastError = "Waiting for interrupted photo deletion recovery",
                    )
                }
                else processDirectory(
                    ctx,
                    dir,
                    mistral,
                    deepseek,
                    forced = forced,
                    workerRetry = runAttemptCount > 0,
                    workAttempt = runAttemptCount,
                )
                if (forced &&
                    (directoryOutcome.permanentError != null || directoryOutcome.retry) &&
                    processingWorkDecision(
                        directoryOutcome.retry,
                        forceReprocess = true,
                        runAttemptCount = runAttemptCount,
                    ) == ProcessingWorkDecision.COMPLETE_CHAIN
                ) {
                    val error = directoryOutcome.permanentError ?: directoryOutcome.lastError
                        ?: "Processing did not complete after ${runAttemptCount + 1} attempts"
                    Entries.find(ctx, dir.name)?.finishReprocess(error)
                }
                directoryOutcome
            }
            transient = transient || outcome.retry
            if (permanent == null) permanent = outcome.permanentError
            if (lastFailure == null) lastFailure = outcome.lastError
        }

        // Kept for the existing top-level warning; the authoritative details
        // now live beside each entry in processing.json.
        Prefs.setLastProcError(ctx, permanent ?: lastFailure)
        // Processing is local-only. Delivery starts only from the explicit
        // capture-sync action; its bounded chain rechecks deferred entries.
        when (processingWorkDecision(transient, forceReprocess, runAttemptCount)) {
            ProcessingWorkDecision.RETRY -> Result.retry()
            ProcessingWorkDecision.COMPLETE_CHAIN -> Result.success()
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
        workAttempt: Int,
    ): DirectoryOutcome {
        var entry = Entries.find(ctx, dir.name) ?: return DirectoryOutcome()
        if (workerRetry && !forced && entry.processing.status == Entries.ProcessingStatus.FAILED &&
            !entry.processing.retryable) {
            return DirectoryOutcome(permanentError = entry.processing.lastError)
        }

        val pendingAtStart = CatalogCheckStore.read(dir)
            ?.takeIf { it.state == CatalogCheckState.PENDING }
        if (pendingAtStart != null && pendingAtStart.targetAssetId == null) {
            // The command intent is durable before CameraX starts. Its stable
            // photo identity is bound only after commit; that commit enqueues
            // fresh work. Do not mistake the reservation window for deletion.
            if (entry.sealed) {
                return catalogCheckFailure(
                    dir,
                    pendingAtStart,
                    "Catalog check photo was not finalized",
                )
            }
            Entries.markWaiting(dir, Entries.ProcessingStage.STANDARDIZING)
            return DirectoryOutcome()
        }
        val allPhotos = entry.photos()
        if (allPhotos.isEmpty()) {
            failPendingCatalogCheck(dir, "Catalog check photo is no longer available")
            return DirectoryOutcome()
        }
        val targetAtStart = pendingAtStart?.let { catalogCheckPhoto(dir, it) }
        if (!entry.sealed && pendingAtStart != null && targetAtStart == null) {
            return catalogCheckFailure(
                dir,
                pendingAtStart,
                "Catalog check photo is no longer available",
            )
        }
        // A check is latency-sensitive and depends on exactly one photographed
        // page. OCR that stable asset first without letting an unrelated older
        // page failure block the answer; Done/a later shutter drains the rest.
        val photos =
            if (!entry.sealed && targetAtStart != null) listOf(targetAtStart)
            else allPhotos
        // Read before the transition below overwrites it. The re-extraction
        // gate further down asks whether this entry was ALREADY complete on
        // entry to the worker; asking after markProcessing always answered
        // PROCESSING, so the gate never fired and every queued capture
        // re-extracted over the network on every Home resume.
        val statusAtStart = entry.processing.status
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
                failPendingCatalogCheck(dir, message)
                entry.finishReprocess(message)
                Entries.markFailed(dir, Entries.ProcessingStage.OCR, message, retryable = false)
                return DirectoryOutcome(permanentError = message)
            } catch (e: Exception) {
                val message = failureMessage("OCR", e)
                if (!shouldRetryProcessingWork(true, forced, workAttempt)) {
                    failPendingCatalogCheck(dir, message)
                }
                Entries.markFailed(dir, Entries.ProcessingStage.OCR, message, retryable = true)
                return DirectoryOutcome(retry = true, lastError = message)
            }
        }

        entry = Entries.find(ctx, dir.name) ?: return DirectoryOutcome()
        val missingOcr = photos.any { !File(dir, it.name + ".txt").isFile }
        if (missingOcr && mistral.isEmpty()) {
            val message = "OCR: Mistral API key is missing"
            failPendingCatalogCheck(dir, message)
            Entries.markFailed(dir, Entries.ProcessingStage.OCR, message, retryable = false)
            entry.finishReprocess(message)
            return DirectoryOutcome(permanentError = message)
        }
        if (missingOcr || waitingForJpeg) {
            Entries.markWaiting(dir, Entries.ProcessingStage.OCR)
            if (pendingAtStart != null &&
                !shouldRetryProcessingWork(
                    retryRequested = true,
                    forceReprocess = forced,
                    runAttemptCount = workAttempt,
                )
            ) {
                return catalogCheckFailure(
                    dir,
                    pendingAtStart,
                    if (waitingForJpeg) {
                        "Catalog check photo did not finish saving"
                    } else {
                        "Catalog check OCR did not become ready"
                    },
                )
            }
            return DirectoryOutcome(retry = true)
        }

        val pendingCheck = CatalogCheckStore.read(dir)
            ?.takeIf { it.state == CatalogCheckState.PENDING }

        // Normal metadata extraction waits for Done so it cannot lock in a
        // half-captured title page. A requested catalog check is deliberately
        // different: it extracts only the photographed check page, stores the
        // result outside meta.json, and leaves the capture open.
        if (!entry.sealed) {
            if (pendingCheck != null) {
                Entries.markProcessing(dir, Entries.ProcessingStage.EXTRACTION)
                val outcome = processOpenCatalogCheck(
                    ctx = ctx,
                    dir = dir,
                    request = pendingCheck,
                    mistral = mistral,
                    deepseek = deepseek,
                    forced = forced,
                    workAttempt = workAttempt,
                )
                Entries.markWaiting(dir, Entries.ProcessingStage.EXTRACTION)
                return outcome
            }
            Entries.markWaiting(dir, Entries.ProcessingStage.EXTRACTION)
            return DirectoryOutcome()
        }
        val existingMetadata = entry.meta
        if (!forced && existingMetadata != null &&
            statusAtStart == Entries.ProcessingStatus.COMPLETE) {
            completePendingCatalogCheck(
                ctx = ctx,
                dir = dir,
                metadata = existingMetadata,
                ch = searchChList(ctx, dir, existingMetadata),
            )
            Entries.markComplete(dir)
            return DirectoryOutcome()
        }
        if (forced && deepseek.isEmpty()) {
            val message = "Extraction: DeepSeek API key is missing"
            failPendingCatalogCheck(dir, message)
            entry.finishReprocess(message)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            return DirectoryOutcome(permanentError = message)
        }
        if (deepseek.isEmpty() && mistral.isEmpty()) {
            val message = "Extraction API key is missing"
            failPendingCatalogCheck(dir, message)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            entry.finishReprocess(message)
            return DirectoryOutcome(permanentError = message)
        }

        val text = entry.ocrText()
        if (text.isEmpty()) {
            val message = "Extraction: OCR returned no text"
            failPendingCatalogCheck(dir, message)
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
            val ch = searchChList(ctx, dir, merged)
            completePendingCatalogCheck(ctx, dir, merged, ch)
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
            failPendingCatalogCheck(dir, message)
            entry.finishReprocess(message)
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = false)
            DirectoryOutcome(permanentError = message)
        } catch (e: Pipeline.InvalidExtractionError) {
            val message = failureMessage("Extraction", e)
            if (!shouldRetryProcessingWork(true, forced, workAttempt)) {
                failPendingCatalogCheck(dir, message)
            }
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = true)
            DirectoryOutcome(retry = true, lastError = message)
        } catch (e: Exception) {
            val message = failureMessage("Extraction", e)
            if (!shouldRetryProcessingWork(true, forced, workAttempt)) {
                failPendingCatalogCheck(dir, message)
            }
            Entries.markFailed(dir, Entries.ProcessingStage.EXTRACTION, message, retryable = true)
            DirectoryOutcome(retry = true, lastError = message)
        }
    }

    private fun catalogCheckPhoto(dir: File, request: CatalogCheckRecord): File? {
        val assetId = request.targetAssetId ?: return null
        val descriptors = PhotoAssetStore.descriptors(dir)
        val descriptor = descriptors.firstOrNull { it.assetId == assetId }
        return descriptor?.captureFile?.takeIf { it.isFile }
    }

    private fun processOpenCatalogCheck(
        ctx: Context,
        dir: File,
        request: CatalogCheckRecord,
        mistral: String,
        deepseek: String,
        forced: Boolean,
        workAttempt: Int,
    ): DirectoryOutcome {
        if (request.targetAssetId == null) {
            // CameraX commit/binding will enqueue the work that can resolve the
            // stable asset. This is a reservation state, not a missing photo.
            return DirectoryOutcome()
        }
        val target = catalogCheckPhoto(dir, request)
            ?: return catalogCheckFailure(
                dir,
                request,
                "Catalog check photo is no longer available",
            )
        val sidecar = File(dir, target.name + ".txt")
        if (!sidecar.isFile) {
            return if (shouldRetryProcessingWork(true, forced, workAttempt)) {
                DirectoryOutcome(
                    retry = true,
                    lastError = "Catalog check is waiting for OCR",
                )
            } else {
                catalogCheckFailure(dir, request, "Catalog check OCR did not complete")
            }
        }
        val text = runCatching {
            sidecar.readText().trim()
        }.getOrElse { error ->
            return catalogCheckFailure(
                dir,
                request,
                "Check OCR could not be read: ${error.message ?: error.javaClass.simpleName}",
            )
        }
        if (text.isEmpty()) {
            return catalogCheckFailure(
                dir,
                request,
                "Check OCR returned no text",
            )
        }
        if (deepseek.isEmpty() && mistral.isEmpty()) {
            return catalogCheckFailure(
                dir,
                request,
                "Catalog check extraction API key is missing",
            )
        }

        return try {
            val entry = Entries.find(ctx, dir.name)
            val instructions = entry?.customInstructions().orEmpty().ifEmpty {
                Prefs.extractionInstructions(ctx)
            }
            val extraction = Pipeline.extract(text, deepseek, mistral, instructions)
            val metadata = extraction.metadata
            if (metadata.optString("title").isBlank()) {
                throw Pipeline.InvalidExtractionError(
                    "Extraction returned no title for catalog matching",
                )
            }
            val current = CatalogCheckStore.read(dir)
            if (current?.state != CatalogCheckState.PENDING ||
                current.requestId != request.requestId
            ) {
                // A later photographed check superseded this network result.
                return DirectoryOutcome()
            }
            // A Check is an independent observation of its photographed page.
            // It must not overwrite the entry-wide CH candidate or a decision
            // the operator already made for the captured book.
            val ch = lookupChList(ctx, metadata)
            if (!completePendingCatalogCheck(
                    ctx,
                    dir,
                    metadata,
                    ch,
                    expectedRequestId = request.requestId,
                )
            ) {
                throw IOException("catalog check result could not be persisted")
            }
            DirectoryOutcome()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Pipeline.PermanentError) {
            catalogCheckFailure(dir, request, failureMessage("Check extraction", error))
        } catch (error: Exception) {
            val message = failureMessage("Check extraction", error)
            if (shouldRetryProcessingWork(
                    retryRequested = true,
                    forceReprocess = forced,
                    runAttemptCount = workAttempt,
                )
            ) {
                DirectoryOutcome(retry = true, lastError = message)
            } else {
                catalogCheckFailure(dir, request, message)
            }
        }
    }

    private fun catalogCheckFailure(
        dir: File,
        request: CatalogCheckRecord,
        message: String,
    ): DirectoryOutcome {
        runCatching {
            CatalogCheckStore.fail(
                dir = dir,
                requestId = request.requestId,
                error = message,
            )
        }.onFailure { error ->
            Log.w("ProcessWorker", "Catalog check failure could not be persisted for ${dir.name}", error)
        }
        return DirectoryOutcome()
    }

    private fun failPendingCatalogCheck(dir: File, message: String) {
        CatalogCheckStore.read(dir)
            ?.takeIf { it.state == CatalogCheckState.PENDING }
            ?.let { request -> catalogCheckFailure(dir, request, message) }
    }

    /**
     * Finish the latest pending request from extracted metadata. WHL index
     * unavailability is itself a result rather than a capture-processing
     * failure: the bundled snapshot can be repaired in a later APK.
     */
    private fun completePendingCatalogCheck(
        ctx: Context,
        dir: File,
        metadata: JSONObject,
        ch: ChMatchState?,
        expectedRequestId: String? = null,
    ): Boolean {
        val request = CatalogCheckStore.read(dir)
            ?.takeIf { it.state == CatalogCheckState.PENDING }
            ?: return true
        // A second Check can commit while the first network request is in
        // flight. The old extraction is then obsolete, not a result for the
        // newer photographed page.
        if (expectedRequestId != null && request.requestId != expectedRequestId) return true
        return runCatching {
            val title = metadata.optString("title").trim()
            val author = metadata.optString("author").trim()
            val year = metadata.optString("year").trim()
            val whlIndex = WhlIndex.get(ctx)
            val whlMatch = whlIndex?.match(title, author)
            CatalogCheckStore.complete(
                dir = dir,
                requestId = request.requestId,
                bibliography = CatalogCheckBibliography(title, author, year),
                ch = CatalogCheckChResult(
                    searched = ch?.searched == true,
                    candidate = ch?.candidate?.let(CatalogCheckChCandidateSummary::from),
                ),
                whl = whlMatch?.toCatalogCheckResult()
                    ?: CatalogCheckWhlResult(CatalogCheckWhlStatus.UNAVAILABLE),
            ) != null
        }.onFailure { error ->
            Log.w("ProcessWorker", "Catalog check completion failed for ${dir.name}", error)
        }.getOrDefault(false)
    }

    private fun WhlMatch.toCatalogCheckResult(): CatalogCheckWhlResult =
        CatalogCheckWhlResult(
            status = when (flag) {
                WhlCatalogFlag.YES -> CatalogCheckWhlStatus.YES
                WhlCatalogFlag.DRAFT -> CatalogCheckWhlStatus.DRAFT
                WhlCatalogFlag.NO -> CatalogCheckWhlStatus.NO
            },
            candidate = candidate?.let { row ->
                CatalogCheckWhlCandidateSummary(
                    title = row.title,
                    author = row.author,
                    year = row.year,
                    permalink = row.permalink,
                    score = row.score,
                )
            },
        )

    /**
     * Perform the bundled CH lookup without mutating entry state.
     *
     * The index is ~2.7 MB of JSON on first touch, so lookup stays on this IO
     * worker. A CH failure must never fail a scan; it simply leaves the result
     * unsearched.
     *
     * Open catalog checks use this result directly. Normal sealed-entry
     * processing adds decision preservation and persistence in [searchChList].
     */
    private fun lookupChList(
        ctx: Context,
        metadata: JSONObject,
    ): ChMatchState? =
        runCatching {
            val title = metadata.optString("title").trim()
            val author = metadata.optString("author").trim()
            if (title.isEmpty() && author.isEmpty()) return@runCatching null

            val index = ChIndex.get(ctx) ?: return@runCatching null
            ChMatchState(
                searched = true,
                candidate = index.bestMatch(title, author),
                decision = ChDecision.UNREVIEWED,
            )
        }.onFailure {
            Log.w("ProcessWorker", "CH lookup failed", it)
        }.getOrNull()

    /**
     * Persist the normal entry-wide CH result. A decision is preserved only
     * when the same candidate returns; a different candidate is unreviewed.
     */
    private fun searchChList(
        ctx: Context,
        dir: File,
        metadata: JSONObject,
    ): ChMatchState? {
        val lookup = lookupChList(ctx, metadata) ?: return null
        return runCatching {
            val previous = ChMatchStore.read(dir)
            val decision =
                if (lookup.candidate != null &&
                    lookup.candidate.key == previous.candidate?.key
                ) {
                    previous.decision
                }
                else ChDecision.UNREVIEWED

            lookup.copy(decision = decision).also { state ->
                ChMatchStore.write(dir, state)
            }
        }.onFailure {
            Log.w("ProcessWorker", "CH result could not be stored for ${dir.name}", it)
        }.getOrNull()
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
