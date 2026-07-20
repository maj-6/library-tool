package org.whl.bookcapture

import org.json.JSONObject

/** A capture replaces the previous-book preview only after it is sealed and
 * submitted to the processing queue. An open entry can therefore coexist with
 * the last submitted book without making that useful reference disappear. */
internal fun selectLastSubmittedEntry(entries: List<Entries.Entry>): Entries.Entry? =
    entries.asSequence()
        .filter { it.sealed }
        .maxByOrNull { it.createdAt }

internal data class CaptureExtraField(
    val key: String,
    val label: String,
    val value: String,
)

/** The capture card itself is intentionally primary-only. Its popup contains
 * every catalog field beyond title/author/year, while excluding capture
 * provenance and transport internals. */
internal fun captureExtraFields(metadata: JSONObject?): List<CaptureExtraField> {
    val details = BookDetailPresenter.from(metadata)
    val fields = mutableListOf<BookDetailField>()
    fields += details.secondary
    if (details.volumeTag.isNotEmpty()) {
        fields += BookDetailField("Volume", details.volumeTag.removePrefix("Vol. "))
    }
    if (details.overview.isNotEmpty()) fields += BookDetailField("Overview", details.overview)
    fields += details.other
    return fields.map { field ->
        CaptureExtraField(
            key = field.label.lowercase().replace(' ', '_'),
            label = field.label,
            value = field.value,
        )
    }
}
