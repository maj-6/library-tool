package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

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

    data class PhotoReservation internal constructor(
        val entryId: String,
        val pageNumber: Int,
        val tempFile: File,
        val finalFile: File,
    )

    var entryId: String? = null
        private set
    var photoCount: Int = 0
        private set
    internal var creator: CaptureCreator? = null
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
        // A same-process Activity recreation can overlap the old CameraX file
        // callback. Delete only temporaries that no live callback still owns;
        // after process death the registry is empty and all leftovers are safe.
        deleteCaptureTemps(dir)
        entryId = id
        creator = creatorFor(dir)
        refreshPhotoCount()
    }

    fun start(): String {
        if (active && cancel() == null) {
            error("Could not discard the current capture")
        }
        val id = UUID.randomUUID().toString()
        val dir = entryDir(id)
        check(dir.mkdirs() || dir.isDirectory) { "Could not create capture directory" }
        val captureCreator = Prefs.captureCreator(ctx)
        if (!writeCreator(dir, captureCreator, Prefs.cameraProfile(ctx))) {
            dir.deleteRecursively()
            error("Could not persist capture creator")
        }
        entryId = id
        creator = captureCreator
        photoCount = 0
        Prefs.setCurrentEntryId(ctx, id)
        return id
    }

    /** Reserve a unique temporary and final path before CameraX submission. */
    fun reservePhoto(pageNumber: Int): PhotoReservation? {
        val id = entryId ?: return null
        refreshPhotoCount()
        if (pageNumber <= photoCount) return null
        val dir = entryDir(id)
        val finalFile = File(dir, "photo_$pageNumber.jpg")
        if (finalFile.exists() || ActiveCaptureWrites.hasPage(dir, pageNumber)) return null
        val reservation = PhotoReservation(
            entryId = id,
            pageNumber = pageNumber,
            tempFile = File(dir, ".capture_${pageNumber}_${UUID.randomUUID()}.pending.jpg"),
            finalFile = finalFile,
        )
        ActiveCaptureWrites.register(reservation.tempFile)
        return reservation
    }

    /** Promote a completely saved temporary JPEG into the dense page sequence. */
    fun commitPhoto(reservation: PhotoReservation): Boolean {
        refreshPhotoCount()
        val valid = entryId == reservation.entryId &&
            reservation.pageNumber == photoCount + 1 &&
            reservation.tempFile.isFile &&
            !reservation.finalFile.exists()
        if (!valid) {
            abortPhoto(reservation)
            return false
        }
        if (!reservation.tempFile.renameTo(reservation.finalFile)) {
            abortPhoto(reservation)
            return false
        }
        ActiveCaptureWrites.unregister(reservation.tempFile)
        photoCount += 1
        return true
    }

    /** Delete a partial or no-longer-needed CameraX output. */
    fun abortPhoto(reservation: PhotoReservation) {
        ActiveCaptureWrites.unregister(reservation.tempFile)
        reservation.tempFile.delete()
    }

    /** Reconcile callbacks owned by an older Activity instance before the
     * next reservation or Finish decides which dense page numbers exist. */
    fun refreshPhotoCount(): Int {
        val id = entryId ?: return photoCount
        photoCount = entryDir(id)
            .listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
            ?.size ?: 0
        return photoCount
    }

    /** True while a CameraX callback from this process still owns a temporary
     * for the restored entry. A replacement Activity waits before rebinding so
     * it does not call unbindAll underneath that callback. */
    fun hasActiveCaptureWrites(): Boolean =
        entryId?.let { ActiveCaptureWrites.hasAny(entryDir(it)) } == true

    /** Seal the current entry for upload; returns its id, or null if empty.
     *  A failed manifest write (disk full) also returns null but leaves the
     *  entry OPEN — check [active] to tell "dropped, empty" from "try again". */
    fun done(): String? {
        val id = entryId ?: return null
        if (ActiveCaptureWrites.hasAny(entryDir(id))) return null
        refreshPhotoCount()
        if (photoCount == 0) {                    // an empty entry is just dropped
            entryId = null
            creator = null
            entryDir(id).deleteRecursively()
            Prefs.setCurrentEntryId(ctx, null)
            return null
        }
        val captureCreator = creator ?: creatorFor(entryDir(id))
        val ok = writeManifest(
            entryDir(id), (1..photoCount).map { "photo_$it.jpg" },
            System.currentTimeMillis(), captureCreator)
        if (!ok) return null                      // keep collecting; user retries
        entryId = null
        creator = null
        photoCount = 0
        Prefs.setCurrentEntryId(ctx, null)
        return id
    }

    /** Discard the open entry — but to the TRASH, not straight to deletion, so a
     *  mis-fired "cancel" stays recoverable (see [restoreFromTrash]). Returns the
     *  discarded entry id for an Undo, or null if nothing was open. */
    fun cancel(): String? {
        val id = entryId ?: return null
        // Do not clear the persisted live-entry pointer until its directory is
        // verifiably outside the upload queue. Otherwise a failed delete/move
        // becomes an orphan that recoverOrphans() can seal and upload later.
        if (!moveToTrash(id)) return null
        entryId = null
        creator = null
        photoCount = 0
        Prefs.setCurrentEntryId(ctx, null)
        return id
    }

    // --- trash: a discard is recoverable for a while -------------------------

    private fun trashRoot(): File = File(ctx.filesDir, "trash").apply { mkdirs() }

    /** Move an entry folder into the trash, stamped so [purgeTrash] can age it
     *  out. Any same-id folder already in the trash is replaced. */
    private fun moveToTrash(id: String): Boolean {
        val src = entryDir(id)
        if (!src.exists()) return true
        val dst = File(trashRoot(), id)
        if (dst.exists() && !dst.deleteRecursively()) return false
        if (!moveDirectoryWithoutCopy(src, dst)) return false
        // A missing stamp only delays cleanup until the directory timestamp;
        // the important invariant is that the entry has left the upload queue.
        runCatching {
            File(dst, TRASH_STAMP).writeText(System.currentTimeMillis().toString())
        }
        return true
    }

    /** Undo a [cancel]: bring a trashed entry back as the live open entry.
     *  No-op (false) if another entry is already open or the trash copy is gone. */
    fun restoreFromTrash(id: String): Boolean {
        if (active) return false
        val src = File(trashRoot(), id)
        if (!src.isDirectory) return false
        val dst = entryDir(id)
        // Never destroy a same-id queue folder to satisfy Undo. If a folder is
        // already there, keep the trash copy recoverable for a later attempt.
        if (dst.exists() || !moveDirectoryWithoutCopy(src, dst)) return false
        File(dst, TRASH_STAMP).delete()
        entryId = id
        creator = creatorFor(dst)
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
            deleteCaptureTemps(dir)
            val photos = dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
                ?.sortedBy { photoNumber(it.name) }
                ?.map { it.name } ?: emptyList()
            if (photos.isEmpty()) { dir.deleteRecursively(); continue }
            val newest = dir.listFiles()?.maxOfOrNull { it.lastModified() }
                ?: System.currentTimeMillis()
            if (writeManifest(dir, photos, newest, creatorFor(dir))) sealed++
        }
        return sealed
    }

    /** A sidecar freezes ownership before the first photo. Missing/corrupt
     *  legacy sidecars fail closed to local ownership, never the account that
     *  merely happens to be signed in during recovery. */
    private fun creatorFor(dir: File): CaptureCreator = readCreator(dir) ?: CaptureCreator(
        Prefs.CREATOR_LOCAL,
        Prefs.anonymousCreatorId(ctx),
    ).also { writeCreator(dir, it, null) }

    private fun readCreator(dir: File): CaptureCreator? = try {
        val data = JSONObject(File(dir, CAPTURE_METADATA_FILE).readText())
        val kind = data.getString("kind").trim()
        val id = data.getString("id").trim()
        if (kind !in setOf(Prefs.CREATOR_ACCOUNT, Prefs.CREATOR_LOCAL) || id.isEmpty()) null
        else CaptureCreator(kind, id)
    } catch (_: Exception) {
        null
    }

    private fun writeCreator(
        dir: File,
        creator: CaptureCreator,
        cameraProfile: String?,
    ): Boolean = try {
        Entries.atomicWrite(
            File(dir, CAPTURE_METADATA_FILE),
            JSONObject()
                .put("kind", creator.kind)
                .put("id", creator.id)
                // Legacy or repaired captures have no reliable profile. Keep
                // their page pixels conservatively instead of downsampling.
                .put("camera_profile", cameraProfile ?: Prefs.CAMERA_PROFILE_DETAIL)
                .toString(),
        )
        true
    } catch (_: Exception) {
        false
    }

    private fun deleteCaptureTemps(dir: File) {
        dir.listFiles { f -> f.isFile && f.name.matches(CAPTURE_TEMP_NAME) }
            ?.filterNot(ActiveCaptureWrites::isActive)
            ?.forEach { it.delete() }
    }

    /** Atomic on purpose: a torn manifest.json would read as corrupt, and the
     *  tmp name never matches what pendingUploads/UploadWorker look for. */
    private fun writeManifest(
        dir: File,
        photos: List<String>,
        createdAt: Long,
        creator: CaptureCreator,
    ): Boolean =
        try {
            val manifest = JSONObject()
                .put("id", dir.name)
                .put("device", Prefs.deviceName(ctx))
                .put("created_at", createdAt)
                .put("photos", JSONArray(photos))
                .put("note", "")
                .put("creator", JSONObject()
                    .put("kind", creator.kind)
                    .put("id", creator.id))
            val tmp = File(dir, "manifest.json.tmp")
            tmp.writeText(manifest.toString())
            tmp.renameTo(File(dir, "manifest.json"))
        } catch (e: Exception) {
            false
        }
}

internal const val CAPTURE_METADATA_FILE = "capture.json"
private val CAPTURE_TEMP_NAME = Regex("\\.capture_\\d+_[0-9a-f-]+\\.pending\\.jpg")

/** Move an entry atomically enough that failure leaves the queue copy intact.
 * Queue and trash both live under filesDir, so a directory move is supported;
 * deliberately avoid copy-then-delete because a failed delete leaves an
 * uploadable orphan behind. The injectable mover keeps failure behavior JVM
 * testable without relying on platform-specific file permissions. */
internal fun moveDirectoryWithoutCopy(
    src: File,
    dst: File,
    mover: (File, File) -> Boolean = { from, to ->
        if (from.renameTo(to)) true
        else try {
            Files.move(from.toPath(), to.toPath())
            true
        } catch (_: Exception) {
            false
        }
    },
): Boolean {
    if (!src.isDirectory || dst.exists()) return false
    if (!mover(src, dst)) return false
    return !src.exists() && dst.isDirectory
}

/** Same-process ownership for CameraX files. The callback owns its temporary
 * across Activity recreation; a short expiry prevents a lost OEM callback
 * from pinning one page number for the rest of the process lifetime. */
private object ActiveCaptureWrites {
    private const val MAX_CALLBACK_MS = 2 * 60 * 1000L
    private val active = ConcurrentHashMap<String, Long>()

    fun register(file: File) {
        active[file.absolutePath] = System.currentTimeMillis()
    }

    fun unregister(file: File) {
        active.remove(file.absolutePath)
    }

    fun isActive(file: File): Boolean {
        val started = active[file.absolutePath] ?: return false
        if (System.currentTimeMillis() - started <= MAX_CALLBACK_MS) return true
        active.remove(file.absolutePath, started)
        return false
    }

    fun hasPage(dir: File, pageNumber: Int): Boolean = active.keys.any { path ->
        val file = File(path)
        file.parentFile == dir &&
            file.name.startsWith(".capture_${pageNumber}_") && isActive(file)
    }

    fun hasAny(dir: File): Boolean = active.keys.any { path ->
        val file = File(path)
        file.parentFile == dir && isActive(file)
    }
}
