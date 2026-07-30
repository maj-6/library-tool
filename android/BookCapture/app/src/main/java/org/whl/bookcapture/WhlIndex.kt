package org.whl.bookcapture

import android.content.Context
import android.util.Log
import org.json.JSONObject

/** The three answers returned by the desktop's `catalog_checks.whl_match`. */
enum class WhlCatalogFlag(val wireValue: String) {
    YES("yes"),
    DRAFT("draft"),
    NO("no"),
}

/** One accepted row from the bundled World Herb Library catalogue snapshot. */
data class WhlCandidate(
    /** Content-derived identity; the CSV row number is not stable across exports. */
    val key: String,
    val title: String,
    val author: String,
    val year: String,
    /** Raw normalized WordPress state (`publish` or `draft` in the current export). */
    val status: String,
    val permalink: String,
    val score: Double,
    /** Zero-based row in the source CSV, useful for diagnostics only. */
    val sourceIndex: Int,
)

data class WhlMatch(
    val flag: WhlCatalogFlag,
    val candidate: WhlCandidate? = null,
)

/**
 * The WHL catalogue snapshot bundled in the APK and searched on-device.
 *
 * Like [ChIndex], this keeps columns in parallel arrays and posting lists in
 * primitive arrays.  The asset has only about 5,500 records, so a one-time
 * background parse gives sublinear candidate lookup without adding a database
 * dependency or a first-run copy/install step.
 */
class WhlIndex private constructor(
    private val sourceIndexes: IntArray,
    private val titles: Array<String>,
    private val authors: Array<String>,
    private val years: Array<String>,
    private val statuses: Array<String>,
    private val permalinks: Array<String>,
    private val byAuthor: Map<String, IntArray>,
    private val byTitle: Map<String, IntArray>,
    val config: ChIndexConfig,
    val sourceSha256: String,
) {

    val size: Int get() = titles.size

    private fun candidates(title: String, author: String): Set<Int> {
        val out = LinkedHashSet<Int>()
        for (token in ChMatcher.authorTokens(author, config.authorStop)) {
            byAuthor[token]?.forEach { position -> out.add(position) }
        }
        val bucket = ChMatcher.normalize(title).take(config.bucketChars)
        if (bucket.isNotEmpty()) {
            byTitle[bucket]?.forEach { position -> out.add(position) }
        }
        return out
    }

    /**
     * Mirror `catalog_checks.whl_match` exactly:
     *
     * - accept rows with the shared title/author test;
     * - rank within a class by full-title similarity;
     * - prefer any published match over every non-published match, even when a
     *   draft has the higher similarity;
     * - return `draft` only when no published row matched.
     */
    fun match(title: String, author: String): WhlMatch {
        if (title.isBlank() && author.isBlank()) return WhlMatch(WhlCatalogFlag.NO)

        var bestPublished = -1
        var bestPublishedScore = 0.0
        var bestAny = -1
        var bestAnyScore = 0.0
        for (position in candidates(title, author)) {
            if (!ChMatcher.titleAuthorMatch(
                    title,
                    author,
                    titles[position],
                    authors[position],
                    config,
                )
            ) {
                continue
            }
            val score = ChMatcher.similarity(title, titles[position])
            if (score > bestAnyScore) {
                bestAny = position
                bestAnyScore = score
            }
            if (statuses[position] == PUBLISHED_STATUS && score > bestPublishedScore) {
                bestPublished = position
                bestPublishedScore = score
            }
        }

        return when {
            bestPublished >= 0 -> WhlMatch(
                WhlCatalogFlag.YES,
                candidateAt(bestPublished, bestPublishedScore),
            )
            bestAny >= 0 -> WhlMatch(
                WhlCatalogFlag.DRAFT,
                candidateAt(bestAny, bestAnyScore),
            )
            else -> WhlMatch(WhlCatalogFlag.NO)
        }
    }

    private fun candidateAt(position: Int, score: Double): WhlCandidate = WhlCandidate(
        key = keyFor(position),
        title = titles[position],
        author = authors[position],
        year = years[position],
        status = statuses[position],
        permalink = permalinks[position],
        score = score,
        sourceIndex = sourceIndexes[position],
    )

    /** Same content-key strategy as CH, extended with WHL status and permalink. */
    private fun keyFor(position: Int): String {
        val basis = buildString {
            append(ChMatcher.normalize(titles[position]))
            append(KEY_SEPARATOR)
            append(ChMatcher.normalize(ChMatcher.flipAuthor(authors[position])))
            append(KEY_SEPARATOR)
            append(years[position])
            append(KEY_SEPARATOR)
            append(statuses[position])
            append(KEY_SEPARATOR)
            append(permalinks[position])
        }
        return Integer.toHexString(basis.hashCode()) + "-" + basis.length
    }

    companion object {
        private const val ASSET = "whl_index.json"
        private const val TAG = "WhlIndex"
        private const val INDEX_VERSION = 1
        private const val PUBLISHED_STATUS = "publish"
        private const val KEY_SEPARATOR = '\u001f'

        @Volatile private var cached: WhlIndex? = null
        @Volatile private var failed = false

        /**
         * Load and cache the bundled index, or return null after an asset error.
         *
         * Parsing inflates the JSON and therefore belongs on a background
         * thread (the capture worker already provides one).
         */
        fun get(context: Context): WhlIndex? {
            cached?.let { return it }
            if (failed) return null
            synchronized(this) {
                cached?.let { return it }
                if (failed) return null
                return runCatching { load(context) }
                    .onFailure {
                        failed = true
                        Log.w(TAG, "WHL index unavailable; offline check skipped", it)
                    }
                    .getOrNull()
                    ?.also { cached = it }
            }
        }

        private fun load(context: Context): WhlIndex = parse(
            context.assets.open(ASSET).bufferedReader().use { it.readText() },
        )

        /** Parse is visible to JVM tests so they exercise the actual APK asset. */
        internal fun parse(text: String): WhlIndex {
            val root = JSONObject(text)
            require(root.getInt("version") == INDEX_VERSION) {
                "unsupported WHL index version"
            }
            val entries = root.getJSONArray("entries")
            val count = entries.length()
            val sourceIndexes = IntArray(count)
            val titles = Array(count) { "" }
            val authors = Array(count) { "" }
            val years = Array(count) { "" }
            val statuses = Array(count) { "" }
            val permalinks = Array(count) { "" }
            for (position in 0 until count) {
                val entry = entries.getJSONObject(position)
                sourceIndexes[position] = entry.optInt("i", position)
                titles[position] = entry.optString("t")
                authors[position] = entry.optString("a")
                years[position] = entry.optString("y")
                statuses[position] = entry.optString("s").trim().lowercase()
                permalinks[position] = entry.optString("u").trim()
            }

            val thresholds = root.getJSONObject("thresholds")
            val stop = root.optJSONArray("author_stop")
            val config = ChIndexConfig(
                bucketChars = root.getInt("bucket_chars"),
                titlePrefix = root.getInt("title_prefix"),
                prefixMin = thresholds.getDouble("prefix_min"),
                fullMin = thresholds.getDouble("full_min"),
                fullMissing = thresholds.getDouble("full_missing"),
                fullStrict = thresholds.getDouble("full_strict"),
                authorStop = buildSet {
                    for (index in 0 until (stop?.length() ?: 0)) add(stop!!.getString(index))
                },
            )
            return WhlIndex(
                sourceIndexes = sourceIndexes,
                titles = titles,
                authors = authors,
                years = years,
                statuses = statuses,
                permalinks = permalinks,
                byAuthor = postings(root.getJSONObject("by_author")),
                byTitle = postings(root.getJSONObject("by_title")),
                config = config,
                sourceSha256 = root.optString("source_sha256").trim(),
            )
        }

        private fun postings(source: JSONObject): Map<String, IntArray> {
            val out = HashMap<String, IntArray>(source.length() * 2)
            val keys = source.keys()
            while (keys.hasNext()) {
                val key = keys.next()
                val list = source.getJSONArray(key)
                out[key] = IntArray(list.length()) { list.getInt(it) }
            }
            return out
        }
    }
}
