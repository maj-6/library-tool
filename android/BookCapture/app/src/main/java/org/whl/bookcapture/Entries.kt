package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * The on-disk life of an entry, read side. Folders move through:
 *
 *   filesDir/queue/<id>/   photo_N.jpg              open (no manifest yet)
 *                          + manifest.json           sealed, waiting to upload
 *                          + photo_N.jpg.txt         OCR sidecar per photo
 *                          + meta.json               extracted bibliography
 *   filesDir/sent/<id>/    the same, moved here once uploaded; manifest
 *                          gains uploaded_at, and cloud_status tracks the
 *                          desktop's import ("pending" -> "imported")
 *
 * Sidecars are separate files on purpose: OCR happens per photo while the
 * entry is still open (no manifest to extend), and a crash between photo and
 * sidecar costs one OCR call, not the entry.
 */
object Entries {

    const val KEEP_SENT = 15    // uploaded entries kept for the recent list
    const val REPROCESS_PENDING = "reprocess.pending"
    private const val INSTRUCTIONS = "instructions.txt"
    private const val REPROCESS_ERROR = "reprocess.error"

    class Entry(
        val id: String,
        val dir: File,
        val sealed: Boolean,
        val uploaded: Boolean,
        val createdAt: Long,
        val photoCount: Int,
        val meta: JSONObject?,          // null until extraction lands
        val cloudStatus: String,        // "", "pending", "imported", "void"
    ) {
        val title: String get() = meta?.optString("title")?.ifEmpty { null } ?: ""
        val author: String get() = meta?.optString("author") ?: ""
        val year: String get() = meta?.optString("year") ?: ""

        fun photos(): List<File> =
            dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
                ?.sortedBy { photoNumber(it.name) } ?: emptyList()

        fun ocrText(): String = photos().mapIndexedNotNull { i, p ->
            val t = File(dir, p.name + ".txt").takeIf { it.isFile }?.readText()?.trim()
            if (t.isNullOrEmpty()) null else "--- Capture ${i + 1} ---\n$t"
        }.joinToString("\n\n")

        fun customInstructions(): String =
            File(dir, INSTRUCTIONS).takeIf { it.isFile }?.readText()?.trim() ?: ""

        fun setCustomInstructions(value: String) {
            val text = value.trim()
            val target = File(dir, INSTRUCTIONS)
            if (text.isEmpty()) target.delete() else Entries.atomicWrite(target, text)
        }

        fun reprocessPending(): Boolean = File(dir, REPROCESS_PENDING).isFile
        fun reprocessError(): String =
            File(dir, REPROCESS_ERROR).takeIf { it.isFile }?.readText()?.trim() ?: ""

        fun requestReprocess() {
            File(dir, REPROCESS_ERROR).delete()
            File(dir, REPROCESS_PENDING).writeText("")
        }

        fun finishReprocess(error: String? = null) {
            File(dir, REPROCESS_PENDING).delete()
            val errorFile = File(dir, REPROCESS_ERROR)
            if (error.isNullOrBlank()) errorFile.delete()
            else Entries.atomicWrite(errorFile, error.trim())
        }
    }

    fun queueRoot(ctx: Context): File = File(ctx.filesDir, "queue").apply { mkdirs() }
    fun sentRoot(ctx: Context): File = File(ctx.filesDir, "sent").apply { mkdirs() }

    /** Everything worth showing, newest first: the queue plus what was sent. */
    fun recent(ctx: Context): List<Entry> {
        val dirs = (queueRoot(ctx).listFiles { f: File -> f.isDirectory } ?: emptyArray()) +
                   (sentRoot(ctx).listFiles { f: File -> f.isDirectory } ?: emptyArray())
        return dirs.mapNotNull { load(ctx, it) }.sortedByDescending { it.createdAt }
    }

    fun find(ctx: Context, id: String): Entry? {
        val q = File(queueRoot(ctx), id)
        val s = File(sentRoot(ctx), id)
        return load(ctx, if (q.isDirectory) q else s)
    }

    /** Remove only this device's browsing/queue copy. Uploaded cloud captures
     *  and desktop imports are intentionally left alone. */
    fun deleteLocal(ctx: Context, entry: Entry) {
        if (Prefs.currentEntryId(ctx) == entry.id) Prefs.setCurrentEntryId(ctx, null)
        entry.dir.deleteRecursively()
    }

    private fun load(ctx: Context, dir: File): Entry? {
        if (!dir.isDirectory) return null
        val manifestFile = File(dir, "manifest.json")
        val manifest = manifestFile.takeIf { it.isFile }?.let {
            try { JSONObject(it.readText()) } catch (_: Exception) { null }
        }
        val uploaded = dir.parentFile?.name == "sent"
        val photos = dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) } ?: emptyArray()
        if (photos.isEmpty() && manifest == null) return null       // empty husk
        val meta = File(dir, "meta.json").takeIf { it.isFile }?.let {
            try { JSONObject(it.readText()) } catch (_: Exception) { null }
        }?.takeIf { it.optString("title").isNotEmpty() || it.optString("author").isNotEmpty() ||
                    it.optString("year").isNotEmpty() }
        return Entry(
            id = dir.name,
            dir = dir,
            sealed = manifestFile.isFile,
            uploaded = uploaded,
            createdAt = manifest?.optLong("created_at", 0L)?.takeIf { it > 0 }
                ?: (photos.maxOfOrNull { it.lastModified() } ?: dir.lastModified()),
            photoCount = photos.size,
            meta = meta,
            cloudStatus = manifest?.optString("cloud_status") ?: "",
        )
    }

    /** The line the recent list prints under a title. */
    fun statusLabel(ctx: Context, e: Entry): String = when {
        !e.sealed -> "capturing"
        e.uploaded && e.cloudStatus == "imported" -> "imported"
        e.uploaded -> "uploaded"
        else -> "pending upload"
    }

    /** Title cell: the book record once extraction lands, progress before. */
    fun titleLabel(ctx: Context, e: Entry): String = when {
        e.title.isNotEmpty() -> e.title
        e.meta != null -> "(no title found)"
        Prefs.mistralKey(ctx).isEmpty() -> "No OCR — add an API key in Settings"
        else -> "Processing…"
    }

    /** Drop the oldest sent entries beyond KEEP_SENT; photos are already in
     *  the cloud, this is only the local browsing copy. */
    fun pruneSent(ctx: Context) {
        val dirs = sentRoot(ctx).listFiles { f: File -> f.isDirectory } ?: return
        dirs.sortedByDescending { load(ctx, it)?.createdAt ?: 0L }
            .drop(KEEP_SENT)
            .forEach { it.deleteRecursively() }
    }

    fun atomicWrite(target: File, text: String) {
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.writeText(text)
        try {
            Files.move(tmp.toPath(), target.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (_: Exception) {
            Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }
}

internal val PHOTO_NAME = Regex("photo_\\d+\\.jpg")
internal fun photoNumber(name: String): Int =
    name.removePrefix("photo_").removeSuffix(".jpg").toIntOrNull() ?: 0
