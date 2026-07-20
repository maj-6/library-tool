package org.whl.bookcapture

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Bitmap
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PointF
import android.graphics.drawable.Drawable
import android.util.AttributeSet
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import androidx.appcompat.widget.AppCompatImageView
import kotlin.math.sqrt

/** A normalized OCR region in the coordinate space of the displayed bitmap. */
data class PhotoOverlayRegion(
    val polygon: List<PointF>,
    val label: String = "",
)

/**
 * A lifecycle-light photo surface: fit-center by default, optional pinch/pan,
 * and normalized polygon overlays drawn through the same image matrix. This
 * keeps boxes aligned under letterboxing and zoom instead of positioning
 * screen-space rectangles beside an ImageView.
 */
class ZoomablePhotoView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : AppCompatImageView(context, attrs, defStyleAttr) {

    var zoomEnabled: Boolean = false
    var onOriginalHoldChanged: ((Boolean) -> Unit)? = null

    private val fitMatrix = Matrix()
    private val workingMatrix = Matrix()
    private var regions: List<PhotoOverlayRegion> = emptyList()
    private var overlayAlpha = .55f
    private var showLabels = false
    private var holdingOriginal = false
    private val overlayPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.rgb(53, 99, 90)
    }
    private val overlayPath = Path()
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = Color.rgb(43, 39, 32)
    }

    private val scaleDetector = ScaleGestureDetector(context,
        object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                if (!zoomEnabled) return false
                val current = matrixScale(workingMatrix)
                val requested = (current * detector.scaleFactor).coerceIn(
                    matrixScale(fitMatrix), matrixScale(fitMatrix) * 6f,
                )
                val factor = requested / current.coerceAtLeast(.0001f)
                workingMatrix.postScale(
                    factor,
                    factor,
                    detector.focusX - paddingLeft,
                    detector.focusY - paddingTop,
                )
                applyWorkingMatrix()
                return true
            }
        })

    private val gestureDetector = GestureDetector(context,
        object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent): Boolean = true

            override fun onSingleTapConfirmed(e: MotionEvent): Boolean {
                return performClick()
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                if (!zoomEnabled) return false
                if (matrixScale(workingMatrix) > matrixScale(fitMatrix) * 1.15f) {
                    resetToFit()
                } else {
                    workingMatrix.postScale(2f, 2f, e.x - paddingLeft, e.y - paddingTop)
                    applyWorkingMatrix()
                }
                return true
            }

            override fun onScroll(
                e1: MotionEvent?,
                e2: MotionEvent,
                distanceX: Float,
                distanceY: Float,
            ): Boolean {
                if (!zoomEnabled || scaleDetector.isInProgress) return false
                workingMatrix.postTranslate(-distanceX, -distanceY)
                applyWorkingMatrix()
                return true
            }

            override fun onLongPress(e: MotionEvent) {
                if (onOriginalHoldChanged == null) return
                holdingOriginal = true
                performLongClick()
                onOriginalHoldChanged?.invoke(true)
            }
        })

    init {
        scaleType = ScaleType.MATRIX
        isClickable = true
        isLongClickable = true
    }

    fun setOverlayRegions(
        value: List<PhotoOverlayRegion>,
        opacity: Float = .55f,
        labels: Boolean = false,
    ) {
        regions = value
        overlayAlpha = opacity.coerceIn(.1f, 1f)
        showLabels = labels
        invalidate()
    }

    fun resetToFit() {
        calculateFitMatrix(drawable)
        workingMatrix.set(fitMatrix)
        applyWorkingMatrix()
    }

    fun setPhotoBitmap(bitmap: Bitmap?) {
        super.setImageBitmap(bitmap)
        post { resetToFit() }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        resetToFit()
    }

    @SuppressLint("ClickableViewAccessibility") // GestureDetector calls performClick/performLongClick.
    override fun onTouchEvent(event: MotionEvent): Boolean {
        scaleDetector.onTouchEvent(event)
        gestureDetector.onTouchEvent(event)
        if ((event.actionMasked == MotionEvent.ACTION_UP ||
                event.actionMasked == MotionEvent.ACTION_CANCEL) && holdingOriginal) {
            holdingOriginal = false
            onOriginalHoldChanged?.invoke(false)
        }
        return true
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    override fun performLongClick(): Boolean {
        super.performLongClick()
        return true
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val d = drawable ?: return
        if (regions.isEmpty() || d.intrinsicWidth <= 0 || d.intrinsicHeight <= 0) return

        val scale = matrixScale(imageMatrix).coerceAtLeast(.0001f)
        overlayPaint.alpha = (overlayAlpha * 255).toInt()
        overlayPaint.strokeWidth = 2f / scale
        labelPaint.alpha = (overlayAlpha * 255).toInt()
        labelPaint.textSize = 13f / scale
        canvas.save()
        // ImageView draws matrix-scaled content after translating into its
        // padded content box. Mirror that exact order for the polygons.
        canvas.translate(paddingLeft.toFloat(), paddingTop.toFloat())
        canvas.concat(imageMatrix)
        regions.forEach { region ->
            if (region.polygon.size < 3) return@forEach
            overlayPath.reset()
            region.polygon.forEachIndexed { index, point ->
                val x = point.x.coerceIn(0f, 1f) * d.intrinsicWidth
                val y = point.y.coerceIn(0f, 1f) * d.intrinsicHeight
                if (index == 0) overlayPath.moveTo(x, y) else overlayPath.lineTo(x, y)
            }
            overlayPath.close()
            canvas.drawPath(overlayPath, overlayPaint)
            if (showLabels && region.label.isNotBlank()) {
                val first = region.polygon.first()
                canvas.drawText(
                    region.label,
                    first.x.coerceIn(0f, 1f) * d.intrinsicWidth,
                    first.y.coerceIn(0f, 1f) * d.intrinsicHeight - (3f / scale),
                    labelPaint,
                )
            }
        }
        canvas.restore()
    }

    private fun calculateFitMatrix(d: Drawable?) {
        fitMatrix.reset()
        if (d == null || width <= paddingLeft + paddingRight || height <= paddingTop + paddingBottom ||
            d.intrinsicWidth <= 0 || d.intrinsicHeight <= 0) return
        val availableWidth = width - paddingLeft - paddingRight
        val availableHeight = height - paddingTop - paddingBottom
        val scale = minOf(
            availableWidth.toFloat() / d.intrinsicWidth,
            availableHeight.toFloat() / d.intrinsicHeight,
        )
        val dx = (availableWidth - d.intrinsicWidth * scale) / 2f
        val dy = (availableHeight - d.intrinsicHeight * scale) / 2f
        fitMatrix.postScale(scale, scale)
        fitMatrix.postTranslate(dx, dy)
    }

    private fun applyWorkingMatrix() {
        imageMatrix = workingMatrix
        invalidate()
    }

    private fun matrixScale(matrix: Matrix): Float {
        val values = FloatArray(9)
        matrix.getValues(values)
        return sqrt(values[Matrix.MSCALE_X] * values[Matrix.MSCALE_X] +
            values[Matrix.MSKEW_Y] * values[Matrix.MSKEW_Y])
    }
}
