package org.whl.bookcapture

import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import kotlin.math.abs

internal const val CLOUD_PHOTO_RESULT_SCHEMA =
    "org.whl.bookcapture.photo-processing-result"
internal const val CLOUD_PHOTO_RESULT_VERSION = 1
internal const val CLOUD_DERIVATIVE_BUCKET = "capture-derivatives"
internal const val MAX_CLOUD_DERIVATIVE_BYTES = 32L * 1024L * 1024L
internal const val MAX_CLOUD_DERIVATIVE_PIXELS = 40L * 1_000_000L

private val CLOUD_SAFE_TOKEN = Regex("[A-Za-z0-9._-]+")
private val CLOUD_SHA256 = Regex("[0-9a-f]{64}")
private val CLOUD_JOB_STATES = setOf(
    "queued", "running", "retrying", "completed", "failed", "cancelled",
)

/** Owner-readable projection of one row in photo_processing_jobs. */
internal data class CloudPhotoProcessingJob(
    val id: String,
    val captureId: String,
    val ownerId: String,
    val assetId: String,
    val requestId: String,
    val requestRevision: Int,
    val sourceSha256: String,
    val state: String,
    val result: JSONObject?,
    val lastError: String,
)

internal data class CloudDisplayArtifact(
    val bucket: String,
    val path: String,
    val sha256: String,
    val bytes: Long,
    val mime: String,
    val width: Int,
    val height: Int,
)

/** A result that has been bound to the exact local request and merge base. */
internal data class CloudDisplayInstallPlan(
    val job: CloudPhotoProcessingJob,
    val artifact: CloudDisplayArtifact,
    val mergeBaseSha256: String,
    val mergeBaseRevision: Int,
    val targetRevision: Int,
    val recipe: String,
    val recipeVersion: String,
    val baseToOutputHomography: List<Double>?,
    val reocrRequired: Boolean,
)

/**
 * Immutable identity for a nonlinear display derivative that still needs OCR
 * in its corrected coordinate space. Workers must match every field again
 * immediately before applying geometry; a newer derivative makes the target
 * a harmless no-op.
 */
internal data class CloudDisplayReocrTarget(
    val captureId: String,
    val assetId: String,
    val jobId: String,
    val displayReference: String,
    val displaySha256: String,
    val displayRevision: Int,
)

internal const val MAX_CLOUD_DISPLAY_REOCR_ATTEMPTS = 3

internal fun shouldRetryCloudDisplayReocr(runAttemptCount: Int): Boolean =
    runAttemptCount + 1 < MAX_CLOUD_DISPLAY_REOCR_ATTEMPTS

internal sealed interface CloudResultDecision {
    /** The row is valid but belongs to a request no longer current locally. */
    data object NotApplicable : CloudResultDecision

    /** A newer local display has already superseded this immutable result. */
    data object Superseded : CloudResultDecision

    /** The row matches the current request, but its completed result is unsafe. */
    data class Rejected(val reason: String) : CloudResultDecision

    data class Ready(val plan: CloudDisplayInstallPlan) : CloudResultDecision
}

internal fun cloudPhotoProcessingJobFromJson(row: JSONObject): CloudPhotoProcessingJob? {
    val id = row.strictCloudString("id") ?: return null
    val captureId = row.strictCloudString("capture_id") ?: return null
    val ownerId = row.strictCloudString("owner_id") ?: return null
    val assetId = row.strictCloudString("asset_id") ?: return null
    val requestId = row.strictCloudString("request_id") ?: return null
    val requestRevision = row.strictCloudInt("request_revision") ?: return null
    val sourceSha256 = row.strictCloudString("source_sha256")?.lowercase() ?: return null
    val state = row.strictCloudString("state")?.lowercase() ?: return null
    if (!listOf(id, captureId, ownerId, assetId, requestId).all(::cloudSafeToken) ||
        requestRevision < 1 || !sourceSha256.matches(CLOUD_SHA256) ||
        state !in CLOUD_JOB_STATES) return null
    val resultValue = row.opt("result")
    val result = when (resultValue) {
        null, JSONObject.NULL -> null
        is JSONObject -> JSONObject(resultValue.toString())
        else -> return null
    }
    val lastError = when (val value = row.opt("last_error")) {
        null, JSONObject.NULL -> ""
        is String -> cleanCloudError(value)
        else -> return null
    }
    return CloudPhotoProcessingJob(
        id, captureId, ownerId, assetId, requestId, requestRevision,
        sourceSha256, state, result, lastError,
    )
}

internal fun cloudJobMatchesAsset(
    captureId: String,
    asset: CapturePhotoAsset,
    job: CloudPhotoProcessingJob,
): Boolean {
    val request = asset.processingRequest ?: return false
    return job.captureId == captureId && job.assetId == asset.assetId &&
        job.requestId == request.requestId &&
        job.requestRevision == request.requestRevision &&
        job.sourceSha256 == request.sourceOriginalSha256 &&
        request.sourceAssetId == asset.assetId &&
        request.sourceOriginalSha256 == asset.original.sha256 &&
        request.sourceOriginalRevision == asset.original.revision
}

/**
 * Validate every authority and lineage boundary before any network bytes are
 * trusted. The display artifact path is derived independently, so a malicious
 * or corrupted result cannot turn the authenticated Storage GET into an
 * arbitrary-object read.
 */
internal fun validateCloudPhotoResult(
    local: CapturePhotoAssets,
    job: CloudPhotoProcessingJob,
    expectedOwnerId: String,
): CloudResultDecision {
    if (job.ownerId != expectedOwnerId || job.captureId != local.captureId) {
        return CloudResultDecision.NotApplicable
    }
    val asset = local.assets.firstOrNull { it.assetId == job.assetId }
        ?: return CloudResultDecision.NotApplicable
    if (!cloudJobMatchesAsset(local.captureId, asset, job)) {
        return CloudResultDecision.NotApplicable
    }
    if (job.state != "completed") return CloudResultDecision.NotApplicable
    val request = checkNotNull(asset.processingRequest)
    val result = job.result ?: return CloudResultDecision.Rejected("missing result")

    if (result.strictCloudString("schema") != CLOUD_PHOTO_RESULT_SCHEMA ||
        result.strictCloudInt("version") != CLOUD_PHOTO_RESULT_VERSION ||
        result.strictCloudString("capture_id") != job.captureId ||
        result.strictCloudString("asset_id") != job.assetId ||
        result.strictCloudString("request_id") != job.requestId ||
        result.strictCloudInt("request_revision") != job.requestRevision) {
        return CloudResultDecision.Rejected("result identity")
    }

    val derived = result.optJSONObject("derived_from")
        ?: return CloudResultDecision.Rejected("missing source lineage")
    val originalHash = derived.strictCloudString("original_sha256")?.lowercase()
    val originalRevision = derived.strictCloudInt("original_revision")
    val displayHash = derived.strictCloudString("display_sha256")?.lowercase()
    val displayRevision = derived.strictCloudInt("display_revision")
    if (originalHash != request.sourceOriginalSha256 ||
        originalRevision != request.sourceOriginalRevision ||
        displayHash != request.sourceDisplaySha256 ||
        displayRevision != request.sourceDisplayRevision) {
        return CloudResultDecision.Rejected("source lineage")
    }

    val processor = result.optJSONObject("processor")
        ?: return CloudResultDecision.Rejected("missing processor")
    if (processor.strictCloudString("name") != "whl-image-processor") {
        return CloudResultDecision.Rejected("processor identity")
    }
    val processorVersion = processor.strictCloudString("version")
        ?.takeIf { it.length <= 80 && it.isNotBlank() }
        ?: return CloudResultDecision.Rejected("processor version")

    val display = result.optJSONObject("display")
        ?: return CloudResultDecision.Rejected("missing display result")
    val targetRevision = display.strictCloudInt("target_revision")
        ?: return CloudResultDecision.Rejected("target revision")
    val mergeBase = display.optJSONObject("merge_base")
        ?: return CloudResultDecision.Rejected("missing merge base")
    val mergeHash = mergeBase.strictCloudString("sha256")?.lowercase()
    val mergeRevision = mergeBase.strictCloudInt("revision")
    if (mergeHash != request.sourceDisplaySha256 ||
        mergeRevision != request.sourceDisplayRevision ||
        targetRevision != request.sourceDisplayRevision + 1) {
        return CloudResultDecision.Rejected("display merge base")
    }
    val recipe = display.strictCloudString("recipe")
    if (recipe != "whl-cloud-book-cleanup") {
        return CloudResultDecision.Rejected("display recipe")
    }
    val recipeVersion = display.strictCloudString("recipe_version")
    if (recipeVersion != processorVersion) {
        return CloudResultDecision.Rejected("display recipe version")
    }

    val strategy = display.strictCloudString("geometry_strategy")
        ?: return CloudResultDecision.Rejected("geometry strategy")
    val reocrRequired = display.strictCloudBoolean("reocr_required")
        ?: return CloudResultDecision.Rejected("OCR strategy")
    val homographyValue = display.opt("base_to_output_homography")
    val homography = when (homographyValue) {
        null, JSONObject.NULL -> null
        is org.json.JSONArray -> validCloudHomography(
            (0 until homographyValue.length()).map { index ->
                val number = homographyValue.opt(index) as? Number
                    ?: return CloudResultDecision.Rejected("display homography")
                number.toDouble()
            },
        ) ?: return CloudResultDecision.Rejected("display homography")
        else -> return CloudResultDecision.Rejected("display homography")
    }
    when (strategy) {
        "homography" -> if (homography == null || reocrRequired) {
            return CloudResultDecision.Rejected("homography OCR strategy")
        }
        "replace_and_reocr" -> if (homography != null || !reocrRequired) {
            return CloudResultDecision.Rejected("nonlinear OCR strategy")
        }
        else -> return CloudResultDecision.Rejected("geometry strategy")
    }

    val artifactJson = result.optJSONObject("artifacts")?.optJSONObject("display")
        ?: return CloudResultDecision.Rejected("missing display artifact")
    val artifact = parseDisplayArtifact(artifactJson)
        ?: return CloudResultDecision.Rejected("display artifact metadata")
    val expectedPath = "${job.ownerId}/${job.captureId}/${job.assetId}/" +
        "r${job.requestRevision}-${job.requestId}/display-${artifact.sha256.take(20)}.jpg"
    if (artifact.bucket != CLOUD_DERIVATIVE_BUCKET || artifact.path != expectedPath) {
        return CloudResultDecision.Rejected("display artifact path")
    }

    val currentDisplay = asset.display
    when {
        currentDisplay.revision == targetRevision &&
            currentDisplay.sha256 == artifact.sha256 -> Unit // idempotent repair/download
        currentDisplay.revision > targetRevision -> return CloudResultDecision.Superseded
        currentDisplay.revision != mergeRevision || currentDisplay.sha256 != mergeHash ->
            return CloudResultDecision.Rejected("local display merge base")
    }

    return CloudResultDecision.Ready(CloudDisplayInstallPlan(
        job = job,
        artifact = artifact,
        mergeBaseSha256 = checkNotNull(mergeHash),
        mergeBaseRevision = checkNotNull(mergeRevision),
        targetRevision = targetRevision,
        recipe = recipe,
        recipeVersion = recipeVersion,
        baseToOutputHomography = homography,
        reocrRequired = reocrRequired,
    ))
}

private fun parseDisplayArtifact(value: JSONObject): CloudDisplayArtifact? {
    val bucket = value.strictCloudString("bucket") ?: return null
    val path = value.strictCloudString("path") ?: return null
    val hash = value.strictCloudString("sha256")?.lowercase() ?: return null
    val bytes = value.strictCloudLong("bytes") ?: return null
    val mime = value.strictCloudString("mime")?.lowercase() ?: return null
    val width = value.strictCloudInt("width") ?: return null
    val height = value.strictCloudInt("height") ?: return null
    if (bucket != CLOUD_DERIVATIVE_BUCKET || !cloudSafeObjectPath(path) ||
        !hash.matches(CLOUD_SHA256) || bytes !in 1..MAX_CLOUD_DERIVATIVE_BYTES ||
        mime != "image/jpeg" || width !in 1..32_768 || height !in 1..32_768 ||
        width.toLong() * height.toLong() > MAX_CLOUD_DERIVATIVE_PIXELS) return null
    return CloudDisplayArtifact(bucket, path, hash, bytes, mime, width, height)
}

internal fun cloudLifecycleForRemoteState(state: String): PhotoAssetLifecycle? = when (state) {
    "queued" -> PhotoAssetLifecycle.QUEUED
    "running" -> PhotoAssetLifecycle.RUNNING
    "retrying" -> PhotoAssetLifecycle.RETRYING
    "failed" -> PhotoAssetLifecycle.FAILED
    "cancelled" -> PhotoAssetLifecycle.CANCELLED
    else -> null // completed is recorded only after verified local installation
}

internal fun cloudPhotoWorkPending(contract: CapturePhotoAssets): Boolean =
    contract.assets.any { asset ->
        photoPostProcessingPending(asset) && asset.lifecycle.state !in setOf(
            PhotoAssetLifecycle.FAILED,
            PhotoAssetLifecycle.CANCELLED,
        )
    }

internal data class CloudJpegDimensions(val width: Int, val height: Int)

/** Returns null for truncated/malformed JPEGs, not merely files with .jpg names. */
internal fun completeCloudJpegDimensions(file: File): CloudJpegDimensions? = try {
    RandomAccessFile(file, "r").use { input ->
        val length = input.length()
        if (length < 4 || input.readUnsignedByte() != 0xff ||
            input.readUnsignedByte() != 0xd8) return@use null
        input.seek(length - 2)
        if (input.readUnsignedByte() != 0xff || input.readUnsignedByte() != 0xd9) {
            return@use null
        }
        var dimensions: CloudJpegDimensions? = null
        input.seek(2)
        while (input.filePointer < length - 2) {
            if (input.readUnsignedByte() != 0xff) return@use null
            var marker = input.readUnsignedByte()
            while (marker == 0xff) marker = input.readUnsignedByte()
            if (marker == 0x00 || marker == 0xd8 || marker == 0xd9) return@use null
            if (marker == 0x01 || marker in 0xd0..0xd7) continue
            if (input.filePointer + 2 > length) return@use null
            val segmentLength = input.readUnsignedShort()
            if (segmentLength < 2) return@use null
            val segmentEnd = input.filePointer + segmentLength - 2
            if (segmentEnd > length - 2) return@use null
            when {
                marker in CLOUD_JPEG_FRAME_MARKERS -> {
                    if (segmentLength < 8) return@use null
                    input.readUnsignedByte()
                    val height = input.readUnsignedShort()
                    val width = input.readUnsignedShort()
                    if (width == 0 || height == 0) return@use null
                    dimensions = CloudJpegDimensions(width, height)
                }
                marker == 0xda -> {
                    if (dimensions == null || segmentLength < 6 || segmentEnd >= length - 2) {
                        return@use null
                    }
                    return@use dimensions
                }
            }
            input.seek(segmentEnd)
        }
        null
    }
} catch (_: Exception) {
    null
}

private val CLOUD_JPEG_FRAME_MARKERS = setOf(
    0xc0, 0xc1, 0xc2, 0xc3,
    0xc5, 0xc6, 0xc7,
    0xc9, 0xca, 0xcb,
    0xcd, 0xce, 0xcf,
)

/** Null means the authenticated download exactly matches its signed metadata. */
internal fun verifyCloudDisplayDownload(
    file: File,
    artifact: CloudDisplayArtifact,
    responseContentType: String,
    receivedBytes: Long,
): String? {
    val mime = responseContentType.substringBefore(';').trim().lowercase()
    if (mime != artifact.mime || mime != "image/jpeg") return "response MIME"
    if (receivedBytes != artifact.bytes || file.length() != artifact.bytes) return "artifact size"
    if (artifact.bytes !in 1..MAX_CLOUD_DERIVATIVE_BYTES) return "artifact size"
    val dimensions = completeCloudJpegDimensions(file) ?: return "JPEG structure"
    if (dimensions.width != artifact.width || dimensions.height != artifact.height) {
        return "JPEG dimensions"
    }
    if (cloudSha256(file) != artifact.sha256) return "artifact checksum"
    return null
}

internal fun cloudDisplayFileName(plan: CloudDisplayInstallPlan): String =
    "cloud_${plan.job.assetId}_r${plan.targetRevision}_${plan.artifact.sha256.take(20)}.jpg"

private fun validCloudHomography(values: List<Double>): List<Double>? {
    if (values.size != 9 || values.any { !it.isFinite() || abs(it) > 1e9 }) return null
    val determinant =
        values[0] * (values[4] * values[8] - values[5] * values[7]) -
            values[1] * (values[3] * values[8] - values[5] * values[6]) +
            values[2] * (values[3] * values[7] - values[4] * values[6])
    return values.takeIf { determinant.isFinite() && abs(determinant) > 1e-12 }
}

private fun cloudSha256(file: File): String {
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

private fun cloudSafeToken(value: String): Boolean =
    value.isNotEmpty() && value.length <= 255 && value.matches(CLOUD_SAFE_TOKEN) &&
        value != "." && value != ".."

private fun cloudSafeObjectPath(value: String): Boolean {
    if (value.isEmpty() || value.length > 1024 || value.startsWith('/') ||
        value.contains('\\')) return false
    val segments = value.split('/')
    return segments.size >= 2 && segments.all(::cloudSafeToken)
}

internal fun cleanCloudError(value: String): String =
    value.replace(Regex("\\s+"), " ").trim().take(500)

private fun JSONObject.strictCloudString(key: String): String? =
    (opt(key) as? String)?.trim()

private fun JSONObject.strictCloudBoolean(key: String): Boolean? = opt(key) as? Boolean

private fun JSONObject.strictCloudInt(key: String): Int? {
    val value = opt(key) as? Number ?: return null
    val number = value.toDouble()
    val integer = value.toInt()
    return integer.takeIf { number.isFinite() && number == integer.toDouble() }
}

private fun JSONObject.strictCloudLong(key: String): Long? {
    val value = opt(key) as? Number ?: return null
    val number = value.toDouble()
    val integer = value.toLong()
    return integer.takeIf { number.isFinite() && number == integer.toDouble() }
}
