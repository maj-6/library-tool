package org.whl.bookcapture

import android.content.Context
import android.util.Log
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
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

internal const val ARCHIVED_METADATA_REFRESH_BATCH_SIZE = CAPTURE_METADATA_BATCH_SIZE * 2
internal const val ARCHIVED_METADATA_REFRESH_INTERVAL_MS = 30L * 60L * 1000L

internal data class ArchivedMetadataRefreshWindow(
    val ids: List<String>,
    val checkpoint: String?,
    val attempted: Boolean,
)

internal fun archivedMetadataRefreshDue(
    lastSuccessMs: Long,
    nowMs: Long,
    minimumIntervalMs: Long = ARCHIVED_METADATA_REFRESH_INTERVAL_MS,
): Boolean {
    require(minimumIntervalMs >= 0L) { "archive metadata interval cannot be negative" }
    return lastSuccessMs <= 0L || nowMs < lastSuccessMs ||
        nowMs - lastSuccessMs >= minimumIntervalMs
}

/**
 * Pick one stable, rotating archive window. A successful window is held for a
 * short cadence so repeatedly foregrounding Home refreshes current captures
 * without re-querying the entire retention archive.
 */
internal fun planArchivedMetadataRefresh(
    ids: Collection<String>,
    cursor: String?,
    lastSuccessMs: Long,
    nowMs: Long,
    batchSize: Int = ARCHIVED_METADATA_REFRESH_BATCH_SIZE,
    minimumIntervalMs: Long = ARCHIVED_METADATA_REFRESH_INTERVAL_MS,
): ArchivedMetadataRefreshWindow {
    require(batchSize > 0) { "archive metadata batch size must be positive" }
    require(minimumIntervalMs >= 0L) { "archive metadata interval cannot be negative" }
    if (!archivedMetadataRefreshDue(lastSuccessMs, nowMs, minimumIntervalMs)) {
        return ArchivedMetadataRefreshWindow(emptyList(), null, attempted = false)
    }
    val ordered = ids.asSequence()
        .map(String::trim)
        .filter(String::isNotEmpty)
        .distinct()
        .sorted()
        .toList()
    if (ordered.isEmpty()) {
        return ArchivedMetadataRefreshWindow(emptyList(), null, attempted = true)
    }

    val start = cursor?.trim()?.takeIf(String::isNotEmpty)?.let { checkpoint ->
        ordered.indexOfFirst { it > checkpoint }.takeIf { it >= 0 } ?: 0
    } ?: 0
    val rotated = if (start == 0) ordered else ordered.drop(start) + ordered.take(start)
    val selected = rotated.take(batchSize)
    return ArchivedMetadataRefreshWindow(selected, selected.lastOrNull(), attempted = true)
}

internal enum class MetadataSyncRouteDecision {
    LAN,
    CLOUD,
    RETRY_LAN,
    CLOUD_AND_RETRY_LAN,
    NONE,
}

/** Pure transport arbitration used after the optional LAN reachability probe. */
internal fun metadataSyncRouteDecision(
    forcedRoute: String,
    hasLanEntries: Boolean,
    hasCloudEntries: Boolean,
    lanConfigured: Boolean,
    cloudConfigured: Boolean,
    lanReachable: Boolean,
): MetadataSyncRouteDecision {
    val cloudAvailable = cloudConfigured && hasCloudEntries
    if (forcedRoute == "cloud") {
        return if (cloudAvailable) MetadataSyncRouteDecision.CLOUD
        else MetadataSyncRouteDecision.NONE
    }
    val lanAvailable = lanConfigured && hasLanEntries
    if (forcedRoute == "lan") {
        return when {
            !lanAvailable -> MetadataSyncRouteDecision.NONE
            lanReachable -> MetadataSyncRouteDecision.LAN
            else -> MetadataSyncRouteDecision.RETRY_LAN
        }
    }
    return when {
        // When both transports have work, cloud stays on the primary chain and
        // LAN gets its own retry chain. A desktop that answers /ping but fails
        // during /metadata must not prevent the cloud projection from landing.
        lanAvailable && cloudAvailable -> MetadataSyncRouteDecision.CLOUD_AND_RETRY_LAN
        lanAvailable && lanReachable -> MetadataSyncRouteDecision.LAN
        lanAvailable -> MetadataSyncRouteDecision.RETRY_LAN
        cloudAvailable -> MetadataSyncRouteDecision.CLOUD
        else -> MetadataSyncRouteDecision.NONE
    }
}

private data class ArchivedMetadataRefreshPlan(
    val scope: String,
    val ids: List<String>,
    val checkpoint: String?,
    val attempted: Boolean,
)

/** Cursor and cadence are transport/destination scoped, so changing accounts
 * or paired desktops cannot suppress the first refresh for the new source. */
private object ArchivedMetadataRefreshStore {
    private const val PREFERENCES = "capture-metadata-archive-refresh"
    private val lock = Any()

    fun isDue(ctx: Context, scope: String, nowMs: Long): Boolean = synchronized(lock) {
        val lastSuccessMs = ctx.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
            .getLong("success:$scope", 0L)
        archivedMetadataRefreshDue(lastSuccessMs, nowMs)
    }

    fun plan(
        ctx: Context,
        scope: String,
        ids: Collection<String>,
        nowMs: Long,
        enabled: Boolean,
    ): ArchivedMetadataRefreshPlan = synchronized(lock) {
        if (!enabled) {
            return@synchronized ArchivedMetadataRefreshPlan(
                scope = scope,
                ids = emptyList(),
                checkpoint = null,
                attempted = false,
            )
        }
        val prefs = ctx.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
        val window = planArchivedMetadataRefresh(
            ids = ids,
            cursor = prefs.getString("cursor:$scope", null),
            lastSuccessMs = prefs.getLong("success:$scope", 0L),
            nowMs = nowMs,
        )
        ArchivedMetadataRefreshPlan(
            scope = scope,
            ids = window.ids,
            checkpoint = window.checkpoint,
            attempted = window.attempted,
        )
    }

    fun markSucceeded(ctx: Context, plan: ArchivedMetadataRefreshPlan, nowMs: Long): Boolean {
        if (!plan.attempted) return true
        return synchronized(lock) {
            val editor = ctx.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE).edit()
            if (plan.checkpoint == null) {
                editor.remove("cursor:${plan.scope}")
            } else {
                editor.putString("cursor:${plan.scope}", plan.checkpoint)
            }
            editor.putLong("success:${plan.scope}", nowMs.coerceAtLeast(1L)).commit()
        }
    }
}

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
        const val LAN_WORK_NAME = "capture-metadata-explicit-lan-retry"
        const val LAN_PULL_WORK_NAME = "capture-metadata-pull-lan-retry"
        private const val TAG = "CaptureMetadataSync"
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

        /** LAN retries use their own unique chain. A retrying local desktop
         * must never sit in front of an Internet-constrained cloud pull. */
        private fun enqueueLanFollowup(ctx: Context, pushReviews: Boolean) {
            if (Prefs.lanHost(ctx).isEmpty()) return
            val request = OneTimeWorkRequestBuilder<CaptureMetadataSyncWorker>()
                .setInputData(workDataOf(
                    KEY_PUSH_REVIEWS to pushReviews,
                    KEY_ROUTE to "lan",
                ))
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.NOT_REQUIRED)
                        .build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                if (pushReviews) LAN_WORK_NAME else LAN_PULL_WORK_NAME,
                ExistingWorkPolicy.KEEP,
                request,
            )
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val pushReviews = inputData.getBoolean(KEY_PUSH_REVIEWS, false)
        val forcedRoute = inputData.getString(KEY_ROUTE).orEmpty()
        val owner = Prefs.userId(ctx)
        val lanHost = Prefs.lanHost(ctx)
        val cloudConfigured = Prefs.configured(ctx) && Auth.signedIn(ctx) && owner.isNotEmpty()
        val recentEntries = Entries.recent(ctx)
        val nowMs = System.currentTimeMillis()
        val lanArchiveScope = "lan:${lanHost.lowercase()}"
        val cloudArchiveScope = "cloud:${owner.lowercase()}"
        val lanArchiveEnabled = forcedRoute != "cloud" && lanHost.isNotEmpty()
        val cloudArchiveEnabled = forcedRoute != "lan" && cloudConfigured
        val archiveRefreshDue =
            (lanArchiveEnabled &&
                ArchivedMetadataRefreshStore.isDue(ctx, lanArchiveScope, nowMs)) ||
                (cloudArchiveEnabled &&
                    ArchivedMetadataRefreshStore.isDue(ctx, cloudArchiveScope, nowMs))
        // Listing archive directory names is cheap. Parse only the bounded
        // windows selected for transports whose cadence is actually due.
        val archivedIds = if (archiveRefreshDue) {
            CaptureArchive.archivedIds(ctx).filter(SAFE_CAPTURE_SYNC_ID::matches)
        } else {
            emptyList()
        }
        val lanArchivePlan = ArchivedMetadataRefreshStore.plan(
            ctx = ctx,
            scope = lanArchiveScope,
            ids = archivedIds,
            nowMs = nowMs,
            enabled = lanArchiveEnabled,
        )
        val cloudArchivePlan = ArchivedMetadataRefreshStore.plan(
            ctx = ctx,
            scope = cloudArchiveScope,
            ids = archivedIds,
            nowMs = nowMs,
            enabled = cloudArchiveEnabled,
        )
        val archivedEntriesById = (lanArchivePlan.ids + cloudArchivePlan.ids)
            .distinct()
            .mapNotNull { id ->
                Entries.findIncludingArchive(ctx, id)?.takeIf(Entries.Entry::archived)
            }
            .associateBy(Entries.Entry::id)
        val lanArchivedEntries = lanArchivePlan.ids
            .mapNotNull(archivedEntriesById::get)
            .filter { entry -> isLanMetadataEntry(ctx, entry) }
        val cloudArchivedEntries = cloudArchivePlan.ids
            .mapNotNull(archivedEntriesById::get)
            .filter { entry -> isOwnedCloudMetadataEntry(ctx, entry, owner) }
        // A due window with no matching/loadable records is still a completed
        // archive check. Persist it so Home does not repeat the traversal.
        if (lanArchivedEntries.isEmpty() &&
            !ArchivedMetadataRefreshStore.markSucceeded(ctx, lanArchivePlan, nowMs)
        ) return@withContext permanentFailure(
            "could not persist empty LAN archive metadata refresh checkpoint",
        )
        if (cloudArchivedEntries.isEmpty() &&
            !ArchivedMetadataRefreshStore.markSucceeded(ctx, cloudArchivePlan, nowMs)
        ) return@withContext permanentFailure(
            "could not persist empty cloud archive metadata refresh checkpoint",
        )
        val hasLanEntries = lanArchivedEntries.isNotEmpty() || recentEntries.any { entry ->
            isLanMetadataEntry(ctx, entry) && SAFE_CAPTURE_SYNC_ID.matches(entry.id)
        }
        val hasCloudEntries = cloudArchivedEntries.isNotEmpty() || recentEntries.any { entry ->
            cloudConfigured && isOwnedCloudMetadataEntry(ctx, entry, owner)
        }
        val shouldProbeLan = forcedRoute != "cloud" && hasLanEntries &&
            lanHost.isNotEmpty() && (forcedRoute == "lan" || !hasCloudEntries)
        val lan = if (shouldProbeLan) try { LanClient(ctx) } catch (_: Exception) { null } else null
        val lanReachable = lan?.let { client ->
            try { client.ping() } catch (_: Exception) { false }
        } ?: false
        when (metadataSyncRouteDecision(
            forcedRoute = forcedRoute,
            hasLanEntries = hasLanEntries,
            hasCloudEntries = hasCloudEntries,
            lanConfigured = lanHost.isNotEmpty(),
            cloudConfigured = cloudConfigured,
            lanReachable = lanReachable,
        )) {
            MetadataSyncRouteDecision.LAN -> return@withContext syncLan(
                ctx,
                checkNotNull(lan),
                pushReviews,
                lanArchivePlan,
                lanArchivedEntries,
            )
            MetadataSyncRouteDecision.RETRY_LAN -> return@withContext Result.retry()
            MetadataSyncRouteDecision.CLOUD_AND_RETRY_LAN -> enqueueLanFollowup(ctx, pushReviews)
            MetadataSyncRouteDecision.CLOUD -> Unit
            MetadataSyncRouteDecision.NONE -> return@withContext Result.success()
        }
        if (!cloudConfigured) return@withContext Result.success()
        val client = SupabaseClient(ctx, owner)
        try {
            repeat(if (pushReviews) MAX_PASSES else 1) { pass ->
                if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                // Archived captures remain read-only. Include them only in the
                // desktop-book projection; every mutable/review/media family
                // below continues to operate on the current-entry set.
                val entries = Entries.recent(ctx).filter { entry ->
                    isOwnedCloudMetadataEntry(ctx, entry, owner)
                }
                val archived = if (pass == 0) cloudArchivedEntries else emptyList()
                val metadataEntries = (entries + archived).distinctBy(Entries.Entry::id)
                if (metadataEntries.isEmpty()) return@withContext Result.success()
                val metadataIds = metadataEntries.map(Entries.Entry::id)
                val desktopRows = client.desktopBookMetadata(metadataIds)
                val refreshedArchivedEntries = mutableListOf<Entries.Entry>()
                for ((captureId, metadata) in desktopRows) {
                    if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                    val (result, archived) = EntryOperationLocks.withLock(captureId) {
                        val entry = Entries.findIncludingArchive(ctx, captureId)
                            ?: return@withLock DesktopMetadataApplyResult.STALE to false
                        if (!isOwnedCloudMetadataEntry(ctx, entry, owner)) {
                            return@withLock DesktopMetadataApplyResult.STALE to entry.archived
                        }
                        CaptureMetadataStore.applyDesktopBook(entry.dir, metadata) to entry.archived
                    }
                    if (result == DesktopMetadataApplyResult.CONFLICT) {
                        throw CaptureMetadataStateException(
                            "conflicting desktop metadata revision for $captureId",
                        )
                    }
                    if (archived && result in setOf(
                            DesktopMetadataApplyResult.APPLIED,
                            DesktopMetadataApplyResult.UNCHANGED,
                        )
                    ) Entries.findIncludingArchive(ctx, captureId)
                        ?.takeIf(Entries.Entry::archived)
                        ?.let(refreshedArchivedEntries::add)
                }
                if (refreshedArchivedEntries.isNotEmpty() &&
                    !CollectionInventory.recordFinalized(ctx, refreshedArchivedEntries)
                ) {
                    throw CaptureMetadataStateException(
                        "could not refresh archived collection inventory",
                    )
                }
                if (pass == 0 &&
                    !ArchivedMetadataRefreshStore.markSucceeded(ctx, cloudArchivePlan, nowMs)
                ) {
                    throw CaptureMetadataStateException(
                        "could not persist cloud archive metadata refresh checkpoint",
                    )
                }
                if (entries.isEmpty()) return@withContext Result.success()
                val ids = entries.map { it.id }
                val reviewRows = client.captureReviews(ids)
                val importRows = client.captureImportStates(ids)
                // Corrections are additive to the pull: a project whose
                // migration has not landed yet (or any family-wide failure)
                // must not take down the established metadata families.
                val correctionRows = try {
                    client.captureCorrections(ids)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: SupabaseClient.SignedOut) {
                    throw e
                } catch (e: SupabaseClient.AccountChanged) {
                    throw e
                } catch (e: Exception) {
                    Log.w(TAG, "capture corrections unavailable: ${e.message}")
                    emptyMap()
                }
                val assetLifecycleRows = try {
                    client.captureAssetLifecycles(ids)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: SupabaseClient.SignedOut) {
                    throw e
                } catch (e: SupabaseClient.AccountChanged) {
                    throw e
                } catch (e: Exception) {
                    // Migration rollout is additive. A project without the
                    // lifecycle table keeps every local asset exactly as-is.
                    Log.w(TAG, "capture asset lifecycle unavailable: ${e.message}")
                    emptyMap()
                }

                for ((captureId, importState) in importRows) {
                    if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                    val applied = EntryOperationLocks.withLock(captureId) {
                        val entry = Entries.find(ctx, captureId)
                            ?: return@withLock CaptureImportStateApplyResult.MISSING
                        if (!entry.uploaded || entry.deliveryTransport == "lan" ||
                            cloudUploadOwnership(
                                readCaptureCreator(ctx, entry.dir),
                                owner,
                            ) != CloudUploadOwnership.ALLOWED
                        ) return@withLock CaptureImportStateApplyResult.MISSING
                        applyCaptureImportState(entry.dir, importState)
                    }
                    if (applied == CaptureImportStateApplyResult.CONFLICT) {
                        throw CaptureMetadataStateException(
                            "conflicting cloud capture state for $captureId",
                        )
                    }
                }

                // Apply visibility before corrected-display rows. A delete in
                // this same pull therefore prevents a correction download,
                // and restoration exposes the same retained bytes/history.
                for ((captureId, rows) in assetLifecycleRows) {
                    for (row in rows) {
                        if (!sameSignedInOwner(ctx, owner)) {
                            return@withContext Result.success()
                        }
                        EntryOperationLocks.withLock(captureId) {
                            val entry = Entries.find(ctx, captureId) ?: return@withLock
                            if (!entry.uploaded || entry.deliveryTransport == "lan" ||
                                cloudUploadOwnership(
                                    readCaptureCreator(ctx, entry.dir),
                                    owner,
                                ) != CloudUploadOwnership.ALLOWED) return@withLock
                            PhotoAssetStore.applyDesktopLifecycle(entry.dir, row, owner)
                        }
                    }
                }

                for ((captureId, corrections) in correctionRows) {
                    var rearmReocr = false
                    for (row in corrections) {
                        if (!sameSignedInOwner(ctx, owner)) return@withContext Result.success()
                        try {
                            if (applyDesktopCorrection(ctx, client, owner, row)) {
                                rearmReocr = true
                            }
                        } catch (e: CancellationException) {
                            throw e
                        } catch (e: SupabaseClient.SignedOut) {
                            throw e
                        } catch (e: SupabaseClient.AccountChanged) {
                            throw e
                        } catch (e: Exception) {
                            // Per-entry: one bad row or failed download must
                            // not block the rest of the pull. The row stays
                            // unapplied and the next pull retries it.
                            Log.w(
                                TAG,
                                "desktop correction skipped for $captureId/" +
                                    "${row.assetId}: ${e.message}",
                            )
                        }
                    }
                    // CloudDisplayReocrWorker fails terminally once its retry
                    // budget is spent (or on a permanent pipeline error) while
                    // deliberately leaving the durable .cloud-reocr marker for
                    // a later retry. A row that validates AlreadyApplied on a
                    // later pull is this capture's only recurring signal, so
                    // re-arm whenever a row addressed our lineage —
                    // enqueuePending is idempotent and marker-gated, mirroring
                    // the cloud-job poll path in UploadWorker.
                    if (rearmReocr) {
                        CloudDisplayReocrWorker.enqueuePending(ctx, captureId)
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
        archivePlan: ArchivedMetadataRefreshPlan,
        archivedEntries: List<Entries.Entry>,
    ): Result {
        try {
            repeat(if (pushReviews) MAX_PASSES else 1) { pass ->
                val entries = Entries.recent(ctx).filter { entry ->
                    isLanMetadataEntry(ctx, entry) &&
                        SAFE_CAPTURE_SYNC_ID.matches(entry.id)
                }
                val entryIds = entries.mapTo(hashSetOf(), Entries.Entry::id)
                val archived = if (pass == 0) archivedEntries else emptyList()
                val metadataEntries = (entries + archived).distinctBy(Entries.Entry::id)
                if (metadataEntries.isEmpty()) {
                    return Result.success()
                }
                val refreshedArchivedEntries = mutableListOf<Entries.Entry>()
                for (batch in metadataEntries.chunked(CAPTURE_METADATA_BATCH_SIZE)) {
                    val currentBatch = batch.filter { it.id in entryIds }
                    val sent = linkedMapOf<String, CaptureReviewMetadata>()
                    val outgoing = if (pushReviews) currentBatch.mapNotNull { entry ->
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
                        val (applied, archived) = EntryOperationLocks.withLock(captureId) {
                            val entry = Entries.findIncludingArchive(ctx, captureId)
                                ?: return@withLock DesktopMetadataApplyResult.STALE to false
                            if (!isLanMetadataEntry(ctx, entry)) {
                                return@withLock DesktopMetadataApplyResult.STALE to entry.archived
                            }
                            CaptureMetadataStore.applyDesktopBook(entry.dir, metadata) to entry.archived
                        }
                        if (applied == DesktopMetadataApplyResult.CONFLICT) {
                            throw CaptureMetadataStateException(
                                "conflicting LAN desktop metadata revision for $captureId",
                            )
                        }
                        if (archived && applied in setOf(
                                DesktopMetadataApplyResult.APPLIED,
                                DesktopMetadataApplyResult.UNCHANGED,
                            )
                        ) Entries.findIncludingArchive(ctx, captureId)
                            ?.takeIf(Entries.Entry::archived)
                            ?.let(refreshedArchivedEntries::add)
                    }
                    for ((captureId, confirmation) in exchange.associations) {
                        if (captureId !in entryIds) continue
                        val applied = EntryOperationLocks.withLock(captureId) {
                            val entry = Entries.find(ctx, captureId)
                                ?: return@withLock CaptureLibApplyResult.STALE
                            CaptureLibAssociationStore.apply(entry.dir, confirmation)
                        }
                        if (applied == CaptureLibApplyResult.CONFLICT) {
                            throw CaptureMetadataStateException(
                                "conflicting LAN archive confirmation revision for $captureId",
                            )
                        }
                    }
                    for (entrySnapshot in currentBatch) {
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
                if (refreshedArchivedEntries.isNotEmpty() &&
                    !CollectionInventory.recordFinalized(ctx, refreshedArchivedEntries)
                ) {
                    throw CaptureMetadataStateException(
                        "could not refresh archived collection inventory",
                    )
                }
                if (pass == 0 &&
                    !ArchivedMetadataRefreshStore.markSucceeded(
                        ctx,
                        archivePlan,
                        System.currentTimeMillis(),
                    )
                ) {
                    throw CaptureMetadataStateException(
                        "could not persist LAN archive metadata refresh checkpoint",
                    )
                }
                if (!pushReviews) {
                    return Result.success()
                }
                val dirtyRemaining = Entries.recent(ctx).any { entry ->
                    isLanMetadataEntry(ctx, entry) &&
                        CaptureMetadataStore.hasPendingReviewSync(entry.dir)
                }
                if (!dirtyRemaining) {
                    return Result.success()
                }
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

    /** Validate, download, and install one published desktop correction,
     * mirroring UploadWorker's cloud-display install choreography. True only
     * when the exact row is already valid locally or its verified bytes were
     * installed successfully — exactly the cases whose durable re-OCR marker
     * may still be pending. */
    private suspend fun applyDesktopCorrection(
        ctx: Context,
        client: SupabaseClient,
        owner: String,
        row: CaptureCorrectionRow,
    ): Boolean {
        val staged = EntryOperationLocks.withLock(row.captureId) {
            val entry = Entries.find(ctx, row.captureId) ?: return@withLock null
            if (!entry.uploaded || entry.deliveryTransport == "lan" ||
                cloudUploadOwnership(
                    readCaptureCreator(ctx, entry.dir), owner,
                ) != CloudUploadOwnership.ALLOWED || !entry.dir.isDirectory) {
                return@withLock null
            }
            val contract = PhotoAssetStore.read(entry.dir)
            fun download(plan: DesktopCorrectionInstallPlan) =
                DesktopCorrectionStage.Download(
                    plan,
                    File.createTempFile(
                        ".desktop-${row.correctionId.take(12)}-",
                        ".part",
                        entry.dir,
                    ),
                )
            when (val decision = validateDesktopCorrection(
                contract,
                row,
                owner,
            )) {
                is DesktopCorrectionDecision.AlreadyApplied -> {
                    val asset = contract.assets.firstOrNull { it.assetId == row.assetId }
                        ?: return@withLock null
                    val installed = File(entry.dir, asset.display.reference)
                    if (verifyCloudDisplayDownload(
                            installed,
                            decision.plan.artifact,
                            decision.plan.artifact.mime,
                            installed.length(),
                        ) == null && PhotoAssetStore.acknowledgeDesktopCorrectionRow(
                            entry.dir,
                            decision.plan,
                        )) {
                        DesktopCorrectionStage.AlreadyApplied
                    } else {
                        download(decision.plan)
                    }
                }
                is DesktopCorrectionDecision.Ready -> download(decision.plan)
                else -> null
            }
        } ?: return false
        val download = when (staged) {
            DesktopCorrectionStage.AlreadyApplied -> return true
            is DesktopCorrectionStage.Download -> staged
        }
        val (plan, temporary) = download
        try {
            val receipt = client.downloadPrivateObject(
                plan.artifact.bucket,
                plan.artifact.path,
                temporary,
                plan.artifact.bytes.coerceAtMost(MAX_CLOUD_DERIVATIVE_BYTES),
            )
            if (verifyCloudDisplayDownload(
                    temporary,
                    plan.artifact,
                    receipt.contentType,
                    receipt.bytes,
                ) != null) return false
            if (Prefs.userId(ctx) != owner) throw SupabaseClient.AccountChanged()
            return EntryOperationLocks.withLock(row.captureId) {
                val entry = Entries.find(ctx, row.captureId)
                    ?: return@withLock false
                entry.dir.isDirectory &&
                    PhotoAssetStore.installDesktopCorrectionDisplay(
                        entry.dir,
                        plan,
                        temporary,
                        receipt,
                    )
            }
        } finally {
            temporary.delete()
        }
    }

    private fun permanentFailure(message: String?): Result = Result.failure(
        Data.Builder().putString("error", message.orEmpty().take(500)).build(),
    )
}

private fun sameSignedInOwner(ctx: Context, expectedOwner: String): Boolean =
    Auth.signedIn(ctx) && expectedOwner.isNotEmpty() && Prefs.userId(ctx) == expectedOwner

private fun isOwnedCloudMetadataEntry(
    ctx: Context,
    entry: Entries.Entry,
    owner: String,
): Boolean = entry.uploaded && entry.deliveryTransport != "lan" &&
    cloudUploadOwnership(readCaptureCreator(ctx, entry.dir), owner) ==
    CloudUploadOwnership.ALLOWED

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

internal fun isCloudMetadataEntry(ctx: Context, entry: Entries.Entry): Boolean {
    if (!entry.uploaded || entry.deliveryTransport == "lan") return false
    if (entry.deliveryTransport == "cloud") return true
    return cloudUploadOwnership(
        readCaptureCreator(ctx, entry.dir), Prefs.userId(ctx),
    ) == CloudUploadOwnership.ALLOWED
}

/** Correction staging outcome computed under the entry lock: a validated
 * download to attempt, or proof an earlier pull already recorded the row. */
private sealed interface DesktopCorrectionStage {
    data object AlreadyApplied : DesktopCorrectionStage

    data class Download(
        val plan: DesktopCorrectionInstallPlan,
        val temporary: File,
    ) : DesktopCorrectionStage
}

private class CaptureMetadataStateException(message: String) : IOException(message)
