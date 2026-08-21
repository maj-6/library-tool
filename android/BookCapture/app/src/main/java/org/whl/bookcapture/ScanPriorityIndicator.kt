package org.whl.bookcapture

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

internal fun scanPriorityPresentation(
    candidate: Boolean?,
    priority: Int?,
    candidateLabel: String,
    priorityLabel: (Int) -> String,
    priorityUnsetLabel: String,
): ScanPriorityPresentation {
    if (candidate != true) return ScanPriorityPresentation(false, "", "")
    val safePriority = priority?.takeIf { it in 1..5 }
    return ScanPriorityPresentation(
        visible = true,
        badge = safePriority?.toString() ?: "?",
        accessibilityLabel = safePriority?.let(priorityLabel)
            ?: "$candidateLabel. $priorityUnsetLabel",
    )
}

/** Bind the shared overlay and opt the containing card into its subtle tint. */
internal fun bindScanPriorityIndicator(
    bookView: View,
    candidate: Boolean?,
    priority: Int?,
): ScanPriorityPresentation {
    val presentation = scanPriorityPresentation(
        candidate = candidate,
        priority = priority,
        candidateLabel = bookView.context.getString(R.string.home_digitization_candidate),
        priorityLabel = { value ->
            bookView.context.getString(R.string.scan_priority_description, value)
        },
        priorityUnsetLabel = bookView.context.getString(R.string.scan_priority_unset),
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
