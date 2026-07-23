package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID

/** A capture replaces the previous-book preview only after it is sealed and
 * submitted to the processing queue. An open entry can therefore coexist with
 * the last submitted book without making that useful reference disappear. */
internal fun selectLastSubmittedEntry(entries: List<Entries.Entry>): Entries.Entry? =
    entries.asSequence()
        .filter { it.sealed }
        .maxByOrNull { it.createdAt }

/** Only a sealed capture which is still in the local queue can be reopened.
 * A sent capture is an immutable delivery record; appending to it would make
 * the phone disagree with the copy already received by the desktop/cloud. */
internal fun selectLastEditableEntry(entries: List<Entries.Entry>): Entries.Entry? =
    entries.asSequence()
        .filter { it.sealed }
        .maxByOrNull { it.createdAt }
        ?.takeIf { !it.uploaded }

internal sealed interface CaptureReopenResult {
    data class Reopened(val cleanupComplete: Boolean) : CaptureReopenResult
    data object NotSealed : CaptureReopenResult
    data object InvalidCapture : CaptureReopenResult
    data object StorageFailure : CaptureReopenResult
}

/**
 * Turn a queued, sealed capture back into an open capture. OCR for individual
 * pages and the last extracted metadata remain useful while editing, but the
 * extraction is marked waiting so sealing after an appended photo or note
 * necessarily refreshes it.
 *
 * All affected files are detached into a hidden same-filesystem directory
 * first. A failed move restores every file; after success, a failed physical
 * cleanup merely leaves an invisible recovery directory and never republishes
 * the old manifest.
 *
 * The caller owns the entry's [EntryOperationLocks] lock and publishes
 * [Prefs.currentEntryId] before releasing it.
 */
internal fun prepareCaptureForEditing(dir: File): CaptureReopenResult {
    if (!dir.isDirectory) return CaptureReopenResult.InvalidCapture
    val manifest = File(dir, "manifest.json")
    if (!manifest.isFile) return CaptureReopenResult.NotSealed
    val photos = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
        ?.sortedBy { photoNumber(it.name) }
        .orEmpty()
    if (photos.isEmpty() || photos.map { photoNumber(it.name) } != (1..photos.size).toList()) {
        return CaptureReopenResult.InvalidCapture
    }

    val stage = File(dir, ".reopen-${UUID.randomUUID()}")
    if (!stage.mkdir()) return CaptureReopenResult.StorageFailure
    val affected = listOf(
        manifest,
        File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE),
        File(dir, Entries.PROCESSING_STATE),
        File(dir, Entries.REPROCESS_PENDING),
        File(dir, "reprocess.error"),
    ).filter { it.isFile }
    val moved = mutableListOf<Pair<File, File>>()
    for ((index, source) in affected.withIndex()) {
        val staged = File(stage, "$index-${source.name}")
        if (!moveCaptureFile(source, staged)) {
            restoreStagedCaptureFiles(moved)
            stage.deleteRecursively()
            return CaptureReopenResult.StorageFailure
        }
        moved += source to staged
    }
    if (!Entries.markWaiting(dir, Entries.ProcessingStage.EXTRACTION)) {
        File(dir, Entries.PROCESSING_STATE).delete()
        restoreStagedCaptureFiles(moved)
        stage.deleteRecursively()
        return CaptureReopenResult.StorageFailure
    }
    return CaptureReopenResult.Reopened(stage.deleteRecursively())
}

internal sealed interface CaptureThumbnailDeleteResult {
    data class Deleted(
        val pageNumber: Int,
        val remainingPhotoCount: Int,
        val cleanupComplete: Boolean,
    ) : CaptureThumbnailDeleteResult

    data object NoPhoto : CaptureThumbnailDeleteResult
    data object SealedCapture : CaptureThumbnailDeleteResult
    data object InvalidCapture : CaptureThumbnailDeleteResult
    data object StorageFailure : CaptureThumbnailDeleteResult
}

private const val COMMITTED_THUMBNAIL_DELETE = ".committed-delete"
private const val THUMBNAIL_DELETE_JOURNAL = ".delete-journal.json"
private const val THUMBNAIL_DELETE_JOURNAL_SCHEMA =
    "org.whl.bookcapture.thumbnail-delete-transaction"
private const val THUMBNAIL_DELETE_JOURNAL_VERSION = 1
private const val THUMBNAIL_DELETE_SIDECAR_MAX_BYTES = 8 * 1024 * 1024
private const val THUMBNAIL_DELETE_JOURNAL_MAX_BYTES = 16 * 1024 * 1024

private data class ThumbnailDeleteMove(
    val sourceName: String,
    val stagedName: String,
)

private data class ThumbnailDeleteRelocation(
    val stagedName: String,
    val destinationName: String,
)

private data class ThumbnailDeleteJournal(
    val sidecarBefore: String?,
    val moves: List<ThumbnailDeleteMove>,
    val relocations: List<ThumbnailDeleteRelocation>,
) {
    fun toJson(captureId: String, pageNumber: Int): JSONObject = JSONObject()
        .put("schema", THUMBNAIL_DELETE_JOURNAL_SCHEMA)
        .put("version", THUMBNAIL_DELETE_JOURNAL_VERSION)
        .put("capture_id", captureId)
        .put("page_number", pageNumber)
        .put("sidecar_before", sidecarBefore ?: JSONObject.NULL)
        .put("moves", JSONArray().apply {
            moves.forEach { move ->
                put(JSONObject()
                    .put("source", move.sourceName)
                    .put("staged", move.stagedName))
            }
        })
        .put("relocations", JSONArray().apply {
            relocations.forEach { relocation ->
                put(JSONObject()
                    .put("staged", relocation.stagedName)
                    .put("destination", relocation.destinationName))
            }
        })
}

/** Finish committed deletes and roll back interrupted, uncommitted ones.
 *
 * A journal is durable before the first live file moves. Therefore an unmarked
 * journal always means rollback, including the narrow window after the new
 * asset contract was published but before the commit marker landed. Unknown
 * pre-journal recovery directories are still preserved rather than guessed
 * disposable.
 */
internal fun cleanupCommittedThumbnailDeletes(dir: File): Boolean =
    CaptureQueueLifecycle.exclusive { cleanupCommittedThumbnailDeletesLocked(dir) }

/** The synchronous queue lifecycle gate complements the coroutine entry lock:
 * Activity/session restoration cannot inspect this entry midway through a
 * thumbnail transaction, even during a configuration-driven recreation. */
private fun cleanupCommittedThumbnailDeletesLocked(dir: File): Boolean {
    val tombstones = dir.listFiles { file ->
        file.isDirectory && file.name.startsWith(".delete-photo-")
    }.orEmpty()
    var cleaned = true
    for (tombstone in tombstones) {
        val marker = File(tombstone, COMMITTED_THUMBNAIL_DELETE)
        if (!marker.isFile) {
            val journalFile = File(tombstone, THUMBNAIL_DELETE_JOURNAL)
            if (journalFile.isFile && !rollbackThumbnailDelete(dir, tombstone, journalFile)) {
                cleaned = false
            }
            continue
        }
        var contentsDeleted = true
        for (child in tombstone.listFiles().orEmpty().filterNot { it == marker }) {
            if (!child.deleteRecursively()) contentsDeleted = false
        }
        if (!contentsDeleted) {
            cleaned = false
            continue
        }
        if (!marker.delete() || !tombstone.delete()) cleaned = false
    }
    return cleaned
}

/** Idempotently restore an interrupted transaction. Never remove the stage
 * until every pre-delete file and the exact prior asset sidecar are verified in
 * the live entry. A failed restore consequently keeps the only remaining bytes
 * available for the next retry or manual recovery. */
private fun rollbackThumbnailDelete(dir: File, stage: File, journalFile: File): Boolean {
    val journal = readThumbnailDeleteJournal(dir, journalFile) ?: return false
    val sourceByStage = journal.moves.associate { it.stagedName to it.sourceName }

    // A relocated page is absent from its stage and occupies the preceding
    // page's live filename. Put it back in the stage first. If its original
    // source already exists, a prior recovery pass completed this step.
    for (relocation in journal.relocations.asReversed()) {
        val staged = File(stage, relocation.stagedName)
        val source = File(dir, sourceByStage.getValue(relocation.stagedName))
        val destination = File(dir, relocation.destinationName)
        when {
            staged.isFile -> Unit
            source.isFile -> Unit
            destination.isFile -> if (!moveCaptureFile(destination, staged)) return false
            else -> return false
        }
    }

    // Restore every source. Both-present is ambiguous (a move implementation
    // may have copied without removing), so retain both and retry rather than
    // deleting either possible sole copy.
    for (move in journal.moves.asReversed()) {
        val source = File(dir, move.sourceName)
        val staged = File(stage, move.stagedName)
        when {
            source.isFile && !staged.exists() -> Unit
            !source.exists() && staged.isFile -> if (!moveCaptureFile(staged, source)) return false
            else -> return false
        }
    }

    val sidecar = File(dir, PHOTO_ASSETS_FILE)
    if (journal.sidecarBefore == null) {
        if (sidecar.exists() && !sidecar.delete()) return false
    } else {
        val restored = runCatching {
            if (!sidecar.isFile || sidecar.readText() != journal.sidecarBefore) {
                Entries.atomicWrite(sidecar, journal.sidecarBefore)
            }
            sidecar.isFile && sidecar.readText() == journal.sidecarBefore
        }.getOrDefault(false)
        if (!restored) return false
    }

    if (journal.moves.any { !File(dir, it.sourceName).isFile }) return false
    val numbers = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
        ?.map { photoNumber(it.name) }
        ?.sorted()
        .orEmpty()
    if (numbers != (1..numbers.size).toList()) return false

    return stage.deleteRecursively() && !stage.exists()
}

private fun readThumbnailDeleteJournal(
    dir: File,
    journalFile: File,
): ThumbnailDeleteJournal? {
    return try {
        if (!journalFile.isFile || journalFile.length() > THUMBNAIL_DELETE_JOURNAL_MAX_BYTES) {
            return null
        }
        val json = JSONObject(journalFile.readText())
        if (json.optString("schema") != THUMBNAIL_DELETE_JOURNAL_SCHEMA ||
            json.optInt("version", -1) != THUMBNAIL_DELETE_JOURNAL_VERSION ||
            json.optString("capture_id") != dir.name ||
            json.optInt("page_number", 0) <= 0
        ) return null
        val sidecarBefore = when (val value = json.opt("sidecar_before")) {
            JSONObject.NULL -> null
            is String -> value.takeIf {
                it.toByteArray(Charsets.UTF_8).size <= THUMBNAIL_DELETE_SIDECAR_MAX_BYTES
            } ?: return null
            else -> return null
        }
        val movesJson = json.optJSONArray("moves") ?: return null
        val moves = (0 until movesJson.length()).map { index ->
            val move = movesJson.optJSONObject(index) ?: return null
            val source = move.optString("source")
            val staged = move.optString("staged")
            if (!safeCaptureReference(source) || !safeCaptureReference(staged) ||
                source == THUMBNAIL_DELETE_JOURNAL || source == COMMITTED_THUMBNAIL_DELETE ||
                staged == THUMBNAIL_DELETE_JOURNAL || staged == COMMITTED_THUMBNAIL_DELETE
            ) return null
            ThumbnailDeleteMove(source, staged)
        }
        if (moves.isEmpty() || moves.map { it.sourceName }.distinct().size != moves.size ||
            moves.map { it.stagedName }.distinct().size != moves.size
        ) return null
        val stages = moves.mapTo(mutableSetOf()) { it.stagedName }
        val sources = moves.mapTo(mutableSetOf()) { it.sourceName }
        val relocationJson = json.optJSONArray("relocations") ?: return null
        val relocations = (0 until relocationJson.length()).map { index ->
            val relocation = relocationJson.optJSONObject(index) ?: return null
            val staged = relocation.optString("staged")
            val destination = relocation.optString("destination")
            if (staged !in stages || destination !in sources ||
                !safeCaptureReference(destination)
            ) return null
            ThumbnailDeleteRelocation(staged, destination)
        }
        if (relocations.map { it.stagedName }.distinct().size != relocations.size ||
            relocations.map { it.destinationName }.distinct().size != relocations.size
        ) return null
        ThumbnailDeleteJournal(sidecarBefore, moves, relocations)
    } catch (_: Exception) {
        null
    }
}

/**
 * Remove any committed photo from an open capture without leaving a hole in
 * CameraX's `photo_1.jpg .. photo_N.jpg` sequence. Later page files and their
 * OCR sidecars are shifted down, while stable asset ids and preserved camera
 * originals remain unchanged. The asset contract is published only after all
 * file moves succeed; any failure restores the original sequence.
 *
 * The caller must hold [EntryOperationLocks] and must have drained/rejected
 * active CameraX writes before entering this function.
 */
internal fun deleteCaptureThumbnail(
    dir: File,
    pageNumber: Int,
): CaptureThumbnailDeleteResult = CaptureQueueLifecycle.exclusive {
    deleteCaptureThumbnailLocked(dir, pageNumber)
}

private fun deleteCaptureThumbnailLocked(
    dir: File,
    pageNumber: Int,
): CaptureThumbnailDeleteResult {
    if (!dir.isDirectory) return CaptureThumbnailDeleteResult.InvalidCapture
    if (!cleanupCommittedThumbnailDeletes(dir)) {
        return CaptureThumbnailDeleteResult.StorageFailure
    }
    if (File(dir, "manifest.json").exists()) return CaptureThumbnailDeleteResult.SealedCapture
    val photos = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
        ?.sortedBy { photoNumber(it.name) }
        .orEmpty()
    if (photos.isEmpty() || pageNumber !in 1..photos.size) {
        return CaptureThumbnailDeleteResult.NoPhoto
    }
    if (photos.map { photoNumber(it.name) } != (1..photos.size).toList()) {
        return CaptureThumbnailDeleteResult.InvalidCapture
    }

    val sidecar = File(dir, PHOTO_ASSETS_FILE)
    val sidecarBefore = sidecar.takeIf { it.isFile }?.let {
        runCatching { it.readText() }.getOrNull()
            ?: return CaptureThumbnailDeleteResult.InvalidCapture
    }
    if (sidecarBefore != null &&
        sidecarBefore.toByteArray(Charsets.UTF_8).size > THUMBNAIL_DELETE_SIDECAR_MAX_BYTES
    ) return CaptureThumbnailDeleteResult.StorageFailure
    val contract = if (sidecarBefore == null) {
        PhotoAssetStore.read(dir)
    } else {
        runCatching { JSONObject(sidecarBefore) }.getOrNull()
            ?.let { capturePhotoAssetsFromJson(it, dir.name) }
            ?: return CaptureThumbnailDeleteResult.InvalidCapture
    }
    val byCapture = contract.assets.groupBy { it.captureFile }
    if (photos.any { photo ->
            val assets = byCapture[photo.name].orEmpty()
            assets.size != 1 || assets.single().captureOrder != photoNumber(photo.name)
        }
    ) return CaptureThumbnailDeleteResult.InvalidCapture
    val targetName = "photo_$pageNumber.jpg"
    val target = byCapture[targetName]?.singleOrNull()
        ?: return CaptureThumbnailDeleteResult.InvalidCapture
    val remainingBefore = contract.assets.filterNot { it.assetId == target.assetId }
    val remainingReferences = remainingBefore.flatMapTo(mutableSetOf()) { asset ->
        listOf(asset.captureFile, asset.original.reference, asset.display.reference)
    }
    if (target.captureFile in remainingReferences) {
        return CaptureThumbnailDeleteResult.InvalidCapture
    }

    val stage = File(dir, ".delete-photo-$pageNumber-${UUID.randomUUID()}")
    if (!stage.mkdir()) return CaptureThumbnailDeleteResult.StorageFailure
    val sources = linkedSetOf<File>()
    fun include(file: File) {
        if (file.isFile) sources += file
    }
    for (number in pageNumber..photos.size) {
        val capture = File(dir, "photo_$number.jpg")
        include(capture)
        include(File(dir, capture.name + ".txt"))
        include(File(dir, capture.name + Entries.MISTRAL_RESPONSE_SUFFIX))
    }
    for (reference in listOf(target.original.reference, target.display.reference)) {
        if (reference !in remainingReferences && safeCaptureReference(reference)) {
            include(File(dir, reference))
        }
    }
    dir.listFiles { file ->
        file.isFile && file.name.startsWith(".cloud-reocr-${target.assetId}-")
    }?.forEach(::include)
    listOf(
        "meta.json",
        Entries.MISTRAL_EXTRACTION_RESPONSE,
        Entries.PROCESSING_STATE,
        Entries.REPROCESS_PENDING,
        "reprocess.error",
    ).forEach { include(File(dir, it)) }

    val moves = sources.mapIndexed { index, source ->
        ThumbnailDeleteMove(source.name, "$index-${source.name}")
    }
    val stagedBySource = moves.associateBy { it.sourceName }
    val relocations = mutableListOf<ThumbnailDeleteRelocation>()
    for (number in (pageNumber + 1)..photos.size) {
        val oldName = "photo_$number.jpg"
        val newName = "photo_${number - 1}.jpg"
        for (suffix in listOf("", ".txt", Entries.MISTRAL_RESPONSE_SUFFIX)) {
            stagedBySource[oldName + suffix]?.let { move ->
                relocations += ThumbnailDeleteRelocation(move.stagedName, newName + suffix)
            }
        }
    }
    val journal = ThumbnailDeleteJournal(sidecarBefore, moves, relocations)
    val journalFile = File(stage, THUMBNAIL_DELETE_JOURNAL)
    val journalWritten = runCatching {
        Entries.atomicWrite(journalFile, journal.toJson(dir.name, pageNumber).toString())
        journalFile.isFile && journalFile.length() <= THUMBNAIL_DELETE_JOURNAL_MAX_BYTES
    }.getOrDefault(false)
    if (!journalWritten) {
        stage.deleteRecursively()
        return CaptureThumbnailDeleteResult.StorageFailure
    }

    for (move in moves) {
        val source = File(dir, move.sourceName)
        val staged = File(stage, move.stagedName)
        if (!moveCaptureFile(source, staged)) {
            rollbackThumbnailDelete(dir, stage, journalFile)
            return CaptureThumbnailDeleteResult.StorageFailure
        }
    }

    for (relocation in relocations) {
        if (!moveCaptureFile(
                File(stage, relocation.stagedName),
                File(dir, relocation.destinationName),
            )
        ) {
            rollbackThumbnailDelete(dir, stage, journalFile)
            return CaptureThumbnailDeleteResult.StorageFailure
        }
    }

    val now = System.currentTimeMillis()
    fun resetChoice(choice: PhotoSelectionChoice): PhotoSelectionChoice =
        if (!choice.manual || choice.assetId == target.assetId) {
            choice.copy(
                assetId = null,
                manual = false,
                revision = choice.revision + 1,
                updatedAt = now,
            )
        } else choice
    val updatedAssets = remainingBefore.map { asset ->
        val oldCapture = asset.captureFile
        val order = if (asset.captureOrder > pageNumber) {
            asset.captureOrder - 1
        } else asset.captureOrder
        val capture = "photo_$order.jpg"
        val role = if (asset.role.manualOverride == null) {
            asset.role.copy(
                suggestedRole = PhotoRole.OTHER,
                confidence = 0.0,
                reason = "Awaiting bibliographic evidence after photo removal",
                algorithm = "android-capture",
                algorithmVersion = "1",
            )
        } else asset.role
        asset.copy(
            captureOrder = order,
            captureFile = capture,
            original = asset.original.copy(
                reference = if (asset.original.reference == oldCapture) {
                    capture
                } else asset.original.reference,
            ),
            display = asset.display.copy(
                reference = if (asset.display.reference == oldCapture) {
                    capture
                } else asset.display.reference,
            ),
            role = role,
            processingRequest = null,
        )
    }
    val updated = contract.copy(
        assets = updatedAssets,
        selections = CapturePhotoSelections(
            primaryTitle = resetChoice(contract.selections.primaryTitle),
            thumbnail = resetChoice(contract.selections.thumbnail),
        ),
        legacyFallback = false,
    )
    val persisted = runCatching {
        Entries.atomicWrite(sidecar, updated.toJson().toString())
        true
    }.getOrDefault(false)
    if (!persisted) {
        rollbackThumbnailDelete(dir, stage, journalFile)
        return CaptureThumbnailDeleteResult.StorageFailure
    }

    val committed = runCatching {
        Entries.atomicWrite(File(stage, COMMITTED_THUMBNAIL_DELETE), "1")
        true
    }.getOrDefault(false)
    if (!committed) {
        rollbackThumbnailDelete(dir, stage, journalFile)
        return CaptureThumbnailDeleteResult.StorageFailure
    }

    return CaptureThumbnailDeleteResult.Deleted(
        pageNumber = pageNumber,
        remainingPhotoCount = photos.size - 1,
        cleanupComplete = cleanupCommittedThumbnailDeletes(dir),
    )
}

private fun moveCaptureFile(source: File, destination: File): Boolean = try {
    try {
        Files.move(source.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE)
    } catch (_: Exception) {
        Files.move(source.toPath(), destination.toPath())
    }
    destination.isFile && !source.exists()
} catch (_: Exception) {
    false
}

private fun restoreStagedCaptureFiles(moved: List<Pair<File, File>>) {
    for ((source, staged) in moved.asReversed()) {
        if (staged.isFile && !source.exists()) moveCaptureFile(staged, source)
    }
}

private val SAFE_CAPTURE_REFERENCE = Regex("[A-Za-z0-9._-]+")
private fun safeCaptureReference(value: String): Boolean =
    value.isNotEmpty() && value.matches(SAFE_CAPTURE_REFERENCE) &&
        !value.contains('/') && !value.contains('\\')

internal data class CaptureExtraField(
    val key: String,
    val label: String,
    val value: String,
)

/** The capture card itself is intentionally primary-only. Its popup contains
 * every catalog field beyond title/author/year, while excluding capture
 * provenance and transport internals. */
internal fun captureExtraFields(metadata: JSONObject?): List<CaptureExtraField> {
    val details = BookDetailPresenter.from(metadata)
    val fields = mutableListOf<BookDetailField>()
    fields += details.secondary
    if (details.volumeTag.isNotEmpty()) {
        fields += BookDetailField("Volume", details.volumeTag.removePrefix("Vol. "))
    }
    if (details.overview.isNotEmpty()) fields += BookDetailField("Overview", details.overview)
    fields += details.other
    return fields.map { field ->
        CaptureExtraField(
            key = field.label.lowercase().replace(' ', '_'),
            label = field.label,
            value = field.value,
        )
    }
}
