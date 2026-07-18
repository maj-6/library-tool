package org.whl.bookcapture

import android.content.Context
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

internal fun readCaptureCreator(ctx: Context, dir: File): CaptureCreator = try {
    captureCreatorFromManifest(
        JSONObject(File(dir, "manifest.json").readText()),
        Prefs.anonymousCreatorId(ctx),
    )
} catch (_: Exception) {
    CaptureCreator(Prefs.CREATOR_LOCAL, Prefs.anonymousCreatorId(ctx))
}

/** Explicitly adopt one local capture into the currently authenticated
 * account. The entry lock makes claim vs. upload/delete/reprocess atomic. */
internal suspend fun claimCaptureForCloud(ctx: Context, entryId: String): ClaimCaptureResult {
    val uid = Prefs.userId(ctx).trim()
    if (!Auth.signedIn(ctx) || uid.isEmpty()) return ClaimCaptureResult.SIGNED_OUT
    return EntryOperationLocks.withLock(entryId) {
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
                val creator = JSONObject().put("kind", Prefs.CREATOR_ACCOUNT).put("id", uid)
                // Sidecar first: a crash between writes remains unclaimed in
                // the authoritative manifest and can safely be retried.
                Entries.atomicWrite(File(entry.dir, "capture.json"), creator.toString())
                Entries.atomicWrite(manifestFile, manifest.put("creator", creator).toString())
                ClaimCaptureResult.CLAIMED
            }
        }
    }
}
