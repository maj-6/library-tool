package org.whl.bookcapture

import android.content.Context
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import androidx.core.content.ContextCompat

/**
 * Curated scan assessment projected from Library Tool.
 *
 * This is deliberately separate from the older numeric candidate rank and
 * from membership in a physical scan queue. Do not derive one from another.
 */
enum class ScanPriorityAssessment(
    val wireValue: String,
    val badge: String,
) {
    NO_SCAN("n/s (no scan)", "N/S"),
    LOW("Low", "LOW"),
    MEDIUM("Medium", "MED"),
    HIGH("High", "HI"),
    ;

    companion object {
        /** Accept only the canonical wire values; malformed values fail closed. */
        fun parse(raw: String?): ScanPriorityAssessment? =
            entries.firstOrNull { it.wireValue == raw }
    }
}

/** Detail text preserves the difference between an explicit catalog null and
 * metadata that has not been fetched (or came from an older projection). */
internal fun scanPriorityDetailValue(
    assessment: ScanPriorityAssessment?,
    assessmentKnown: Boolean,
    unassessedLabel: String,
    unavailableLabel: String,
): String = when {
    !assessmentKnown -> unavailableLabel
    assessment == null -> unassessedLabel
    else -> assessment.wireValue
}

internal enum class ScanPriorityTone {
    NONE,
    CANDIDATE,
    NO_SCAN,
    LOW,
    MEDIUM,
    HIGH,
}

/**
 * One presentation contract for every place Android draws a book.
 *
 * A legacy candidate can predate numbered priorities. It remains visible with
 * a question-mark badge instead of inventing a priority that a curator did not
 * assign.
 */
internal data class ScanPriorityPresentation(
    val visible: Boolean,
    val candidateGlyphVisible: Boolean,
    val badge: String,
    val accessibilityLabel: String,
    val tone: ScanPriorityTone,
)

/** Effective candidate state for views backed directly by an Entry. */
internal fun entryScanCandidate(ctx: Context, entry: Entries.Entry): Boolean? {
    if (entry.desktopBook?.digitizationCandidateClassification == true) return true
    val pending = InspectBookMemberships.read(ctx)
        .takeIf { it.valid }
        ?.memberships
        ?.get(entry.id)
    if (pending != null) {
        if (pending.removed) return entry.desktopBook?.digitizationCandidateClassification
        return if (Collections.byId(ctx, pending.collectionId)?.collectionType ==
            CollectionType.SCAN
        ) {
            true
        } else {
            entry.desktopBook?.digitizationCandidateClassification
        }
    }
    if (CaptureScanMarkStore.read(entry.dir) != null) return true
    return entry.desktopBook?.digitizationCandidateClassification
}

internal fun scanPriorityPresentation(
    candidate: Boolean?,
    rank: Int?,
    assessment: ScanPriorityAssessment?,
    candidateLabel: String,
    rankLabel: (Int) -> String,
    rankUnsetLabel: String,
    assessmentLabel: (ScanPriorityAssessment) -> String,
): ScanPriorityPresentation {
    val isCandidate = candidate == true
    if (!isCandidate && assessment == null) {
        return ScanPriorityPresentation(
            visible = false,
            candidateGlyphVisible = false,
            badge = "",
            accessibilityLabel = "",
            tone = ScanPriorityTone.NONE,
        )
    }

    if (assessment != null) {
        return ScanPriorityPresentation(
            visible = true,
            candidateGlyphVisible = isCandidate,
            badge = assessment.badge,
            accessibilityLabel = listOfNotNull(
                candidateLabel.takeIf { isCandidate },
                assessmentLabel(assessment),
            ).joinToString(". "),
            tone = when (assessment) {
                ScanPriorityAssessment.NO_SCAN -> ScanPriorityTone.NO_SCAN
                ScanPriorityAssessment.LOW -> ScanPriorityTone.LOW
                ScanPriorityAssessment.MEDIUM -> ScanPriorityTone.MEDIUM
                ScanPriorityAssessment.HIGH -> ScanPriorityTone.HIGH
            },
        )
    }

    val safeRank = rank?.takeIf { it in 1..5 }
    return ScanPriorityPresentation(
        visible = true,
        candidateGlyphVisible = true,
        badge = safeRank?.toString() ?: "?",
        accessibilityLabel = safeRank?.let(rankLabel)
            ?: "$candidateLabel. $rankUnsetLabel",
        tone = ScanPriorityTone.CANDIDATE,
    )
}

/** Compatibility overload for existing call sites while assessment is wired. */
internal fun scanPriorityPresentation(
    candidate: Boolean?,
    rank: Int?,
    candidateLabel: String,
    rankLabel: (Int) -> String,
    rankUnsetLabel: String,
): ScanPriorityPresentation = scanPriorityPresentation(
    candidate = candidate,
    rank = rank,
    assessment = null,
    candidateLabel = candidateLabel,
    rankLabel = rankLabel,
    rankUnsetLabel = rankUnsetLabel,
    assessmentLabel = { "" },
)

/** Bind the shared overlay and opt the containing card into its subtle tint. */
internal fun bindScanPriorityIndicator(
    bookView: View,
    candidate: Boolean?,
    rank: Int?,
    assessment: ScanPriorityAssessment? = null,
): ScanPriorityPresentation {
    val presentation = scanPriorityPresentation(
        candidate = candidate,
        rank = rank,
        assessment = assessment,
        candidateLabel = bookView.context.getString(R.string.home_digitization_candidate),
        rankLabel = { value ->
            bookView.context.getString(R.string.scan_priority_description, value)
        },
        rankUnsetLabel = bookView.context.getString(R.string.scan_priority_unset),
        assessmentLabel = { value ->
            bookView.context.getString(
                when (value) {
                    ScanPriorityAssessment.NO_SCAN -> R.string.scan_priority_assessment_no_scan
                    ScanPriorityAssessment.LOW -> R.string.scan_priority_assessment_low
                    ScanPriorityAssessment.MEDIUM -> R.string.scan_priority_assessment_medium
                    ScanPriorityAssessment.HIGH -> R.string.scan_priority_assessment_high
                },
            )
        },
    )

    // Assessment is metadata, not a user selection. Foregrounds keep the
    // treatment visual-only and suppress themselves while an Inspect row is
    // activated, leaving its real selection styling and semantics intact.
    val foreground = when (presentation.tone) {
        ScanPriorityTone.NONE -> null
        ScanPriorityTone.CANDIDATE -> R.drawable.whl_scan_candidate_foreground
        ScanPriorityTone.NO_SCAN -> R.drawable.whl_scan_priority_no_scan_foreground
        ScanPriorityTone.LOW -> R.drawable.whl_scan_priority_low_foreground
        ScanPriorityTone.MEDIUM -> R.drawable.whl_scan_priority_medium_foreground
        ScanPriorityTone.HIGH -> R.drawable.whl_scan_priority_high_foreground
    }
    bookView.foreground = foreground?.let {
        ContextCompat.getDrawable(bookView.context, it)
    }

    bookView.findViewById<View?>(R.id.scanPriorityIndicator)?.apply {
        visibility = if (presentation.visible) View.VISIBLE else View.GONE
        contentDescription = presentation.accessibilityLabel
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }
    bookView.findViewById<ImageView?>(R.id.scanPriorityIcon)?.apply {
        visibility = if (presentation.candidateGlyphVisible) View.VISIBLE else View.GONE
        // Always restore the shared candidate glyph. Rows can be rebound after
        // displaying any assessment, so no drawable/tint state may leak.
        setImageResource(R.drawable.ic_scan_priority)
        setBackgroundResource(R.drawable.whl_scan_priority_plate)
        imageTintList = null
        clearColorFilter()
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }
    bookView.findViewById<TextView?>(R.id.scanPriorityBadge)?.apply {
        text = presentation.badge
        visibility = if (presentation.visible) View.VISIBLE else View.GONE
        setBackgroundResource(
            if (presentation.tone == ScanPriorityTone.NO_SCAN) {
                R.drawable.whl_scan_priority_no_scan_badge
            } else {
                R.drawable.whl_scan_priority_badge
            },
        )
        setTextColor(ContextCompat.getColor(
            context,
            if (presentation.tone == ScanPriorityTone.NO_SCAN) {
                R.color.whl_ink
            } else {
                R.color.whl_face_hi
            },
        ))
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }
    return presentation
}
