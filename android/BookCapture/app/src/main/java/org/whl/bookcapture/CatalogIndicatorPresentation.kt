package org.whl.bookcapture

/**
 * View state for the compact CH / WHL / IA indicator row.
 *
 * Pure: it maps a capture's known catalogue state onto tones and labels and
 * nothing else, so the rules can be tested without inflating a view. Follows
 * the same two-overload shape as [BookDiagnosticsPresenter] — an [Entries.Entry]
 * overload that does the reading, and a value overload that does the deciding.
 *
 * The row is deliberately uniform across surfaces: three slots, always in the
 * same order, so the eye learns one position per source. A slot that has
 * nothing to say is hidden rather than shown grey, because a row of grey dots
 * on every book is noise that trains you to stop looking.
 */

enum class CatalogSource { CH, WHL, IA }

/**
 * What a slot is saying.
 *
 * PROPOSED is the only actionable state and exists only for CH: the phone found
 * a candidate on the master list and is waiting for the user to accept or
 * reject it. Keeping it distinct from CONFIRMED is the whole point — an
 * unreviewed guess must never look like a decided fact.
 */
enum class CatalogTone { CONFIRMED, PROPOSED, ABSENT, PENDING, HIDDEN }

data class CatalogIndicator(
    val source: CatalogSource,
    val tone: CatalogTone,
    /** Short screen-reader sentence; the glyph carries the meaning visually. */
    val description: String,
    /** True when a tap/long-press should approve/reject. */
    val actionable: Boolean = false,
)

data class CatalogIndicatorRow(val indicators: List<CatalogIndicator>) {
    fun of(source: CatalogSource): CatalogIndicator? = indicators.firstOrNull { it.source == source }
    val visible: List<CatalogIndicator> get() = indicators.filter { it.tone != CatalogTone.HIDDEN }
    val hasAction: Boolean get() = indicators.any { it.actionable }
}

/**
 * The phone-side CH verdict for one capture.
 *
 * `candidate` is what the bundled index proposed; `decision` is what the user
 * said about it. They are separate because rejecting a proposal must not erase
 * which row was proposed — otherwise the next search offers the same wrong book
 * again with no memory that it was already refused.
 */
enum class ChDecision { UNREVIEWED, APPROVED, REJECTED }

data class ChCandidate(
    /** Stable, content-derived key — NOT the array index, which moves when the
     *  source catalogue is reordered (see migration 012's warning). */
    val key: String,
    val title: String,
    val author: String,
    val year: String = "",
    /** 0..1 from the shared matcher; carried for display and for re-ranking. */
    val score: Double = 0.0,
    /**
     * CH's values, already mapped onto this app's field names.
     *
     * Carried on the candidate rather than fetched at merge time so an approval
     * merges exactly what was proposed and shown — re-reading the index later
     * could silently merge a different row if the bundled index has since been
     * replaced by an app update.
     */
    val fields: Map<String, String> = emptyMap(),
)

data class ChMatchState(
    val searched: Boolean = false,
    val candidate: ChCandidate? = null,
    val decision: ChDecision = ChDecision.UNREVIEWED,
) {
    companion object {
        val NOT_SEARCHED = ChMatchState()
    }
}

internal object CatalogIndicatorPresenter {

    fun from(entry: Entries.Entry, ch: ChMatchState): CatalogIndicatorRow = from(
        ch = ch,
        whl = entry.desktopBook?.whl,
        ia = entry.desktopBook?.internetArchive,
    )

    internal fun from(
        ch: ChMatchState,
        whl: DesktopAvailability?,
        ia: DesktopAvailability?,
    ): CatalogIndicatorRow = CatalogIndicatorRow(
        listOf(
            chIndicator(ch),
            availabilityIndicator(CatalogSource.WHL, whl),
            availabilityIndicator(CatalogSource.IA, ia),
        ),
    )

    private fun chIndicator(ch: ChMatchState): CatalogIndicator {
        // Not searched yet says nothing, and must not be confused with searched
        // and found nothing — the first is "ask again later", the second is a
        // real answer worth showing.
        if (!ch.searched) {
            return CatalogIndicator(CatalogSource.CH, CatalogTone.PENDING, "CH list not checked yet")
        }
        val candidate = ch.candidate
            ?: return CatalogIndicator(CatalogSource.CH, CatalogTone.ABSENT, "Not on the CH list")

        return when (ch.decision) {
            ChDecision.APPROVED -> CatalogIndicator(
                CatalogSource.CH,
                CatalogTone.CONFIRMED,
                "On the CH list as ${candidate.title}",
            )
            // A rejected proposal is a decided answer: this book is not that CH
            // row. It stops being actionable, so a stray long-press cannot
            // toggle it back and forth.
            ChDecision.REJECTED -> CatalogIndicator(
                CatalogSource.CH,
                CatalogTone.ABSENT,
                "Not on the CH list (match rejected)",
            )
            ChDecision.UNREVIEWED -> CatalogIndicator(
                CatalogSource.CH,
                CatalogTone.PROPOSED,
                "Possible CH match: ${candidate.title}. " +
                    "Tap to approve, long press to reject.",
                actionable = true,
            )
        }
    }

    /**
     * WHL and IA are desktop/cloud-authored availability, not user decisions.
     *
     * `available == null` means the check has not answered, which is hidden
     * rather than drawn: an absent answer is not evidence of absence, and the
     * existing home row already treats it that way.
     */
    private fun availabilityIndicator(
        source: CatalogSource,
        availability: DesktopAvailability?,
    ): CatalogIndicator {
        val label = if (source == CatalogSource.WHL) "World Herb Library" else "Internet Archive"
        val detail = availability?.detail.orEmpty().trim()
        return when (availability?.available) {
            true -> CatalogIndicator(
                source,
                CatalogTone.CONFIRMED,
                if (detail.isEmpty()) "Available on $label" else "Available on $label: $detail",
            )
            false -> CatalogIndicator(
                source,
                CatalogTone.ABSENT,
                if (detail.isEmpty()) "Not on $label" else "Not on $label: $detail",
            )
            null -> CatalogIndicator(source, CatalogTone.HIDDEN, "$label not checked")
        }
    }
}
