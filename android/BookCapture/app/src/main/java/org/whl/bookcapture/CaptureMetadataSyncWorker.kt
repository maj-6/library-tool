package org.whl.bookcapture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.Operation
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Pulls desktop-authored registered-book metadata and shared review state.
 * Home may enqueue [enqueuePull], which never uploads local edits.
 * [enqueueExplicitSync] is the only path that sends dirty phone review state.
 */
class CaptureMetadataSyncWorker(ctx: Context, params: WorkerParameters) :
    CoroutineWorker(ctx, params) {

    companion object {
        const val WORK_NAME = "capture-metadata-explicit-sync"
        const val PULL_WORK_NAME = "capture-metadata-pull"
        private const val KEY_PUSH_REVIEWS = "push-reviews"
        private const val KEY_ROUTE = "metadata-route"
        private const val MAX_PASSES = 4

        fun enqueuePull(ctx: Context) {
            enqueue(ctx, pushReviews = false)
        }

        fun enqueueExplicitSync(ctx: Context) {
            enqueue(ctx, pushReviews = true)
        }

        /** Persist the recovery work before UploadWorker closes its receipt.
         * This is called on Dispatchers.IO; waiting here closes the crash
         * window between an asynchronous enqueue and markCaptureSynced. */
        internal fun enqueueExplicitSyncDurably(ctx: Context): Boolean {
            val operation = enqueue(ctx, pushReviews = true) ?: return false
            operation.result.get()
            return true
        }

        private fun enqueue(ctx: Context, pushReviews: Boolean): Operation? {
            val lanConfigured = Prefs.lanHost(ctx).isNotEmpty()
            val cloudConfigured = Prefs.configured(ctx) && Auth.signedIn(ctx) &&
                Prefs.userId(ctx).isNotEmpty()
            if (!lanConfigured && !cloudConfigured) return null
            val request = OneTimeWorkRequestBuilder<CaptureMetadataSyncWorker>()
                .setInputData(workDataOf(
                    KEY_PUSH_REVIEWS to pushReviews,
                    KEY_ROUTE to "",
                ))
                .setConstraints(
                    Constraints.Builder()
                        // Offline Wi-Fi to a paired desktop is still a valid
                        // network even when Android has not validated Internet
                        // access. The worker probes the chosen transport and
                        // explicitly retries transient failures.
                        .setRequiredNetworkType(
                            if (lanConfigured) NetworkType.NOT_REQUIRED
                            else NetworkType.CONNECTED,
                        )
                        .build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .build()
            return WorkManager.getInstance(ctx).enqueueUniqueWork(
                if (pushReviews) WORK_NAME else PULL_WORK_NAME,
                if (pushReviews) ExistingWorkPolicy.APPEND_OR_REPLACE else ExistingWorkPolicy.KEEP,
                request,
            )
        }

        private fun enqueueCloudFollowup(ctx: Context, pushReviews: Boolean) {
            if (!Prefs.configured(ctx) || !Auth.signedIn(ctx) ||
                Prefs.userId(ctx).isEmpty()) return
            val request = OneTimeWorkRequestBuilder<CaptureMetadataSyncWorker>()
                .setInputData(workDataOf(
                    KEY_PUSH_REVIEWS to pushReviews,
                    KEY_ROUTE to "cloud",
                ))
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                if (pushReviews) WORK_NAME else PULL_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request,
            )
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val pushReviews = inputData.getBoolean(KEY_PUSH_REVIEWS, false)
        val forcedRoute = inputData.getString(KEY_ROUTE).orEmpty()
        val hasLanEntries = Entries.recent(ctx).any { isLanMetadataEntry(ctx, it) }
        if (forcedRoute != "cloud" && hasLanEntries &&
            Prefs.lanHost(ctx).isNotEmpty()) {
            val lan = try { LanClient(ctx) } catch (_: Exception) { null }
            if (lan != null && lan.ping()) {
                return@withContext syncLan(ctx, lan, pushReviews)
            }
            if (Entries.recent(ctx).any { isCloudMetadataEntry(ctx, it) }) {
                enqueueCloudFollowup(ctx, pushReviews)
            }
            return@withContext Result.retry()
        }
        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) return@withContext Result.success()
        val owner = Prefs.userId(ctx)
        if (owner.isEmpty()) return@withContext Result.success()
        val client = SupabaseClient(ctx, owner)
        try {
            repeat(if (pushReviews) MAX_PASSES else 1) {
                if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                val entries = Entries.recent(ctx).filter { entry ->
                    entry.uploaded && cloudUploadOwnership(
                        readCaptureCreator(ctx, entry.dir),
                        owner,
                    ) == CloudUploadOwnership.ALLOWED &&
                        entry.deliveryTransport != "lan"
                }
                if (entries.isEmpty()) return@withContext Result.success()
                val ids = entries.map { it.id }
                val desktopRows = client.desktopBookMetadata(ids)
                val reviewRows = client.captureReviews(ids)

                for ((captureId, metadata) in desktopRows) {
                    if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                    val result = EntryOperationLocks.withLock(captureId) {
                        val entry = Entries.find(ctx, captureId)
                            ?: return@withLock DesktopMetadataApplyResult.STALE
                        if (!entry.uploaded || cloudUploadOwnership(
                                readCaptureCreator(ctx, entry.dir), owner,
                            ) != CloudUploadOwnership.ALLOWED) {
                            return@withLock DesktopMetadataApplyResult.STALE
                        }
                        CaptureMetadataStore.applyDesktopBook(entry.dir, metadata)
                    }
                    if (result == DesktopMetadataApplyResult.CONFLICT) {
                        throw CaptureMetadataStateException(
                            "conflicting desktop metadata revision for $captureId",
                        )
                    }
                }

                var plannedWrites = 0
                for (entrySnapshot in entries) {
                    if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                    val plan = EntryOperationLocks.withLock(entrySnapshot.id) {
                        val entry = Entries.find(ctx, entrySnapshot.id) ?: return@withLock null
                        var planned: CaptureReviewCloudWrite? = null
                        if (!CaptureMetadataStore.mutateReview(entry.dir) { local ->
                                val merged = mergeCaptureReview(local, reviewRows[entry.id])
                                    ?: return@mutateReview null
                                merged.conflict?.let { throw CaptureMetadataStateException(it) }
                                planned = merged.write
                                merged.store
                            }
                        ) {
                            throw CaptureMetadataStateException("could not persist capture review")
                        }
                        planned
                    }
                    if (!pushReviews || plan == null) continue
                    plannedWrites += 1
                    val accepted = client.writeCaptureReview(plan) ?: continue
                    EntryOperationLocks.withLock(entrySnapshot.id) {
                        val entry = Entries.find(ctx, entrySnapshot.id) ?: return@withLock
                        if (!CaptureMetadataStore.mutateReview(entry.dir) { latest ->
                                latest?.let {
                                    acknowledgeCaptureReviewWrite(it, plan.state, accepted)
                                }
                            }
                        ) {
                            throw CaptureMetadataStateException(
                                "could not acknowledge capture review",
                            )
                        }
                    }
                }
                if (!pushReviews || plannedWrites == 0) return@withContext Result.success()
                // Re-fetch after every write pass. A CAS miss or UI edit made
                // during HTTP remains dirty and is reconciled against the new
                // server revision before the worker can finish.
            }
            Result.retry()
        } catch (e: SupabaseClient.SignedOut) {
            if (sameSignedInOwner(ctx, owner)) Result.retry() else Result.success()
        } catch (e: SupabaseClient.AccountChanged) {
            Result.success()
        } catch (e: SupabaseClient.InvalidResponse) {
            permanentFailure(e.message)
        } catch (e: SupabaseClient.HttpException) {
            if (e.code in 400..499 && e.code != 408 && e.code != 429) {
                permanentFailure(e.message)
            } else {
                Result.retry()
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: CaptureMetadataStateException) {
            permanentFailure(e.message)
        } catch (_: IOException) {
            Result.retry()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    private suspend fun syncLan(
        ctx: Context,
        client: LanClient,
        pushReviews: Boolean,
    ): Result {
        try {
            repeat(if (pushReviews) MAX_PASSES else 1) {
                val entries = Entries.recent(ctx).filter { entry ->
                    isLanMetadataEntry(ctx, entry) &&
                        SAFE_CAPTURE_SYNC_ID.matches(entry.id)
                }
                if (entries.isEmpty()) return finishLanMetadata(ctx, pushReviews)
                for (batch in entries.chunked(CAPTURE_METADATA_BATCH_SIZE)) {
                    val sent = linkedMapOf<String, CaptureReviewMetadata>()
                    val outgoing = if (pushReviews) batch.mapNotNull { entry ->
                        val store = EntryOperationLocks.withLock(entry.id) {
                            Entries.find(ctx, entry.id)?.let {
                                when (val state = CaptureMetadataStore.reviewState(it.dir)) {
                                    CaptureReviewFileState.Missing -> null
                                    is CaptureReviewFileState.Valid -> state.store
                                    CaptureReviewFileState.Corrupt ->
                                        throw CaptureMetadataStateException(
                                            "capture review sidecar is corrupt",
                                        )
                                }
                            }
                        }
                        store?.takeIf { it.dirty }?.current?.also {
                            sent[entry.id] = it
                        }?.let(::captureReviewLanBody)
                    } else emptyList()
                    val exchange = client.syncMetadata(batch.map { it.id }, outgoing)
                    for ((captureId, metadata) in exchange.books) {
                        val applied = EntryOperationLocks.withLock(captureId) {
                            val entry = Entries.find(ctx, captureId)
                                ?: return@withLock DesktopMetadataApplyResult.STALE
                            CaptureMetadataStore.applyDesktopBook(entry.dir, metadata)
                        }
                        if (applied == DesktopMetadataApplyResult.CONFLICT) {
                            throw CaptureMetadataStateException(
                                "conflicting LAN desktop metadata revision for $captureId",
                            )
                        }
                    }
                    for (entrySnapshot in batch) {
                        val remote = exchange.reviews[entrySnapshot.id] ?: continue
                        EntryOperationLocks.withLock(entrySnapshot.id) {
                            val entry = Entries.find(ctx, entrySnapshot.id)
                                ?: return@withLock
                            if (!CaptureMetadataStore.mutateReview(entry.dir) { local ->
                                    val sentState = sent[entry.id]
                                    if (sentState != null &&
                                        entry.id !in exchange.rejectedReviewIds && local != null) {
                                        acknowledgeCaptureReviewWrite(local, sentState, remote)
                                    } else {
                                        mergeCaptureReview(local, remote)?.let { merged ->
                                            merged.conflict?.let {
                                                throw CaptureMetadataStateException(it)
                                            }
                                            merged.store
                                        }
                                    }
                                }
                            ) {
                                throw CaptureMetadataStateException(
                                    "could not persist LAN capture review",
                                )
                            }
                        }
                    }
                }
                if (!pushReviews) return finishLanMetadata(ctx, pushReviews)
                val dirtyRemaining = Entries.recent(ctx).any { entry ->
                    isLanMetadataEntry(ctx, entry) &&
                        CaptureMetadataStore.hasPendingReviewSync(entry.dir)
                }
                if (!dirtyRemaining) return finishLanMetadata(ctx, pushReviews)
            }
            return Result.retry()
        } catch (e: LanClient.HttpException) {
            return if (e.code in 400..499 && e.code != 408 && e.code != 429) {
                permanentFailure(e.message)
            } else Result.retry()
        } catch (e: CancellationException) {
            throw e
        } catch (e: CaptureMetadataStateException) {
            return permanentFailure(e.message)
        } catch (_: IOException) {
            return Result.retry()
        } catch (_: Exception) {
            return Result.retry()
        }
    }

    private fun finishLanMetadata(ctx: Context, pushReviews: Boolean): Result {
        if (Entries.recent(ctx).any { isCloudMetadataEntry(ctx, it) }) {
            enqueueCloudFollowup(ctx, pushReviews)
        }
        return Result.success()
    }

    private fun permanentFailure(message: String?): Result = Result.failure(
        Data.Builder().putString("error", message.orEmpty().take(500)).build(),
    )
}

private fun sameSignedInOwner(ctx: Context, expectedOwner: String): Boolean =
    Auth.signedIn(ctx) && expectedOwner.isNotEmpty() && Prefs.userId(ctx) == expectedOwner

private fun isLanMetadataEntry(ctx: Context, entry: Entries.Entry): Boolean {
    if (!entry.uploaded) return false
    if (entry.deliveryTransport == "lan") return true
    if (entry.deliveryTransport.isNotEmpty()) return false
    // Legacy sent folders predate the explicit marker. An account-owned row
    // remains cloud-routed; an imported local capture is the conservative LAN
    // fallback and will only be queried from the paired desktop.
    return entry.cloudStatus == "imported" && cloudUploadOwnership(
        readCaptureCreator(ctx, entry.dir), Prefs.userId(ctx),
    ) != CloudUploadOwnership.ALLOWED
}

private fun isCloudMetadataEntry(ctx: Context, entry: Entries.Entry): Boolean {
    if (!entry.uploaded || entry.deliveryTransport == "lan") return false
    if (entry.deliveryTransport == "cloud") return true
    return cloudUploadOwnership(
        readCaptureCreator(ctx, entry.dir), Prefs.userId(ctx),
    ) == CloudUploadOwnership.ALLOWED
}

private class CaptureMetadataStateException(message: String) : IOException(message)
