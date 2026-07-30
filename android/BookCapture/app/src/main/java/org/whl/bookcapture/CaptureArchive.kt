package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File

/**
 * Retention moves captures aside; it never destroys them.
 *
 *   filesDir/archive/<id>/   the whole sent/ directory, moved intact, plus
 *                            archived.json recording when and why it left the
 *                            browsing list
 *
 * Two tiers, because photos are the only part big enough to matter:
 *
 *   full          everything the sent copy had, photos included
 *   record-only   the diagnostic skeleton — every .json and .txt sidecar, so
 *                 the manifest, extracted metadata, processing state and OCR
 *                 text all survive; photos and binary derivatives are gone
 *
 * A record is never removed by the app. Photos are only dropped past an
 * explicit, user-set budget, and the stamp then says photos_retained=false so
 * a later investigation can tell "no photos here" from "no photos ever". The
 * budget is off by default: an archive that silently discarded the evidence
 * would recreate the problem it exists to solve.
 *
 * Record-only keeps files by extension rather than deleting a known list of
 * derivatives. A new binary derivative added later is then dropped by default
 * instead of silently defeating the budget.
 */
internal object CaptureArchive {

    const val ARCHIVE_STAMP = "archived.json"

    /** 0 means "keep every archived photo"; see the class comment. */
    const val UNLIMITED_ARCHIVED_PHOTOS = 0

    const val REASON_RETENTION = "retention"
    const val REASON_MANUAL = "manual"

    fun archiveRoot(ctx: Context): File = File(ctx.filesDir, "archive").apply { mkdirs() }

    internal data class ArchiveStamp(
        val archivedAt: Long,
        val reason: String,
        val photosRetained: Boolean,
    )

    /** Ordering key for the photo budget. Archived-at is the primary key so a
     * capture keeps its photos for a while after it leaves the browsing list,
     * regardless of how old the capture itself is. */
    internal data class ArchivePhotoCandidate(
        val entryId: String,
        val archivedAt: Long,
        val photosRetained: Boolean,
    )

    /** Which archived entries must give up their photos to satisfy [keepCount].
     * Entries that already gave them up are not counted against the budget and
     * are never returned twice. */
    internal fun archivePhotoOverflow(
        candidates: Collection<ArchivePhotoCandidate>,
        keepCount: Int,
    ): List<String> {
        require(keepCount >= 0)
        if (keepCount == UNLIMITED_ARCHIVED_PHOTOS) return emptyList()
        return candidates.asSequence()
            .filter(ArchivePhotoCandidate::photosRetained)
            .sortedWith(
                compareByDescending<ArchivePhotoCandidate> { it.archivedAt }
                    .thenBy { it.entryId },
            )
            .drop(keepCount)
            .map(ArchivePhotoCandidate::entryId)
            .toList()
    }

    internal fun stampJson(reason: String, now: Long, photosRetained: Boolean): String =
        JSONObject()
            .put("archived_at", now)
            .put("reason", reason.trim().take(120))
            .put("photos_retained", photosRetained)
            .toString()

    internal fun readStamp(dir: File): ArchiveStamp? {
        val file = File(dir, ARCHIVE_STAMP).takeIf { it.isFile } ?: return null
        return try {
            val data = JSONObject(file.readText())
            val archivedAt = data.optLong("archived_at", 0L).takeIf { it > 0 } ?: return null
            ArchiveStamp(
                archivedAt = archivedAt,
                reason = data.optString("reason").trim(),
                photosRetained = data.optBoolean("photos_retained", true),
            )
        } catch (_: Exception) {
            null
        }
    }

    /** A stamp write that fails leaves the directory archived but undated;
     * [readStamp] then reports null and the budget treats it as oldest. The
     * capture is still present, which is the invariant that matters. */
    internal fun stamp(
        dir: File,
        reason: String,
        now: Long,
        photosRetained: Boolean,
    ): Boolean = try {
        Entries.atomicWrite(File(dir, ARCHIVE_STAMP), stampJson(reason, now, photosRetained))
        true
    } catch (_: Exception) {
        false
    }

    internal fun isRecordOnly(dir: File): Boolean = readStamp(dir)?.photosRetained == false

    /** Keep every textual sidecar, drop everything else. Returns false if any
     * intended deletion failed, so a caller can leave the stamp alone and try
     * again rather than claim a demotion that only half happened. */
    internal fun demoteToRecordOnly(dir: File, now: Long): Boolean {
        if (!dir.isDirectory) return false
        var complete = true
        for (child in dir.listFiles() ?: return false) {
            if (child.isDirectory) {
                if (!child.deleteRecursively() || child.exists()) complete = false
                continue
            }
            if (retainedInRecordOnly(child.name)) continue
            if (!child.delete() || child.exists()) complete = false
        }
        if (!complete) return false
        val existing = readStamp(dir)
        return stamp(
            dir,
            existing?.reason ?: REASON_RETENTION,
            existing?.archivedAt ?: now,
            photosRetained = false,
        )
    }

    internal fun retainedInRecordOnly(name: String): Boolean {
        val lower = name.lowercase()
        return lower.endsWith(".json") || lower.endsWith(".txt")
    }

    /** Archived entries, newest first. Unstamped directories sort last: they
     * are either a failed stamp write or a pre-archive leftover, and in both
     * cases they are the least recently set aside thing we know of. */
    internal fun archivedIds(ctx: Context): List<String> =
        (archiveRoot(ctx).listFiles { f: File -> f.isDirectory } ?: emptyArray())
            .sortedWith(
                compareByDescending<File> { readStamp(it)?.archivedAt ?: 0L }
                    .thenBy { it.name },
            )
            .map { it.name }

    internal fun photoCandidates(ctx: Context): List<ArchivePhotoCandidate> =
        (archiveRoot(ctx).listFiles { f: File -> f.isDirectory } ?: emptyArray())
            .map { dir ->
                val stamp = readStamp(dir)
                ArchivePhotoCandidate(
                    entryId = dir.name,
                    archivedAt = stamp?.archivedAt ?: 0L,
                    photosRetained = stamp?.photosRetained ?: true,
                )
            }

    /** Bytes currently held by the archive, for the Settings readout. */
    internal fun archiveBytes(ctx: Context): Long =
        archiveRoot(ctx).walkBottomUp().filter { it.isFile }.sumOf { it.length() }

    /** Drop photos from archived entries past [keepCount]. Runs after pruning
     * so a freshly archived capture is measured with the rest. */
    internal fun enforcePhotoBudget(ctx: Context, keepCount: Int, now: Long) {
        if (keepCount == UNLIMITED_ARCHIVED_PHOTOS) return
        val root = archiveRoot(ctx)
        for (entryId in archivePhotoOverflow(photoCandidates(ctx), keepCount)) {
            demoteToRecordOnly(File(root, entryId), now)
        }
    }
}
