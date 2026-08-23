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

internal enum class ScanSearchFailureRefreshAction {
    ACKNOWLEDGE_ABSENT,
    RETRY_BLANK_RESERVATION,
    MERGE_ADVANCED,
}

internal fun scanSearchFailureRefreshAction(
    queueId: String,
    cloudItems: List<ScanSearchQueueItem>,
): ScanSearchFailureRefreshAction {
    val remote = cloudItems.firstOrNull { it.id == queueId }
        ?: return ScanSearchFailureRefreshAction.ACKNOWLEDGE_ABSENT
    return if (remote.isBlankCloudReservation()) {
        ScanSearchFailureRefreshAction.RETRY_BLANK_RESERVATION
    } else {
        ScanSearchFailureRefreshAction.MERGE_ADVANCED
    }
}

/** Pushes durable text/cover descriptors and pulls deferred review proposals. */
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
        var notificationItemId = ""
        var notificationItemIds = emptyList<String>()
        fun terminalSyncFailure(): Result {
            notificationItemId.takeIf(SAFE_CAPTURE_SYNC_ID::matches)?.let {
                ScanSearchNotifications.syncFailure(ctx, it)
            }
            return Result.failure()
        }
        try {
            val store = ScanSearchQueue.read(ctx)
            if (!store.valid) return@withContext Result.failure()
            notificationItemIds = store.items.asSequence()
                .filter { it.ownerId == owner && it.scanCollectionId.isNotEmpty() }
                .map(ScanSearchQueueItem::id)
                .toList()
            notificationItemId = notificationItemIds.firstOrNull().orEmpty()
            store.items.asSequence()
                .filter {
                    it.dirty && it.ownerId == owner &&
                        (it.status == ScanSearchStatus.PENDING ||
                            it.status == ScanSearchStatus.MATCHED ||
                            it.status == ScanSearchStatus.REJECTED ||
                            (it.status == ScanSearchStatus.FAILED &&
                                it.errorMessage.isNotEmpty())) &&
                        it.scanCollectionId.isNotEmpty()
                }
                .forEach { expected ->
                    notificationItemId = expected.id
                    if (!Auth.signedIn(ctx) || Prefs.userId(ctx).trim().lowercase() != owner) {
                        return@withContext Result.success()
                    }
                    if (expected.status == ScanSearchStatus.FAILED) {
                        var cleanupComplete = client.fail(expected.id, expected.revision)
                        var authoritative: List<ScanSearchQueueItem>? = null
                        if (!cleanupComplete) {
                            val firstSnapshot = client.queue()
                            authoritative = firstSnapshot
                            when (scanSearchFailureRefreshAction(expected.id, firstSnapshot)) {
                                ScanSearchFailureRefreshAction.ACKNOWLEDGE_ABSENT -> {
                                    // A prior delete may have committed even if its
                                    // response was lost. Retain the clean local error
                                    // until the inspector dismisses it.
                                    cleanupComplete = true
                                }
                                ScanSearchFailureRefreshAction.RETRY_BLANK_RESERVATION -> {
                                    // A rev-0 local failure can race its already-created
                                    // rev-1 reservation. Retry once with the revision we
                                    // just observed; never delete a later evidenced row.
                                    val remote = firstSnapshot.first { it.id == expected.id }
                                    cleanupComplete = client.fail(remote.id, remote.revision)
                                    if (!cleanupComplete) {
                                        val refreshedSnapshot = client.queue()
                                        authoritative = refreshedSnapshot
                                        when (scanSearchFailureRefreshAction(
                                            expected.id,
                                            refreshedSnapshot,
                                        )) {
                                            ScanSearchFailureRefreshAction.ACKNOWLEDGE_ABSENT ->
                                                cleanupComplete = true
                                            ScanSearchFailureRefreshAction.RETRY_BLANK_RESERVATION ->
                                                return@withContext Result.retry()
                                            ScanSearchFailureRefreshAction.MERGE_ADVANCED -> Unit
                                        }
                                    }
                                }
                                ScanSearchFailureRefreshAction.MERGE_ADVANCED -> Unit
                            }
                        }
                        if (cleanupComplete) {
                            if (!ScanSearchQueue.acknowledgeFailureCleanup(ctx, expected)) {
                                return@withContext Result.retry()
                            }
                        } else {
                            if (!ScanSearchQueue.mergeCloudAfterStaleMutation(
                                    ctx,
                                    owner,
                                    expected,
                                    requireNotNull(authoritative),
                                )
                            ) {
                                return@withContext Result.retry()
                            }
                            ScanSearchNotifications.clearFailure(ctx, expected.id)
                        }
                        ScanSearchNotifications.clearSyncFailure(ctx, expected.id)
                        return@forEach
                    }
                    val accepted = try {
                        when (expected.status) {
                            ScanSearchStatus.PENDING -> client.enqueue(expected)
                            ScanSearchStatus.MATCHED ->
                                client.approve(expected.id, expected.matchedCaptureId)
                            ScanSearchStatus.REJECTED ->
                                client.reject(expected.id, expected.candidateCaptureId)
                            else -> error("unsupported local scan-search mutation")
                        }
                    } catch (error: SupabaseClient.HttpException) {
                        if (expected.status !in setOf(
                                ScanSearchStatus.MATCHED,
                                ScanSearchStatus.REJECTED,
                            ) || !isStaleScanProposalError(error)
                        ) throw error
                        val authoritative = client.queue()
                        if (!ScanSearchQueue.mergeCloudAfterStaleMutation(
                                ctx,
                                owner,
                                expected,
                                authoritative,
                            )
                        ) {
                            return@withContext Result.retry()
                        }
                        ScanSearchNotifications.clearSyncFailure(ctx, expected.id)
                        return@forEach
                    }
                    if (!ScanSearchQueue.acknowledge(ctx, expected, accepted)) {
                        return@withContext Result.retry()
                    }
                    ScanSearchNotifications.clearSyncFailure(ctx, expected.id)
                }
            if (!ScanSearchQueue.mergeCloud(ctx, owner, client.queue())) {
                return@withContext terminalSyncFailure()
            }
            notificationItemIds.forEach { ScanSearchNotifications.clearSyncFailure(ctx, it) }
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
            if (e.code in 400..499 && e.code !in setOf(408, 429)) terminalSyncFailure()
            else Result.retry()
        } catch (_: SupabaseClient.InvalidResponse) {
            terminalSyncFailure()
        } catch (e: CancellationException) {
            throw e
        } catch (_: IOException) {
            Result.retry()
        } catch (_: Exception) {
            terminalSyncFailure()
        }
    }
}
