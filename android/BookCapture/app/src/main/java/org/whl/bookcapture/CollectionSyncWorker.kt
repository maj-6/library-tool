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
import java.util.concurrent.TimeUnit

internal fun retryCollectionSyncAfterTokenFailure(
    stillSignedIn: Boolean,
    currentOwner: String,
    expectedOwner: String,
): Boolean = stillSignedIn && currentOwner == expectedOwner && expectedOwner.isNotEmpty()

internal fun collectionSyncWorkPolicy(guaranteed: Boolean): ExistingWorkPolicy =
    if (guaranteed) ExistingWorkPolicy.APPEND_OR_REPLACE else ExistingWorkPolicy.KEEP

/**
 * Reconciles the phone's local-first collection store with shared Supabase
 * rows. Signed-out mode never enters this worker; edits remain durable locally
 * and are picked up on the first sign-in.
 */
class CollectionSyncWorker(ctx: Context, params: WorkerParameters) :
    CoroutineWorker(ctx, params) {

    companion object {
        const val WORK_NAME = "collection-sync"
        private const val MAX_PASSES = 4

        /** Guaranteed tail pass for a persisted mutation or first sign-in. */
        fun enqueue(ctx: Context) = enqueue(ctx, guaranteed = true)

        /** Best-effort pull on Home resume; repeated offline resumes coalesce. */
        fun enqueueCoalesced(ctx: Context) = enqueue(ctx, guaranteed = false)

        private fun enqueue(ctx: Context, guaranteed: Boolean) {
            if (!Prefs.configured(ctx) || !Auth.signedIn(ctx) || Prefs.userId(ctx).isEmpty()) {
                return
            }
            val request = OneTimeWorkRequestBuilder<CollectionSyncWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .build()
            // A mutation can land while an older pass is in flight, so its
            // guaranteed request appends. Opportunistic resume pulls use KEEP
            // to avoid an unbounded offline chain of identical GETs.
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                WORK_NAME,
                collectionSyncWorkPolicy(guaranteed),
                request,
            )
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) {
            return@withContext Result.success()
        }
        val owner = Prefs.userId(ctx)
        if (owner.isEmpty()) return@withContext Result.success()
        val client = SupabaseClient(ctx, owner)

        try {
            repeat(MAX_PASSES) {
                if (!Auth.signedIn(ctx) || Prefs.userId(ctx) != owner) {
                    return@withContext Result.success()
                }
                val cloud = client.collections()
                val writes = Collections.planSync(ctx, cloud)
                if (writes.isEmpty()) return@withContext Result.success()
                for (write in writes) {
                    if (!Auth.signedIn(ctx) || Prefs.userId(ctx) != owner) {
                        return@withContext Result.success()
                    }
                    // A null response means insert/CAS lost a race. The next
                    // pass re-fetches instead of overwriting that winner.
                    client.writeCollection(write)?.let { accepted ->
                        Collections.acknowledgeWrite(ctx, write.row, accepted)
                    }
                }
                // Always fetch and plan again. That hydrates returned server
                // timestamps, advances the shadow only after success, and sees
                // any local UI mutation that happened during these requests.
            }
            Result.retry()
        } catch (e: SupabaseClient.SignedOut) {
            // accessToken() also returns null when refresh hit a transient
            // network failure. Consume the work only if the session truly went
            // away; otherwise preserve the first-sign-in/edit sync intent.
            if (retryCollectionSyncAfterTokenFailure(
                    Auth.signedIn(ctx), Prefs.userId(ctx), owner,
                )
            ) Result.retry() else Result.success()
        } catch (e: SupabaseClient.AccountChanged) {
            Result.success()
        } catch (e: SupabaseClient.HttpException) {
            if (e.code in 400..499 && e.code != 408 && e.code != 429) {
                Result.failure()
            } else {
                Result.retry()
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            Result.retry()
        }
    }
}
