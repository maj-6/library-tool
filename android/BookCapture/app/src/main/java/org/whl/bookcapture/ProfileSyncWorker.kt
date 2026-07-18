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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/** Retries locally saved profile edits without conflating device persistence
 * with cloud success. Pending edits are tied to their account so signing into
 * a different account can never upload the prior user's values. */
class ProfileSyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        const val WORK_NAME = "profile-sync"

        fun enqueue(ctx: Context) {
            if (!Auth.signedIn(ctx) || Prefs.pendingProfileFields(ctx).isEmpty()) return
            val request = OneTimeWorkRequestBuilder<ProfileSyncWorker>()
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                WORK_NAME, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        // Pin the account before reading values or acquiring a bearer token.
        // pushProfile checks it again after token refresh, and Prefs uses the
        // same owner guard when clearing or recording the result.
        val owner = Prefs.pendingProfileOwner(ctx)
        val fields = Prefs.pendingProfileFields(ctx)
        if (owner.isEmpty() || fields.isEmpty()) return@withContext Result.success()
        if (!Auth.signedIn(ctx) || owner != Prefs.userId(ctx)) {
            return@withContext Result.success()
        }

        val displayName = Prefs.displayName(ctx)
            .takeIf { Prefs.PROFILE_DISPLAY_NAME in fields }
        val mistral = Prefs.mistralKey(ctx)
            .takeIf { Prefs.PROFILE_MISTRAL in fields }
        val deepseek = Prefs.deepseekKey(ctx)
            .takeIf { Prefs.PROFILE_DEEPSEEK in fields }
        val error = Auth.pushProfile(
            ctx,
            expectedOwner = owner,
            displayName = displayName,
            mistral = mistral,
            deepseek = deepseek,
        )
        if (error == null) {
            // A later Save can land while this blocking request is in flight.
            // Clear only values that are still the ones this worker uploaded;
            // the appended worker then sends any newer local intent.
            val unchanged = fields.filterTo(mutableSetOf()) { field ->
                when (field) {
                    Prefs.PROFILE_DISPLAY_NAME -> Prefs.displayName(ctx) == displayName
                    Prefs.PROFILE_MISTRAL -> Prefs.mistralKey(ctx) == mistral
                    Prefs.PROFILE_DEEPSEEK -> Prefs.deepseekKey(ctx) == deepseek
                    else -> false
                }
            }
            Prefs.clearProfilePending(ctx, unchanged, expectedOwner = owner)
            Result.success()
        } else {
            Prefs.setProfileSyncError(ctx, error, expectedOwner = owner)
            // WorkManager's exponential backoff is the durable retry budget;
            // never turn a still-pending offline edit into a false success.
            Result.retry()
        }
    }
}
