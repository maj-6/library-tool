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
                    for ((captureId, confirmation) in exchange.associations) {
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

    /** Validate, download, and install one published desktop correction,
     * mirroring UploadWorker's cloud-display install choreography. True when
     * the row addressed this handset's asset lineage — a fresh install
     * (Ready) or one recorded by an earlier pull (AlreadyApplied) — exactly
     * the cases whose durable re-OCR marker may still be pending. */
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
            when (val decision = validateDesktopCorrection(
                PhotoAssetStore.read(entry.dir),
                row,
                owner,
            )) {
                DesktopCorrectionDecision.AlreadyApplied ->
                    DesktopCorrectionStage.AlreadyApplied
                is DesktopCorrectionDecision.Ready -> DesktopCorrectionStage.Download(
                    decision.plan,
                    File.createTempFile(
                        ".desktop-${row.correctionId.take(12)}-",
                        ".part",
                        entry.dir,
                    ),
                )
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
                ) == null) {
                if (Prefs.userId(ctx) != owner) throw SupabaseClient.AccountChanged()
                EntryOperationLocks.withLock(row.captureId) {
                    val entry = Entries.find(ctx, row.captureId) ?: return@withLock
                    if (entry.dir.isDirectory) {
                        PhotoAssetStore.installDesktopCorrectionDisplay(
                            entry.dir,
                            plan,
                            temporary,
                            receipt,
                        )
                    }
                }
            }
            // A failed byte verification leaves the row unapplied for the
            // next pull, but the lineage matched, so re-arming stays correct.
            return true
        } finally {
            temporary.delete()
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
