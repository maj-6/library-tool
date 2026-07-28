package org.whl.bookcapture

import org.json.JSONObject

/**
 * Merging an approved CH master-list record into a scanned book, and the
 * preview that shows what it would do.
 *
 * Two rules decide everything here, both inherited from how the desktop already
 * treats suggested metadata:
 *
 *  - **A blank never overwrites.** CH filling an empty field is the whole point;
 *    CH blanking a field you captured is data loss.
 *  - **A disagreement is never resolved silently.** Your value came off the
 *    title page in front of you; CH's came from a catalogue typed years ago and
 *    is known to be dirty (impossible years, `_x000D_` artefacts). So a
 *    conflict keeps YOUR value and is surfaced, rather than being auto-adopted
 *    because you approved the match.
 *
 * Approving a match therefore means "this is the same book", not "overwrite me".
 */

enum class FieldOrigin {
    /** Scan had nothing; CH supplied the value. */
    ADOPTED,

    /** Both agree, or CH had nothing to add. */
    UNCHANGED,

    /** Both present and different. The scan value is kept. */
    CONFLICT,
}

data class MergeField(
    val key: String,
    val label: String,
    val scanValue: String,
    val chValue: String,
    val mergedValue: String,
    val origin: FieldOrigin,
)

data class ChMergePreview(
    val fields: List<MergeField>,
    val candidate: ChCandidate?,
) {
    val adopted: List<MergeField> get() = fields.filter { it.origin == FieldOrigin.ADOPTED }
    val conflicts: List<MergeField> get() = fields.filter { it.origin == FieldOrigin.CONFLICT }
    /** Nothing to write: every field either agreed or CH was empty. */
    val isNoOp: Boolean get() = adopted.isEmpty()
}

internal object ChMergePresenter {

    /**
     * Field order for the preview.
     *
     * The identity fields lead, because they are what you check to decide the
     * match was right. CH-only cataloguing fields follow — they are the ones a
     * scan almost never has, so they are where the merge earns its keep.
     */
    internal val FIELD_ORDER = listOf(
        "title", "author", "year", "edition", "volume", "publisher", "city",
        "pages", "condition", "illustrations", "price", "categories", "notes",
    )

    private val LABELS = mapOf(
        "title" to "Title", "author" to "Author", "year" to "Year",
        "edition" to "Edition", "volume" to "Volume", "publisher" to "Publisher",
        "city" to "City", "pages" to "Pages", "condition" to "Condition",
        "illustrations" to "Illustrations", "price" to "Price",
        "categories" to "Categories", "notes" to "Notes",
    )

    /**
     * Loose equality for deciding "these agree".
     *
     * Case, punctuation and spacing differences between a dictated title and a
     * catalogue entry are not disagreements, and flagging them as conflicts
     * would bury the handful that matter.
     */
    internal fun sameValue(a: String, b: String): Boolean =
        normalize(a) == normalize(b)

    private fun normalize(value: String): String =
        value.lowercase().replace(Regex("[^a-z0-9]+"), " ").trim()

    fun preview(scan: Map<String, String>, candidate: ChCandidate?): ChMergePreview {
        if (candidate == null) return ChMergePreview(emptyList(), null)
        val ch = candidate.fields

        val keys = FIELD_ORDER + (ch.keys + scan.keys).filterNot { it in FIELD_ORDER }.sorted()
        val fields = keys.mapNotNull { key ->
            val scanValue = scan[key].orEmpty().trim()
            val chValue = ch[key].orEmpty().trim()
            if (scanValue.isEmpty() && chValue.isEmpty()) return@mapNotNull null

            val origin = when {
                chValue.isEmpty() -> FieldOrigin.UNCHANGED
                scanValue.isEmpty() -> FieldOrigin.ADOPTED
                sameValue(scanValue, chValue) -> FieldOrigin.UNCHANGED
                else -> FieldOrigin.CONFLICT
            }
            MergeField(
                key = key,
                label = LABELS[key] ?: humanizeKey(key),
                scanValue = scanValue,
                chValue = chValue,
                mergedValue = if (origin == FieldOrigin.ADOPTED) chValue else scanValue,
                origin = origin,
            )
        }
        return ChMergePreview(fields, candidate)
    }

    private fun humanizeKey(key: String): String = key
        .replace('_', ' ')
        .replaceFirstChar { it.uppercase() }

    /**
     * Apply a preview to the persisted extraction JSON.
     *
     * Mutates a COPY and preserves every key it does not own: the diagnostics
     * tab renders the exact persisted bytes, and unknown future fields from the
     * extraction contract must survive a merge untouched.
     *
     * The CH link is recorded alongside the values so the association survives
     * independently of the fields — copying alone would lose which row this was.
     */
    fun apply(meta: JSONObject?, preview: ChMergePreview): JSONObject {
        val out = meta?.let { JSONObject(it.toString()) } ?: JSONObject()
        preview.adopted.forEach { field -> out.put(field.key, field.mergedValue) }
        preview.candidate?.let { candidate ->
            out.put(
                "ch_match",
                JSONObject()
                    .put("key", candidate.key)
                    .put("title", candidate.title)
                    .put("author", candidate.author)
                    .put("score", candidate.score)
                    .put("adopted", preview.adopted.joinToString(",") { it.key })
                    .put("conflicts", preview.conflicts.joinToString(",") { it.key }),
            )
        }
        return out
    }

    /** The scan-side field map the preview compares against. */
    fun scanFields(meta: JSONObject?): Map<String, String> {
        if (meta == null) return emptyMap()
        val extra = meta.optJSONObject("extra")
        val out = linkedMapOf<String, String>()
        fun collect(source: JSONObject?) {
            source ?: return
            source.keys().forEach { key ->
                if (key == "extra" || key.startsWith("_")) return@forEach
                val value = source.opt(key)
                if (value is JSONObject || value is org.json.JSONArray) return@forEach
                val text = value?.toString().orEmpty().trim()
                if (text.isNotEmpty() && text != "null" && !out.containsKey(key)) out[key] = text
            }
        }
        collect(meta)
        collect(extra)
        return out
    }
}
