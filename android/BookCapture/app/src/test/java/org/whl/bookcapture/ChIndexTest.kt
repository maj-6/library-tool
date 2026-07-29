package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Exercises the index that actually ships in the APK.
 *
 * The conformance test proves the matcher agrees with Python on synthetic
 * pairs; this proves the bundled asset parses, carries the merge payload, and
 * returns the right CH row for a realistic scan. Without it, a regenerated or
 * truncated asset would only be caught on a device.
 */
class ChIndexTest {

    private val index: ChIndex by lazy {
        val asset = File("src/main/assets/ch_index.json")
        assertTrue(
            "ch_index.json missing — regenerate with tools/build_ch_index.py",
            asset.isFile,
        )
        ChIndex.parse(asset.readText())
    }

    /**
     * The asset must be plain JSON, not gzipped.
     *
     * The Android build un-gzips a `.gz` asset and packages it under the
     * stripped name, so a `ch_index.json.gz` in the source tree becomes
     * `assets/ch_index.json` in the APK — and the app's open() of the `.gz`
     * name throws FileNotFoundException on device while every host-side test
     * still passes, because they read the source tree rather than the APK.
     * That is exactly how this shipped broken once.
     */
    @Test
    fun assetIsPlainJsonSoTheApkEntryNameMatchesWhatTheAppOpens() {
        val assets = File("src/main/assets")
        val gz = assets.listFiles { f: File -> f.name.startsWith("ch_index") && f.name.endsWith(".gz") }
        assertTrue(
            "ch_index must ship uncompressed: the build would rename ${gz?.firstOrNull()?.name} " +
                "to ch_index.json in the APK and ChIndex.open() would miss it",
            gz.isNullOrEmpty(),
        )
        assertTrue(File(assets, "ch_index.json").isFile)
    }

    @Test
    fun bundledIndexCoversTheWholeList() {
        assertTrue("expected the full CH list, got ${index.size}", index.size > 5000)
        assertEquals(12, index.config.bucketChars)
        assertTrue(index.config.authorStop.contains("anonymous"))
        // Thresholds must arrive from the desktop, not default to zero, or
        // every candidate would be accepted.
        assertTrue(index.config.prefixMin > 0.5)
        assertTrue(index.config.fullStrict > index.config.fullMin)
    }

    @Test
    fun findsAKnownRowFromAnExactTitle() {
        val match = index.bestMatch("Dr Kings Domestic Medicines and Hydropathy", "Anonymous")
        assertNotNull("expected a match for a verbatim CH title", match)
        assertTrue(
            "unexpected title: ${match!!.title}",
            match.title.contains("Domestic Medicines", ignoreCase = true),
        )
        assertTrue("score should be near-exact, was ${match.score}", match.score > 0.9)
    }

    @Test
    fun matchCarriesTheMergePayload() {
        val match = index.bestMatch("Dr Kings Domestic Medicines and Hydropathy", "Anonymous")
        val fields = requireNotNull(match).fields
        // The merge is the point of approving; an empty payload would make it
        // a no-op while still looking like it worked.
        assertTrue("expected merge fields, got $fields", fields.size >= 4)
        assertEquals("1860", fields["year"])
        assertTrue(fields.containsKey("publisher"))
        // CH's acquisition date is about CH's purchase, not the book.
        assertTrue("date must not be offered for merge", !fields.containsKey("date"))
    }

    @Test
    fun toleratesTitleDriftFromDictation() {
        // A leading article dropped and case flattened is the common shape of
        // a dictated title; it must still find the row.
        val match = index.bestMatch("dr kings domestic medicines and hydropathy", "")
        assertNotNull(match)
    }

    @Test
    fun refusesAnUnrelatedBook() {
        val match = index.bestMatch(
            "Zzz Nonexistent Treatise On Absolutely Nothing At All",
            "Nobody, Q.",
        )
        assertNull("an unrelated title must not match", match)
    }

    @Test
    fun emptyQueryDoesNotMatch() {
        assertNull(index.bestMatch("", ""))
    }

    @Test
    fun keysAreStableAcrossParsesAndDistinctBetweenRows() {
        val a = index.bestMatch("Dr Kings Domestic Medicines and Hydropathy", "Anonymous")
        val again = ChIndex.parse(File("src/main/assets/ch_index.json").readText())
            .bestMatch("Dr Kings Domestic Medicines and Hydropathy", "Anonymous")
        // The key is what a stored decision is matched against later, so it has
        // to survive a reload of the same data.
        assertEquals(requireNotNull(a).key, requireNotNull(again).key)

        val other = index.bestMatch("Kaya an ethnobotanical perspective", "Anonymous")
        if (other != null) assertTrue("distinct rows must not share a key", other.key != a.key)
    }
}
