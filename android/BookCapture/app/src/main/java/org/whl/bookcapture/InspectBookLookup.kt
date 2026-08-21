package org.whl.bookcapture

import java.text.Normalizer

/**
 * The small, presentation-independent record Inspect needs in order to locate a
 * physical book. Keeping lookup independent from Activity and disk types makes
 * the same ranking usable for typed queries and text read from a cover.
 */
internal data class InspectLookupBook(
    val entryId: String,
    val collectionId: String,
    val title: String,
    val author: String = "",
    val year: String = "",
    val createdAt: Long = 0L,
)

internal enum class InspectLookupMode {
    TYPED,
    COVER_OCR,
}

internal enum class InspectBookMatchKind {
    EXACT,
    SUBSTRING,
    TOKENS,
}

internal data class InspectLookupMatch(
    val book: InspectLookupBook,
    val score: Int,
    val kind: InspectBookMatchKind,
) {
    /** Compatibility-friendly name for ranking internals and tests. */
    val record: InspectLookupBook get() = book
}

internal typealias InspectBookLookupRecord = InspectLookupBook
internal typealias InspectBookLookupMatch = InspectLookupMatch

/** Raw bibliography from one lookup source. UI placeholders never belong here. */
internal data class InspectLookupBibliography(
    val title: String = "",
    val author: String = "",
    val year: String = "",
)

/**
 * Combine the local/cached row with a freshly listed cloud row.
 *
 * The fresh row owns mutable collection membership. Bibliography remains
 * field-wise conservative: an explicit current desktop value wins, then an
 * existing non-blank capture value, while fresh cloud metadata fills blanks.
 */
internal fun mergeInspectLookupBookSources(
    entryId: String,
    existingCollectionId: String,
    existing: InspectLookupBibliography,
    currentDesktop: InspectLookupBibliography = InspectLookupBibliography(),
    freshCloudCollectionId: String = "",
    freshCloud: InspectLookupBibliography = InspectLookupBibliography(),
    createdAt: Long = 0L,
): InspectLookupBook = InspectLookupBook(
    entryId = entryId,
    collectionId = freshCloudCollectionId.ifBlank { existingCollectionId },
    title = currentDesktop.title.ifBlank { existing.title.ifBlank { freshCloud.title } },
    author = currentDesktop.author.ifBlank { existing.author.ifBlank { freshCloud.author } },
    year = currentDesktop.year.ifBlank { existing.year.ifBlank { freshCloud.year } },
    createdAt = createdAt,
)

internal enum class InspectLookupStatusKind {
    SEARCHING_CLOUD,
    NO_MATCHES_CLOUD_UNAVAILABLE,
    NO_MATCHES,
    MATCHES_CLOUD_UNAVAILABLE,
    AMBIGUOUS_MATCHES,
    MATCHES_SEARCHING_CLOUD,
    MATCHES,
}

/** Keep partial cloud failures visible even when the phone already knows a match. */
internal fun inspectLookupStatusKind(
    matchCount: Int,
    ambiguous: Boolean,
    cloudLoading: Boolean,
    cloudFailed: Boolean,
): InspectLookupStatusKind = when {
    matchCount <= 0 && cloudLoading -> InspectLookupStatusKind.SEARCHING_CLOUD
    matchCount <= 0 && cloudFailed -> InspectLookupStatusKind.NO_MATCHES_CLOUD_UNAVAILABLE
    matchCount <= 0 -> InspectLookupStatusKind.NO_MATCHES
    cloudFailed -> InspectLookupStatusKind.MATCHES_CLOUD_UNAVAILABLE
    ambiguous -> InspectLookupStatusKind.AMBIGUOUS_MATCHES
    cloudLoading -> InspectLookupStatusKind.MATCHES_SEARCHING_CLOUD
    else -> InspectLookupStatusKind.MATCHES
}

internal enum class InspectLookupCloudCompletion {
    ACCEPT,
    IGNORE_RETIRED_GENERATION,
    RESET_STALE_OWNER,
}

/** A retired request must not clear a newer load; a live request must not publish across owners. */
internal fun inspectLookupCloudCompletion(
    requestGeneration: Long,
    currentGeneration: Long,
    requestOwner: String,
    currentOwner: String,
    signedIn: Boolean,
): InspectLookupCloudCompletion = when {
    requestGeneration != currentGeneration ->
        InspectLookupCloudCompletion.IGNORE_RETIRED_GENERATION
    !signedIn || requestOwner != currentOwner ->
        InspectLookupCloudCompletion.RESET_STALE_OWNER
    else -> InspectLookupCloudCompletion.ACCEPT
}

internal data class InspectBookLookupResult(
    val matches: List<InspectLookupMatch> = emptyList(),
) {
    val bestMatch: InspectLookupMatch? get() = matches.firstOrNull()

    /**
     * More than one equally plausible *collection* is ambiguous. Duplicate
     * captures in one box still answer the user's physical-location question.
     */
    val topCollectionIds: Set<String>
        get() {
            val topScore = matches.firstOrNull()?.score ?: return emptySet()
            return matches.asSequence()
                .takeWhile { it.score == topScore }
                .map { it.record.collectionId }
                .toCollection(linkedSetOf())
        }

    val ambiguous: Boolean get() = topCollectionIds.size > 1
}

/** Accent-folded, punctuation-insensitive normalization for typed and OCR text. */
internal fun normalizeInspectBookLookupText(value: String?): String {
    if (value.isNullOrBlank()) return ""
    val decomposed = Normalizer.normalize(value, Normalizer.Form.NFKD)
    val out = StringBuilder(decomposed.length)
    var pendingSpace = false
    decomposed.forEach { raw ->
        val type = Character.getType(raw)
        if (type == Character.NON_SPACING_MARK.toInt() ||
            type == Character.COMBINING_SPACING_MARK.toInt() ||
            type == Character.ENCLOSING_MARK.toInt()
        ) {
            return@forEach
        }
        val ch = raw.lowercaseChar()
        if (ch in 'a'..'z' || ch in '0'..'9') {
            if (pendingSpace && out.isNotEmpty()) out.append(' ')
            pendingSpace = false
            out.append(ch)
        } else {
            pendingSpace = true
        }
    }
    return out.toString()
}

/** Pure ranking used by Home after it has assembled local and cached books. */
internal object InspectBookLookup {
    private const val DEFAULT_LIMIT = 12
    private const val EXACT_SCORE = 10_000
    private const val SUBSTRING_SCORE = 8_000
    private const val TOKEN_SCORE = 5_000

    private val insignificantTokens = setOf(
        "a", "an", "and", "by", "for", "from", "in", "of", "on", "or", "the", "to",
    )

    /**
     * Match a book name typed by the operator. Exact normalized titles outrank
     * phrase containment, which in turn outranks unordered significant tokens.
     */
    fun byTypedName(
        query: String,
        records: Collection<InspectLookupBook>,
        limit: Int = DEFAULT_LIMIT,
    ): InspectBookLookupResult {
        if (limit <= 0) return InspectBookLookupResult()
        val normalizedQuery = normalizeInspectBookLookupText(query)
        if (normalizedQuery.isEmpty()) return InspectBookLookupResult()
        val queryTokens = significantTokens(normalizedQuery)

        val matches = usableRecords(records).mapNotNull { record ->
            val title = normalizeInspectBookLookupText(record.title)
            val author = normalizeInspectBookLookupText(record.author)
            val year = normalizeInspectBookLookupText(record.year)
            val (kind, base) = when {
                normalizedQuery == title -> InspectBookMatchKind.EXACT to EXACT_SCORE
                containsPhrase(title, normalizedQuery) -> {
                    val prefixBonus = if (title.startsWith(normalizedQuery)) 250 else 0
                    InspectBookMatchKind.SUBSTRING to SUBSTRING_SCORE + prefixBonus
                }
                containsPhrase(normalizedQuery, title) ->
                    InspectBookMatchKind.SUBSTRING to SUBSTRING_SCORE
                else -> {
                    val titleTokens = significantTokens(title)
                    val overlap = queryTokens.intersect(titleTokens)
                    if (!acceptTypedTokenMatch(queryTokens, titleTokens, overlap)) {
                        return@mapNotNull null
                    }
                    val coverage = overlap.size * 1_000 / titleTokens.size.coerceAtLeast(1)
                    InspectBookMatchKind.TOKENS to TOKEN_SCORE + coverage
                }
            }
            InspectLookupMatch(
                book = record,
                score = base + evidenceBonus(normalizedQuery, author, year),
                kind = kind,
            )
        }
        return result(matches, limit)
    }

    /**
     * Rank records against unstructured Latin text read from a photographed
     * cover. A title must be present as a phrase or contribute enough meaningful
     * tokens; author/year evidence may disambiguate but can never create a match.
     */
    fun byCoverOcr(
        recognizedText: String,
        records: Collection<InspectLookupBook>,
        limit: Int = DEFAULT_LIMIT,
    ): InspectBookLookupResult {
        if (limit <= 0) return InspectBookLookupResult()
        val normalizedText = normalizeInspectBookLookupText(recognizedText)
        val ocrTokens = significantTokens(normalizedText)
        if (normalizedText.length < MINIMUM_OCR_CHARS || ocrTokens.isEmpty()) {
            return InspectBookLookupResult()
        }
        val normalizedLines = recognizedText.lineSequence()
            .map(::normalizeInspectBookLookupText)
            .filter(String::isNotEmpty)
            .toSet()

        val matches = usableRecords(records).mapNotNull { record ->
            val title = normalizeInspectBookLookupText(record.title)
            val titleTokens = significantTokens(title)
            if (!strongEnoughTitle(title, titleTokens)) return@mapNotNull null

            val (kind, base) = when {
                title in normalizedLines || normalizedText == title ->
                    InspectBookMatchKind.EXACT to EXACT_SCORE
                containsPhrase(normalizedText, title) ->
                    InspectBookMatchKind.SUBSTRING to SUBSTRING_SCORE
                else -> {
                    val overlap = titleTokens.intersect(ocrTokens)
                    if (!acceptCoverTokenMatch(titleTokens, overlap)) return@mapNotNull null
                    val coverage = overlap.size * 1_000 / titleTokens.size.coerceAtLeast(1)
                    InspectBookMatchKind.TOKENS to TOKEN_SCORE + coverage
                }
            }
            InspectLookupMatch(
                book = record,
                score = base + evidenceBonus(
                    normalizedText,
                    normalizeInspectBookLookupText(record.author),
                    normalizeInspectBookLookupText(record.year),
                ),
                kind = kind,
            )
        }
        return result(matches, limit)
    }

    private fun usableRecords(
        records: Collection<InspectLookupBook>,
    ): Sequence<InspectLookupBook> = records.asSequence()
        .filter {
            it.entryId.isNotBlank() && it.collectionId.isNotBlank() &&
                normalizeInspectBookLookupText(it.title).isNotEmpty()
        }
        .distinctBy(InspectLookupBook::entryId)

    private fun result(
        matches: Sequence<InspectLookupMatch>,
        limit: Int,
    ): InspectBookLookupResult = InspectBookLookupResult(
        matches.sortedWith(
            compareByDescending<InspectLookupMatch> { it.score }
                .thenBy { normalizeInspectBookLookupText(it.record.title) }
                .thenBy { normalizeInspectBookLookupText(it.record.author) }
                .thenBy { it.record.year }
                .thenByDescending { it.record.createdAt }
                .thenBy { it.record.entryId },
        ).take(limit).toList(),
    )

    private fun significantTokens(normalized: String): Set<String> = normalized
        .split(' ')
        .asSequence()
        .filter { token ->
            token.isNotEmpty() && token !in insignificantTokens &&
                (token.length >= 3 || token.all(Char::isDigit))
        }
        .toCollection(linkedSetOf())

    private fun containsPhrase(haystack: String, needle: String): Boolean =
        needle.isNotEmpty() && " $haystack ".contains(" $needle ")

    private fun acceptTypedTokenMatch(
        queryTokens: Set<String>,
        titleTokens: Set<String>,
        overlap: Set<String>,
    ): Boolean {
        if (queryTokens.isEmpty() || titleTokens.isEmpty() || overlap.isEmpty()) return false
        if (queryTokens.size == 1) return overlap.single().length >= 4
        val queryCoverage = overlap.size.toDouble() / queryTokens.size
        val titleCoverage = overlap.size.toDouble() / titleTokens.size
        return overlap.size >= 2 && queryCoverage >= 0.6 && titleCoverage >= 0.5
    }

    private fun acceptCoverTokenMatch(titleTokens: Set<String>, overlap: Set<String>): Boolean {
        if (titleTokens.isEmpty()) return false
        if (titleTokens.size == 1) return false // handled only by exact phrase evidence
        val coverage = overlap.size.toDouble() / titleTokens.size
        return overlap.size >= 2 && coverage >= 0.6
    }

    private fun strongEnoughTitle(title: String, titleTokens: Set<String>): Boolean =
        titleTokens.size >= 2 || (titleTokens.size == 1 && title.length >= 5)

    private fun evidenceBonus(text: String, author: String, year: String): Int {
        val authorTokens = significantTokens(author)
        val textTokens = significantTokens(text)
        val authorBonus = when {
            authorTokens.isEmpty() -> 0
            authorTokens.all { it in textTokens } -> 180
            authorTokens.any { it in textTokens } -> 60
            else -> 0
        }
        val yearBonus = if (year.length == 4 && year.all(Char::isDigit) &&
            containsPhrase(text, year)
        ) 40 else 0
        return authorBonus + yearBonus
    }

    private const val MINIMUM_OCR_CHARS = 4
}

/**
 * Stable, minimal API for HomeActivity. The caller owns membership resolution
 * and presentation; this function only returns ranked, bounded book matches.
 */
internal fun findInspectBookMatches(
    books: Collection<InspectLookupBook>,
    query: String,
    mode: InspectLookupMode = InspectLookupMode.TYPED,
    limit: Int = 20,
): List<InspectLookupMatch> = when (mode) {
    InspectLookupMode.TYPED -> InspectBookLookup.byTypedName(query, books, limit)
    InspectLookupMode.COVER_OCR -> InspectBookLookup.byCoverOcr(query, books, limit)
}.matches
