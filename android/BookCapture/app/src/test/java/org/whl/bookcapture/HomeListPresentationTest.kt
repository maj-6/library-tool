package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class HomeListPresentationTest {

    private data class Scan(val id: String, val collectionId: String?, val collection: String)

    private fun groups(scans: List<Scan>, current: String? = null) =
        groupScansByCollection(
            items = scans,
            currentCollectionId = current,
            collectionId = Scan::collectionId,
            collectionLabel = Scan::collection,
            unfiledLabel = "No collection",
        )

    @Test
    fun scansAreGroupedByFrozenCollectionWithCurrentFirst() {
        val grouped = groups(
            listOf(
                Scan("new-a", "a", "Shelf A"),
                Scan("new-b", "b", "Shelf B"),
                Scan("old-a", "a", "Shelf A"),
            ),
            current = "b",
        )

        assertEquals(listOf("b", "a"), grouped.map { it.key })
        assertEquals(listOf("new-b"), grouped[0].items.map { it.id })
        assertEquals(listOf("new-a", "old-a"), grouped[1].items.map { it.id })
    }

    @Test
    fun legacyScansShareAnExplicitUnfiledGroup() {
        val grouped = groups(
            listOf(
                Scan("legacy-1", null, ""),
                Scan("legacy-2", "  ", ""),
            ),
        )

        assertEquals(1, grouped.size)
        assertEquals(UNFILED_SCAN_GROUP, grouped.single().key)
        assertEquals("No collection", grouped.single().label)
    }

    @Test
    fun initialExpansionPrefersCurrentThenNewestAvailableGroup() {
        val grouped = groups(
            listOf(Scan("a", "a", "A"), Scan("b", "b", "B")),
        )
        assertEquals("b", initiallyExpandedScanGroup(grouped, "b"))
        assertEquals("a", initiallyExpandedScanGroup(grouped, "missing"))
        assertNull(initiallyExpandedScanGroup(emptyList<ScanCollectionGroup<Scan>>(), "a"))
    }

    @Test
    fun compactMetricsAreAboutFifteenPercentSmallerAndTwentyPercentTighter() {
        val standard = scanListLayoutMetrics(compact = false)
        val compact = scanListLayoutMetrics(compact = true)

        assertEquals(46, standard.thumbnailWidthDp)
        assertEquals(60, standard.thumbnailHeightDp)
        assertEquals(39, compact.thumbnailWidthDp)
        assertEquals(51, compact.thumbnailHeightDp)
        assertEquals(7, compact.rowVerticalPaddingDp)
        assertTrue(compact.thumbnailWidthDp.toDouble() / standard.thumbnailWidthDp in 0.84..0.86)
        assertTrue(compact.thumbnailHeightDp.toDouble() / standard.thumbnailHeightDp in 0.84..0.86)
        assertTrue(compact.rowVerticalPaddingDp.toDouble() / standard.rowVerticalPaddingDp in 0.75..0.82)
    }
}
