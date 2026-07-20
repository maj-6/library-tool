package org.whl.bookcapture

import org.json.JSONObject

/**
 * A catalog-oriented projection of extraction JSON for both historical and
 * live book-detail surfaces. Keeping this policy out of the Activity prevents
 * the camera preview and full-screen detail page from drifting into different
 * field groupings as the extraction schema grows.
 */
internal data class BookDetailPresentation(
    val title: String,
    val author: String,
    val year: String,
    val volumeTag: String,
    val secondary: List<BookDetailField>,
    val overview: String,
    val other: List<BookDetailField>,
)

internal data class BookDetailField(val label: String, val value: String)

internal object BookDetailPresenter {
    private val primaryKeys = setOf("title", "author", "year")
    private val secondaryKeys = listOf("publisher", "language", "edition", "subtitle")
    private val overviewKeys = listOf("description", "overview", "summary")
    private val spineTitleKeys = listOf("spine_title", "spineTitle", "spine-title")
    private val consumedKeys = primaryKeys + secondaryKeys + overviewKeys +
        setOf("volume", "volume_number")
    private val hiddenTransportKeys = setOf(
        "scan_collection_id", "scan_collection", "scan_from", "capture_id", "photo_assets",
        "_capture_photo_assets",
    )

    fun from(meta: JSONObject?): BookDetailPresentation {
        if (meta == null) {
            return BookDetailPresentation("", "", "", "", emptyList(), "", emptyList())
        }

        val extra = meta.optJSONObject("extra")
        fun value(key: String): String = sequenceOf(meta, extra)
            .filterNotNull()
            .map { objectValue(it, key) }
            .firstOrNull { it.isNotEmpty() }
            .orEmpty()
        val publishedTitle = value("title")
        val spineTitle = consistentAliasValue(meta, extra, spineTitleKeys)
            .takeUnless { candidate ->
                publishedTitle.isNotEmpty() &&
                    normalizeComparableTitle(candidate) == normalizeComparableTitle(publishedTitle)
            }
            .orEmpty()

        val secondary = secondaryKeys.mapNotNull { key ->
            value(key).takeIf { it.isNotEmpty() }?.let { BookDetailField(humanize(key), it) }
        }
        val overview = overviewKeys.asSequence().map(::value).firstOrNull { it.isNotEmpty() }.orEmpty()

        val other = linkedMapOf<String, BookDetailField>()
        // Preserve the extraction contract's stable order before adding
        // provider-specific fields alphabetically.
        Pipeline.FIELDS.filterNot { it in consumedKeys }.forEach { key ->
            val displayedValue = if (key == "spine_title") spineTitle else value(key)
            displayedValue.takeIf { it.isNotEmpty() }?.let {
                other[key] = BookDetailField(humanize(key), it)
            }
        }
        val unknownKeys = buildSet {
            meta.keys().forEach { add(it) }
            extra?.keys()?.forEach { add(it) }
        }.filterNot {
            it == "extra" || it in consumedKeys || it in spineTitleKeys ||
                isHiddenTransportKey(it) || it in other
        }
            .sortedBy { humanize(it).lowercase() }
        unknownKeys.forEach { key ->
            value(key).takeIf { it.isNotEmpty() }?.let {
                other[key] = BookDetailField(humanize(key), it)
            }
        }

        val volume = value("volume").ifEmpty { value("volume_number") }
        return BookDetailPresentation(
            title = publishedTitle,
            author = value("author"),
            year = value("year"),
            volumeTag = volume.takeIf { it.isNotEmpty() }?.let { "Vol. $it" }.orEmpty(),
            secondary = secondary,
            overview = overview,
            other = other.values.toList(),
        )
    }

    private fun objectValue(source: JSONObject, key: String): String {
        val raw = source.opt(key) ?: return ""
        if (raw == JSONObject.NULL || raw is JSONObject) return ""
        return raw.toString().trim()
    }

    /**
     * Formatting aliases are accepted only when every populated alias agrees.
     * A canonical value always wins, while a conflicting alias is ignored
     * instead of being surfaced as a second, contradictory catalog field.
     */
    private fun consistentAliasValue(
        primary: JSONObject,
        extra: JSONObject?,
        keys: List<String>,
    ): String {
        val canonical = sequenceOf(primary, extra)
            .filterNotNull()
            .map { objectValue(it, keys.first()) }
            .firstOrNull { it.isNotEmpty() }
            .orEmpty()
        val aliases = keys.drop(1).flatMap { key ->
            sequenceOf(primary, extra)
                .filterNotNull()
                .map { objectValue(it, key) }
                .filter { it.isNotEmpty() }
                .toList()
        }
        if (canonical.isNotEmpty()) {
            return canonical
        }
        if (aliases.isEmpty()) return ""
        val expected = normalizeAliasValue(aliases.first())
        return aliases.first().takeIf { aliases.all { normalizeAliasValue(it) == expected } }.orEmpty()
    }

    private fun normalizeAliasValue(value: String): String =
        value.trim().replace(Regex("\\s+"), " ").lowercase()

    private fun normalizeComparableTitle(value: String): String = value.lowercase()
        .replace(Regex("[^\\p{L}\\p{N}]+"), " ")
        .trim()
        .replace(Regex("\\s+"), " ")

    private fun isHiddenTransportKey(key: String): Boolean =
        key in hiddenTransportKeys || key.startsWith("_")

    internal fun humanize(key: String): String = key
        .replace(Regex("([a-z0-9])([A-Z])"), "$1 $2")
        .replace('_', ' ')
        .replace('-', ' ')
        .trim()
        .split(Regex("\\s+"))
        .filter { it.isNotEmpty() }
        .joinToString(" ") { word ->
            if (word.equals("ocr", ignoreCase = true) || word.equals("isbn", ignoreCase = true)) {
                word.uppercase()
            } else {
                word.lowercase().replaceFirstChar { it.titlecase() }
            }
        }
}
