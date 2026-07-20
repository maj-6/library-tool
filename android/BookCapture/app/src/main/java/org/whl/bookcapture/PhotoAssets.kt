package org.whl.bookcapture

import android.graphics.BitmapFactory
import androidx.exifinterface.media.ExifInterface
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID

internal const val PHOTO_ASSETS_FILE = "photo_assets.json"
internal const val PHOTO_ASSETS_MANIFEST_KEY = "photo_assets"
internal const val PHOTO_ASSETS_META_KEY = "_capture_photo_assets"
internal const val PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
internal const val PHOTO_ASSETS_VERSION = 1
internal const val PHOTO_PROCESSING_REQUEST_SCHEMA =
    "org.whl.bookcapture.photo-processing-request"
internal const val PHOTO_PROCESSING_REQUEST_VERSION = 1

internal enum class PhotoRole(val wireValue: String) {
    TITLE_PAGE("title_page"),
    COVER("cover"),
    SPINE("spine"),
    OTHER("other");

    companion object {
        fun fromWire(value: String): PhotoRole? =
            values().firstOrNull { it.wireValue == value.trim().lowercase() }
    }
}

internal enum class PhotoAssetLifecycle(val wireValue: String) {
    CAPTURED("captured"),
    QUEUED("queued"),
    RUNNING("running"),
    RETRYING("retrying"),
    COMPLETED("completed"),
    FAILED("failed"),
    CANCELLED("cancelled"),
    OFFLINE_WAITING("offline_waiting");

    companion object {
        fun fromWire(value: String): PhotoAssetLifecycle? =
            values().firstOrNull { it.wireValue == value.trim().lowercase() }
    }
}

/** Requested post-capture outcomes. These are recipe intentions, not claims
 * that Android or a remote service has produced a derivative. */
internal enum class PhotoProcessingOutcome(val wireValue: String) {
    PAGE_DEWARP("page_dewarp"),
    DETECTED_MARGIN_CROP("detected_margin_crop"),
    CONTRAST_NORMALIZATION("contrast_normalization"),
    SPINE_CROP("spine_crop");

    companion object {
        fun fromWire(value: String): PhotoProcessingOutcome? =
            values().firstOrNull { it.wireValue == value.trim().lowercase() }
    }
}

internal data class PhotoProcessingOperation(
    val outcome: PhotoProcessingOutcome,
    /** A derived crop may request a catalog role for its eventual output. */
    val resultRole: PhotoRole? = null,
)

/**
 * Immutable recipe snapshot for one source asset. Version 1 deliberately has
 * only the `requested` state and a null result: no post-processing service is
 * shipped yet. A later schema must prove a produced derivative before it can
 * link one into the original/display lineage.
 */
internal data class PhotoProcessingRequest(
    val requestId: String,
    val requestRevision: Int,
    val profile: PostProcessingProfile,
    val requestedAt: Long,
    val sourceAssetId: String,
    val sourceRole: PhotoRole,
    val sourceOriginalSha256: String,
    val sourceOriginalRevision: Int,
    val sourceDisplaySha256: String,
    val sourceDisplayRevision: Int,
    val operations: List<PhotoProcessingOperation>,
)

internal data class PhotoImageProperties(
    val width: Int = 0,
    val height: Int = 0,
    val orientationDegrees: Int = 0,
)

internal data class PhotoOriginal(
    val reference: String,
    val sha256: String = "",
    val revision: Int = 1,
    val width: Int = 0,
    val height: Int = 0,
    val orientationDegrees: Int = 0,
)

internal data class PhotoDisplayDerivative(
    val reference: String,
    val sha256: String = "",
    val revision: Int = 1,
    val width: Int = 0,
    val height: Int = 0,
    val orientationDegrees: Int = 0,
    val recipe: String = "camera-original",
    val recipeVersion: String = "1",
    /**
     * Row-major 3x3 projective transform from the immediately preceding
     * display revision's normalized coordinates into this revision's
     * normalized coordinates. A correction response can therefore move the
     * persisted OCR polygons without re-running OCR. Null means that the
     * provider did not prove how the pixels moved, so geometry must not be
     * carried forward speculatively.
     */
    val sourceToDisplayHomography: List<Double>? = null,
)

internal data class PhotoLifecycleState(
    val state: PhotoAssetLifecycle = PhotoAssetLifecycle.COMPLETED,
    val jobId: String = "",
    val error: String = "",
    val updatedAt: Long = 0L,
)

internal data class PhotoRoleAssignment(
    val suggestedRole: PhotoRole = PhotoRole.OTHER,
    val confidence: Double = 0.0,
    val reason: String = "No role evidence",
    val algorithm: String = "legacy-fallback",
    val algorithmVersion: String = "1",
    val manualOverride: PhotoRole? = null,
    val manualRevision: Int = 0,
    val manualUpdatedAt: Long = 0L,
) {
    val effectiveRole: PhotoRole get() = manualOverride ?: suggestedRole
    val isSuggested: Boolean get() = manualOverride == null
}

internal data class NormalizedPoint(val x: Double, val y: Double)

internal data class PhotoOcrRegion(
    val id: String,
    val regionType: String,
    val polygon: List<NormalizedPoint>,
    val text: String = "",
    val confidence: Double? = null,
)

internal data class PhotoOcrGeometry(
    val assetId: String,
    val sourceSha256: String,
    val sourceRevision: Int,
    val displayRevision: Int,
    val coordinateSpace: String,
    val width: Int,
    val height: Int,
    val orientationDegrees: Int,
    val engine: String,
    val model: String,
    val engineVersion: String = "",
    val regions: List<PhotoOcrRegion>,
)

internal data class OcrGeometryDraft(
    val coordinateSpace: String = "display_normalized",
    val width: Int,
    val height: Int,
    val orientationDegrees: Int = 0,
    val engine: String,
    val model: String,
    val engineVersion: String = "",
    val regions: List<PhotoOcrRegion>,
)

internal data class CapturePhotoAsset(
    val assetId: String,
    val captureOrder: Int,
    val captureFile: String,
    val original: PhotoOriginal,
    val display: PhotoDisplayDerivative,
    val lifecycle: PhotoLifecycleState = PhotoLifecycleState(),
    val role: PhotoRoleAssignment = PhotoRoleAssignment(),
    val geometries: List<PhotoOcrGeometry> = emptyList(),
    val processingRequest: PhotoProcessingRequest? = null,
)

internal data class PhotoSelectionChoice(
    val assetId: String? = null,
    val manual: Boolean = false,
    val revision: Int = 0,
    val updatedAt: Long = 0L,
)

internal data class CapturePhotoSelections(
    val primaryTitle: PhotoSelectionChoice = PhotoSelectionChoice(),
    val thumbnail: PhotoSelectionChoice = PhotoSelectionChoice(),
)

internal data class CapturePhotoAssets(
    val captureId: String,
    val assets: List<CapturePhotoAsset>,
    val selections: CapturePhotoSelections = CapturePhotoSelections(),
    val legacyFallback: Boolean = false,
) {
    fun orderedAssets(): List<CapturePhotoAsset> =
        assets.sortedWith(compareBy<CapturePhotoAsset> { it.captureOrder }.thenBy { it.assetId })

    fun resolvedPrimaryTitleAsset(): CapturePhotoAsset? {
        val usable = orderedAssets()
        if (usable.isEmpty()) return null
        selections.primaryTitle.assetId?.let { selected ->
            usable.firstOrNull { it.assetId == selected }?.let { return it }
        }
        return usable.filter { it.role.effectiveRole == PhotoRole.TITLE_PAGE }
            .sortedWith(compareByDescending<CapturePhotoAsset> { it.role.confidence }
                .thenBy { it.captureOrder }.thenBy { it.assetId })
            .firstOrNull() ?: usable.first()
    }

    fun resolvedThumbnailAsset(): CapturePhotoAsset? {
        val usable = orderedAssets()
        if (usable.isEmpty()) return null
        selections.thumbnail.assetId?.let { selected ->
            usable.firstOrNull { it.assetId == selected }?.let { return it }
        }
        return usable.filter {
            it.role.effectiveRole == PhotoRole.COVER &&
                (it.role.manualOverride == PhotoRole.COVER ||
                    it.role.confidence >= MIN_AUTO_COVER_CONFIDENCE)
        }
            .sortedWith(compareByDescending<CapturePhotoAsset> { it.role.confidence }
                .thenBy { it.captureOrder }.thenBy { it.assetId })
            .firstOrNull() ?: resolvedPrimaryTitleAsset() ?: usable.first()
    }
}

/** Result of removing a committed page from an open capture.
 *
 * This operation deliberately understands only the final member of a dense
 * `photo_1.jpg .. photo_N.jpg` sequence. Removing an interior page would make
 * CameraX's next reservation ambiguous and would require renumbering immutable
 * asset identities, so callers get [NotFinalDensePage] instead.
 */
internal sealed interface FinalCommittedPhotoRemoval {
    data class Removed(
        val pageNumber: Int,
        val remainingPhotoCount: Int,
        /** False means the files are already detached in a hidden staging
         * directory, but the best-effort physical cleanup should be retried. */
        val cleanupComplete: Boolean,
    ) : FinalCommittedPhotoRemoval

    data object NoPhotos : FinalCommittedPhotoRemoval
    data object NotFinalDensePage : FinalCommittedPhotoRemoval
    data object InvalidContract : FinalCommittedPhotoRemoval
    data object StorageFailure : FinalCommittedPhotoRemoval
}

internal data class EntryPhotoDescriptor(
    val assetId: String,
    /** Zero-based UI order; the persisted contract keeps a one-based capture order. */
    val order: Int,
    val captureFile: File,
    val rawFile: File,
    val displayFile: File,
    val role: PhotoRole,
    val roleSuggested: Boolean,
    val confidence: Double,
    val reason: String,
    val algorithm: String,
    val algorithmVersion: String,
    val lifecycle: PhotoLifecycleState,
    val displayRevision: Int,
    val geometry: List<EntryPhotoGeometryRegion>,
    val postProcessingPending: Boolean = false,
) {
    val captureOrder: Int get() = order + 1
    val roleLabel: String get() = role.wireValue
    val originalPreserved: Boolean get() = rawFile != displayFile
}

internal data class EntryPhotoGeometryRegion(
    val normalizedPolygon: List<NormalizedPoint>,
    val label: String,
    val text: String = "",
    val confidence: Double? = null,
)

internal data class BibliographicPhotoEvidence(
    val assetId: String,
    val captureOrder: Int,
    val matchedFields: List<String>,
) {
    val score: Int get() = matchedFields.size
}

private val SAFE_ASSET_TOKEN = Regex("[A-Za-z0-9._-]+")
private val SHA256_HEX = Regex("[0-9a-f]{64}")
private const val TITLE_ROLE_ALGORITHM = "android-bibliographic-title-page"
private const val TITLE_ROLE_ALGORITHM_VERSION = "1"
private const val COVER_ROLE_ALGORITHM = "android-bibliographic-cover"
private const val SPINE_ROLE_ALGORITHM = "android-aspect-spine"
private const val MIN_AUTO_COVER_CONFIDENCE = 0.65
private const val MAX_OCR_REGIONS_PER_ASSET = 500
private const val MAX_OCR_REGION_TEXT_CHARS = 500
private const val MAX_OCR_POLYGON_POINTS = 16
private const val PHOTO_PROCESSING_REQUESTED = "requested"
private const val PHOTO_PROCESSING_DISABLED = "disabled"

internal object PhotoAssetStore {
    private val monitor = Any()

    fun read(dir: File): CapturePhotoAssets = synchronized(monitor) {
        readCurrent(dir) ?: legacyContract(dir)
    }

    fun descriptors(dir: File): List<EntryPhotoDescriptor> = synchronized(monitor) {
        val contract = reconcileContract(dir, readCurrent(dir) ?: legacyContract(dir))
        contract.orderedAssets().mapNotNull { asset -> descriptor(dir, asset) }
    }

    fun descriptor(dir: File, photo: File): EntryPhotoDescriptor? = synchronized(monitor) {
        descriptors(dir).firstOrNull { descriptor ->
            sameFile(descriptor.captureFile, photo) || sameFile(descriptor.displayFile, photo) ||
                sameFile(descriptor.rawFile, photo)
        }
    }

    fun detailHero(dir: File): EntryPhotoDescriptor? = synchronized(monitor) {
        val contract = reconcileContract(dir, readCurrent(dir) ?: legacyContract(dir))
        val descriptors = contract.orderedAssets().mapNotNull { descriptor(dir, it) }
        val selected = contract.resolvedPrimaryTitleAsset()?.assetId
        descriptors.firstOrNull { it.assetId == selected } ?: descriptors.firstOrNull()
    }

    fun thumbnail(dir: File): EntryPhotoDescriptor? = synchronized(monitor) {
        val contract = reconcileContract(dir, readCurrent(dir) ?: legacyContract(dir))
        val descriptors = contract.orderedAssets().mapNotNull { descriptor(dir, it) }
        val selected = contract.resolvedThumbnailAsset()?.assetId
        descriptors.firstOrNull { it.assetId == selected } ?: descriptors.firstOrNull()
    }

    /**
     * Remove the final committed page and every derivative that belongs only
     * to it. Files first move into a same-directory staging folder; only after
     * that succeeds is the asset contract atomically rewritten. A contract
     * write failure moves the files back, so an undo cannot publish a contract
     * that names missing camera evidence.
     *
     * The caller must hold [EntryOperationLocks] for [File.name]. This method
     * also rejects sealed entries and non-dense sequences, keeping it safe to
     * expose only through the active-capture lifecycle.
     */
    fun removeFinalCommittedPhoto(
        dir: File,
        pageNumber: Int,
    ): FinalCommittedPhotoRemoval = synchronized(monitor) {
        if (!dir.isDirectory || File(dir, "manifest.json").exists()) {
            return@synchronized FinalCommittedPhotoRemoval.InvalidContract
        }
        val photos = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
            ?.sortedBy { photoNumber(it.name) }
            .orEmpty()
        if (photos.isEmpty()) return@synchronized FinalCommittedPhotoRemoval.NoPhotos
        val numbers = photos.map { photoNumber(it.name) }
        if (numbers != (1..photos.size).toList() || pageNumber != photos.size) {
            return@synchronized FinalCommittedPhotoRemoval.NotFinalDensePage
        }
        val targetPhoto = photos.last()
        if (photoNumber(targetPhoto.name) != pageNumber) {
            return@synchronized FinalCommittedPhotoRemoval.NotFinalDensePage
        }

        val sidecar = File(dir, PHOTO_ASSETS_FILE)
        val sidecarBefore = sidecar.takeIf { it.isFile }?.let {
            runCatching { it.readText() }.getOrNull()
                ?: return@synchronized FinalCommittedPhotoRemoval.InvalidContract
        }
        var current = if (sidecarBefore != null) {
            runCatching { JSONObject(sidecarBefore) }.getOrNull()
                ?.let { capturePhotoAssetsFromJson(it, dir.name) }
                ?: return@synchronized FinalCommittedPhotoRemoval.InvalidContract
        } else {
            legacyContract(dir)
        }
        var target = current.assets.firstOrNull { it.captureFile == targetPhoto.name }
        // Registration is additive and intentionally best-effort in the
        // CameraX callback. A valid JPEG may therefore predate its sidecar;
        // synthesize the deterministic legacy identity so undo remains usable.
        if (target == null) {
            target = legacyAsset(dir.name, targetPhoto, pageNumber)
            current = current.copy(assets = current.assets + target)
        }
        if (target.captureOrder != pageNumber) {
            return@synchronized FinalCommittedPhotoRemoval.InvalidContract
        }

        val remainingBeforeInvalidation = current.assets.filterNot { it.assetId == target.assetId }
        val remainingReferences = remainingBeforeInvalidation.flatMapTo(mutableSetOf()) { asset ->
            listOf(asset.captureFile, asset.original.reference, asset.display.reference)
        }
        // A second asset may not borrow the final page's capture filename. If
        // it does, fail closed rather than detach evidence used by both.
        if (target.captureFile in remainingReferences) {
            return@synchronized FinalCommittedPhotoRemoval.InvalidContract
        }

        val candidates = linkedSetOf<File>()
        fun addOwnedReference(reference: String) {
            if (reference !in remainingReferences && safeReference(reference)) {
                File(dir, reference).takeIf { it.isFile }?.let(candidates::add)
            }
        }
        addOwnedReference(target.captureFile)
        addOwnedReference(target.original.reference)
        addOwnedReference(target.display.reference)
        listOf(
            target.captureFile + ".txt",
            target.captureFile + Entries.MISTRAL_RESPONSE_SUFFIX,
            // Extraction and processing describe the complete photo set. Once
            // a page disappears they must not make the remaining set look done.
            "meta.json",
            Entries.MISTRAL_EXTRACTION_RESPONSE,
            Entries.PROCESSING_STATE,
            Entries.REPROCESS_PENDING,
            "reprocess.error",
        ).forEach { name -> File(dir, name).takeIf { it.isFile }?.let(candidates::add) }
        if (targetPhoto !in candidates) {
            return@synchronized FinalCommittedPhotoRemoval.InvalidContract
        }

        val stage = File(dir, ".undo-photo-$pageNumber-${UUID.randomUUID()}")
        if (!stage.mkdir()) return@synchronized FinalCommittedPhotoRemoval.StorageFailure
        val moved = mutableListOf<Pair<File, File>>()
        fun move(source: File, destination: File): Boolean = try {
            try {
                Files.move(source.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE)
            } catch (_: Exception) {
                Files.move(source.toPath(), destination.toPath())
            }
            destination.isFile && !source.exists()
        } catch (_: Exception) {
            false
        }
        fun restoreMovedFiles() {
            for ((source, staged) in moved.asReversed()) {
                if (staged.isFile && !source.exists()) move(staged, source)
            }
            stage.deleteRecursively()
        }
        for (source in candidates) {
            val staged = File(stage, source.name)
            if (!move(source, staged)) {
                restoreMovedFiles()
                return@synchronized FinalCommittedPhotoRemoval.StorageFailure
            }
            moved += source to staged
        }

        // Metadata-derived roles, selections, and processing recipes describe
        // the old complete photo set. Preserve explicit choices on surviving
        // assets, but make every automatic result eligible for recomputation.
        val automaticRoleAlgorithms = setOf(
            "legacy-fallback",
            "android-capture",
            TITLE_ROLE_ALGORITHM,
            COVER_ROLE_ALGORITHM,
            SPINE_ROLE_ALGORITHM,
        )
        val remaining = remainingBeforeInvalidation.map { asset ->
            val resetRole = if (asset.role.algorithm in automaticRoleAlgorithms) {
                asset.role.copy(
                    suggestedRole = PhotoRole.OTHER,
                    confidence = 0.0,
                    reason = "Awaiting bibliographic evidence after page undo",
                    algorithm = "android-capture",
                    algorithmVersion = "1",
                )
            } else asset.role
            asset.copy(role = resetRole, processingRequest = null)
        }
        val now = System.currentTimeMillis()
        fun resetAutomaticOrRemovedChoice(choice: PhotoSelectionChoice): PhotoSelectionChoice =
            if (!choice.manual || choice.assetId == target.assetId) {
                choice.copy(
                    assetId = null,
                    manual = false,
                    revision = choice.revision + 1,
                    updatedAt = now,
                )
            } else choice
        val selections = CapturePhotoSelections(
            resetAutomaticOrRemovedChoice(current.selections.primaryTitle),
            resetAutomaticOrRemovedChoice(current.selections.thumbnail),
        )

        // Reconcile after staging so any already-missing legacy entries are
        // also removed from the new contract.
        val updated = reconcileContract(
            dir,
            current.copy(
                assets = remaining,
                selections = selections,
                legacyFallback = false,
            ),
        )
        if (!persistCurrent(dir, updated)) {
            // persistCurrent is atomic for an open entry, but restore the prior
            // bytes defensively if a storage implementation reported failure.
            runCatching {
                if (sidecarBefore == null) sidecar.delete()
                else Entries.atomicWrite(sidecar, sidecarBefore)
            }
            restoreMovedFiles()
            return@synchronized FinalCommittedPhotoRemoval.StorageFailure
        }

        val cleanupComplete = stage.deleteRecursively()
        FinalCommittedPhotoRemoval.Removed(pageNumber, photos.size - 1, cleanupComplete)
    }

    /** Register immediately after CameraX promotes a complete JPEG. A hard link
     * makes preservation cheap on Android's internal filesystem; copy is a
     * fallback. Checksums/dimensions are completed later on the IO worker. */
    fun registerCapturedPhoto(dir: File, photo: File, captureOrder: Int): CapturePhotoAssets =
        synchronized(monitor) {
            val current = readCurrent(dir) ?: legacyContract(dir)
            val id = stablePhotoAssetId(dir.name, photo.name)
            val originalName = originalFileName(id)
            val originalFile = File(dir, originalName)
            val preserved = preserveOriginalFile(photo, originalFile)
            val prior = current.assets.firstOrNull { it.assetId == id }
            val isNewCapture = prior == null || current.legacyFallback
            val asset = (prior ?: legacyAsset(dir.name, photo, captureOrder)).copy(
                captureOrder = captureOrder,
                captureFile = photo.name,
                original = (prior?.original ?: PhotoOriginal(photo.name)).copy(
                    reference = if (preserved) originalName else photo.name,
                ),
                display = (prior?.display ?: PhotoDisplayDerivative(photo.name)).copy(
                    reference = photo.name,
                ),
                lifecycle = if (isNewCapture) {
                    PhotoLifecycleState(
                        state = PhotoAssetLifecycle.CAPTURED,
                        updatedAt = System.currentTimeMillis(),
                    )
                } else {
                    checkNotNull(prior).lifecycle
                },
                role = if (isNewCapture) {
                    PhotoRoleAssignment(
                        suggestedRole = PhotoRole.OTHER,
                        confidence = 0.0,
                        reason = "Awaiting bibliographic evidence",
                        algorithm = "android-capture",
                        algorithmVersion = "1",
                    )
                } else {
                    checkNotNull(prior).role
                },
            )
            val updated = reconcileContract(
                dir,
                current.copy(
                    assets = current.assets.filterNot { it.assetId == id } + asset,
                    legacyFallback = false,
                ),
            )
            persistCurrent(dir, updated)
            updated
        }

    /** Ensure an immutable source exists and fill its checksum/image facts.
     * False means callers must leave the display file untouched. */
    fun prepareForProcessing(dir: File, photo: File): Boolean = synchronized(monitor) {
        var current = readCurrent(dir) ?: legacyContract(dir)
        var asset = current.assets.firstOrNull { it.captureFile == photo.name }
            ?: legacyAsset(dir.name, photo, photoNumber(photo.name)).also {
                current = current.copy(assets = current.assets + it)
            }
        val originalName = originalFileName(asset.assetId)
        val originalFile = File(dir, originalName)
        val preserved = when {
            asset.original.reference != photo.name && File(dir, asset.original.reference).isFile -> true
            else -> preserveOriginalFile(photo, originalFile)
        }
        val sourceFile = if (preserved) {
            val referenced = File(dir, asset.original.reference)
            if (referenced.isFile && referenced != photo) referenced else originalFile
        } else photo
        val sourceHash = runCatching { sha256(sourceFile) }.getOrDefault("")
        val sourceInfo = imageProperties(sourceFile)
        val displayHash = runCatching { sha256(photo) }.getOrDefault("")
        val displayInfo = imageProperties(photo)
        asset = asset.copy(
            original = asset.original.copy(
                reference = if (preserved) sourceFile.name else photo.name,
                sha256 = sourceHash,
                width = sourceInfo.width,
                height = sourceInfo.height,
                orientationDegrees = sourceInfo.orientationDegrees,
            ),
            display = asset.display.copy(
                reference = photo.name,
                sha256 = displayHash,
                width = displayInfo.width,
                height = displayInfo.height,
                orientationDegrees = displayInfo.orientationDegrees,
            ),
        )
        val updated = reconcileContract(
            dir,
            current.copy(assets = current.assets.filterNot { it.assetId == asset.assetId } + asset),
        )
        persistCurrent(dir, updated)
        preserved && sourceFile != photo && sourceFile.isFile && sourceHash.isNotEmpty()
    }

    /** Record a changed display derivative without ever rewriting the original. */
    fun recordDisplayVersion(
        dir: File,
        photo: File,
        recipe: String,
        recipeVersion: String,
        homography: List<Double>? = null,
    ): Boolean = synchronized(monitor) {
        val current = readCurrent(dir) ?: legacyContract(dir)
        val asset = current.assets.firstOrNull { it.captureFile == photo.name } ?: return@synchronized false
        val hash = runCatching { sha256(photo) }.getOrDefault("")
        if (hash.isEmpty()) return@synchronized false
        val info = imageProperties(photo)
        val changed = asset.display.sha256.isNotEmpty() && asset.display.sha256 != hash
        val display = asset.display.copy(
            reference = photo.name,
            sha256 = hash,
            revision = if (changed) asset.display.revision + 1 else asset.display.revision,
            width = info.width,
            height = info.height,
            orientationDegrees = info.orientationDegrees,
            recipe = if (changed) recipe else asset.display.recipe,
            recipeVersion = if (changed) recipeVersion else asset.display.recipeVersion,
            sourceToDisplayHomography = if (changed) {
                validHomography(homography)
            } else {
                asset.display.sourceToDisplayHomography
            },
        )
        val lifecycle = if (changed || asset.lifecycle.state != PhotoAssetLifecycle.COMPLETED) {
            PhotoLifecycleState(PhotoAssetLifecycle.COMPLETED, updatedAt = System.currentTimeMillis())
        } else asset.lifecycle
        val migratedGeometry = if (changed) {
            asset.geometries.mapNotNull { geometry ->
                transformGeometryForDisplay(geometry, asset.original, asset.display, display)
            }
        } else emptyList()
        val next = asset.copy(
            display = display,
            lifecycle = lifecycle,
            geometries = (asset.geometries + migratedGeometry).distinctBy(::geometryKey),
        )
        if (next == asset) return@synchronized true
        persistCurrent(
            dir,
            current.copy(assets = current.assets.filterNot { it.assetId == asset.assetId } + next),
        )
    }

    /**
     * Persist a versioned post-processing recipe without claiming that any
     * service accepted or completed it. The immutable original and current
     * display checksums are frozen into the request so a later response can be
     * checked against the exact pixels it was asked to process.
     */
    fun requestProcessing(
        dir: File,
        assetId: String,
        profile: PostProcessingProfile,
    ): Boolean = synchronized(monitor) {
        if (!safeToken(assetId) || !validPostProcessingProfile(profile)) {
            return@synchronized false
        }

        var current = readCurrent(dir) ?: return@synchronized false
        var asset = current.assets.firstOrNull { it.assetId == assetId }
            ?: return@synchronized false
        if (asset.original.sha256.isEmpty() || asset.display.sha256.isEmpty()) {
            val capture = File(dir, asset.captureFile).takeIf { it.isFile }
                ?: return@synchronized false
            prepareForProcessing(dir, capture)
            current = readCurrent(dir) ?: return@synchronized false
            asset = current.assets.firstOrNull { it.assetId == assetId }
                ?: return@synchronized false
        }
        if (asset.original.sha256.isEmpty() || asset.display.sha256.isEmpty()) {
            return@synchronized false
        }
        if (!isPostProcessingRole(asset.role.effectiveRole)) return@synchronized false
        val operations = processingOperationsFor(profile, asset.role.effectiveRole)
        if (!validProcessingOperations(operations)) return@synchronized false

        val existing = asset.processingRequest
        if (existing != null && existing.profile == profile &&
            existing.operations == operations &&
            existing.sourceAssetId == asset.assetId &&
            existing.sourceRole == asset.role.effectiveRole &&
            existing.sourceOriginalSha256 == asset.original.sha256 &&
            existing.sourceOriginalRevision == asset.original.revision &&
            existing.sourceDisplaySha256 == asset.display.sha256 &&
            existing.sourceDisplayRevision == asset.display.revision) {
            return@synchronized true
        }

        val request = PhotoProcessingRequest(
            requestId = UUID.randomUUID().toString(),
            requestRevision = (asset.processingRequest?.requestRevision ?: 0) + 1,
            profile = profile,
            requestedAt = System.currentTimeMillis(),
            sourceAssetId = asset.assetId,
            sourceRole = asset.role.effectiveRole,
            sourceOriginalSha256 = asset.original.sha256,
            sourceOriginalRevision = asset.original.revision,
            sourceDisplaySha256 = asset.display.sha256,
            sourceDisplayRevision = asset.display.revision,
            operations = operations.toList(),
        )
        if (!validProcessingRequestForAsset(request, asset.assetId, asset.original, asset.display)) {
            return@synchronized false
        }
        val next = asset.copy(processingRequest = request)
        persistCurrent(
            dir,
            current.copy(assets = current.assets.filterNot { it.assetId == asset.assetId } + next),
        )
    }

    /** Complete upload lineage facts only. This must not mark an unprocessed
     * asset completed: cloud post-processing is merely requested elsewhere. */
    fun completeForUpload(dir: File, photos: List<File>) = synchronized(monitor) {
        for (photo in photos.sortedBy { photoNumber(it.name) }) {
            prepareForProcessing(dir, photo)
        }
    }

    /** A missing/geometry-less OCR result is deliberately a no-op. */
    fun mergeGeometry(dir: File, photo: File, draft: OcrGeometryDraft?): Boolean =
        synchronized(monitor) {
            if (draft == null || draft.regions.isEmpty()) return@synchronized false
            val current = readCurrent(dir) ?: return@synchronized false
            val asset = current.assets.firstOrNull { it.captureFile == photo.name } ?: return@synchronized false
            mergeGeometryLocked(dir, current, asset, draft)
        }

    /**
     * Apply OCR produced from a verified nonlinear cloud derivative. The
     * target is checked against the current contract both before the network
     * call (by the worker) and again here at commit time. This cannot replace
     * the capture transport or immutable original because it writes geometry
     * only.
     */
    fun mergeCloudDisplayReocrGeometry(
        dir: File,
        target: CloudDisplayReocrTarget,
        draft: OcrGeometryDraft?,
    ): Boolean = synchronized(monitor) {
        if (draft == null || draft.regions.isEmpty()) return@synchronized false
        val current = readCurrent(dir) ?: return@synchronized false
        val asset = current.assets.firstOrNull { it.assetId == target.assetId }
            ?: return@synchronized false
        if (!cloudDisplayReocrTargetMatches(current, asset, target) ||
            asset.display.width > 0 && draft.width != asset.display.width ||
            asset.display.height > 0 && draft.height != asset.display.height) {
            return@synchronized false
        }
        val saved = mergeGeometryLocked(dir, current, asset, draft)
        if (saved) cloudDisplayReocrMarker(dir, target).delete()
        saved
    }

    private fun mergeGeometryLocked(
        dir: File,
        current: CapturePhotoAssets,
        asset: CapturePhotoAsset,
        draft: OcrGeometryDraft,
    ): Boolean {
        val geometry = PhotoOcrGeometry(
            assetId = asset.assetId,
            sourceSha256 = asset.original.sha256,
            sourceRevision = asset.original.revision,
            displayRevision = asset.display.revision,
            coordinateSpace = draft.coordinateSpace,
            width = draft.width,
            height = draft.height,
            orientationDegrees = asset.display.orientationDegrees,
            engine = draft.engine,
            model = draft.model,
            engineVersion = draft.engineVersion,
            regions = draft.regions.asSequence()
                .filter(::validRegion)
                .take(MAX_OCR_REGIONS_PER_ASSET)
                .map { region ->
                    region.copy(
                        id = region.id.take(120),
                        regionType = region.regionType.take(80),
                        polygon = region.polygon.take(MAX_OCR_POLYGON_POINTS),
                        text = region.text.take(MAX_OCR_REGION_TEXT_CHARS),
                    )
                }
                .toList(),
        )
        if (geometry.regions.isEmpty()) return false
        val keep = asset.geometries.filterNot {
            it.displayRevision == geometry.displayRevision &&
                it.engine == geometry.engine && it.model == geometry.model &&
                it.coordinateSpace == geometry.coordinateSpace
        }
        if (asset.geometries.contains(geometry)) return true
        val next = asset.copy(geometries = keep + geometry)
        return persistCurrent(
            dir,
            current.copy(assets = current.assets.filterNot { it.assetId == asset.assetId } + next),
        )
    }

    fun applyBibliographicSuggestions(dir: File, metadata: JSONObject): Boolean =
        synchronized(monitor) {
            val current = readCurrent(dir) ?: return@synchronized false
            val evidence = current.orderedAssets().map { asset ->
                val text = File(dir, "${asset.captureFile}.txt")
                    .takeIf { it.isFile }?.readText().orEmpty()
                BibliographicPhotoEvidence(
                    asset.assetId,
                    asset.captureOrder,
                    matchedBibliographicFields(text, metadata),
                )
            }
            val byId = evidence.associateBy { it.assetId }
            val ranked = rankTitlePageEvidence(evidence)
            val titleWinner = ranked.firstOrNull()?.takeIf { it.score > 0 }?.assetId
            val updatedAssets = current.assets.map { asset ->
                val item = byId.getValue(asset.assetId)
                val shortSide = minOf(asset.display.width, asset.display.height)
                val longSide = maxOf(asset.display.width, asset.display.height)
                val spineLike = shortSide > 0 && longSide.toDouble() / shortSide >= 3.0
                val assignment = when {
                    item.assetId == titleWinner -> asset.role.copy(
                        suggestedRole = PhotoRole.TITLE_PAGE,
                        confidence = (0.35 + item.score * 0.18).coerceAtMost(0.89),
                        reason = "Matched ${item.score}/3 primary bibliographic fields: " +
                            item.matchedFields.joinToString(", "),
                        algorithm = TITLE_ROLE_ALGORITHM,
                        algorithmVersion = TITLE_ROLE_ALGORITHM_VERSION,
                    )
                    spineLike && item.score > 0 -> asset.role.copy(
                        suggestedRole = PhotoRole.SPINE,
                        confidence = 0.45,
                        reason = "Elongated page aspect and matched bibliographic text",
                        algorithm = SPINE_ROLE_ALGORITHM,
                        algorithmVersion = TITLE_ROLE_ALGORITHM_VERSION,
                    )
                    item.matchedFields.containsAll(listOf("title", "author")) -> asset.role.copy(
                        suggestedRole = PhotoRole.COVER,
                        confidence = 0.68,
                        reason = "Matched title and author outside the primary title-page candidate",
                        algorithm = COVER_ROLE_ALGORITHM,
                        algorithmVersion = TITLE_ROLE_ALGORITHM_VERSION,
                    )
                    item.score > 0 -> asset.role.copy(
                        suggestedRole = PhotoRole.TITLE_PAGE,
                        confidence = (0.25 + item.score * 0.15).coerceAtMost(0.55),
                        reason = "Possible title-page evidence: " +
                            item.matchedFields.joinToString(", "),
                        algorithm = TITLE_ROLE_ALGORITHM,
                        algorithmVersion = TITLE_ROLE_ALGORITHM_VERSION,
                    )
                    asset.role.algorithm in setOf(
                        "legacy-fallback", "android-capture", TITLE_ROLE_ALGORITHM,
                        COVER_ROLE_ALGORITHM, SPINE_ROLE_ALGORITHM,
                    ) ->
                        asset.role.copy(
                            suggestedRole = PhotoRole.OTHER,
                            confidence = 0.0,
                            reason = "No title, author, or year evidence matched",
                            algorithm = TITLE_ROLE_ALGORITHM,
                            algorithmVersion = TITLE_ROLE_ALGORITHM_VERSION,
                        )
                    else -> asset.role
                }
                asset.copy(role = assignment)
            }
            val oldChoice = current.selections.primaryTitle
            val automaticChoice = if (!oldChoice.manual && ranked.firstOrNull()?.score ?: 0 > 0) {
                val chosen = ranked.first().assetId
                if (oldChoice.assetId == chosen) oldChoice else oldChoice.copy(
                    assetId = chosen,
                    manual = false,
                    revision = oldChoice.revision + 1,
                    updatedAt = System.currentTimeMillis(),
                )
            } else oldChoice
            val updated = current.copy(
                assets = updatedAssets,
                selections = current.selections.copy(primaryTitle = automaticChoice),
            )
            if (updated == current) true else persistCurrent(dir, updated)
        }

    fun setManualRole(dir: File, assetId: String, role: PhotoRole?): Boolean =
        synchronized(monitor) {
            val current = readCurrent(dir) ?: return@synchronized false
            val asset = current.assets.firstOrNull { it.assetId == assetId } ?: return@synchronized false
            val next = asset.copy(role = asset.role.copy(
                manualOverride = role,
                manualRevision = asset.role.manualRevision + 1,
                manualUpdatedAt = System.currentTimeMillis(),
            ))
            persistCurrent(dir, current.copy(
                assets = current.assets.filterNot { it.assetId == assetId } + next,
            ))
        }

    fun selectPrimaryTitle(dir: File, assetId: String): Boolean =
        select(dir, assetId, primary = true)

    fun selectThumbnail(dir: File, assetId: String): Boolean =
        select(dir, assetId, primary = false)

    private fun select(dir: File, assetId: String, primary: Boolean): Boolean =
        synchronized(monitor) {
            val current = readCurrent(dir) ?: return@synchronized false
            if (current.assets.none { it.assetId == assetId }) return@synchronized false
            val old = if (primary) current.selections.primaryTitle else current.selections.thumbnail
            val choice = old.copy(
                assetId = assetId,
                manual = true,
                revision = old.revision + 1,
                updatedAt = System.currentTimeMillis(),
            )
            val selections = if (primary) current.selections.copy(primaryTitle = choice)
            else current.selections.copy(thumbnail = choice)
            persistCurrent(dir, current.copy(selections = selections))
        }

    /** Future download boundary: merge a returned contract without allowing a
     * stale checksum/revision to replace newer local state. */
    fun mergeIncoming(dir: File, incomingJson: JSONObject): Boolean = synchronized(monitor) {
        val incoming = capturePhotoAssetsFromJson(incomingJson, dir.name) ?: return@synchronized false
        val local = readCurrent(dir) ?: legacyContract(dir)
        val merged = reconcileContract(dir, mergePhotoAssetContracts(local, incoming))
        persistCurrent(dir, merged)
    }

    /** Mirror a live or terminal server job state without ever treating a
     * completed row as installed. Completion is written only by
     * [installCloudDisplayDerivative] after byte verification. */
    fun recordCloudJobState(dir: File, job: CloudPhotoProcessingJob): Boolean =
        synchronized(monitor) {
            val state = cloudLifecycleForRemoteState(job.state) ?: return@synchronized false
            val error = when (state) {
                PhotoAssetLifecycle.FAILED -> job.lastError.ifEmpty {
                    "Cloud image processing failed"
                }
                PhotoAssetLifecycle.CANCELLED -> job.lastError.ifEmpty {
                    "Cloud image processing cancelled"
                }
                PhotoAssetLifecycle.RETRYING -> job.lastError
                else -> ""
            }
            recordCloudLifecycle(dir, job, state, error)
        }

    /** A completed server job whose object fetch failed remains retryable. */
    fun recordCloudInstallRetry(
        dir: File,
        job: CloudPhotoProcessingJob,
        error: String = "Cloud display download will retry",
    ): Boolean = synchronized(monitor) {
        recordCloudLifecycle(dir, job, PhotoAssetLifecycle.RETRYING, error)
    }

    /** Persist a permanent schema/integrity failure so it cannot look pending
     * forever or silently fall back to an unverified derivative. */
    fun recordCloudResultFailure(
        dir: File,
        job: CloudPhotoProcessingJob,
        error: String,
    ): Boolean = synchronized(monitor) {
        recordCloudLifecycle(dir, job, PhotoAssetLifecycle.FAILED, error)
    }

    /** Avoid redownloading an immutable artifact on every later import poll,
     * while still repairing a missing or locally damaged display file. */
    fun hasVerifiedCloudDisplay(
        dir: File,
        plan: CloudDisplayInstallPlan,
    ): Boolean = synchronized(monitor) {
        val current = readCurrent(dir) ?: return@synchronized false
        val asset = current.assets.firstOrNull { it.assetId == plan.job.assetId }
            ?: return@synchronized false
        if (!cloudJobMatchesAsset(current.captureId, asset, plan.job) ||
            asset.lifecycle.state != PhotoAssetLifecycle.COMPLETED ||
            asset.lifecycle.jobId != plan.job.id ||
            asset.display.revision != plan.targetRevision ||
            asset.display.sha256 != plan.artifact.sha256) return@synchronized false
        val file = File(dir, asset.display.reference).takeIf { it.isFile }
            ?: return@synchronized false
        verifyCloudDisplayDownload(
            file,
            plan.artifact,
            plan.artifact.mime,
            file.length(),
        ) == null
    }

    /** Pending nonlinear derivatives only. A marker is created in the same
     * installation transaction as the new display contract, so scheduling can
     * be recovered by a later cloud poll without guessing from stale pixels. */
    fun pendingCloudDisplayReocrTargets(dir: File): List<CloudDisplayReocrTarget> =
        synchronized(monitor) {
            val current = readCurrent(dir) ?: return@synchronized emptyList()
            current.orderedAssets().mapNotNull { asset ->
                cloudDisplayReocrTarget(current, asset)?.takeIf { target ->
                    val marker = cloudDisplayReocrMarker(dir, target)
                    if (hasCurrentDisplayGeometry(asset)) {
                        marker.delete()
                        false
                    } else marker.isFile
                }
            }
        }

    /** Resolve and checksum the exact corrected pixels immediately before OCR. */
    fun cloudDisplayReocrFile(
        dir: File,
        target: CloudDisplayReocrTarget,
    ): File? = synchronized(monitor) {
        val current = readCurrent(dir) ?: return@synchronized null
        val asset = current.assets.firstOrNull { it.assetId == target.assetId }
            ?: return@synchronized null
        if (!cloudDisplayReocrTargetMatches(current, asset, target) ||
            hasCurrentDisplayGeometry(asset) ||
            !cloudDisplayReocrMarker(dir, target).isFile) return@synchronized null
        val file = File(dir, asset.display.reference).takeIf { it.isFile }
            ?: return@synchronized null
        file.takeIf { runCatching { sha256(it) }.getOrDefault("") == target.displaySha256 }
    }

    private fun recordCloudLifecycle(
        dir: File,
        job: CloudPhotoProcessingJob,
        state: PhotoAssetLifecycle,
        error: String,
    ): Boolean {
        val current = readCurrent(dir) ?: return false
        val asset = current.assets.firstOrNull { it.assetId == job.assetId } ?: return false
        if (!cloudJobMatchesAsset(current.captureId, asset, job)) return false
        val terminal = setOf(
            PhotoAssetLifecycle.COMPLETED,
            PhotoAssetLifecycle.FAILED,
            PhotoAssetLifecycle.CANCELLED,
        )
        // A stale live projection must not resurrect a terminal result for the
        // same immutable job.
        if (asset.lifecycle.jobId == job.id && asset.lifecycle.state in terminal &&
            state !in terminal) return true
        val cleanError = cleanCloudError(error)
        if (asset.lifecycle.jobId == job.id && asset.lifecycle.state == state &&
            asset.lifecycle.error == cleanError) return true
        val next = asset.copy(lifecycle = PhotoLifecycleState(
            state = state,
            jobId = job.id,
            error = cleanError,
            updatedAt = System.currentTimeMillis(),
        ))
        return persistCurrent(
            dir,
            current.copy(assets = current.assets.filterNot { it.assetId == asset.assetId } + next),
        )
    }

    /**
     * Atomically promote a verified private derivative into a new local
     * display revision. The destination is a separate cloud_* file; neither
     * the immutable original nor the established photo_N transport is ever
     * overwritten. A nonlinear result receives no carried-forward OCR boxes.
     */
    fun installCloudDisplayDerivative(
        dir: File,
        proposed: CloudDisplayInstallPlan,
        downloaded: File,
        receipt: PrivateObjectDownload,
    ): Boolean = synchronized(monitor) {
        if (!dir.isDirectory || downloaded.parentFile != dir) return@synchronized false
        val current = readCurrent(dir) ?: return@synchronized false
        val checked = when (val decision = validateCloudPhotoResult(
            current,
            proposed.job,
            proposed.job.ownerId,
        )) {
            is CloudResultDecision.Ready -> decision.plan
            else -> return@synchronized false
        }
        if (checked.artifact != proposed.artifact ||
            checked.targetRevision != proposed.targetRevision ||
            checked.baseToOutputHomography != proposed.baseToOutputHomography ||
            checked.reocrRequired != proposed.reocrRequired) {
            return@synchronized false
        }
        if (verifyCloudDisplayDownload(
                downloaded,
                checked.artifact,
                receipt.contentType,
                receipt.bytes,
            ) != null) return@synchronized false

        val asset = current.assets.firstOrNull { it.assetId == checked.job.assetId }
            ?: return@synchronized false
        val atMergeBase = asset.display.revision == checked.mergeBaseRevision &&
            asset.display.sha256 == checked.mergeBaseSha256
        val repairingTarget = asset.display.revision == checked.targetRevision &&
            asset.display.sha256 == checked.artifact.sha256
        if (!atMergeBase && !repairingTarget) return@synchronized false

        val destination = File(dir, cloudDisplayFileName(checked))
        if (destination.name == asset.original.reference ||
            destination.name == asset.captureFile) return@synchronized false
        val destinationExisted = destination.isFile
        val destinationAlreadyValid = destinationExisted &&
            verifyCloudDisplayDownload(
                destination,
                checked.artifact,
                checked.artifact.mime,
                destination.length(),
            ) == null
        var movedDownload = false
        if (destinationAlreadyValid) {
            downloaded.delete()
        } else {
            try {
                try {
                    Files.move(
                        downloaded.toPath(),
                        destination.toPath(),
                        StandardCopyOption.ATOMIC_MOVE,
                        StandardCopyOption.REPLACE_EXISTING,
                    )
                } catch (_: Exception) {
                    Files.move(
                        downloaded.toPath(),
                        destination.toPath(),
                        StandardCopyOption.REPLACE_EXISTING,
                    )
                }
                movedDownload = true
            } catch (_: Exception) {
                return@synchronized false
            }
        }

        val display = PhotoDisplayDerivative(
            reference = destination.name,
            sha256 = checked.artifact.sha256,
            revision = checked.targetRevision,
            width = checked.artifact.width,
            height = checked.artifact.height,
            orientationDegrees = 0,
            recipe = checked.recipe,
            recipeVersion = checked.recipeVersion,
            sourceToDisplayHomography = checked.baseToOutputHomography,
        )
        val geometries = when {
            atMergeBase && checked.baseToOutputHomography != null -> {
                val migrated = asset.geometries.mapNotNull { geometry ->
                    transformGeometryForDisplay(geometry, asset.original, asset.display, display)
                }
                (asset.geometries.filterNot { it.displayRevision == display.revision } + migrated)
                    .distinctBy(::geometryKey)
            }
            checked.baseToOutputHomography == null ->
                asset.geometries.filterNot { it.displayRevision == display.revision }
            else -> asset.geometries
        }
        val next = asset.copy(
            display = display,
            lifecycle = PhotoLifecycleState(
                state = PhotoAssetLifecycle.COMPLETED,
                jobId = checked.job.id,
                error = "",
                updatedAt = System.currentTimeMillis(),
            ),
            geometries = geometries,
        )
        val reocrTarget = if (checked.reocrRequired) CloudDisplayReocrTarget(
            captureId = current.captureId,
            assetId = asset.assetId,
            jobId = checked.job.id,
            displayReference = display.reference,
            displaySha256 = display.sha256,
            displayRevision = display.revision,
        ) else null
        val reocrMarker = reocrTarget?.let { cloudDisplayReocrMarker(dir, it) }
        val markerExisted = reocrMarker?.isFile == true
        if (reocrMarker != null && !markerExisted) {
            try {
                Entries.atomicWrite(reocrMarker, "pending\n")
            } catch (_: Exception) {
                if (movedDownload && !destinationExisted &&
                    asset.display.reference != destination.name) destination.delete()
                return@synchronized false
            }
        }
        val saved = persistCurrent(
            dir,
            current.copy(assets = current.assets.filterNot { it.assetId == asset.assetId } + next),
        )
        if (!saved && !markerExisted) reocrMarker?.delete()
        if (!saved && movedDownload && !destinationExisted &&
            asset.display.reference != destination.name) destination.delete()
        saved
    }

    fun payload(dir: File, manifest: JSONObject? = null): JSONObject = synchronized(monitor) {
        val current = readCurrent(dir, manifest) ?: legacyContract(dir)
        current.toJson()
    }

    private fun readCurrent(dir: File, manifest: JSONObject? = null): CapturePhotoAssets? {
        val sidecar = File(dir, PHOTO_ASSETS_FILE)
        if (sidecar.isFile) {
            runCatching { JSONObject(sidecar.readText()) }.getOrNull()
                ?.let { capturePhotoAssetsFromJson(it, dir.name) }
                ?.let { return it }
        }
        val mf = manifest ?: File(dir, "manifest.json").takeIf { it.isFile }?.let {
            runCatching { JSONObject(it.readText()) }.getOrNull()
        }
        return mf?.optJSONObject(PHOTO_ASSETS_MANIFEST_KEY)
            ?.let { capturePhotoAssetsFromJson(it, dir.name) }
    }

    private fun persistCurrent(dir: File, contract: CapturePhotoAssets): Boolean {
        val sidecar = File(dir, PHOTO_ASSETS_FILE)
        val manifestFile = File(dir, "manifest.json")
        // Never overwrite an unknown future schema or corrupt evidence.
        if (sidecar.isFile) {
            val parsed = runCatching { JSONObject(sidecar.readText()) }.getOrNull()
                ?.let { capturePhotoAssetsFromJson(it, dir.name) }
            if (parsed == null) return false
        }
        if (manifestFile.isFile) {
            val manifest = runCatching { JSONObject(manifestFile.readText()) }.getOrNull()
                ?: return false
            if (manifest.has(PHOTO_ASSETS_MANIFEST_KEY)) {
                val embedded = manifest.optJSONObject(PHOTO_ASSETS_MANIFEST_KEY)
                    ?.let { capturePhotoAssetsFromJson(it, dir.name) }
                if (embedded == null) return false
            }
        }
        return try {
            Entries.atomicWrite(sidecar, contract.toJson().toString())
            if (manifestFile.isFile) {
                val manifest = JSONObject(manifestFile.readText())
                    .put(PHOTO_ASSETS_MANIFEST_KEY, contract.toJson())
                Entries.atomicWrite(manifestFile, manifest.toString())
            }
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun descriptor(dir: File, asset: CapturePhotoAsset): EntryPhotoDescriptor? {
        val capture = File(dir, asset.captureFile).takeIf { it.isFile }
        val raw = File(dir, asset.original.reference).takeIf { it.isFile }
        val declaredDisplay = File(dir, asset.display.reference).takeIf { it.isFile }
        val display = declaredDisplay ?: capture ?: raw ?: return null
        // Geometry is revision-bound to the declared display derivative. If that
        // derivative is unavailable (for example, during a partial correction
        // download), the fallback capture/raw pixels are not in the same
        // coordinate space and must never inherit its overlays.
        val geometry = if (declaredDisplay == null) emptyList() else asset.geometries
            .filter {
                it.assetId == asset.assetId &&
                    it.coordinateSpace == "display_normalized" &&
                    it.displayRevision == asset.display.revision &&
                    it.sourceRevision == asset.original.revision &&
                    (asset.original.sha256.isEmpty() ||
                        it.sourceSha256 == asset.original.sha256) &&
                    (asset.display.width == 0 || it.width == 0 ||
                        it.width == asset.display.width) &&
                    (asset.display.height == 0 || it.height == 0 ||
                        it.height == asset.display.height) &&
                    it.orientationDegrees == asset.display.orientationDegrees
            }
            .sortedWith(compareBy<PhotoOcrGeometry> { it.engine }.thenBy { it.model })
            .flatMap { record ->
                record.regions.map { region ->
                    EntryPhotoGeometryRegion(
                        normalizedPolygon = region.polygon,
                        label = region.regionType,
                        text = region.text,
                        confidence = region.confidence,
                    )
                }
            }
        return EntryPhotoDescriptor(
            assetId = asset.assetId,
            order = (asset.captureOrder - 1).coerceAtLeast(0),
            captureFile = capture ?: display,
            rawFile = raw ?: capture ?: display,
            displayFile = display,
            role = asset.role.effectiveRole,
            roleSuggested = asset.role.isSuggested,
            confidence = asset.role.confidence,
            reason = asset.role.reason,
            algorithm = asset.role.algorithm,
            algorithmVersion = asset.role.algorithmVersion,
            lifecycle = asset.lifecycle,
            displayRevision = asset.display.revision,
            geometry = geometry,
            postProcessingPending = photoPostProcessingPending(asset),
        )
    }
}

private fun cloudDisplayReocrTarget(
    contract: CapturePhotoAssets,
    asset: CapturePhotoAsset,
): CloudDisplayReocrTarget? {
    val request = asset.processingRequest ?: return null
    if (asset.lifecycle.state != PhotoAssetLifecycle.COMPLETED ||
        asset.lifecycle.jobId.isBlank() ||
        asset.display.recipe != "whl-cloud-book-cleanup" ||
        asset.display.sourceToDisplayHomography != null ||
        asset.display.sha256.isEmpty() ||
        request.sourceAssetId != asset.assetId ||
        request.sourceOriginalSha256 != asset.original.sha256 ||
        request.sourceOriginalRevision != asset.original.revision ||
        request.sourceDisplayRevision + 1 != asset.display.revision ||
        request.operations.none { it.outcome == PhotoProcessingOutcome.PAGE_DEWARP }) return null
    return CloudDisplayReocrTarget(
        captureId = contract.captureId,
        assetId = asset.assetId,
        jobId = asset.lifecycle.jobId,
        displayReference = asset.display.reference,
        displaySha256 = asset.display.sha256,
        displayRevision = asset.display.revision,
    )
}

private fun cloudDisplayReocrTargetMatches(
    contract: CapturePhotoAssets,
    asset: CapturePhotoAsset,
    target: CloudDisplayReocrTarget,
): Boolean = cloudDisplayReocrTarget(contract, asset) == target

private fun cloudDisplayReocrMarker(dir: File, target: CloudDisplayReocrTarget): File =
    File(
        dir,
        ".cloud-reocr-${target.assetId}-r${target.displayRevision}-" +
            "${target.displaySha256.take(20)}.pending",
    )

private fun hasCurrentDisplayGeometry(asset: CapturePhotoAsset): Boolean =
    asset.geometries.any { geometry ->
        geometry.assetId == asset.assetId &&
            geometry.coordinateSpace == "display_normalized" &&
            geometry.displayRevision == asset.display.revision &&
            geometry.sourceRevision == asset.original.revision &&
            (asset.original.sha256.isEmpty() ||
                geometry.sourceSha256 == asset.original.sha256) &&
            (asset.display.width == 0 || geometry.width == asset.display.width) &&
            (asset.display.height == 0 || geometry.height == asset.display.height) &&
            geometry.orientationDegrees == asset.display.orientationDegrees &&
            geometry.regions.any(::validRegion)
    }

internal fun rankTitlePageEvidence(
    evidence: List<BibliographicPhotoEvidence>,
): List<BibliographicPhotoEvidence> = evidence.sortedWith(
    compareByDescending<BibliographicPhotoEvidence> { it.score }
        .thenBy { it.captureOrder }
        .thenBy { it.assetId },
)

internal fun matchedBibliographicFields(ocrText: String, metadata: JSONObject): List<String> {
    val haystack = normalizeEvidence(ocrText)
    if (haystack.isEmpty()) return emptyList()
    return listOf("title", "author", "year").filter { field ->
        val needle = normalizeEvidence(metadata.optString(field))
        needle.isNotEmpty() && haystack.contains(needle)
    }
}

/**
 * A request is pending only while it still names the pixels currently shown.
 * Once a newer display derivative lands, the v1 request has reached its only
 * locally observable terminal condition even though no fake service result is
 * stored in the request contract.
 */
internal fun photoPostProcessingPending(asset: CapturePhotoAsset): Boolean {
    val request = asset.processingRequest ?: return false
    return asset.lifecycle.state !in setOf(
        PhotoAssetLifecycle.FAILED,
        PhotoAssetLifecycle.CANCELLED,
    ) && request.operations.isNotEmpty() &&
        request.sourceAssetId == asset.assetId &&
        request.sourceOriginalRevision == asset.original.revision &&
        request.sourceOriginalSha256 == asset.original.sha256 &&
        request.sourceDisplayRevision == asset.display.revision &&
        request.sourceDisplaySha256 == asset.display.sha256
}

/**
 * Carry OCR polygons onto a proved corrected derivative. The transform is
 * applied in normalized display space and clipped to the corrected image, so
 * a crop cannot leave boxes floating outside the visible pixels.
 */
internal fun transformGeometryForDisplay(
    geometry: PhotoOcrGeometry,
    original: PhotoOriginal,
    sourceDisplay: PhotoDisplayDerivative,
    targetDisplay: PhotoDisplayDerivative,
): PhotoOcrGeometry? {
    val homography = validHomography(targetDisplay.sourceToDisplayHomography) ?: return null
    if (targetDisplay.revision <= sourceDisplay.revision ||
        geometry.coordinateSpace != "display_normalized" ||
        geometry.sourceRevision != original.revision ||
        original.sha256.isNotEmpty() && geometry.sourceSha256 != original.sha256 ||
        geometry.displayRevision != sourceDisplay.revision ||
        sourceDisplay.width > 0 && geometry.width > 0 && geometry.width != sourceDisplay.width ||
        sourceDisplay.height > 0 && geometry.height > 0 && geometry.height != sourceDisplay.height ||
        geometry.orientationDegrees != sourceDisplay.orientationDegrees) return null

    val transformed = geometry.regions.mapNotNull { region ->
        val polygon = transformAndClipPolygon(region.polygon, homography)
        region.copy(polygon = polygon).takeIf(::validRegion)
    }
    if (transformed.isEmpty()) return null
    return geometry.copy(
        displayRevision = targetDisplay.revision,
        width = targetDisplay.width,
        height = targetDisplay.height,
        orientationDegrees = targetDisplay.orientationDegrees,
        regions = transformed,
    )
}

internal fun mergePhotoAssetContracts(
    local: CapturePhotoAssets,
    incoming: CapturePhotoAssets,
): CapturePhotoAssets {
    if (local.captureId != incoming.captureId) return local
    val incomingById = incoming.assets.associateBy { it.assetId }
    val merged = local.assets.map { existing ->
        val candidate = incomingById[existing.assetId] ?: return@map existing
        if (existing.original.sha256.isNotEmpty() && candidate.original.sha256.isNotEmpty() &&
            existing.original.sha256 != candidate.original.sha256) return@map existing

        val acceptDisplay = when {
            candidate.display.revision > existing.display.revision &&
                candidate.display.sha256.isNotEmpty() -> true
            candidate.display.revision == existing.display.revision &&
                candidate.display.sha256.isNotEmpty() &&
                (existing.display.sha256.isEmpty() ||
                    candidate.display.sha256 == existing.display.sha256) -> true
            else -> false
        }
        val display = if (acceptDisplay) candidate.display else existing.display
        val lifecycle = if (acceptDisplay && candidate.lifecycle.updatedAt >= existing.lifecycle.updatedAt)
            candidate.lifecycle else existing.lifecycle
        val manual = when {
            candidate.role.manualRevision > existing.role.manualRevision -> candidate.role
            else -> existing.role
        }
        val suggestion = if (acceptDisplay) candidate.role else existing.role
        val role = suggestion.copy(
            manualOverride = manual.manualOverride,
            manualRevision = manual.manualRevision,
            manualUpdatedAt = manual.manualUpdatedAt,
        )
        val original = if (existing.original.sha256.isEmpty()) candidate.original else existing.original
        val processingRequest = mergeProcessingRequest(
            existing.processingRequest,
            candidate.processingRequest,
            existing.assetId,
            original,
            display,
        )
        val migratedGeometry = if (acceptDisplay &&
            display.revision > existing.display.revision) {
            existing.geometries.mapNotNull { geometry ->
                transformGeometryForDisplay(geometry, original, existing.display, display)
            }
        } else emptyList()
        val acceptedIncomingGeometry = if (acceptDisplay) candidate.geometries.filter { geometry ->
            geometry.assetId == existing.assetId &&
                geometry.displayRevision == display.revision &&
                geometry.sourceRevision == original.revision &&
                (original.sha256.isEmpty() || geometry.sourceSha256 == original.sha256) &&
                geometry.coordinateSpace == "display_normalized" &&
                geometry.orientationDegrees == display.orientationDegrees &&
                (display.width == 0 || geometry.width == display.width) &&
                (display.height == 0 || geometry.height == display.height) &&
                geometry.regions.any(::validRegion)
        } else emptyList()
        val incomingGeometryKeys = acceptedIncomingGeometry.map(::geometryKey).toSet()
        val geometry = ((existing.geometries + migratedGeometry).filterNot {
            geometryKey(it) in incomingGeometryKeys
        } + acceptedIncomingGeometry)
            .filter { it.assetId == existing.assetId }
            .distinctBy(::geometryKey)
        existing.copy(
            original = original,
            display = display,
            lifecycle = lifecycle,
            role = role,
            geometries = geometry,
            processingRequest = processingRequest,
        )
    }
    return local.copy(
        assets = merged,
        selections = CapturePhotoSelections(
            primaryTitle = mergeChoice(local.selections.primaryTitle, incoming.selections.primaryTitle),
            thumbnail = mergeChoice(local.selections.thumbnail, incoming.selections.thumbnail),
        ),
        legacyFallback = local.legacyFallback && incoming.legacyFallback,
    )
}

private fun mergeProcessingRequest(
    local: PhotoProcessingRequest?,
    incoming: PhotoProcessingRequest?,
    assetId: String,
    original: PhotoOriginal,
    display: PhotoDisplayDerivative,
): PhotoProcessingRequest? {
    if (incoming == null ||
        !validProcessingRequestForAsset(incoming, assetId, original, display)) return local
    if (local == null) return incoming
    return when {
        incoming.requestRevision > local.requestRevision -> incoming
        incoming.requestRevision == local.requestRevision && incoming == local -> local
        else -> local
    }
}

private fun mergeChoice(local: PhotoSelectionChoice, incoming: PhotoSelectionChoice): PhotoSelectionChoice =
    when {
        local.manual && !incoming.manual -> local
        incoming.manual && !local.manual -> incoming
        incoming.revision > local.revision -> incoming
        else -> local
    }

internal fun CapturePhotoAssets.toJson(): JSONObject = JSONObject()
    .put("schema", PHOTO_ASSETS_SCHEMA)
    .put("version", PHOTO_ASSETS_VERSION)
    .put("capture_id", captureId)
    .put("legacy_fallback", legacyFallback)
    .put("assets", JSONArray().apply { orderedAssets().forEach { put(it.toJson()) } })
    .put("selections", JSONObject()
        .put("primary_title", selections.primaryTitle.toJson())
        .put("thumbnail", selections.thumbnail.toJson()))

internal fun capturePhotoAssetsFromJson(
    value: JSONObject,
    expectedCaptureId: String? = null,
): CapturePhotoAssets? {
    return try {
        if (value.optString("schema") != PHOTO_ASSETS_SCHEMA) return null
        val version = value.opt("version") as? Number ?: return null
        if (version.toInt() != PHOTO_ASSETS_VERSION || version.toDouble() != version.toInt().toDouble()) return null
        val captureId = value.getString("capture_id").trim()
        if (!safeToken(captureId) || expectedCaptureId != null && captureId != expectedCaptureId) return null
        val array = value.getJSONArray("assets")
        val assets = (0 until array.length()).mapNotNull { array.optJSONObject(it)?.toPhotoAsset() }
        // An asset is the unit of identity and lineage. Silently dropping a
        // malformed entry would make a damaged remote payload look like a
        // valid partial manifest and could detach its immutable original.
        if (assets.size != array.length() ||
            assets.map { it.assetId }.distinct().size != assets.size ||
            assets.map { it.captureFile }.distinct().size != assets.size) return null
        val selections = value.optJSONObject("selections")
        CapturePhotoAssets(
            captureId,
            assets,
            CapturePhotoSelections(
                selections?.optJSONObject("primary_title")?.toSelection() ?: PhotoSelectionChoice(),
                selections?.optJSONObject("thumbnail")?.toSelection() ?: PhotoSelectionChoice(),
            ),
            value.optBoolean("legacy_fallback", false),
        )
    } catch (_: Exception) {
        null
    }
}

private fun CapturePhotoAsset.toJson(): JSONObject = JSONObject()
    .put("asset_id", assetId)
    .put("capture_order", captureOrder)
    .put("capture_file", captureFile)
    .put("original", original.toJson())
    .put("display", display.toJson())
    .put("lifecycle", lifecycle.toJson())
    .put("role", role.toJson())
    .put("geometry", JSONArray().apply { geometries.forEach { put(it.toJson()) } })
    .put("processing_request", processingRequest?.toJson() ?: JSONObject.NULL)

private fun JSONObject.toPhotoAsset(): CapturePhotoAsset? {
    return try {
        val id = getString("asset_id").trim()
        val file = getString("capture_file").trim()
        val order = getInt("capture_order")
        if (!safeToken(id) || !safeReference(file) || order <= 0) return null
        val original = getJSONObject("original").toOriginal() ?: return null
        val display = getJSONObject("display").toDisplay() ?: return null
        val requestValue = opt("processing_request")
        val processingRequest = when {
            !has("processing_request") || requestValue == null || requestValue == JSONObject.NULL -> null
            requestValue is JSONObject ->
                requestValue.toProcessingRequest(id, original, display) ?: return null
            else -> return null
        }
        CapturePhotoAsset(
            id,
            order,
            file,
            original,
            display,
            optJSONObject("lifecycle")?.toLifecycle() ?: PhotoLifecycleState(),
            optJSONObject("role")?.toRole() ?: PhotoRoleAssignment(),
            optJSONArray("geometry")?.let { array ->
                (0 until array.length()).mapNotNull { array.optJSONObject(it)?.toGeometry() }
            }.orEmpty(),
            processingRequest,
        )
    } catch (_: Exception) {
        null
    }
}

private fun PhotoProcessingRequest.toJson(): JSONObject = JSONObject()
    .put("schema", PHOTO_PROCESSING_REQUEST_SCHEMA)
    .put("version", PHOTO_PROCESSING_REQUEST_VERSION)
    .put("request_id", requestId)
    .put("request_revision", requestRevision)
    .put("profile", profile.toJson())
    .put("requested_at", requestedAt)
    .put(
        "status",
        if (operations.isEmpty()) PHOTO_PROCESSING_DISABLED else PHOTO_PROCESSING_REQUESTED,
    )
    .put("source", JSONObject()
        .put("asset_id", sourceAssetId)
        .put("role", sourceRole.wireValue)
        .put("original_sha256", sourceOriginalSha256)
        .put("original_revision", sourceOriginalRevision)
        .put("display_sha256", sourceDisplaySha256)
        .put("display_revision", sourceDisplayRevision))
    .put("operations", JSONArray().apply { operations.forEach { put(it.toJson()) } })
    // This app can request a recipe but has no service integration that may
    // truthfully populate a result. A non-null result requires a future schema.
    .put("result", JSONObject.NULL)

private fun PhotoProcessingOperation.toJson(): JSONObject = JSONObject()
    .put("outcome", outcome.wireValue)
    .put("result_role", resultRole?.wireValue ?: JSONObject.NULL)

private fun PostProcessingProfile.toJson(): JSONObject = JSONObject()
    .put("version", contractVersion)
    .put("selected_preset", selectedPreset.storedValue)
    .put("resolved_treatment", resolvedTreatment.contractValue)
    .put("publication_year", publicationYear ?: JSONObject.NULL)
    .put("features", JSONObject()
        .put("page_dewarp", features.dewarpPerspectiveAndPageCurvature)
        .put("detected_margin_crop", features.cropToDetectedPageMargins)
        .put("contrast_normalization", features.normalizePageAndTextContrast)
        .put("spine_crop", features.detectAndCropSpine))
    .put("page_dewarp_strength_percent", pageDewarpStrengthPercent)
    .put("detected_margin_padding_percent", detectedMarginPaddingPercent)
    .put("contrast_strength_percent", contrastStrengthPercent)
    .put("paper_tone_retention_percent", paperToneRetentionPercent)

private fun JSONObject.toProcessingRequest(
    expectedAssetId: String,
    original: PhotoOriginal,
    display: PhotoDisplayDerivative,
): PhotoProcessingRequest? {
    val expectedKeys = setOf(
        "schema", "version", "request_id", "request_revision", "profile",
        "requested_at", "status", "source", "operations", "result",
    )
    if (keys().asSequence().toSet() != expectedKeys ||
        opt("schema") !is String || optString("schema") != PHOTO_PROCESSING_REQUEST_SCHEMA ||
        strictInt("version") != PHOTO_PROCESSING_REQUEST_VERSION ||
        opt("status") !is String ||
        !has("result") || opt("result") != JSONObject.NULL) return null
    val status = optString("status")

    val requestId = (opt("request_id") as? String)?.trim().orEmpty()
    val requestRevision = strictInt("request_revision") ?: return null
    val requestedAt = strictLong("requested_at") ?: return null
    val profile = optJSONObject("profile")?.toPostProcessingProfile() ?: return null
    if (!safeToken(requestId) || requestRevision < 1 || requestedAt <= 0L) return null

    val source = optJSONObject("source") ?: return null
    val expectedSourceKeys = setOf(
        "asset_id", "role", "original_sha256", "original_revision",
        "display_sha256", "display_revision",
    )
    if (source.keys().asSequence().toSet() != expectedSourceKeys) return null
    val sourceAssetId = (source.opt("asset_id") as? String)?.trim().orEmpty()
    val sourceRoleValue = source.opt("role") as? String ?: return null
    val sourceRole = PhotoRole.fromWire(sourceRoleValue) ?: return null
    val sourceOriginalHash = (source.opt("original_sha256") as? String)
        ?.trim()?.lowercase().orEmpty()
    val sourceDisplayHash = (source.opt("display_sha256") as? String)
        ?.trim()?.lowercase().orEmpty()
    val sourceOriginalRevision = source.strictInt("original_revision") ?: return null
    val sourceDisplayRevision = source.strictInt("display_revision") ?: return null

    val array = optJSONArray("operations") ?: return null
    if (array.length() !in 0..PhotoProcessingOutcome.values().size) return null
    val operations = (0 until array.length()).mapNotNull { index ->
        array.optJSONObject(index)?.toProcessingOperation()
    }
    if (operations.size != array.length() || !validProcessingOperations(operations)) return null
    val expectedStatus = if (operations.isEmpty()) {
        PHOTO_PROCESSING_DISABLED
    } else {
        PHOTO_PROCESSING_REQUESTED
    }
    if (status != expectedStatus) return null

    val request = PhotoProcessingRequest(
        requestId = requestId,
        requestRevision = requestRevision,
        profile = profile,
        requestedAt = requestedAt,
        sourceAssetId = sourceAssetId,
        sourceRole = sourceRole,
        sourceOriginalSha256 = sourceOriginalHash,
        sourceOriginalRevision = sourceOriginalRevision,
        sourceDisplaySha256 = sourceDisplayHash,
        sourceDisplayRevision = sourceDisplayRevision,
        operations = operations,
    )
    return request.takeIf {
        validProcessingRequestForAsset(it, expectedAssetId, original, display)
    }
}

private fun JSONObject.toPostProcessingProfile(): PostProcessingProfile? {
    val expectedKeys = setOf(
        "version", "selected_preset", "resolved_treatment", "publication_year", "features",
        "page_dewarp_strength_percent", "detected_margin_padding_percent",
        "contrast_strength_percent", "paper_tone_retention_percent",
    )
    if (keys().asSequence().toSet() != expectedKeys ||
        strictInt("version") != PostProcessingProfile.CONTRACT_VERSION) return null
    val selectedValue = opt("selected_preset") as? String ?: return null
    val selected = PostProcessingPreset.entries
        .firstOrNull { it.storedValue == selectedValue } ?: return null
    val treatmentValue = opt("resolved_treatment") as? String ?: return null
    val treatment = PostProcessingTreatment.entries
        .firstOrNull { it.contractValue == treatmentValue } ?: return null
    val yearValue = opt("publication_year")
    val publicationYear = when {
        yearValue == JSONObject.NULL -> null
        yearValue is Number -> strictInt("publication_year")?.takeIf { it in 1..9999 }
            ?: return null
        else -> return null
    }
    val featuresJson = optJSONObject("features") ?: return null
    if (featuresJson.keys().asSequence().toSet() != setOf(
            "page_dewarp", "detected_margin_crop", "contrast_normalization", "spine_crop",
        )) return null
    val features = PostProcessingFeatures(
        dewarpPerspectiveAndPageCurvature =
            featuresJson.opt("page_dewarp") as? Boolean ?: return null,
        cropToDetectedPageMargins =
            featuresJson.opt("detected_margin_crop") as? Boolean ?: return null,
        normalizePageAndTextContrast =
            featuresJson.opt("contrast_normalization") as? Boolean ?: return null,
        detectAndCropSpine = featuresJson.opt("spine_crop") as? Boolean ?: return null,
    )
    val parsed = PostProcessingProfile(
        contractVersion = PostProcessingProfile.CONTRACT_VERSION,
        selectedPreset = selected,
        resolvedTreatment = treatment,
        publicationYear = publicationYear,
        features = features,
        pageDewarpStrengthPercent = strictInt("page_dewarp_strength_percent") ?: return null,
        detectedMarginPaddingPercent = strictInt("detected_margin_padding_percent") ?: return null,
        contrastStrengthPercent = strictInt("contrast_strength_percent") ?: return null,
        paperToneRetentionPercent = strictInt("paper_tone_retention_percent") ?: return null,
    )
    return parsed.takeIf(::validPostProcessingProfile)
}

private fun JSONObject.toProcessingOperation(): PhotoProcessingOperation? {
    if (keys().asSequence().toSet() != setOf("outcome", "result_role")) return null
    val outcomeValue = opt("outcome") as? String ?: return null
    val outcome = PhotoProcessingOutcome.fromWire(outcomeValue) ?: return null
    val roleValue = opt("result_role")
    val resultRole = when {
        roleValue == JSONObject.NULL -> null
        roleValue is String -> PhotoRole.fromWire(roleValue) ?: return null
        else -> return null
    }
    return PhotoProcessingOperation(outcome, resultRole)
}

private fun PhotoOriginal.toJson(): JSONObject = JSONObject()
    .put("reference", reference).put("sha256", sha256).put("revision", revision)
    .put("width", width).put("height", height).put("orientation", orientationDegrees)

private fun JSONObject.toOriginal(): PhotoOriginal? {
    val ref = optString("reference").trim()
    val hash = optString("sha256").trim().lowercase()
    val revision = optInt("revision", 1)
    val orientation = normalizedOrientation(optInt("orientation", 0)) ?: return null
    if (!safeReference(ref) || hash.isNotEmpty() && !hash.matches(SHA256_HEX) || revision < 1) return null
    return PhotoOriginal(ref, hash, revision, optInt("width", 0).coerceAtLeast(0),
        optInt("height", 0).coerceAtLeast(0), orientation)
}

private fun PhotoDisplayDerivative.toJson(): JSONObject = JSONObject()
    .put("reference", reference).put("sha256", sha256).put("revision", revision)
    .put("width", width).put("height", height).put("orientation", orientationDegrees)
    .put("recipe", recipe).put("recipe_version", recipeVersion)
    .apply {
        sourceToDisplayHomography?.let { values ->
            put("source_to_display_homography", JSONArray().apply { values.forEach(::put) })
        }
    }

private fun JSONObject.toDisplay(): PhotoDisplayDerivative? {
    val ref = optString("reference").trim()
    val hash = optString("sha256").trim().lowercase()
    val revision = optInt("revision", 1)
    val orientation = normalizedOrientation(optInt("orientation", 0)) ?: return null
    if (!safeReference(ref) || hash.isNotEmpty() && !hash.matches(SHA256_HEX) || revision < 1) return null
    val h = optJSONArray("source_to_display_homography")?.let { array ->
        validHomography((0 until array.length()).map { array.optDouble(it, Double.NaN) })
    }
    return PhotoDisplayDerivative(
        ref, hash, revision, optInt("width", 0).coerceAtLeast(0),
        optInt("height", 0).coerceAtLeast(0), orientation,
        optString("recipe", "camera-original").take(120),
        optString("recipe_version", "1").take(80), h,
    )
}

private fun PhotoLifecycleState.toJson(): JSONObject = JSONObject()
    .put("state", state.wireValue).put("job_id", jobId).put("error", error)
    .put("updated_at", updatedAt)

private fun JSONObject.toLifecycle(): PhotoLifecycleState = PhotoLifecycleState(
    PhotoAssetLifecycle.fromWire(optString("state")) ?: PhotoAssetLifecycle.CAPTURED,
    optString("job_id").take(160), optString("error").take(500),
    optLong("updated_at", 0L).coerceAtLeast(0L),
)

private fun PhotoRoleAssignment.toJson(): JSONObject = JSONObject()
    .put("suggested", suggestedRole.wireValue).put("confidence", confidence)
    .put("reason", reason).put("algorithm", algorithm)
    .put("algorithm_version", algorithmVersion)
    .put("manual_override", manualOverride?.wireValue ?: JSONObject.NULL)
    .put("manual_revision", manualRevision).put("manual_updated_at", manualUpdatedAt)

private fun JSONObject.toRole(): PhotoRoleAssignment = PhotoRoleAssignment(
    PhotoRole.fromWire(optString("suggested")) ?: PhotoRole.OTHER,
    finiteDouble("confidence")?.coerceIn(0.0, 1.0) ?: 0.0,
    optString("reason").take(500), optString("algorithm", "legacy-fallback").take(120),
    optString("algorithm_version", "1").take(80),
    opt("manual_override").takeUnless { it == null || it == JSONObject.NULL }
        ?.toString()?.let(PhotoRole::fromWire),
    optInt("manual_revision", 0).coerceAtLeast(0),
    optLong("manual_updated_at", 0L).coerceAtLeast(0L),
)

private fun PhotoSelectionChoice.toJson(): JSONObject = JSONObject()
    .put("asset_id", assetId ?: JSONObject.NULL).put("manual", manual)
    .put("revision", revision).put("updated_at", updatedAt)

private fun JSONObject.toSelection(): PhotoSelectionChoice {
    val id = opt("asset_id").takeUnless { it == null || it == JSONObject.NULL }
        ?.toString()?.trim()?.takeIf(::safeToken)
    return PhotoSelectionChoice(id, optBoolean("manual", false),
        optInt("revision", 0).coerceAtLeast(0), optLong("updated_at", 0L).coerceAtLeast(0L))
}

private fun PhotoOcrGeometry.toJson(): JSONObject = JSONObject()
    .put("asset_id", assetId).put("source_sha256", sourceSha256)
    .put("source_revision", sourceRevision).put("display_revision", displayRevision)
    .put("coordinate_space", coordinateSpace).put("width", width).put("height", height)
    .put("orientation", orientationDegrees).put("engine", engine).put("model", model)
    .put("engine_version", engineVersion)
    .put("regions", JSONArray().apply { regions.forEach { put(it.toJson()) } })

private fun JSONObject.toGeometry(): PhotoOcrGeometry? {
    return try {
        val assetId = getString("asset_id").trim()
        val sourceHash = optString("source_sha256").trim().lowercase()
        val sourceRevision = getInt("source_revision")
        val displayRevision = getInt("display_revision")
        val orientation = normalizedOrientation(optInt("orientation", 0)) ?: return null
        if (!safeToken(assetId) || sourceHash.isNotEmpty() && !sourceHash.matches(SHA256_HEX) ||
            sourceRevision < 1 || displayRevision < 1) return null
        val array = getJSONArray("regions")
        val regions = (0 until minOf(array.length(), MAX_OCR_REGIONS_PER_ASSET))
            .mapNotNull { array.optJSONObject(it)?.toRegion() }
        PhotoOcrGeometry(
            assetId, sourceHash, sourceRevision, displayRevision,
            optString("coordinate_space", "display_normalized").take(80),
            optInt("width", 0).coerceAtLeast(0), optInt("height", 0).coerceAtLeast(0),
            orientation, optString("engine").take(80), optString("model").take(120),
            optString("engine_version").take(80), regions,
        )
    } catch (_: Exception) {
        null
    }
}

private fun PhotoOcrRegion.toJson(): JSONObject = JSONObject()
    .put("id", id).put("type", regionType).put("text", text)
    .put("confidence", confidence ?: JSONObject.NULL)
    .put("polygon", JSONArray().apply {
        polygon.forEach { point -> put(JSONArray().put(point.x).put(point.y)) }
    })

private fun JSONObject.toRegion(): PhotoOcrRegion? {
    val polygonArray = optJSONArray("polygon") ?: return null
    val polygon = (0 until minOf(polygonArray.length(), MAX_OCR_POLYGON_POINTS))
        .mapNotNull { index ->
            val point = polygonArray.optJSONArray(index) ?: return@mapNotNull null
            val x = point.optDouble(0, Double.NaN)
            val y = point.optDouble(1, Double.NaN)
            NormalizedPoint(x, y).takeIf(::validPoint)
        }
    val region = PhotoOcrRegion(
        optString("id").take(120), optString("type", "text").take(80), polygon,
        optString("text").take(MAX_OCR_REGION_TEXT_CHARS),
        finiteDouble("confidence")?.coerceIn(0.0, 1.0),
    )
    return region.takeIf(::validRegion)
}

private fun legacyContract(dir: File): CapturePhotoAssets {
    val assets = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
        ?.sortedBy { photoNumber(it.name) }
        ?.mapIndexed { index, photo -> legacyAsset(dir.name, photo, photoNumber(photo.name).takeIf { it > 0 } ?: index + 1) }
        .orEmpty()
    return CapturePhotoAssets(dir.name, assets, legacyFallback = true)
}

private fun legacyAsset(captureId: String, photo: File, order: Int): CapturePhotoAsset {
    val id = stablePhotoAssetId(captureId, photo.name)
    val originalName = originalFileName(id)
    val originalRef = originalName.takeIf { File(photo.parentFile, it).isFile } ?: photo.name
    return CapturePhotoAsset(
        id, order.coerceAtLeast(1), photo.name,
        PhotoOriginal(originalRef), PhotoDisplayDerivative(photo.name),
        PhotoLifecycleState(PhotoAssetLifecycle.COMPLETED),
        PhotoRoleAssignment(
            suggestedRole = PhotoRole.OTHER,
            confidence = 0.0,
            reason = "Legacy capture; no role evidence",
            algorithm = "legacy-fallback",
            algorithmVersion = "1",
        ),
    )
}

private fun reconcileContract(dir: File, contract: CapturePhotoAssets): CapturePhotoAssets {
    val captureNames = dir.listFiles { file -> file.isFile && file.name.matches(PHOTO_NAME) }
        ?.map { it.name }?.toSet().orEmpty()
    val assets = contract.assets.filter { it.captureFile in captureNames }
        .sortedWith(compareBy<CapturePhotoAsset> { it.captureOrder }.thenBy { it.assetId })
    val ids = assets.map { it.assetId }.toSet()
    fun validChoice(choice: PhotoSelectionChoice): PhotoSelectionChoice =
        if (choice.assetId == null || choice.assetId in ids) choice else choice.copy(assetId = null)
    return contract.copy(
        assets = assets,
        selections = CapturePhotoSelections(
            validChoice(contract.selections.primaryTitle),
            validChoice(contract.selections.thumbnail),
        ),
    )
}

private fun stablePhotoAssetId(captureId: String, captureFile: String): String =
    UUID.nameUUIDFromBytes("$captureId\u0000$captureFile".toByteArray(Charsets.UTF_8)).toString()

private fun originalFileName(assetId: String) = "original_$assetId.jpg"

private fun preserveOriginalFile(source: File, target: File): Boolean {
    if (target.isFile) return true
    if (!source.isFile || source == target) return false
    return try {
        Files.createLink(target.toPath(), source.toPath())
        true
    } catch (_: Exception) {
        val tmp = File(target.parentFile, ".${target.name}.${UUID.randomUUID()}.tmp")
        try {
            source.inputStream().use { input -> tmp.outputStream().use { input.copyTo(it) } }
            try {
                Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE)
            } catch (_: Exception) {
                Files.move(tmp.toPath(), target.toPath())
            }
            target.isFile && target.length() == source.length()
        } catch (_: Exception) {
            false
        } finally {
            tmp.delete()
        }
    }
}

private fun sha256(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { input ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            if (read > 0) digest.update(buffer, 0, read)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}

private fun imageProperties(file: File): PhotoImageProperties = try {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.absolutePath, bounds)
    val orientation = when (ExifInterface(file.absolutePath).getAttributeInt(
        ExifInterface.TAG_ORIENTATION,
        ExifInterface.ORIENTATION_NORMAL,
    )) {
        ExifInterface.ORIENTATION_ROTATE_90 -> 90
        ExifInterface.ORIENTATION_ROTATE_180 -> 180
        ExifInterface.ORIENTATION_ROTATE_270 -> 270
        else -> 0
    }
    PhotoImageProperties(bounds.outWidth.coerceAtLeast(0), bounds.outHeight.coerceAtLeast(0), orientation)
} catch (_: Exception) {
    PhotoImageProperties()
}

private fun normalizeEvidence(value: String): String = value.lowercase()
    .replace(Regex("[^\\p{L}\\p{N}]+"), " ").trim().replace(Regex("\\s+"), " ")

private fun safeToken(value: String): Boolean = value.isNotEmpty() && value.matches(SAFE_ASSET_TOKEN) &&
    value != "." && value != ".."

private fun safeReference(value: String): Boolean = safeToken(value) && !value.contains('/') && !value.contains('\\')

private fun normalizedOrientation(value: Int): Int? = value.takeIf { it in setOf(0, 90, 180, 270) }

private fun validPoint(point: NormalizedPoint): Boolean = point.x.isFinite() && point.y.isFinite() &&
    point.x in 0.0..1.0 && point.y in 0.0..1.0

private fun validRegion(region: PhotoOcrRegion): Boolean = region.polygon.size >= 3 &&
    region.polygon.all(::validPoint)

private fun validHomography(values: List<Double>?): List<Double>? =
    values?.takeIf { it.size == 9 && it.all(Double::isFinite) }

private fun geometryKey(value: PhotoOcrGeometry): String =
    "${value.displayRevision}|${value.engine}|${value.model}|${value.coordinateSpace}"

private fun transformAndClipPolygon(
    polygon: List<NormalizedPoint>,
    homography: List<Double>,
): List<NormalizedPoint> {
    if (polygon.size < 3) return emptyList()
    var transformed = polygon.map { point ->
        val denominator = homography[6] * point.x + homography[7] * point.y + homography[8]
        if (!denominator.isFinite() || kotlin.math.abs(denominator) < 1e-12) {
            return emptyList()
        }
        NormalizedPoint(
            (homography[0] * point.x + homography[1] * point.y + homography[2]) /
                denominator,
            (homography[3] * point.x + homography[4] * point.y + homography[5]) /
                denominator,
        )
    }
    if (transformed.any { !it.x.isFinite() || !it.y.isFinite() }) return emptyList()
    transformed = clipPolygon(transformed, { it.x >= 0.0 }) { a, b -> intersectX(a, b, 0.0) }
    transformed = clipPolygon(transformed, { it.x <= 1.0 }) { a, b -> intersectX(a, b, 1.0) }
    transformed = clipPolygon(transformed, { it.y >= 0.0 }) { a, b -> intersectY(a, b, 0.0) }
    transformed = clipPolygon(transformed, { it.y <= 1.0 }) { a, b -> intersectY(a, b, 1.0) }
    if (transformed.size < 3) return emptyList()
    val cleaned = transformed.map { point ->
        NormalizedPoint(point.x.coerceIn(0.0, 1.0), point.y.coerceIn(0.0, 1.0))
    }.fold(mutableListOf<NormalizedPoint>()) { result, point ->
        if (result.lastOrNull()?.let { nearlySamePoint(it, point) } != true) result += point
        result
    }.also { points ->
        if (points.size > 1 && nearlySamePoint(points.first(), points.last())) {
            points.removeAt(points.lastIndex)
        }
    }
    if (cleaned.size < 3 || kotlin.math.abs(polygonArea(cleaned)) < 1e-10) return emptyList()
    return cleaned
}

private fun clipPolygon(
    polygon: List<NormalizedPoint>,
    inside: (NormalizedPoint) -> Boolean,
    intersection: (NormalizedPoint, NormalizedPoint) -> NormalizedPoint?,
): List<NormalizedPoint> {
    if (polygon.isEmpty()) return emptyList()
    val output = mutableListOf<NormalizedPoint>()
    var previous = polygon.last()
    var previousInside = inside(previous)
    polygon.forEach { current ->
        val currentInside = inside(current)
        when {
            currentInside && !previousInside -> {
                intersection(previous, current)?.let(output::add)
                output += current
            }
            currentInside -> output += current
            previousInside -> intersection(previous, current)?.let(output::add)
        }
        previous = current
        previousInside = currentInside
    }
    return output
}

private fun intersectX(a: NormalizedPoint, b: NormalizedPoint, x: Double): NormalizedPoint? {
    val delta = b.x - a.x
    if (kotlin.math.abs(delta) < 1e-12) return null
    val fraction = (x - a.x) / delta
    return NormalizedPoint(x, a.y + (b.y - a.y) * fraction)
}

private fun intersectY(a: NormalizedPoint, b: NormalizedPoint, y: Double): NormalizedPoint? {
    val delta = b.y - a.y
    if (kotlin.math.abs(delta) < 1e-12) return null
    val fraction = (y - a.y) / delta
    return NormalizedPoint(a.x + (b.x - a.x) * fraction, y)
}

private fun nearlySamePoint(a: NormalizedPoint, b: NormalizedPoint): Boolean =
    kotlin.math.abs(a.x - b.x) < 1e-10 && kotlin.math.abs(a.y - b.y) < 1e-10

private fun polygonArea(points: List<NormalizedPoint>): Double = points.indices.sumOf { index ->
    val next = points[(index + 1) % points.size]
    points[index].x * next.y - next.x * points[index].y
} / 2.0

private fun validProcessingOperations(operations: List<PhotoProcessingOperation>): Boolean =
    operations.size <= PhotoProcessingOutcome.values().size &&
        operations.map { it.outcome }.distinct().size == operations.size &&
        operations.all { operation ->
            when (operation.outcome) {
                PhotoProcessingOutcome.SPINE_CROP -> operation.resultRole == PhotoRole.SPINE
                else -> operation.resultRole == null
            }
        }

internal fun processingOperationsFor(
    profile: PostProcessingProfile,
    role: PhotoRole,
): List<PhotoProcessingOperation> {
    if (!isPostProcessingRole(role)) return emptyList()
    return buildList {
        if (role != PhotoRole.SPINE && profile.features.dewarpPerspectiveAndPageCurvature) {
            add(PhotoProcessingOperation(PhotoProcessingOutcome.PAGE_DEWARP))
        }
        if (role != PhotoRole.SPINE && profile.features.cropToDetectedPageMargins) {
            add(PhotoProcessingOperation(PhotoProcessingOutcome.DETECTED_MARGIN_CROP))
        }
        if (profile.features.normalizePageAndTextContrast) {
            add(PhotoProcessingOperation(PhotoProcessingOutcome.CONTRAST_NORMALIZATION))
        }
        if (role == PhotoRole.SPINE && profile.features.detectAndCropSpine) {
            add(PhotoProcessingOperation(PhotoProcessingOutcome.SPINE_CROP, PhotoRole.SPINE))
        }
    }
}

internal fun isPostProcessingRole(role: PhotoRole): Boolean =
    role in setOf(PhotoRole.TITLE_PAGE, PhotoRole.COVER, PhotoRole.SPINE)

private fun validPostProcessingProfile(profile: PostProcessingProfile): Boolean =
    profile.contractVersion == PostProcessingProfile.CONTRACT_VERSION &&
        profile == resolvePostProcessingProfile(
            profile.selectedPreset,
            profile.publicationYear,
            profile.features,
        )

private fun validProcessingRequestForAsset(
    request: PhotoProcessingRequest,
    assetId: String,
    original: PhotoOriginal,
    display: PhotoDisplayDerivative,
): Boolean {
    if (!safeToken(request.requestId) || !validPostProcessingProfile(request.profile) ||
        request.requestRevision < 1 || request.requestedAt <= 0L ||
        request.sourceAssetId != assetId ||
        request.sourceRole !in setOf(PhotoRole.TITLE_PAGE, PhotoRole.COVER, PhotoRole.SPINE) ||
        !request.sourceOriginalSha256.matches(SHA256_HEX) ||
        !request.sourceDisplaySha256.matches(SHA256_HEX) ||
        request.sourceOriginalRevision != original.revision ||
        request.sourceOriginalSha256 != original.sha256 ||
        request.sourceDisplayRevision !in 1..display.revision ||
        request.sourceDisplayRevision == display.revision &&
            request.sourceDisplaySha256 != display.sha256) return false
    return validProcessingOperations(request.operations) &&
        request.operations == processingOperationsFor(request.profile, request.sourceRole)
}

private fun sameFile(left: File, right: File): Boolean = try {
    left.canonicalFile == right.canonicalFile
} catch (_: Exception) {
    left.absoluteFile == right.absoluteFile
}

private fun JSONObject.finiteDouble(key: String): Double? = opt(key).let { value ->
    when (value) {
        is Number -> value.toDouble()
        is String -> value.toDoubleOrNull()
        else -> null
    }?.takeIf(Double::isFinite)
}

private fun JSONObject.strictInt(key: String): Int? {
    val value = opt(key) as? Number ?: return null
    val number = value.toDouble()
    val integer = value.toInt()
    return integer.takeIf { number.isFinite() && number == integer.toDouble() }
}

private fun JSONObject.strictLong(key: String): Long? {
    val value = opt(key) as? Number ?: return null
    val number = value.toDouble()
    val integer = value.toLong()
    return integer.takeIf { number.isFinite() && number == integer.toDouble() }
}
