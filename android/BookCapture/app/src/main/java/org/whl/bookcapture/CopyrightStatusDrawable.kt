package org.whl.bookcapture

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.drawable.Drawable
import androidx.annotation.ColorInt
import androidx.core.content.ContextCompat

internal enum class CopyrightStatusTone {
    PUBLIC_DOMAIN,
    IN_COPYRIGHT,
    INCONCLUSIVE,
    UNKNOWN,
}

internal fun copyrightStatusTone(status: String): CopyrightStatusTone {
    val value = status.trim().lowercase()
    return when {
        value.startsWith("public domain") && value.contains("no renewal") ->
            CopyrightStatusTone.INCONCLUSIVE
        value.startsWith("public domain") || value == "cleared" ->
            CopyrightStatusTone.PUBLIC_DOMAIN
        value.startsWith("in copyright") || value == "restricted" ||
            value == "no public text" -> CopyrightStatusTone.IN_COPYRIGHT
        value.contains("inconclusive") || value.contains("no renewal") ||
            value == "search only" || value == "searchable only" ->
            CopyrightStatusTone.INCONCLUSIVE
        else -> CopyrightStatusTone.UNKNOWN
    }
}

internal fun resolvedCopyrightStatusTone(
    status: String,
    hasRegistrationEvidence: Boolean,
): CopyrightStatusTone {
    val base = copyrightStatusTone(status)
    return if (base == CopyrightStatusTone.INCONCLUSIVE && hasRegistrationEvidence &&
        status.trim().lowercase().startsWith("public domain") &&
        status.lowercase().contains("no renewal")
    ) CopyrightStatusTone.PUBLIC_DOMAIN else base
}

/**
 * Android projection of the desktop split copyright tag: registration evidence
 * occupies the upper-left triangle and the resolved status the lower-right.
 */
internal class CopyrightStatusDrawable(
    @ColorInt private val registrationColor: Int,
    @ColorInt private val statusColor: Int,
    @ColorInt private val borderColor: Int,
    density: Float,
) : Drawable() {
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = density.coerceAtLeast(1f)
        color = borderColor
    }
    private val diagonal = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = density.coerceAtLeast(1f)
        color = Color.argb(90, 0, 0, 0)
    }
    private val path = Path()

    override fun draw(canvas: Canvas) {
        val b = bounds
        val inset = border.strokeWidth / 2f
        val left = b.left + inset
        val top = b.top + inset
        val right = b.right - inset
        val bottom = b.bottom - inset

        path.reset()
        path.moveTo(left, top)
        path.lineTo(right, top)
        path.lineTo(left, bottom)
        path.close()
        fill.color = registrationColor
        canvas.drawPath(path, fill)

        path.reset()
        path.moveTo(right, top)
        path.lineTo(right, bottom)
        path.lineTo(left, bottom)
        path.close()
        fill.color = statusColor
        canvas.drawPath(path, fill)

        canvas.drawLine(right, top, left, bottom, diagonal)
        canvas.drawRect(left, top, right, bottom, border)
    }

    override fun setAlpha(alpha: Int) {
        fill.alpha = alpha
        border.alpha = alpha
        diagonal.alpha = alpha
        invalidateSelf()
    }

    override fun setColorFilter(colorFilter: ColorFilter?) {
        fill.colorFilter = colorFilter
        border.colorFilter = colorFilter
        diagonal.colorFilter = colorFilter
        invalidateSelf()
    }

    @Deprecated("Deprecated in Java")
    override fun getOpacity(): Int = PixelFormat.TRANSLUCENT
}

internal fun copyrightStatusDrawable(
    context: Context,
    copyright: DesktopCopyrightMetadata,
): Drawable {
    fun color(id: Int) = ContextCompat.getColor(context, id)
    val hasRegistrationEvidence = copyright.registrationRecords.isNotEmpty() ||
        copyright.renewalRecords.any { record ->
            record.optString("registration_number").isNotBlank() ||
                record.optString("registration_date").isNotBlank()
        }
    val registration = color(
        if (hasRegistrationEvidence) R.color.whl_copyright_registration
        else R.color.whl_copyright_unknown,
    )
    val status = color(
        when (resolvedCopyrightStatusTone(copyright.status, hasRegistrationEvidence)) {
            CopyrightStatusTone.PUBLIC_DOMAIN -> R.color.whl_copyright_public_domain
            CopyrightStatusTone.IN_COPYRIGHT -> R.color.whl_copyright_in_copyright
            CopyrightStatusTone.INCONCLUSIVE -> R.color.whl_copyright_inconclusive
            CopyrightStatusTone.UNKNOWN -> R.color.whl_copyright_unknown
        },
    )
    return CopyrightStatusDrawable(
        registration,
        status,
        color(R.color.whl_face_sh2),
        context.resources.displayMetrics.density,
    )
}
