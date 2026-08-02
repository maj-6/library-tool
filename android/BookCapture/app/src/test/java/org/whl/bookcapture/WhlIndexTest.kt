package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

class WhlIndexTest {

    private val bundled: WhlIndex by lazy {
        val asset = File("src/main/assets/whl_index.json")
        assertTrue(
            "whl_index.json missing — regenerate with tools/build_whl_index.py",
            asset.isFile,
        )
        WhlIndex.parse(asset.readText())
    }

    @Test
    fun bundledIndexIsPlainCurrentAndSearchable() {
        val assets = File("src/main/assets")
        assertTrue(
            "WHL index must be plain JSON; Android may strip a .gz suffix",
            assets.listFiles { file ->
                file.name.startsWith("whl_index") && file.name.endsWith(".gz")
            }.isNullOrEmpty(),
        )
        assertTrue("expected the full WHL export, got ${bundled.size}", bundled.size > 5_000)
        assertEquals(12, bundled.config.bucketChars)
        assertTrue(bundled.config.fullStrict > bundled.config.fullMin)

        val source = File("../../../whl_catalog.csv")
        assertTrue("tracked whl_catalog.csv is missing", source.isFile)
        assertEquals(canonicalSourceSha256(source.readBytes()), bundled.sourceSha256)

        val match = bundled.match("A modern herbal", "M. Grieve")
        assertEquals(WhlCatalogFlag.YES, match.flag)
        assertNotNull(match.candidate)
        assertEquals("publish", match.candidate?.status)
        assertTrue(match.candidate!!.score > 0.9)
    }

    @Test
    fun publishedMatchBeatsACloserDraft() {
        val index = syntheticIndex(
            Row(
                title = "A Modern Herbal of England",
                author = "Maud Grieve",
                status = "draft",
                permalink = "https://example.test/draft",
            ),
            Row(
                title = "A Modern Herbal of the Scottish Isles",
                author = "Grieve, M.",
                status = "publish",
                permalink = "https://example.test/published",
            ),
        )

        val match = index.match("A Modern Herbal of England", "Maud Grieve")
        assertEquals(WhlCatalogFlag.YES, match.flag)
        assertEquals("https://example.test/published", match.candidate?.permalink)
        assertTrue("published row should be the deliberately weaker match", match.candidate!!.score < 1.0)
    }

    @Test
    fun closestDraftIsReturnedWhenNothingPublishedMatches() {
        val index = syntheticIndex(
            Row(
                title = "A Modern Herbal of the Scottish Isles",
                author = "Grieve, M.",
                status = "draft",
                permalink = "https://example.test/weaker",
            ),
            Row(
                title = "A Modern Herbal of England",
                author = "Maud Grieve",
                status = "draft",
                permalink = "https://example.test/exact",
            ),
        )

        val match = index.match("A Modern Herbal of England", "Maud Grieve")
        assertEquals(WhlCatalogFlag.DRAFT, match.flag)
        assertEquals("https://example.test/exact", match.candidate?.permalink)
        assertEquals(1.0, match.candidate?.score ?: -1.0, 0.0)
    }

    @Test
    fun unrelatedAndEmptyQueriesReturnNoCandidate() {
        val index = syntheticIndex(
            Row("A Modern Herbal", "Maud Grieve", "publish", "https://example.test/book"),
        )
        val unrelated = index.match("Moby Dick", "Herman Melville")
        assertEquals(WhlCatalogFlag.NO, unrelated.flag)
        assertNull(unrelated.candidate)

        val empty = index.match("", "")
        assertEquals(WhlCatalogFlag.NO, empty.flag)
        assertNull(empty.candidate)
    }

    @Test
    fun sourceFingerprintIgnoresOnlyLineEndingDifferences() {
        val lf = "Title,Authors\nHerbal,Ada\n".toByteArray()
        val crlf = "Title,Authors\r\nHerbal,Ada\r\n".toByteArray()
        val bareCr = "Title,Authors\nHerbal,Ad\ra\n".toByteArray()
        val changed = "Title,Authors\nHerbal,Grace\n".toByteArray()

        val expected = canonicalSourceSha256(lf)
        assertEquals(expected, canonicalSourceSha256(crlf))
        assertTrue(expected != canonicalSourceSha256(bareCr))
        assertTrue(expected != canonicalSourceSha256(changed))
    }

    private data class Row(
        val title: String,
        val author: String,
        val status: String,
        val permalink: String,
        val year: String = "",
    )

    private fun syntheticIndex(vararg rows: Row): WhlIndex {
        val stop = setOf(
            "and", "anon", "anonymous", "author", "by", "co", "company", "dr",
            "edited", "editor", "esq", "inc", "jr", "md", "mr", "mrs", "new",
            "of", "phd", "press", "prof", "sir", "sons", "sr", "the", "unknown",
            "various",
        )
        val byAuthor = linkedMapOf<String, MutableList<Int>>()
        val byTitle = linkedMapOf<String, MutableList<Int>>()
        val entries = JSONArray()
        rows.forEachIndexed { position, row ->
            entries.put(
                JSONObject()
                    .put("i", position)
                    .put("t", row.title)
                    .put("a", row.author)
                    .put("y", row.year)
                    .put("s", row.status)
                    .put("u", row.permalink),
            )
            ChMatcher.authorTokens(row.author, stop).forEach { token ->
                byAuthor.getOrPut(token) { mutableListOf() }.add(position)
            }
            val bucket = ChMatcher.normalize(row.title).take(12)
            if (bucket.isNotEmpty()) byTitle.getOrPut(bucket) { mutableListOf() }.add(position)
        }
        fun postings(source: Map<String, List<Int>>): JSONObject = JSONObject().apply {
            source.forEach { (key, positions) ->
                put(key, JSONArray().apply { positions.forEach { position -> put(position) } })
            }
        }

        val root = JSONObject()
            .put("version", 1)
            .put("source_sha256", "test")
            .put("bucket_chars", 12)
            .put("title_prefix", 16)
            .put(
                "thresholds",
                JSONObject()
                    .put("prefix_min", 0.72)
                    .put("full_min", 0.62)
                    .put("full_missing", 0.82)
                    .put("full_strict", 0.90),
            )
            .put(
                "author_stop",
                JSONArray().apply { stop.sorted().forEach { token -> put(token) } },
            )
            .put("entries", entries)
            .put("by_author", postings(byAuthor))
            .put("by_title", postings(byTitle))
        return WhlIndex.parse(root.toString())
    }

    private fun canonicalSourceSha256(source: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256")
        var index = 0
        while (index < source.size) {
            if (source[index] == '\r'.code.toByte()) {
                if (
                    index + 1 < source.size &&
                    source[index + 1] == '\n'.code.toByte()
                ) {
                    digest.update('\n'.code.toByte())
                    index += 2
                } else {
                    digest.update(source[index])
                    index += 1
                }
            } else {
                digest.update(source[index])
                index += 1
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
