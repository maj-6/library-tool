package org.whl.bookcapture

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.exifinterface.media.ExifInterface
import java.io.File

/** Sample a JPEG within a memory bound and apply every EXIF orientation form. */
internal fun decodeSampledOriented(
    file: File,
    maxWidth: Int,
    maxHeight: Int,
): Bitmap? {
    if (!file.isFile || maxWidth <= 0 || maxHeight <= 0) return null
    val orientation = readExifOrientation(file)
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.absolutePath, bounds)
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null

    // A quarter turn swaps the encoded axes. Scale in encoded orientation first
    // so EXIF rotation never needs to allocate another camera-sized bitmap.
    val encodedMaxWidth = if (exifOrientationSwapsAxes(orientation)) maxHeight else maxWidth
    val encodedMaxHeight = if (exifOrientationSwapsAxes(orientation)) maxWidth else maxHeight
    val sample = bitmapDecodeSampleSize(
        sourceWidth = bounds.outWidth,
        sourceHeight = bounds.outHeight,
        maxWidth = encodedMaxWidth,
        maxHeight = encodedMaxHeight,
    )
    val decoded = BitmapFactory.decodeFile(
        file.absolutePath,
        BitmapFactory.Options().apply { inSampleSize = sample },
    ) ?: return null
    val bounded = scaleBitmapWithin(decoded, encodedMaxWidth, encodedMaxHeight) ?: return null
    return applyExifOrientation(orientation, bounded)
}

internal data class DecodedBitmapSize(
    val width: Int,
    val height: Int,
)

/**
 * Select a decoder-supported power-of-two sample whenever either axis needs it.
 *
 * BitmapFactory may still return dimensions slightly different from the estimate,
 * so [scaleBitmapWithin] enforces the exact final bound after decoding.
 */
internal fun bitmapDecodeSampleSize(
    sourceWidth: Int,
    sourceHeight: Int,
    maxWidth: Int,
    maxHeight: Int,
): Int {
    require(sourceWidth > 0 && sourceHeight > 0)
    require(maxWidth > 0 && maxHeight > 0)
    var sample = 1
    while (sample <= Int.MAX_VALUE / 2) {
        val next = sample * 2
        if (sourceWidth / next < maxWidth && sourceHeight / next < maxHeight) break
        sample = next
    }
    return sample
}

/** Return an aspect-preserving size that never exceeds either requested axis. */
internal fun boundedBitmapSize(
    sourceWidth: Int,
    sourceHeight: Int,
    maxWidth: Int,
    maxHeight: Int,
): DecodedBitmapSize {
    require(sourceWidth > 0 && sourceHeight > 0)
    require(maxWidth > 0 && maxHeight > 0)
    if (sourceWidth <= maxWidth && sourceHeight <= maxHeight) {
        return DecodedBitmapSize(sourceWidth, sourceHeight)
    }
    return if (sourceWidth.toLong() * maxHeight >= sourceHeight.toLong() * maxWidth) {
        DecodedBitmapSize(
            width = maxWidth,
            height = ((sourceHeight.toLong() * maxWidth) / sourceWidth)
                .coerceAtLeast(1)
                .toInt(),
        )
    } else {
        DecodedBitmapSize(
            width = ((sourceWidth.toLong() * maxHeight) / sourceHeight)
                .coerceAtLeast(1)
                .toInt(),
            height = maxHeight,
        )
    }
}

internal fun exifOrientationSwapsAxes(orientation: Int): Boolean = when (orientation) {
    ExifInterface.ORIENTATION_TRANSPOSE,
    ExifInterface.ORIENTATION_ROTATE_90,
    ExifInterface.ORIENTATION_TRANSVERSE,
    ExifInterface.ORIENTATION_ROTATE_270,
    -> true
    else -> false
}

private fun readExifOrientation(file: File): Int = runCatching {
        ExifInterface(file.absolutePath).getAttributeInt(
            ExifInterface.TAG_ORIENTATION,
            ExifInterface.ORIENTATION_NORMAL,
        )
    }.getOrDefault(ExifInterface.ORIENTATION_NORMAL)

private fun scaleBitmapWithin(
    bitmap: Bitmap,
    maxWidth: Int,
    maxHeight: Int,
): Bitmap? {
    val target = boundedBitmapSize(bitmap.width, bitmap.height, maxWidth, maxHeight)
    if (target.width == bitmap.width && target.height == bitmap.height) return bitmap
    return runCatching {
        Bitmap.createScaledBitmap(bitmap, target.width, target.height, true)
    }.fold(
        onSuccess = { scaled ->
            if (scaled !== bitmap) bitmap.recycle()
            scaled
        },
        onFailure = {
            bitmap.recycle()
            null
        },
    )
}

private fun applyExifOrientation(orientation: Int, bitmap: Bitmap): Bitmap? {
    val transform = Matrix()
    when (orientation) {
        ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> transform.setScale(-1f, 1f)
        ExifInterface.ORIENTATION_ROTATE_180 -> transform.setRotate(180f)
        ExifInterface.ORIENTATION_FLIP_VERTICAL -> {
            transform.setRotate(180f)
            transform.postScale(-1f, 1f)
        }
        ExifInterface.ORIENTATION_TRANSPOSE -> {
            transform.setRotate(90f)
            transform.postScale(-1f, 1f)
        }
        ExifInterface.ORIENTATION_ROTATE_90 -> transform.setRotate(90f)
        ExifInterface.ORIENTATION_TRANSVERSE -> {
            transform.setRotate(-90f)
            transform.postScale(-1f, 1f)
        }
        ExifInterface.ORIENTATION_ROTATE_270 -> transform.setRotate(-90f)
        else -> return bitmap
    }
    return runCatching {
        Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, transform, true)
    }.fold(
        onSuccess = { oriented ->
            if (oriented !== bitmap) bitmap.recycle()
            oriented
        },
        onFailure = {
            bitmap.recycle()
            null
        },
    )
}
