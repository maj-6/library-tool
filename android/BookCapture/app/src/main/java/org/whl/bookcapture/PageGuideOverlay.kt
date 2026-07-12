package org.whl.bookcapture

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat

/**
 * Draws the detected page outline over the preview: green when the page is
 * squarely in frame, amber when it's clipped or too small, nothing when no
 * page is seen — a hint toward whether the shot needs re-taking.
 *
 * The hint arrives in normalised UPRIGHT coordinates; this maps them through
 * PreviewView's default FILL_CENTER scaling (fill the view, centre, crop the
 * overflow) so the outline tracks the real page.
 */
class PageGuideOverlay @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null
) : View(context, attrs) {

    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 2f * resources.displayMetrics.density
    }
    private val radius = 4f * resources.displayMetrics.density
    private val green = ContextCompat.getColor(context, R.color.whl_green)
    private val amber = ContextCompat.getColor(context, R.color.whl_amber)
    private var hint: PageHint? = null

    fun setHint(h: PageHint?) {
        hint = h
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val hh = hint ?: return
        val vw = width.toFloat(); val vh = height.toFloat()
        if (vw <= 0f || vh <= 0f) return
        val viewAspect = vw / vh
        val dw: Float; val dh: Float
        if (hh.aspect > viewAspect) { dh = vh; dw = vh * hh.aspect }   // wider: crop sides
        else { dw = vw; dh = vw / hh.aspect }                          // taller: crop top/bottom
        val ox = (vw - dw) / 2f; val oy = (vh - dh) / 2f
        val l = ox + hh.left * dw; val r = ox + hh.right * dw
        val t = oy + hh.top * dh; val b = oy + hh.bottom * dh
        stroke.color = if (hh.good) green else amber
        canvas.drawRoundRect(l, t, r, b, radius, radius, stroke)
    }
}
