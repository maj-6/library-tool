package org.whl.bookcapture

import android.graphics.Bitmap
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

internal const val COVER_VISUAL_SIGNATURE_ALGORITHM = "whl-cover-v1"
private const val SIGNATURE_WIDTH = 48
private const val SIGNATURE_HEIGHT = 64
private const val GRID_COLUMNS = 6
private const val GRID_ROWS = 8

/**
 * Decode an EXIF-upright, memory-bounded bitmap and immediately collapse it to
 * the non-reversible whl-cover-v1 descriptor. The caller still owns deletion
 * of [file]; this function never writes a sibling or retains source pixels.
 */
internal fun extractCoverVisualSignature(file: File): String? {
    val bitmap = decodeSampledOriented(file, 192, 256) ?: return null
    return try {
        val pixels = IntArray(bitmap.width * bitmap.height)
        bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
        coverVisualSignature(bitmap.width, bitmap.height, pixels)
    } finally {
        bitmap.recycle()
    }
}

/** Pure signature core, separated from Bitmap so exposure behavior is unit-testable. */
internal fun coverVisualSignature(
    sourceWidth: Int,
    sourceHeight: Int,
    argb: IntArray,
): String? {
    if (sourceWidth <= 0 || sourceHeight <= 0 ||
        argb.size != sourceWidth * sourceHeight
    ) return null
    val sampled = centeredCoverSample(sourceWidth, sourceHeight, argb)
    val count = sampled.size
    val red = IntArray(count)
    val green = IntArray(count)
    val blue = IntArray(count)
    val luma = IntArray(count)
    val saturation = DoubleArray(count)
    val hue = DoubleArray(count)
    for (index in sampled.indices) {
        val color = sampled[index]
        val r = (color ushr 16) and 0xff
        val g = (color ushr 8) and 0xff
        val b = color and 0xff
        red[index] = r
        green[index] = g
        blue[index] = b
        luma[index] = (.299 * r + .587 * g + .114 * b).roundToInt().coerceIn(0, 255)
        val hi = max(r, max(g, b)).toDouble()
        val lo = min(r, min(g, b)).toDouble()
        val spread = hi - lo
        saturation[index] = if (hi <= 0.0) 0.0 else spread / hi
        hue[index] = when {
            spread <= 0.0 -> 0.0
            hi == r.toDouble() -> positiveModulo(((g - b) / spread) / 6.0, 1.0)
            hi == g.toDouble() -> ((b - r) / spread + 2.0) / 6.0
            else -> ((r - g) / spread + 4.0) / 6.0
        }
    }

    val hueWeights = DoubleArray(12)
    val chromaWeights = DoubleArray(16)
    for (index in sampled.indices) {
        val sat = saturation[index]
        if (sat >= .06) {
            hueWeights[min(11, floor(hue[index] * 12.0).toInt())] += sat
        }
        val sum = red[index] + green[index] + blue[index]
        if (sum >= 24) {
            val rc = red[index].toDouble() / sum
            val gc = green[index].toDouble() / sum
            val rb = min(3, floor(rc * 5.0).toInt())
            val gb = min(3, floor(gc * 5.0).toInt())
            chromaWeights[gb * 4 + rb] += 1.0
        }
    }

    val normalizedLuma = stretchLuma(luma)
    val gradients = coverGradients(normalizedLuma)
    val root = JSONObject()
        .put("version", 1)
        .put("algorithm", COVER_VISUAL_SIGNATURE_ALGORITHM)
        .put(
            "aspect_milli",
            (1000.0 * sourceWidth / sourceHeight).roundToInt().coerceIn(250, 4000),
        )
        .put("hue_hist", jsonArray(normalizeHistogram(hueWeights)))
        .put("chroma_hist", jsonArray(normalizeHistogram(chromaWeights)))
        .put("chroma_grid", jsonArray(chromaGrid(red, green, blue, saturation)))
        .put("tone_grid", jsonArray(cellMeans(normalizedLuma)))
        .put("edge_grid", jsonArray(edgeGrid(gradients.magnitudes)))
        .put("gradient_hist", jsonArray(normalizeHistogram(gradients.orientationWeights)))
        .put("dhash", differenceHash(normalizedLuma))
    val encoded = root.toString()
    return encoded.takeIf {
        it.toByteArray(Charsets.UTF_8).size <= ScanSearchQueue.MAX_VISUAL_SIGNATURE_BYTES
    }
}

internal fun validCoverVisualSignature(value: String): Boolean {
    return try {
        if (value.isBlank() ||
            value.toByteArray(Charsets.UTF_8).size > ScanSearchQueue.MAX_VISUAL_SIGNATURE_BYTES
        ) return false
        val root = JSONObject(value)
        val expectedKeys = setOf(
            "version", "algorithm", "aspect_milli", "hue_hist", "chroma_hist",
            "chroma_grid", "tone_grid", "edge_grid", "gradient_hist", "dhash",
        )
        val actualKeys = buildSet {
            val iterator = root.keys()
            while (iterator.hasNext()) add(iterator.next())
        }
        if (actualKeys != expectedKeys ||
            root.opt("version") != 1 ||
            root.opt("algorithm") != COVER_VISUAL_SIGNATURE_ALGORITHM ||
            (root.opt("aspect_milli") as? Int)?.let { it in 250..4000 } != true ||
            !validByteArray(root.optJSONArray("hue_hist"), 12, normalized = true) ||
            !validByteArray(root.optJSONArray("chroma_hist"), 16, normalized = true) ||
            !validByteArray(root.optJSONArray("chroma_grid"), 144) ||
            !validByteArray(root.optJSONArray("tone_grid"), 48) ||
            !validByteArray(root.optJSONArray("edge_grid"), 48) ||
            !validByteArray(root.optJSONArray("gradient_hist"), 8, normalized = true) ||
            (root.opt("dhash") as? String)?.matches(Regex("^[0-9a-f]{16}$")) != true
        ) return false
        true
    } catch (_: Exception) {
        false
    }
}

private fun validByteArray(
    value: JSONArray?,
    size: Int,
    normalized: Boolean = false,
): Boolean {
    if (value == null || value.length() != size) return false
    val values = (0 until size).map { index ->
        (value.opt(index) as? Int)?.takeIf { it in 0..255 } ?: return false
    }
    return !normalized || values.sum() in setOf(0, 255)
}

/** Center crop to the shared 3:4 sample, using bilinear RGB interpolation. */
private fun centeredCoverSample(width: Int, height: Int, pixels: IntArray): IntArray {
    val targetAspect = SIGNATURE_WIDTH.toDouble() / SIGNATURE_HEIGHT
    val sourceAspect = width.toDouble() / height
    val cropWidth: Double
    val cropHeight: Double
    if (sourceAspect > targetAspect) {
        cropHeight = height.toDouble()
        cropWidth = cropHeight * targetAspect
    } else {
        cropWidth = width.toDouble()
        cropHeight = cropWidth / targetAspect
    }
    val left = (width - cropWidth) / 2.0
    val top = (height - cropHeight) / 2.0
    return IntArray(SIGNATURE_WIDTH * SIGNATURE_HEIGHT) { index ->
        val x = index % SIGNATURE_WIDTH
        val y = index / SIGNATURE_WIDTH
        val sx = left + (x + .5) * cropWidth / SIGNATURE_WIDTH - .5
        val sy = top + (y + .5) * cropHeight / SIGNATURE_HEIGHT - .5
        bilinearArgb(width, height, pixels, sx, sy)
    }
}

private fun bilinearArgb(
    width: Int,
    height: Int,
    pixels: IntArray,
    sourceX: Double,
    sourceY: Double,
): Int {
    val x0 = floor(sourceX).toInt().coerceIn(0, width - 1)
    val y0 = floor(sourceY).toInt().coerceIn(0, height - 1)
    val x1 = (x0 + 1).coerceAtMost(width - 1)
    val y1 = (y0 + 1).coerceAtMost(height - 1)
    val fx = (sourceX - floor(sourceX)).coerceIn(0.0, 1.0)
    val fy = (sourceY - floor(sourceY)).coerceIn(0.0, 1.0)
    fun channel(selector: (Int) -> Int): Int {
        val top = selector(pixels[y0 * width + x0]) * (1 - fx) +
            selector(pixels[y0 * width + x1]) * fx
        val bottom = selector(pixels[y1 * width + x0]) * (1 - fx) +
            selector(pixels[y1 * width + x1]) * fx
        return (top * (1 - fy) + bottom * fy).roundToInt().coerceIn(0, 255)
    }
    val r = channel { (it ushr 16) and 0xff }
    val g = channel { (it ushr 8) and 0xff }
    val b = channel { it and 0xff }
    return (0xff shl 24) or (r shl 16) or (g shl 8) or b
}

private fun normalizeHistogram(weights: DoubleArray): IntArray {
    val total = weights.sum()
    if (total <= 0.0) return IntArray(weights.size)
    val exact = DoubleArray(weights.size) { weights[it] * 255.0 / total }
    val result = IntArray(weights.size) { floor(exact[it]).toInt() }
    var remainder = 255 - result.sum()
    exact.indices.sortedWith(
        compareByDescending<Int> { exact[it] - floor(exact[it]) }.thenBy { it },
    ).forEach { index ->
        if (remainder > 0) {
            result[index] += 1
            remainder -= 1
        }
    }
    return result
}

private fun stretchLuma(luma: IntArray): IntArray {
    val sorted = luma.sortedArray()
    val low = sorted[floor((sorted.size - 1) * .03).toInt()]
    val high = sorted[floor((sorted.size - 1) * .97).toInt()]
    if (high - low < 8) return IntArray(luma.size) { 128 }
    return IntArray(luma.size) { index ->
        ((luma[index] - low) * 255.0 / (high - low)).roundToInt().coerceIn(0, 255)
    }
}

private data class CoverGradients(
    val magnitudes: DoubleArray,
    val orientationWeights: DoubleArray,
)

private fun coverGradients(luma: IntArray): CoverGradients {
    val magnitudes = DoubleArray(luma.size)
    val orientations = DoubleArray(8)
    for (y in 1 until SIGNATURE_HEIGHT - 1) {
        for (x in 1 until SIGNATURE_WIDTH - 1) {
            val index = y * SIGNATURE_WIDTH + x
            val dx = luma[index + 1] - luma[index - 1]
            val dy = luma[index + SIGNATURE_WIDTH] - luma[index - SIGNATURE_WIDTH]
            val magnitude = hypot(dx.toDouble(), dy.toDouble())
            magnitudes[index] = magnitude
            if (magnitude >= 4.0) {
                val angle = positiveModulo(atan2(dy.toDouble(), dx.toDouble()), PI)
                val bin = min(7, floor(angle * 8.0 / PI).toInt())
                orientations[bin] += magnitude
            }
        }
    }
    return CoverGradients(magnitudes, orientations)
}

private fun positiveModulo(value: Double, modulus: Double): Double =
    ((value % modulus) + modulus) % modulus

private fun chromaGrid(
    red: IntArray,
    green: IntArray,
    blue: IntArray,
    saturation: DoubleArray,
): IntArray {
    val output = IntArray(GRID_COLUMNS * GRID_ROWS * 3)
    forEachCell { cell, indices ->
        var rc = 0.0
        var gc = 0.0
        var sat = 0.0
        var samples = 0
        indices.forEach { index ->
            val sum = red[index] + green[index] + blue[index]
            if (sum >= 24) {
                rc += red[index].toDouble() / sum
                gc += green[index].toDouble() / sum
                sat += saturation[index]
                samples += 1
            }
        }
        val target = cell * 3
        if (samples == 0) {
            output[target] = 85
            output[target + 1] = 85
            output[target + 2] = 0
        } else {
            output[target] = (255 * rc / samples).roundToInt().coerceIn(0, 255)
            output[target + 1] = (255 * gc / samples).roundToInt().coerceIn(0, 255)
            output[target + 2] = (255 * sat / samples).roundToInt().coerceIn(0, 255)
        }
    }
    return output
}

private fun cellMeans(values: IntArray): IntArray {
    val output = IntArray(GRID_COLUMNS * GRID_ROWS)
    forEachCell { cell, indices ->
        output[cell] = indices.map(values::get).average().roundToInt().coerceIn(0, 255)
    }
    return output
}

private fun edgeGrid(magnitudes: DoubleArray): IntArray {
    val nonzero = magnitudes.filter { it > 0.0 }.sorted()
    val p90 = if (nonzero.isEmpty()) 0.0 else
        nonzero[floor((nonzero.size - 1) * .90).toInt()]
    val scale = max(12.0, p90)
    val output = IntArray(GRID_COLUMNS * GRID_ROWS)
    forEachCell { cell, indices ->
        val mean = indices.map(magnitudes::get).average()
        output[cell] = (255 * min(1.0, mean / scale)).roundToInt().coerceIn(0, 255)
    }
    return output
}

private inline fun forEachCell(block: (Int, IntArray) -> Unit) {
    for (row in 0 until GRID_ROWS) {
        for (column in 0 until GRID_COLUMNS) {
            val indices = ArrayList<Int>(64)
            val yStart = row * SIGNATURE_HEIGHT / GRID_ROWS
            val yEnd = (row + 1) * SIGNATURE_HEIGHT / GRID_ROWS
            val xStart = column * SIGNATURE_WIDTH / GRID_COLUMNS
            val xEnd = (column + 1) * SIGNATURE_WIDTH / GRID_COLUMNS
            for (y in yStart until yEnd) for (x in xStart until xEnd) {
                indices += y * SIGNATURE_WIDTH + x
            }
            block(row * GRID_COLUMNS + column, indices.toIntArray())
        }
    }
}

private fun differenceHash(luma: IntArray): String {
    var bits = 0uL
    for (y in 0 until 8) {
        var previous = bilinearScalar(luma, 9, 8, 0, y)
        for (x in 1 until 9) {
            val current = bilinearScalar(luma, 9, 8, x, y)
            bits = (bits shl 1) or if (previous < current) 1uL else 0uL
            previous = current
        }
    }
    return bits.toString(16).padStart(16, '0')
}

private fun bilinearScalar(
    values: IntArray,
    targetWidth: Int,
    targetHeight: Int,
    x: Int,
    y: Int,
): Double {
    val sx = (x + .5) * SIGNATURE_WIDTH / targetWidth - .5
    val sy = (y + .5) * SIGNATURE_HEIGHT / targetHeight - .5
    val x0 = floor(sx).toInt().coerceIn(0, SIGNATURE_WIDTH - 1)
    val y0 = floor(sy).toInt().coerceIn(0, SIGNATURE_HEIGHT - 1)
    val x1 = (x0 + 1).coerceAtMost(SIGNATURE_WIDTH - 1)
    val y1 = (y0 + 1).coerceAtMost(SIGNATURE_HEIGHT - 1)
    val fx = (sx - floor(sx)).coerceIn(0.0, 1.0)
    val fy = (sy - floor(sy)).coerceIn(0.0, 1.0)
    val top = values[y0 * SIGNATURE_WIDTH + x0] * (1 - fx) +
        values[y0 * SIGNATURE_WIDTH + x1] * fx
    val bottom = values[y1 * SIGNATURE_WIDTH + x0] * (1 - fx) +
        values[y1 * SIGNATURE_WIDTH + x1] * fx
    return top * (1 - fy) + bottom * fy
}

private fun jsonArray(values: IntArray): JSONArray = JSONArray().apply {
    values.forEach(::put)
}
