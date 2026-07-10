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
 */
class CaptureSession(private val ctx: Context) {

    var entryId: String? = null
        private set
    var photoCount: Int = 0
        private set

    val active: Boolean get() = entryId != null

    private fun queueRoot(): File = File(ctx.filesDir, "queue").apply { mkdirs() }
    fun entryDir(id: String): File = File(queueRoot(), id)

    fun start(): String {
        cancel()                                  // an unfinished entry is voided
        val id = UUID.randomUUID().toString()
        entryDir(id).mkdirs()
        entryId = id
        photoCount = 0
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

    /** Seal the current entry for upload; returns its id, or null if empty. */
    fun done(): String? {
        val id = entryId ?: return null
        entryId = null
        if (photoCount == 0) {                    // an empty entry is just dropped
            entryDir(id).deleteRecursively()
            photoCount = 0
            return null
        }
        val manifest = JSONObject()
            .put("id", id)
            .put("device", Prefs.deviceName(ctx))
            .put("created_at", System.currentTimeMillis())
            .put("photos", JSONArray((1..photoCount).map { "photo_$it.jpg" }))
            .put("note", "")
        File(entryDir(id), "manifest.json").writeText(manifest.toString())
        photoCount = 0
        return id
    }

    fun cancel(): Boolean {
        val id = entryId ?: return false
        entryId = null
        photoCount = 0
        entryDir(id).deleteRecursively()
        return true
    }

    /** Entry folders sealed with a manifest and not yet uploaded. */
    fun pendingUploads(): List<File> =
        queueRoot().listFiles { f -> f.isDirectory && File(f, "manifest.json").isFile }
            ?.sortedBy { it.name } ?: emptyList()
}
