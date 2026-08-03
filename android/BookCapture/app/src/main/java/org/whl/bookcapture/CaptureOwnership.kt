package org.whl.bookcapture

import android.content.Context
import kotlinx.coroutines.CancellationException
import org.json.JSONObject
import java.io.File

internal enum class CloudUploadOwnership { ALLOWED, NEEDS_CLAIM, DIFFERENT_ACCOUNT }
internal enum class ClaimCaptureResult { CLAIMED, ALREADY_OWNED, DIFFERENT_ACCOUNT, SIGNED_OUT, MISSING }

/** Missing/corrupt legacy ownership fails closed to the stable local identity;
 * it is never inferred from whichever account happens to be signed in now. */
internal fun captureCreatorFromManifest(
    manifest: JSONObject,
    localCreatorId: String,
): CaptureCreator {
    val value = manifest.optJSONObject("creator")
    val kind = value?.optString("kind")?.trim().orEmpty()
    val id = value?.optString("id")?.trim().orEmpty()
    return if (kind in setOf(Prefs.CREATOR_ACCOUNT, Prefs.CREATOR_LOCAL) && id.isNotEmpty()) {
        CaptureCreator(kind, id)
    } else {
        CaptureCreator(Prefs.CREATOR_LOCAL, localCreatorId)
    }
}

internal fun cloudUploadOwnership(
    creator: CaptureCreator,
    currentAccountId: String,
): CloudUploadOwnership = when {
    creator.kind == Prefs.CREATOR_LOCAL -> CloudUploadOwnership.NEEDS_CLAIM
    creator.id == currentAccountId.trim() && currentAccountId.isNotBlank() ->
        CloudUploadOwnership.ALLOWED
    else -> CloudUploadOwnership.DIFFERENT_ACCOUNT
}

/** The only captures a bulk confirmation may adopt. Account-owned rows—even
 * ones owned by another account—are deliberately excluded. */
internal fun captureIdsNeedingCloudClaim(
    captures: Collection<Pair<String, CaptureCreator>>,
    currentAccountId: String,
): List<String> = normalizedCaptureSyncIds(
    captures.asSequence()
        .filter { (_, creator) ->
            cloudUploadOwnership(creator, currentAccountId) == CloudUploadOwnership.NEEDS_CLAIM
        }
        .map { it.first }
        .toList(),
).toList()

internal fun readCaptureCreator(ctx: Context, dir: File): CaptureCreator = try {
    captureCreatorFromManifest(
        JSONObject(File(dir, "manifest.json").readText()),
        Prefs.anonymousCreatorId(ctx),
    )
} catch (_: Exception) {
    CaptureCreator(Prefs.CREATOR_LOCAL, Prefs.anonymousCreatorId(ctx))
}

/** Only offer account adoption for an entry that can reach the ownership
 * check in UploadWorker. Damaged manifests/photos need their own recovery
 * message and must not be rewritten merely because missing data looks local. */
internal fun readClaimableCaptureCreator(ctx: Context, dir: File): CaptureCreator? = try {
    val manifest = JSONObject(File(dir, "manifest.json").readText())
    val id = manifest.getString("id")
    if (id != dir.name || !id.matches(Regex("[A-Za-z0-9._-]+")) || id == "." || id == "..") {
        null
    } else {
        val photos = manifest.getJSONArray("photos")
        val names = (0 until photos.length()).map(photos::getString)
        validateUploadPhotos(dir, names)
        captureCreatorFromManifest(manifest, Prefs.anonymousCreatorId(ctx))
    }
} catch (_: Exception) {
    null
}

/** Explicitly adopt one local capture into the currently authenticated
 * account. The entry lock makes claim vs. upload/delete/reprocess atomic. */
internal suspend fun claimCaptureForCloud(
    ctx: Context,
    entryId: String,
    expectedAccountId: String? = null,
): ClaimCaptureResult {
    val uid = Prefs.userId(ctx).trim()
    if (!Auth.signedIn(ctx) || uid.isEmpty()) return ClaimCaptureResult.SIGNED_OUT
    if (expectedAccountId != null && uid != expectedAccountId.trim()) {
        return ClaimCaptureResult.DIFFERENT_ACCOUNT
    }
    return try {
        EntryOperationLocks.withLock(entryId) {
            // The account may change while this operation waits for processing or
            // upload to release the entry lock. Never write ownership for a session
            // other than the one the user confirmed.
            if (!Auth.signedIn(ctx)) return@withLock ClaimCaptureResult.SIGNED_OUT
            if (Prefs.userId(ctx).trim() != uid) {
                return@withLock ClaimCaptureResult.DIFFERENT_ACCOUNT
            }
            val entry = Entries.find(ctx, entryId) ?: return@withLock ClaimCaptureResult.MISSING
            if (entry.uploaded) return@withLock ClaimCaptureResult.ALREADY_OWNED
            val manifestFile = File(entry.dir, "manifest.json")
            val manifest = try { JSONObject(manifestFile.readText()) }
                catch (_: Exception) { return@withLock ClaimCaptureResult.MISSING }
            when (cloudUploadOwnership(
                captureCreatorFromManifest(manifest, Prefs.anonymousCreatorId(ctx)),
                uid,
            )) {
                CloudUploadOwnership.ALLOWED -> ClaimCaptureResult.ALREADY_OWNED
                CloudUploadOwnership.DIFFERENT_ACCOUNT -> ClaimCaptureResult.DIFFERENT_ACCOUNT
                CloudUploadOwnership.NEEDS_CLAIM -> {
                    // Session preferences are not protected by the entry lock.
                    // Recheck at the last possible point before either durable
                    // ownership write.
                    if (!Auth.signedIn(ctx)) return@withLock ClaimCaptureResult.SIGNED_OUT
                    if (Prefs.userId(ctx).trim() != uid) {
                        return@withLock ClaimCaptureResult.DIFFERENT_ACCOUNT
                    }
                    val creator = JSONObject()
                        .put("kind", Prefs.CREATOR_ACCOUNT)
                        .put("id", uid)
                    // Sidecar first: a crash between writes remains unclaimed in
                    // the authoritative manifest and can safely be retried.
                    Entries.atomicWrite(File(entry.dir, "capture.json"), creator.toString())
                    Entries.atomicWrite(manifestFile, manifest.put("creator", creator).toString())
                    ClaimCaptureResult.CLAIMED
                }
            }
        }
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        // Disk/read failures leave the authoritative manifest unchanged or
        // still local. The caller reports a partial claim and can safely retry.
        ClaimCaptureResult.MISSING
    }
}
