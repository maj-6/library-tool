package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID

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
    const val PROCESSING_STATE = "processing.json"
    private const val INSTRUCTIONS = "instructions.txt"
    private const val REPROCESS_ERROR = "reprocess.error"

    enum class ProcessingStatus(val wireValue: String) {
        WAITING("waiting"),
        PROCESSING("processing"),
        FAILED("failed"),
        PARTIAL("partial"),
        COMPLETE("complete"),
    }

    enum class ProcessingStage(val wireValue: String) {
        WAITING("waiting"),
        STANDARDIZING("standardizing"),
        OCR("ocr"),
        EXTRACTION("extraction"),
        COMPLETE("complete"),
    }

    enum class DeleteResult {
        DELETED,
        ACTIVE_CAPTURE,
        ALREADY_UPLOADED,
        MISSING,
        DELETE_FAILED,
    }

    data class ProcessingState(
        val status: ProcessingStatus,
        val stage: ProcessingStage,
        val retryable: Boolean,
        val lastError: String,
        val updatedAt: Long,
        val bestStatus: ProcessingStatus? = null,
    )

    class Entry(
        val id: String,
        val dir: File,
        val sealed: Boolean,
        val uploaded: Boolean,
        val createdAt: Long,
        val photoCount: Int,
        val meta: JSONObject?,          // null until extraction lands
        val cloudStatus: String,        // "", "pending", "imported", "void"
        val processing: ProcessingState,
        val processingRecorded: Boolean,
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

        fun requestReprocess(): Boolean {
            File(dir, REPROCESS_ERROR).delete()
            if (!holdForProcessing(dir)) return false
            if (!markWaiting(dir, ProcessingStage.EXTRACTION)) {
                File(dir, REPROCESS_PENDING).delete()
                return false
            }
            return true
        }

        fun finishReprocess(error: String? = null) {
            File(dir, REPROCESS_PENDING).delete()
            val errorFile = File(dir, REPROCESS_ERROR)
            if (error.isNullOrBlank()) errorFile.delete()
            else Entries.atomicWrite(errorFile, error.trim())
        }
    }

    fun markWaiting(dir: File, stage: ProcessingStage = ProcessingStage.WAITING) =
        writeTransition(dir, ProcessingStatus.WAITING, stage, retryable = true, lastError = "")

    fun markProcessing(dir: File, stage: ProcessingStage) =
        writeTransition(dir, ProcessingStatus.PROCESSING, stage, retryable = true, lastError = "")

    fun markFailed(dir: File, stage: ProcessingStage, error: String, retryable: Boolean) =
        writeTransition(dir, ProcessingStatus.FAILED, stage, retryable, error.trim().take(500))

    fun markPartial(dir: File, warning: String) =
        writeTransition(
            dir,
            ProcessingStatus.PARTIAL,
            ProcessingStage.EXTRACTION,
            retryable = true,
            lastError = warning.trim().take(500),
        )

    fun markComplete(dir: File) =
        writeTransition(
            dir,
            ProcessingStatus.COMPLETE,
            ProcessingStage.COMPLETE,
            retryable = false,
            lastError = "",
        )

    /** UploadWorker already honors this marker. Keep an explicitly requested
     *  reprocess local until a validated retry completes, so the entry cannot
     *  be moved out from under the user-requested operation. Automatic partial
     *  extraction does not create this hold; photos still ship after grace. */
    fun holdForProcessing(dir: File): Boolean = try {
        atomicWrite(File(dir, REPROCESS_PENDING), "")
        true
    } catch (_: Exception) {
        false
    }

    /** Record the current attempt truthfully while retaining the best accepted
     * output separately. A failed retry must say failed, without deleting the
     * complete/partial metadata that remains useful. */
    private fun writeTransition(
        dir: File,
        requestedStatus: ProcessingStatus,
        stage: ProcessingStage,
        retryable: Boolean,
        lastError: String,
    ): Boolean = try {
        val current = readProcessingState(dir) ?: inferredProcessingState(dir)
        val bestStatus = when {
            requestedStatus == ProcessingStatus.COMPLETE -> ProcessingStatus.COMPLETE
            current?.bestStatus == ProcessingStatus.COMPLETE ||
                current?.status == ProcessingStatus.COMPLETE -> ProcessingStatus.COMPLETE
            requestedStatus == ProcessingStatus.PARTIAL -> ProcessingStatus.PARTIAL
            current?.bestStatus == ProcessingStatus.PARTIAL ||
                current?.status == ProcessingStatus.PARTIAL -> ProcessingStatus.PARTIAL
            else -> null
        }
        val state = JSONObject()
            .put("status", requestedStatus.wireValue)
            .put("best_status", bestStatus?.wireValue ?: "")
            .put("stage", stage.wireValue)
            .put("retryable", retryable)
            .put("last_error", lastError)
            .put("updated_at", System.currentTimeMillis())
        atomicWrite(File(dir, PROCESSING_STATE), state.toString())
        true
    } catch (_: Exception) {
        false
    }

    fun readProcessingState(dir: File): ProcessingState? {
        val file = File(dir, PROCESSING_STATE).takeIf { it.isFile } ?: return null
        return try {
            val data = JSONObject(file.readText())
            val status = ProcessingStatus.entries.firstOrNull {
                it.wireValue == data.optString("status")
            } ?: return null
            val stage = ProcessingStage.entries.firstOrNull {
                it.wireValue == data.optString("stage")
            } ?: return null
            val bestStatus = ProcessingStatus.entries.firstOrNull {
                it.wireValue == data.optString("best_status")
            } ?: status.takeIf {
                it == ProcessingStatus.COMPLETE || it == ProcessingStatus.PARTIAL
            }
            ProcessingState(
                status = status,
                stage = stage,
                retryable = data.optBoolean("retryable", false),
                lastError = data.optString("last_error").trim(),
                updatedAt = data.optLong("updated_at", 0L),
                bestStatus = bestStatus,
            )
        } catch (_: Exception) {
            null
        }
    }

    private fun inferredProcessingState(dir: File): ProcessingState? {
        val metadata = readMetadata(dir)
        return if (metadata != null) ProcessingState(
            ProcessingStatus.COMPLETE,
            ProcessingStage.COMPLETE,
            retryable = false,
            lastError = "",
            updatedAt = File(dir, "meta.json").lastModified(),
        ) else null
    }

    fun queueRoot(ctx: Context): File = File(ctx.filesDir, "queue").apply { mkdirs() }
    fun sentRoot(ctx: Context): File = File(ctx.filesDir, "sent").apply { mkdirs() }

    /** Everything worth showing, newest first: the queue plus what was sent. */
    fun recent(ctx: Context): List<Entry> {
        val dirs = (queueRoot(ctx).listFiles { f: File -> f.isDirectory } ?: emptyArray()) +
                   (sentRoot(ctx).listFiles { f: File -> f.isDirectory } ?: emptyArray())
        return dirs.mapNotNull(::load).sortedByDescending { it.createdAt }
    }

    fun find(ctx: Context, id: String): Entry? {
        val q = File(queueRoot(ctx), id)
        val s = File(sentRoot(ctx), id)
        return load(if (q.isDirectory) q else s)
    }

    /** Remove only this device's browsing/queue copy while excluding delivery
     *  and processing for the same entry. The active in-memory capture must be
     *  discarded from Camera, never out from under its CaptureSession. */
    suspend fun deleteLocalSafely(
        ctx: Context,
        entryId: String,
        allowUploaded: Boolean,
    ): DeleteResult = EntryOperationLocks.withLock(entryId) {
        if (Prefs.currentEntryId(ctx) == entryId) return@withLock DeleteResult.ACTIVE_CAPTURE
        val entry = find(ctx, entryId) ?: return@withLock DeleteResult.MISSING
        if (entry.uploaded && !allowUploaded) return@withLock DeleteResult.ALREADY_UPLOADED
        deleteDirectoryResult(entry.dir)
    }

    private fun load(dir: File): Entry? {
        if (!dir.isDirectory) return null
        val manifestFile = File(dir, "manifest.json")
        val manifest = manifestFile.takeIf { it.isFile }?.let {
            try { JSONObject(it.readText()) } catch (_: Exception) { null }
        }
        val uploaded = dir.parentFile?.name == "sent"
        val photos = dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) } ?: emptyArray()
        if (photos.isEmpty() && manifest == null) return null       // empty husk
        val meta = readMetadata(dir)
        val recorded = File(dir, PROCESSING_STATE).isFile
        val processing = readProcessingState(dir) ?: inferredProcessingState(dir)
            ?: ProcessingState(
                ProcessingStatus.WAITING,
                ProcessingStage.WAITING,
                retryable = true,
                lastError = "",
                updatedAt = 0L,
            )
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
            processing = processing,
            processingRecorded = recorded,
        )
    }

    private fun readMetadata(dir: File): JSONObject? =
        File(dir, "meta.json").takeIf { it.isFile }?.let {
            try { JSONObject(it.readText()) } catch (_: Exception) { null }
        }?.takeIf { Pipeline.hasPopulatedMetadata(it) }

    /** The line the recent list prints under a title. */
    fun statusLabel(ctx: Context, e: Entry): String {
        val importOutcome = remoteImportTerminalLabel(e.cloudStatus)
        return when {
            // A cloud-side failure/void is final and must not masquerade as a
            // generic successful upload, even when local processing succeeded.
            e.uploaded && importOutcome != null && importOutcome != "imported" -> importOutcome
            // Preserve the compact label for legacy sent entries that predate
            // per-entry processing state and never had extraction metadata.
            e.uploaded && !e.processingRecorded && e.meta == null && importOutcome == "imported" -> "imported"
            e.uploaded && !e.processingRecorded && e.meta == null -> "uploaded"
            !e.sealed && e.processing.status == ProcessingStatus.WAITING -> "capturing \u00b7 waiting"
            !e.sealed && e.processing.status == ProcessingStatus.PROCESSING -> "capturing \u00b7 processing"
            e.processing.status == ProcessingStatus.WAITING -> "waiting"
            e.processing.status == ProcessingStatus.PROCESSING -> "processing"
            e.processing.status == ProcessingStatus.FAILED -> "failed"
            e.processing.status == ProcessingStatus.PARTIAL -> "partial"
            e.uploaded && importOutcome == "imported" -> "complete \u00b7 imported"
            e.uploaded -> "complete \u00b7 uploaded"
            else -> when {
                Prefs.transport(ctx) != "cloud" -> "complete \u00b7 pending delivery"
                cloudUploadOwnership(readCaptureCreator(ctx, e.dir), Prefs.userId(ctx)) ==
                    CloudUploadOwnership.NEEDS_CLAIM -> "complete \u00b7 claim for cloud"
                cloudUploadOwnership(readCaptureCreator(ctx, e.dir), Prefs.userId(ctx)) ==
                    CloudUploadOwnership.DIFFERENT_ACCOUNT -> "complete \u00b7 different account"
                else -> "complete \u00b7 pending upload"
            }
        }
    }

    /** Title cell: the book record once extraction lands, progress before. */
    fun titleLabel(ctx: Context, e: Entry): String = when {
        e.title.isNotEmpty() -> e.title
        e.meta != null -> metadataFallbackLabel(e.meta)
        e.processing.status == ProcessingStatus.FAILED && e.processing.lastError.isNotEmpty() ->
            "Processing failed \u2014 ${e.processing.lastError.take(120)}"
        Prefs.mistralKey(ctx).isEmpty() -> "No OCR — add an API key in Settings"
        else -> "Processing…"
    }

    private fun metadataFallbackLabel(meta: JSONObject): String {
        for (key in Pipeline.FIELDS.filterNot { it == "title" }) {
            val value = meta.optString(key).trim()
            if (value.isNotEmpty()) return if (key == "subtitle") value else "$key: $value"
        }
        val extra = meta.optJSONObject("extra")
        if (extra != null) {
            for (key in extra.keys()) {
                val value = extra.optString(key).trim()
                if (value.isNotEmpty()) return "${key.replace('_', ' ')}: $value"
            }
        }
        return "(no title found)"
    }

    /** Drop the oldest sent entries beyond KEEP_SENT; photos are already in
     *  the cloud, this is only the local browsing copy. */
    suspend fun pruneSent(ctx: Context) {
        val dirs = sentRoot(ctx).listFiles { f: File -> f.isDirectory } ?: return
        dirs.sortedByDescending { load(it)?.createdAt ?: 0L }
            .drop(KEEP_SENT)
            .forEach { dir ->
                EntryOperationLocks.withLock(dir.name) { dir.deleteRecursively() }
            }
    }

    fun atomicWrite(target: File, text: String) {
        val tmp = File(target.parentFile, ".${target.name}.${UUID.randomUUID()}.tmp")
        try {
            tmp.writeText(text)
            try {
                Files.move(tmp.toPath(), target.toPath(),
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            } catch (_: Exception) {
                Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
            }
        } finally {
            tmp.delete()
        }
    }
}

internal val PHOTO_NAME = Regex("photo_\\d+\\.jpg")
internal fun photoNumber(name: String): Int =
    name.removePrefix("photo_").removeSuffix(".jpg").toIntOrNull() ?: 0

/** Keep "already gone" distinct from an attempted deletion that left files
 * behind. Callers must not dismiss a book as deleted after a storage failure. */
internal fun deleteDirectoryResult(
    dir: File,
    delete: (File) -> Boolean = { it.deleteRecursively() },
): Entries.DeleteResult {
    if (!dir.exists()) return Entries.DeleteResult.MISSING
    val reportedSuccess = try { delete(dir) } catch (_: Exception) { false }
    return if (reportedSuccess && !dir.exists()) Entries.DeleteResult.DELETED
    else Entries.DeleteResult.DELETE_FAILED
}
