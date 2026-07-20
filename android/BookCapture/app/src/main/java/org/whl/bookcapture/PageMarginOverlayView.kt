package org.whl.bookcapture

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.min

/**
 * A deliberately faint title-page framing guide. The four shaded bands leave
 * the intended page area untouched, so the live preview is never processed or
 * copied merely to draw the mask.
 */
class PageMarginOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {

    private val density = resources.displayMetrics.density
    private val shadePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(42, 0, 0, 0)
        style = Paint.Style.FILL
    }
    private val framePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(150, 74, 116, 58)
        style = Paint.Style.STROKE
        // Keep the framing hint faint, but never let it collapse to a
        // hairline on mdpi displays or emulator screenshots.
        strokeWidth = maxOf(2f, 2f * density)
    }
    private val focusPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(190, 74, 116, 58)
        style = Paint.Style.STROKE
        strokeWidth = 1.5f * density
    }

    internal var captureOrientation: CameraCaptureOrientation = CameraCaptureOrientation.PORTRAIT
        set(value) {
            field = value
            invalidate()
        }

    private var focusX = 0.5f
    private var focusY = 0.5f
    private var showLockedFocus = false

    init {
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO
        isClickable = false
        isFocusable = false
    }

    fun setLockedFocusPoint(normalizedX: Float, normalizedY: Float, visible: Boolean) {
        focusX = normalizedX.coerceIn(0f, 1f)
        focusY = normalizedY.coerceIn(0f, 1f)
        showLockedFocus = visible
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (width <= 0 || height <= 0) return

        val frame = pageFrame()
        canvas.drawRect(0f, 0f, width.toFloat(), frame.top, shadePaint)
        canvas.drawRect(0f, frame.bottom, width.toFloat(), height.toFloat(), shadePaint)
        canvas.drawRect(0f, frame.top, frame.left, frame.bottom, shadePaint)
        canvas.drawRect(frame.right, frame.top, width.toFloat(), frame.bottom, shadePaint)
        canvas.drawRect(frame, framePaint)

        if (showLockedFocus) {
            val x = focusX * width
            val y = focusY * height
            val radius = 12f * density
            canvas.drawCircle(x, y, radius, focusPaint)
            canvas.drawLine(x - radius * 1.4f, y, x - radius * 0.65f, y, focusPaint)
            canvas.drawLine(x + radius * 0.65f, y, x + radius * 1.4f, y, focusPaint)
            canvas.drawLine(x, y - radius * 1.4f, x, y - radius * 0.65f, focusPaint)
            canvas.drawLine(x, y + radius * 0.65f, x, y + radius * 1.4f, focusPaint)
        }
    }

    private fun pageFrame(): RectF {
        val availableWidth = width * 0.86f
        val availableHeight = height * 0.88f
        val targetAspect = when (captureOrientation) {
            CameraCaptureOrientation.PORTRAIT -> 0.70f
            CameraCaptureOrientation.LANDSCAPE -> 1.43f
        }
        val frameWidth = min(availableWidth, availableHeight * targetAspect)
        val frameHeight = frameWidth / targetAspect
        val left = (width - frameWidth) / 2f
        val top = (height - frameHeight) / 2f
        return RectF(left, top, left + frameWidth, top + frameHeight)
    }
}
