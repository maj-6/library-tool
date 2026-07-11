package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

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

    init { restore() }

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

    fun cancel(): Boolean {
        val id = entryId ?: return false
        entryId = null
        photoCount = 0
        entryDir(id).deleteRecursively()
        Prefs.setCurrentEntryId(ctx, null)
        return true
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
