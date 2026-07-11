package org.whl.bookcapture

/**
 * Normalised (0..1) page-bound hint in UPRIGHT preview orientation, plus the
 * upright image aspect (w/h) so an overlay can place it under FILL_CENTER.
 * [good] = the page fills the frame and no edge is clipped.
 */
data class PageHint(
    val left: Float, val top: Float, val right: Float, val bottom: Float,
    val aspect: Float, val good: Boolean,
)

/**
 * A fast, dependency-free "is a page squarely in frame?" heuristic — a HINT,
 * not a cropper. On a small luma grid it thresholds the bright page against a
 * darker desk, takes the bright region's bounding box via row/column
 * projections, and reports whether that box fills the frame without running
 * into an edge. No OpenCV, no ML Kit, no Play Services — it runs on the
 * analysis thread in well under a millisecond.
 *
 * The single reused [g] grid is fine because CameraX drives the analyzer on one
 * executor thread; do not call [detect] concurrently.
 */
object PageDetector {

    private const val GW = 96
    private const val GH = 96
    private val g = IntArray(GW * GH)

    /**
     * @param y        the luma (Y) plane bytes
     * @param w,h      its pixel dimensions
     * @param rowStride bytes per row in [y]
     * @param rotation degrees to rotate the buffer to upright (0/90/180/270)
     * @return the hint, or null when no page-like region is seen
     */
    fun detect(y: ByteArray, w: Int, h: Int, rowStride: Int, rotation: Int): PageHint? {
        var sum = 0L
        for (gy in 0 until GH) {
            val ufy = gy / (GH - 1f)
            for (gx in 0 until GW) {
                val ufx = gx / (GW - 1f)
                // upright (ufx,ufy) -> source (sfx,sfy) for the buffer's rotation
                val sfx: Float; val sfy: Float
                when (rotation) {
                    90 -> { sfx = ufy; sfy = 1f - ufx }
                    270 -> { sfx = 1f - ufy; sfy = ufx }
                    180 -> { sfx = 1f - ufx; sfy = 1f - ufy }
                    else -> { sfx = ufx; sfy = ufy }
                }
                val sx = (sfx * (w - 1)).toInt()
                val sy = (sfy * (h - 1)).toInt()
                val v = y[sy * rowStride + sx].toInt() and 0xFF
                g[gy * GW + gx] = v
                sum += v
            }
        }
        val thresh = (sum / (GW * GH)).toInt() + 12   // page a touch brighter than the mean
        val rowB = IntArray(GH); val colB = IntArray(GW); var bright = 0
        for (gy in 0 until GH) for (gx in 0 until GW)
            if (g[gy * GW + gx] > thresh) { rowB[gy]++; colB[gx]++; bright++ }
        if (bright.toFloat() / (GW * GH) < 0.18f) return null   // not enough page

        val left = firstAbove(colB, GH / 3); val right = lastAbove(colB, GH / 3)
        val top = firstAbove(rowB, GW / 3); val bottom = lastAbove(rowB, GW / 3)
        if (left < 0 || top < 0 || right <= left || bottom <= top) return null

        val aspect = if (rotation == 90 || rotation == 270) h.toFloat() / w else w.toFloat() / h
        val m = 2
        val clipped = left <= m || top <= m || right >= GW - 1 - m || bottom >= GH - 1 - m
        val fills = (right - left) > GW * 0.5f && (bottom - top) > GH * 0.5f
        return PageHint(
            left / (GW - 1f), top / (GH - 1f), right / (GW - 1f), bottom / (GH - 1f),
            aspect, good = !clipped && fills)
    }

    private fun firstAbove(a: IntArray, t: Int): Int { for (i in a.indices) if (a[i] > t) return i; return -1 }
    private fun lastAbove(a: IntArray, t: Int): Int { for (i in a.indices.reversed()) if (a[i] > t) return i; return -1 }
}
