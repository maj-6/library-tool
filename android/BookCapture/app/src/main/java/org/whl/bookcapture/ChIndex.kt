package org.whl.bookcapture

import android.content.Context
import android.util.Log
import org.json.JSONObject

/**
 * The CH master list, bundled in the APK and searched on-device.
 *
 * Held as parallel arrays rather than a list of objects: 5264 rows is enough
 * that per-row object overhead is worth avoiding on a phone, and the search
 * only ever touches the normalized title until it has a candidate worth
 * scoring. The merge payload stays as raw JSON until a row actually matches,
 * which is at most a handful per scan.
 *
 * Loading is explicitly NOT done in a constructor or an init block — it reads
 * and inflates ~2.7 MB of JSON and must never land on the main thread.
 */
class ChIndex private constructor(
    private val titles: Array<String>,
    private val authors: Array<String>,
    private val years: Array<String>,
    private val normTitles: Array<String>,
    private val fields: Array<JSONObject?>,
    private val byAuthor: Map<String, IntArray>,
    private val byTitle: Map<String, IntArray>,
    val config: ChIndexConfig,
) {

    val size: Int get() = titles.size

    /**
     * Candidate row positions for a query, mirroring catalog_checks._candidates:
     * anything sharing a surname token or the title's leading bucket.
     */
    private fun candidates(title: String, author: String): Set<Int> {
        val out = LinkedHashSet<Int>()
        for (token in ChMatcher.authorTokens(author, config.authorStop)) {
            byAuthor[token]?.forEach { out.add(it) }
        }
        val bucket = ChMatcher.normalize(title).take(config.bucketChars)
        if (bucket.isNotEmpty()) byTitle[bucket]?.forEach { out.add(it) }
        return out
    }

    /**
     * The best CH row for a scanned book, or null when nothing clears the
     * desktop's acceptance test.
     *
     * Ranking among accepted rows is by full-title similarity. Ties are left to
     * index order, which is CH's own row order — arbitrary, but stable, so the
     * same scan does not propose a different row on a re-run.
     */
    fun bestMatch(title: String, author: String): ChCandidate? {
        if (title.isBlank() && author.isBlank()) return null
        var best: ChCandidate? = null
        var bestScore = -1.0
        for (position in candidates(title, author)) {
            if (!ChMatcher.titleAuthorMatch(
                    title, author, titles[position], authors[position], config,
                )
            ) continue
            val score = ChMatcher.similarity(title, titles[position])
            if (score > bestScore) {
                bestScore = score
                best = ChCandidate(
                    key = keyFor(position),
                    title = titles[position],
                    author = authors[position],
                    year = years[position],
                    score = score,
                    fields = fieldsAt(position),
                )
            }
        }
        return best
    }

    /**
     * A content-derived key, NOT the row position: the CH sheet gets reordered,
     * and a stored position would silently start pointing at a different book.
     */
    private fun keyFor(position: Int): String {
        val basis = normTitles[position] + "" +
            ChMatcher.normalize(ChMatcher.flipAuthor(authors[position])) + "" +
            years[position]
        return Integer.toHexString(basis.hashCode()) + "-" + basis.length
    }

    private fun fieldsAt(position: Int): Map<String, String> {
        val json = fields[position] ?: return emptyMap()
        val out = LinkedHashMap<String, String>(json.length())
        val keys = json.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val value = json.optString(key).trim()
            if (value.isNotEmpty()) out[key] = value
        }
        return out
    }

    companion object {
        /**
         * Plain JSON, deliberately NOT ch_index.json.gz.
         *
         * The Android build un-gzips a `.gz` asset and packages it under the
         * stripped name, so shipping the gzip would put `ch_index.json` in the
         * APK and this open() would throw FileNotFoundException on device —
         * while every host-side test still passed, because they read the source
         * tree. The APK's own zip deflates this to ~640 KB, the same size the
         * gzip was, so there is nothing to win by pre-compressing it.
         */
        private const val ASSET = "ch_index.json"
        private const val TAG = "ChIndex"

        @Volatile private var cached: ChIndex? = null
        @Volatile private var failed = false

        /**
         * The loaded index, or null when it is unavailable.
         *
         * A failure is cached too: a corrupt or absent asset will not recover by
         * being retried once per scan, and the CH indicator simply stays hidden.
         * Callers must be on a background thread.
         */
        fun get(context: Context): ChIndex? {
            cached?.let { return it }
            if (failed) return null
            synchronized(this) {
                cached?.let { return it }
                if (failed) return null
                return runCatching { load(context) }
                    .onFailure {
                        failed = true
                        Log.w(TAG, "CH index unavailable; indicator stays hidden", it)
                    }
                    .getOrNull()
                    ?.also { cached = it }
            }
        }

        private fun load(context: Context): ChIndex =
            parse(
                context.assets.open(ASSET).bufferedReader().use { it.readText() },
            )

        /** Visible for tests, so the actually-shipped asset can be parsed and
         *  searched off-device instead of only on an emulator. */
        internal fun parse(text: String): ChIndex {
            val root = JSONObject(text)

            val entries = root.getJSONArray("entries")
            val count = entries.length()
            val titles = Array(count) { "" }
            val authors = Array(count) { "" }
            val years = Array(count) { "" }
            val normTitles = Array(count) { "" }
            val fields = arrayOfNulls<JSONObject>(count)
            for (i in 0 until count) {
                val entry = entries.getJSONObject(i)
                titles[i] = entry.optString("t")
                authors[i] = entry.optString("a")
                years[i] = entry.optString("y")
                normTitles[i] = entry.optString("nt")
                fields[i] = entry.optJSONObject("f")
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
                    for (i in 0 until (stop?.length() ?: 0)) add(stop!!.getString(i))
                },
            )

            return ChIndex(
                titles, authors, years, normTitles, fields,
                postings(root.getJSONObject("by_author")),
                postings(root.getJSONObject("by_title")),
                config,
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
