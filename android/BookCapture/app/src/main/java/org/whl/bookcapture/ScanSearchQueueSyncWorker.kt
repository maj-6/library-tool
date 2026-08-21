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
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Pushes durable OCR-only search requests and pulls decisions from cloud. */
internal class ScanSearchQueueSyncWorker(ctx: Context, params: WorkerParameters) :
    CoroutineWorker(ctx, params) {

    companion object {
        const val WORK_NAME = "scan-search-queue-sync"

        fun enqueue(ctx: Context, guaranteed: Boolean = true) {
            if (!Prefs.configured(ctx) || !Auth.signedIn(ctx) || Prefs.userId(ctx).isEmpty()) {
                return
            }
            val request = OneTimeWorkRequestBuilder<ScanSearchQueueSyncWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                WORK_NAME,
                if (guaranteed) ExistingWorkPolicy.APPEND_OR_REPLACE else ExistingWorkPolicy.KEEP,
                request,
            )
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) return@withContext Result.success()
        val owner = Prefs.userId(ctx).trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(owner)) return@withContext Result.success()
        val client = ScanWorkflowClient(ctx, owner)
        try {
            val store = ScanSearchQueue.read(ctx)
            if (!store.valid) return@withContext Result.failure()
            store.items.asSequence()
                .filter {
                    it.dirty && it.ownerId == owner &&
                        (it.status == ScanSearchStatus.PENDING ||
                            it.status == ScanSearchStatus.MATCHED)
                }
                .forEach { expected ->
                    if (!Auth.signedIn(ctx) || Prefs.userId(ctx).trim().lowercase() != owner) {
                        return@withContext Result.success()
                    }
                    var accepted = client.enqueue(expected)
                    if (expected.status == ScanSearchStatus.MATCHED) {
                        accepted = client.complete(expected.id, expected.matchedCaptureId)
                    }
                    if (!ScanSearchQueue.acknowledge(ctx, expected, accepted)) {
                        return@withContext Result.retry()
                    }
                }
            if (!ScanSearchQueue.mergeCloud(ctx, owner, client.queue())) {
                return@withContext Result.failure()
            }
            Result.success()
        } catch (e: SupabaseClient.SignedOut) {
            if (Auth.signedIn(ctx) && Prefs.userId(ctx).trim().lowercase() == owner) {
                Result.retry()
            } else {
                Result.success()
            }
        } catch (_: SupabaseClient.AccountChanged) {
            Result.success()
        } catch (e: SupabaseClient.HttpException) {
            if (e.code in 400..499 && e.code !in setOf(408, 429)) Result.failure()
            else Result.retry()
        } catch (_: SupabaseClient.InvalidResponse) {
            Result.failure()
        } catch (e: CancellationException) {
            throw e
        } catch (_: IOException) {
            Result.retry()
        } catch (_: Exception) {
            Result.failure()
        }
    }
}
