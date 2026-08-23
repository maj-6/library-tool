package org.whl.bookcapture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.concurrent.TimeUnit

/** Durable Mistral OCR for photos already accepted into the physical-scan queue. */
internal class ScanSearchOcrWorker(ctx: Context, params: WorkerParameters) :
    CoroutineWorker(ctx, params) {

    companion object {
        const val WORK_TAG = "scan-search-ocr"
        private const val WORK_NAME_PREFIX = "scan-search-ocr:"
        private const val INPUT_ITEM_ID = "scan_search_item_id"
        private const val STAGING_DIRECTORY = "scan-search"
        private const val MAX_STAGED_IMAGE_BYTES = 32L * 1024L * 1024L
        private const val MAX_TRANSIENT_RETRIES = 3

        /**
         * Move one app-private CameraX image to its UUID-derived durable path,
         * then schedule OCR. Only the UUID, never an arbitrary path or API key,
         * enters WorkManager's database.
         */
        fun enqueue(ctx: Context, itemId: String, path: String): Boolean {
            val id = itemId.trim().lowercase()
            if (!SAFE_CAPTURE_SYNC_ID.matches(id)) return false
            val source = privateSourceFile(ctx, path)
            val staged = stagedFile(ctx, id)
            if (source == null || staged == null) {
                persistSchedulingFailure(
                    ctx,
                    id,
                    ctx.getString(R.string.scan_queue_notification_staging_failed),
                )
                return false
            }
            if (!stage(source, staged)) {
                cleanupStagedImage(ctx, id)
                persistSchedulingFailure(
                    ctx,
                    id,
                    ctx.getString(R.string.scan_queue_notification_staging_failed),
                )
                return false
            }
            val request = OneTimeWorkRequestBuilder<ScanSearchOcrWorker>()
                .setInputData(Data.Builder().putString(INPUT_ITEM_ID, id).build())
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .addTag(WORK_TAG)
                .build()
            return try {
                WorkManager.getInstance(ctx).enqueueUniqueWork(
                    WORK_NAME_PREFIX + id,
                    ExistingWorkPolicy.KEEP,
                    request,
                ).result.get()
                true
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                persistSchedulingFailure(
                    ctx,
                    id,
                    ctx.getString(R.string.scan_queue_notification_enqueue_failed),
                )
                cleanupStagedImage(ctx, id)
                false
            } catch (_: Exception) {
                persistSchedulingFailure(
                    ctx,
                    id,
                    ctx.getString(R.string.scan_queue_notification_enqueue_failed),
                )
                cleanupStagedImage(ctx, id)
                false
            }
        }

        /** Cancel queued OCR and remove this account's raw identifying photos. */
        fun abandonOwner(ctx: Context, ownerId: String) {
            val owner = ownerId.trim().lowercase()
            if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return
            val pending = ScanSearchQueue.processingItemsForWorker(ctx, owner)
            try {
                WorkManager.getInstance(ctx).cancelAllWorkByTag(WORK_TAG).result.get()
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            } catch (_: Exception) {
                // Reconciliation below makes any still-running worker harmless.
            }
            pending.forEach { item ->
                ScanSearchQueue.failProcessingForWorker(
                    ctx,
                    item.id,
                    item.ownerId,
                    ctx.getString(R.string.scan_queue_notification_account_changed),
                )
                cleanupStagedImage(ctx, item.id)
            }
        }

        /** Deterministic private destination used by capture and crash recovery. */
        fun stagedFile(ctx: Context, itemId: String): File? {
            val id = itemId.trim().lowercase()
            if (!SAFE_CAPTURE_SYNC_ID.matches(id)) return null
            return try {
                val requestedRoot = File(ctx.noBackupFilesDir, STAGING_DIRECTORY)
                if ((!requestedRoot.exists() && !requestedRoot.mkdirs()) ||
                    !requestedRoot.isDirectory || Files.isSymbolicLink(requestedRoot.toPath())
                ) return null
                val root = requestedRoot.canonicalFile
                if (root != requestedRoot.absoluteFile) return null
                File(root, "$id.jpg").takeIf { it.parentFile == root }
            } catch (_: Exception) {
                null
            }
        }

        /** Reconcile a process death between durable row creation and WorkManager enqueue. */
        fun resumePending(ctx: Context): Int {
            val store = ScanSearchQueue.read(ctx)
            if (!store.valid) return 0
            var handled = 0
            store.items.asSequence().filter(ScanSearchQueueItem::processing).forEach { item ->
                val staged = stagedFile(ctx, item.id)
                if (staged == null || !validStagedImage(ctx, item.id, staged)) {
                    val failed = ScanSearchQueue.failProcessing(
                        ctx,
                        item.id,
                        ctx.getString(R.string.scan_queue_notification_photo_missing),
                    )
                    if (failed != null) {
                        ScanSearchNotifications.failure(ctx, failed.id, failed.errorMessage)
                        handled += 1
                    }
                } else if (enqueue(ctx, item.id, staged.absolutePath)) {
                    handled += 1
                }
            }
            return handled
        }

        private fun persistSchedulingFailure(ctx: Context, id: String, message: String) {
            val failed = ScanSearchQueue.failProcessing(ctx, id, message) ?: return
            ScanSearchNotifications.failure(ctx, failed.id, failed.errorMessage)
            if (failed.dirty) ScanSearchQueueSyncWorker.enqueue(ctx)
        }

        private fun privateSourceFile(ctx: Context, path: String): File? {
            return try {
                val requested = File(path)
                if (!requested.isAbsolute || Files.isSymbolicLink(requested.toPath())) return null
                val source = requested.canonicalFile
                val privateRoots = listOf(ctx.cacheDir, ctx.filesDir, ctx.noBackupFilesDir)
                    .map(File::getCanonicalFile)
                if (privateRoots.none { root -> source != root && source.parentWithin(root) }) {
                    return null
                }
                source.takeIf {
                    it.isFile && it.length() in 1..MAX_STAGED_IMAGE_BYTES &&
                        it == requested.absoluteFile
                }
            } catch (_: Exception) {
                null
            }
        }

        private fun stage(source: File, destination: File): Boolean {
            if (source == destination) return source.isFile
            if (destination.exists()) return false
            if (source.renameTo(destination)) return validStagedImageFile(destination)
            val temporary = File(destination.parentFile, destination.name + ".staging")
            return try {
                if (temporary.exists() || Files.isSymbolicLink(temporary.toPath())) return false
                source.copyTo(temporary, overwrite = false)
                if (!validStagedImageFile(temporary) || !temporary.renameTo(destination) ||
                    !validStagedImageFile(destination)
                ) return false
                if (!source.delete()) {
                    destination.delete()
                    return false
                }
                true
            } catch (_: Exception) {
                false
            } finally {
                if (temporary.exists() && temporary.isFile &&
                    !Files.isSymbolicLink(temporary.toPath())
                ) temporary.delete()
            }
        }

        private fun validStagedImage(ctx: Context, id: String, candidate: File): Boolean {
            val expected = stagedFile(ctx, id) ?: return false
            return candidate.absoluteFile == expected.absoluteFile &&
                validStagedImageFile(candidate)
        }

        private fun validStagedImageFile(candidate: File): Boolean = try {
            candidate.isFile && !Files.isSymbolicLink(candidate.toPath()) &&
                candidate.canonicalFile == candidate.absoluteFile &&
                candidate.length() in 1..MAX_STAGED_IMAGE_BYTES
        } catch (_: Exception) {
            false
        }

        private fun cleanupStagedImage(ctx: Context, itemId: String): Boolean {
            val staged = stagedFile(ctx, itemId) ?: return false
            val files = listOf(
                staged,
                File(staged.parentFile, staged.name + ".tmp"),
                File(staged.parentFile, staged.name + ".staging"),
            )
            return files.all { target ->
                try {
                    when {
                        !target.exists() -> true
                        Files.isSymbolicLink(target.toPath()) -> false
                        target.canonicalFile != target.absoluteFile -> false
                        !target.isFile -> false
                        else -> target.delete()
                    }
                } catch (_: Exception) {
                    false
                }
            }
        }

        private fun File.parentWithin(root: File): Boolean {
            var cursor: File? = parentFile
            while (cursor != null) {
                if (cursor == root) return true
                cursor = cursor.parentFile
            }
            return false
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val itemId = inputData.getString(INPUT_ITEM_ID)?.trim()?.lowercase().orEmpty()
        if (!SAFE_CAPTURE_SYNC_ID.matches(itemId)) return@withContext Result.failure()
        val item = ScanSearchQueue.processingItemForWorker(ctx, itemId)
        if (item == null) {
            return@withContext if (cleanupStagedImage(ctx, itemId)) Result.success()
            else Result.retry()
        }
        val currentOwner = Prefs.userId(ctx).trim().lowercase()
        if (!Auth.signedIn(ctx) || currentOwner != item.ownerId) {
            ScanSearchQueue.failProcessingForWorker(
                ctx,
                item.id,
                item.ownerId,
                ctx.getString(R.string.scan_queue_notification_account_changed),
            )
            return@withContext if (cleanupStagedImage(ctx, itemId)) Result.success()
            else Result.retry()
        }
        val staged = stagedFile(ctx, itemId)
        if (staged == null || !validStagedImage(ctx, itemId, staged)) {
            return@withContext terminalFailure(
                itemId,
                ctx.getString(R.string.scan_queue_notification_photo_missing),
            )
        }
        val mistralKey = Prefs.mistralKey(ctx).trim()
        if (mistralKey.isEmpty()) {
            return@withContext terminalFailure(
                itemId,
                ctx.getString(R.string.scan_queue_notification_key_missing),
            )
        }

        try {
            Pipeline.standardizeInPlace(staged)
            val signature = if (item.photoRole == ScanSearchPhotoRole.COVER) {
                extractCoverVisualSignature(staged).orEmpty()
            } else {
                ""
            }
            var ocrWarning: String? = null
            val recognized = try {
                boundedScanSearchOcrText(Pipeline.coverOcr(staged, mistralKey))
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                if (error is IOException && error !is Pipeline.PermanentError &&
                    runAttemptCount < MAX_TRANSIENT_RETRIES
                ) throw error
                if (item.photoRole != ScanSearchPhotoRole.COVER || signature.isEmpty()) throw error
                ocrWarning = ctx.getString(R.string.scan_queue_notification_visual_fallback)
                ""
            }
            if (!CoverScannerActivity.hasReadableCoverText(recognized) && signature.isEmpty()) {
                return@withContext terminalFailure(
                    itemId,
                    ctx.getString(R.string.scan_queue_no_evidence),
                )
            }
            if (!CoverScannerActivity.hasReadableCoverText(recognized) &&
                signature.isNotEmpty() && ocrWarning == null
            ) {
                ocrWarning = ctx.getString(R.string.scan_queue_notification_visual_fallback)
            }
            val completed = ScanSearchQueue.completeProcessing(
                ctx,
                itemId,
                recognized,
                signature,
            ) ?: return@withContext processingStateChanged(itemId)
            ocrWarning?.let { ScanSearchNotifications.failure(ctx, completed.id, it) }
            if (completed.scanCollectionId.isNotEmpty()) {
                ScanSearchQueueSyncWorker.enqueue(ctx)
            }
            if (cleanupStagedImage(ctx, itemId)) Result.success() else Result.retry()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Pipeline.PermanentError) {
            terminalFailure(
                itemId,
                ctx.getString(R.string.scan_queue_notification_ocr_failed),
            )
        } catch (_: IOException) {
            if (runAttemptCount < MAX_TRANSIENT_RETRIES) Result.retry()
            else terminalFailure(
                itemId,
                ctx.getString(R.string.scan_queue_notification_ocr_failed),
            )
        } catch (_: Exception) {
            terminalFailure(itemId, ctx.getString(R.string.scan_queue_notification_ocr_failed))
        }
    }

    private fun processingStateChanged(itemId: String): Result {
        val current = ScanSearchQueue.processingItemForWorker(applicationContext, itemId)
        if (current != null) return Result.retry()
        return if (cleanupStagedImage(applicationContext, itemId)) Result.success()
        else Result.retry()
    }

    private fun terminalFailure(itemId: String, message: String): Result {
        val failed = ScanSearchQueue.failProcessing(applicationContext, itemId, message)
            ?: return processingStateChanged(itemId)
        ScanSearchNotifications.failure(applicationContext, failed.id, failed.errorMessage)
        if (failed.dirty) ScanSearchQueueSyncWorker.enqueue(applicationContext)
        return if (cleanupStagedImage(applicationContext, itemId)) Result.success()
        else Result.retry()
    }
}
