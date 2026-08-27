package org.whl.bookcapture

import android.content.Context
import android.view.View
import android.widget.TextView
import androidx.core.content.ContextCompat

/**
 * One presentation contract for every place Android draws a book.
 *
 * A legacy candidate can predate numbered priorities. It remains visible with
 * a question-mark badge instead of inventing a priority that a curator did not
 * assign.
 */
internal data class ScanPriorityPresentation(
    val visible: Boolean,
    val badge: String,
    val accessibilityLabel: String,
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
    candidateLabel: String,
    rankLabel: (Int) -> String,
    rankUnsetLabel: String,
): ScanPriorityPresentation {
    if (candidate != true) return ScanPriorityPresentation(false, "", "")
    val safeRank = rank?.takeIf { it in 1..5 }
    return ScanPriorityPresentation(
        visible = true,
        badge = safeRank?.toString() ?: "?",
        accessibilityLabel = safeRank?.let(rankLabel)
            ?: "$candidateLabel. $rankUnsetLabel",
    )
}

/** Bind the shared overlay and opt the containing card into its subtle tint. */
internal fun bindScanPriorityIndicator(
    bookView: View,
    candidate: Boolean?,
    rank: Int?,
): ScanPriorityPresentation {
    val presentation = scanPriorityPresentation(
        candidate = candidate,
        rank = rank,
        candidateLabel = bookView.context.getString(R.string.home_digitization_candidate),
        rankLabel = { value ->
            bookView.context.getString(R.string.scan_priority_description, value)
        },
        rankUnsetLabel = bookView.context.getString(R.string.scan_priority_unset),
    )
    // A scan candidate is metadata, not a user selection. A foreground keeps
    // the highlight visual-only, so TalkBack and Inspect action mode retain
    // their real selected/activated semantics. The drawable suppresses itself
    // while an Inspect row is activated.
    bookView.foreground = if (presentation.visible) {
        ContextCompat.getDrawable(bookView.context, R.drawable.whl_scan_candidate_foreground)
    } else {
        null
    }
    bookView.findViewById<View?>(R.id.scanPriorityIndicator)?.apply {
        visibility = if (presentation.visible) View.VISIBLE else View.GONE
        contentDescription = presentation.accessibilityLabel
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }
    bookView.findViewById<TextView?>(R.id.scanPriorityBadge)?.apply {
        text = presentation.badge
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }
    return presentation
}
