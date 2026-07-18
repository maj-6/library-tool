package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

private const val TRASH_STAMP = ".trashed_at"
private const val TRASH_TTL_MS = 7L * 24 * 60 * 60 * 1000   // ~7 days

/**
 * The voice-driven entry state machine.
 *
 *   "start"  -> a new entry (UUID) begins collecting photos
 *   "photo"  -> the next shot joins the current entry
 *   "done"   -> the entry is sealed into the upload queue
 *   "cancel" -> the entry and its photos are discarded
 *
 * Photos land under filesDir/queue/<entryId>/photo_N.jpg; "done" writes a
 * manifest.json marking the folder ready, and UploadWorker drains ready
 * folders whenever the network allows — nothing is lost if WiFi is out of
 * reach in the stacks.
 *
 * The open entry survives the Activity: its id is persisted (Prefs), and a
 * fresh CaptureSession RESTORES it from disk. So a rotation, a dark-mode flip,
 * or a process death mid-book brings the same open entry back — the book is
 * never silently split. The persisted id is also the single source of truth
 * for "which folder is live", so [recoverOrphans] can reclaim every OTHER
 * unsealed folder without ever racing an in-progress capture.
 */
class CaptureSession(private val ctx: Context) {

    var entryId: String? = null
        private set
    var photoCount: Int = 0
        private set

    init { restore(); purgeTrash() }

    val active: Boolean get() = entryId != null

    private fun queueRoot(): File = File(ctx.filesDir, "queue").apply { mkdirs() }
    fun entryDir(id: String): File = File(queueRoot(), id)

    /** Re-adopt the persisted open entry (config change / process death). Only
     *  an un-sealed folder that still exists counts; photoCount is recovered by
     *  counting its photos, so "done" and the next photo number stay correct. */
    private fun restore() {
        val id = Prefs.currentEntryId(ctx) ?: return
        val dir = entryDir(id)
        if (!dir.isDirectory || File(dir, "manifest.json").isFile) {
            Prefs.setCurrentEntryId(ctx, null)    // already sealed, or gone
            return
        }
        entryId = id
        photoCount = dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }?.size ?: 0
    }

    fun start(): String {
        cancel()                                  // an unfinished entry is voided
        val id = UUID.randomUUID().toString()
        entryDir(id).mkdirs()
        entryId = id
        photoCount = 0
        Prefs.setCurrentEntryId(ctx, id)
        return id
    }

    /** The file the next camera shot should be written to. */
    fun nextPhotoFile(): File? {
        val id = entryId ?: return null
        return File(entryDir(id), "photo_${photoCount + 1}.jpg")
    }

    fun photoSaved() {
        photoCount += 1
    }

    /** Seal the current entry for upload; returns its id, or null if empty.
     *  A failed manifest write (disk full) also returns null but leaves the
     *  entry OPEN — check [active] to tell "dropped, empty" from "try again". */
    fun done(): String? {
        val id = entryId ?: return null
        if (photoCount == 0) {                    // an empty entry is just dropped
            entryId = null
            entryDir(id).deleteRecursively()
            Prefs.setCurrentEntryId(ctx, null)
            return null
        }
        val ok = writeManifest(
            entryDir(id), (1..photoCount).map { "photo_$it.jpg" }, System.currentTimeMillis())
        if (!ok) return null                      // keep collecting; user retries
        entryId = null
        photoCount = 0
        Prefs.setCurrentEntryId(ctx, null)
        return id
    }

    /** Discard the open entry — but to the TRASH, not straight to deletion, so a
     *  mis-fired "cancel" stays recoverable (see [restoreFromTrash]). Returns the
     *  discarded entry id for an Undo, or null if nothing was open. */
    fun cancel(): String? {
        val id = entryId ?: return null
        entryId = null
        photoCount = 0
        moveToTrash(id)
        Prefs.setCurrentEntryId(ctx, null)
        return id
    }

    // --- trash: a discard is recoverable for a while -------------------------

    private fun trashRoot(): File = File(ctx.filesDir, "trash").apply { mkdirs() }

    /** Move an entry folder into the trash, stamped so [purgeTrash] can age it
     *  out. Any same-id folder already in the trash is replaced. */
    private fun moveToTrash(id: String) {
        val src = entryDir(id)
        if (!src.exists()) return
        val dst = File(trashRoot(), id)
        dst.deleteRecursively()
        if (!src.renameTo(dst)) {              // across mount points: copy + delete
            src.copyRecursively(dst, overwrite = true)
            src.deleteRecursively()
        }
        File(dst, TRASH_STAMP).writeText(System.currentTimeMillis().toString())
    }

    /** Undo a [cancel]: bring a trashed entry back as the live open entry.
     *  No-op (false) if another entry is already open or the trash copy is gone. */
    fun restoreFromTrash(id: String): Boolean {
        if (active) return false
        val src = File(trashRoot(), id)
        if (!src.isDirectory) return false
        val dst = entryDir(id)
        dst.deleteRecursively()
        if (!src.renameTo(dst)) {
            src.copyRecursively(dst, overwrite = true)
            src.deleteRecursively()
        }
        File(dst, TRASH_STAMP).delete()
        entryId = id
        photoCount = dst.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }?.size ?: 0
        Prefs.setCurrentEntryId(ctx, id)
        return true
    }

    /** Delete trashed entries past the retention window; runs at construction so
     *  an abandoned discard can't linger forever. */
    fun purgeTrash(now: Long = System.currentTimeMillis()) {
        for (dir in trashRoot().listFiles { f -> f.isDirectory } ?: return) {
            val stamp = File(dir, TRASH_STAMP).takeIf { it.isFile }
                ?.readText()?.trim()?.toLongOrNull()
            if (now - (stamp ?: dir.lastModified()) >= TRASH_TTL_MS) dir.deleteRecursively()
        }
    }

    /** Entry folders sealed with a manifest and not yet uploaded. */
    fun pendingUploads(): List<File> =
        queueRoot().listFiles { f -> f.isDirectory && File(f, "manifest.json").isFile }
            ?.sortedBy { it.name } ?: emptyList()

    /** Seal folders orphaned by a crash so their pages upload instead of
     *  leaking; folders with no photos at all are deleted. The live entry is
     *  identified solely by the persisted id (which a fresh session re-adopts,
     *  see [restore]), so this NEVER touches an entry that is being captured
     *  into — no matter how long the user has paused mid-book. Runs off the UI
     *  thread (UploadWorker). Returns how many entries were rescued. */
    fun recoverOrphans(): Int {
        val live = Prefs.currentEntryId(ctx)
        var sealed = 0
        for (dir in queueRoot().listFiles { f -> f.isDirectory } ?: emptyArray()) {
            if (dir.name == live) continue                   // the open entry, hands off
            if (File(dir, "manifest.json").isFile) continue
            val photos = dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
                ?.sortedBy { photoNumber(it.name) }
                ?.map { it.name } ?: emptyList()
            if (photos.isEmpty()) { dir.deleteRecursively(); continue }
            val newest = dir.listFiles()?.maxOfOrNull { it.lastModified() }
                ?: System.currentTimeMillis()
            if (writeManifest(dir, photos, newest)) sealed++
        }
        return sealed
    }

    /** Atomic on purpose: a torn manifest.json would read as corrupt, and the
     *  tmp name never matches what pendingUploads/UploadWorker look for. */
    private fun writeManifest(dir: File, photos: List<String>, createdAt: Long): Boolean =
        try {
            val manifest = JSONObject()
                .put("id", dir.name)
                .put("device", Prefs.deviceName(ctx))
                .put("created_at", createdAt)
                .put("photos", JSONArray(photos))
                .put("note", "")
            val tmp = File(dir, "manifest.json.tmp")
            tmp.writeText(manifest.toString())
            tmp.renameTo(File(dir, "manifest.json"))
        } catch (e: Exception) {
            false
        }
}
