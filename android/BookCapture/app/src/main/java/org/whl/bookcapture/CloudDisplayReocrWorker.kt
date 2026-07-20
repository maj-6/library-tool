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
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Re-runs Mistral OCR against a nonlinear corrected display derivative so its
 * polygons use the pixels the details screen actually renders. This worker
 * never rewrites canonical OCR text, metadata, photo_N.jpg, or original_*.
 */
class CloudDisplayReocrWorker(ctx: Context, params: WorkerParameters) :
    CoroutineWorker(ctx, params) {

    companion object {
        private const val WORK_PREFIX = "cloud-display-reocr-"
        private const val KEY_CAPTURE_ID = "capture-id"
        private const val KEY_ASSET_ID = "asset-id"
        private const val KEY_JOB_ID = "job-id"
        private const val KEY_DISPLAY_REFERENCE = "display-reference"
        private const val KEY_DISPLAY_SHA256 = "display-sha256"
        private const val KEY_DISPLAY_REVISION = "display-revision"

        fun enqueuePending(ctx: Context, captureId: String) {
            val entry = Entries.find(ctx, captureId) ?: return
            PhotoAssetStore.pendingCloudDisplayReocrTargets(entry.dir).forEach { target ->
                val request = OneTimeWorkRequestBuilder<CloudDisplayReocrWorker>()
                    .setInputData(workDataOf(
                        KEY_CAPTURE_ID to target.captureId,
                        KEY_ASSET_ID to target.assetId,
                        KEY_JOB_ID to target.jobId,
                        KEY_DISPLAY_REFERENCE to target.displayReference,
                        KEY_DISPLAY_SHA256 to target.displaySha256,
                        KEY_DISPLAY_REVISION to target.displayRevision,
                    ))
                    .setConstraints(
                        Constraints.Builder()
                            .setRequiredNetworkType(NetworkType.CONNECTED)
                            .build(),
                    )
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                    .build()
                WorkManager.getInstance(ctx).enqueueUniqueWork(
                    workName(target),
                    ExistingWorkPolicy.KEEP,
                    request,
                )
            }
        }

        /** Settings uses this after a key change, including for already-sent
         * entries whose original import polling chain has ended. */
        fun enqueueAllPending(ctx: Context) {
            Entries.recent(ctx).forEach { entry -> enqueuePending(ctx, entry.id) }
        }

        internal fun workName(target: CloudDisplayReocrTarget): String =
            "$WORK_PREFIX${target.captureId.take(48)}-${target.assetId.take(48)}-" +
                "r${target.displayRevision}-${target.displaySha256.take(16)}"
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val target = inputTarget() ?: return@withContext Result.failure()
        // Keep the durable pending marker intact. Saving a key later calls
        // enqueueAllPending(), so missing credentials cannot become a silent,
        // permanent no-op.
        val mistralKey = Prefs.mistralKey(applicationContext)
        if (mistralKey.isBlank()) return@withContext Result.success()

        EntryOperationLocks.withLock(target.captureId) {
            val entry = Entries.find(applicationContext, target.captureId)
                ?: return@withLock Result.success()
            val display = PhotoAssetStore.cloudDisplayReocrFile(entry.dir, target)
                ?: return@withLock Result.success()
            try {
                val result = Pipeline.ocrResult(display, mistralKey)
                val geometry = result.geometry
                if (geometry == null || geometry.regions.isEmpty()) {
                    // Do not carry the pre-dewarp boxes. Leave the marker so a
                    // later cloud poll or credential save can explicitly retry.
                    return@withLock Result.failure()
                }
                if (PhotoAssetStore.mergeCloudDisplayReocrGeometry(
                        entry.dir,
                        target,
                        geometry,
                    )) Result.success()
                else if (PhotoAssetStore.cloudDisplayReocrFile(entry.dir, target) == null) {
                    // The target was superseded while work was queued.
                    Result.success()
                } else retryOrFail()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Pipeline.PermanentError) {
                // A replacement key can make this actionable later. The
                // pending marker intentionally remains discoverable.
                Result.failure()
            } catch (_: Exception) {
                retryOrFail()
            }
        }
    }

    private fun inputTarget(): CloudDisplayReocrTarget? {
        val captureId = inputData.getString(KEY_CAPTURE_ID)?.takeIf { it.isNotBlank() }
            ?: return null
        val assetId = inputData.getString(KEY_ASSET_ID)?.takeIf { it.isNotBlank() }
            ?: return null
        val jobId = inputData.getString(KEY_JOB_ID)?.takeIf { it.isNotBlank() }
            ?: return null
        val reference = inputData.getString(KEY_DISPLAY_REFERENCE)?.takeIf { it.isNotBlank() }
            ?: return null
        val hash = inputData.getString(KEY_DISPLAY_SHA256)
            ?.takeIf { it.matches(Regex("[0-9a-f]{64}")) } ?: return null
        val revision = inputData.getInt(KEY_DISPLAY_REVISION, 0).takeIf { it > 0 }
            ?: return null
        return CloudDisplayReocrTarget(
            captureId,
            assetId,
            jobId,
            reference,
            hash,
            revision,
        )
    }

    private fun retryOrFail(): Result =
        if (shouldRetryCloudDisplayReocr(runAttemptCount)) Result.retry() else Result.failure()
}
