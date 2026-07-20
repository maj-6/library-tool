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
    fun collectionLabelsIncludeLiteralAndNestedParentLocations() {
        val paths = collectionDisplayPaths(
            listOf(
                BookCollection("building", "Building", "Campus"),
                BookCollection("office", "Office", "Annex", parentId = "building"),
                BookCollection("periodicals", "Periodicals", "Storage", parentId = "office"),
                BookCollection("drawer", "Loose leaves", "Archive > Drawer 4"),
            ),
        )

        assertEquals("Campus > Building", paths["building"])
        assertEquals("Campus > Building > Office", paths["office"])
        assertEquals("Campus > Building > Office > Periodicals", paths["periodicals"])
        assertEquals("Archive > Drawer 4 > Loose leaves", paths["drawer"])
        assertEquals(
            "Office > Periodicals",
            collectionDisplayLabel("Office", "Periodicals"),
        )
    }

    @Test
    fun missingParentsAndCyclesProduceFiniteUsefulPaths() {
        val paths = collectionDisplayPaths(
            listOf(
                BookCollection("a", "Alpha", "", parentId = "b"),
                BookCollection("b", "Beta", "", parentId = "a"),
                BookCollection("self", "Self", "Offsite", parentId = "self"),
                BookCollection("missing", "Pamphlets", "Offsite", parentId = "gone"),
                BookCollection("deleted", "Archive", "", deleted = true),
                BookCollection("child", "Maps", "Offsite", parentId = "deleted"),
            ),
        )

        assertEquals("Beta > Alpha", paths["a"])
        assertEquals("Alpha > Beta", paths["b"])
        assertEquals("Self", paths["self"])
        assertEquals("Pamphlets", paths["missing"])
        assertEquals("Maps", paths["child"])
        assertEquals("Archive", paths["deleted"])
    }

    @Test
    fun physicalLocationIsNeverInferredAsAParentIdentity() {
        val paths = collectionDisplayPaths(
            listOf(
                BookCollection("building", "Building", ""),
                BookCollection("office", "Office", "", parentId = "building"),
                BookCollection("periodicals", "Periodicals", "Office"),
            ),
        )

        assertEquals("Building > Office", paths["office"])
        assertEquals("Office > Periodicals", paths["periodicals"])
    }

    @Test
    fun mergedParentIdentityFollowsItsLiveSurvivor() {
        val paths = collectionDisplayPaths(
            listOf(
                BookCollection("office", "Office", ""),
                BookCollection(
                    "old-office",
                    "Old office",
                    "",
                    deleted = true,
                    mergedInto = "office",
                ),
                BookCollection("periodicals", "Periodicals", "", parentId = "old-office"),
            ),
        )

        assertEquals("Office > Periodicals", paths["periodicals"])
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

    @Test
    fun regularTapReplacesAnyExistingSelectionWithOneScan() {
        val selected = linkedSetOf("one", "two")

        assertEquals(linkedSetOf("three"), replaceScanSelection(selected, "three"))
        assertEquals(linkedSetOf("one", "two"), selected)
    }

    @Test
    fun longPressTogglesOneScanWithoutClearingOtherSelections() {
        assertEquals(
            linkedSetOf("one", "two", "three"),
            toggleScanSelectionAdditively(linkedSetOf("one", "two"), "three"),
        )
        assertEquals(
            linkedSetOf("two"),
            toggleScanSelectionAdditively(linkedSetOf("one", "two"), "one"),
        )
    }

    @Test
    fun completeTextIsSuppressedAndDeliveryUsesACloudIcon() {
        assertEquals(HomeStatusPresentation(), homeStatusPresentation("complete"))
        assertEquals(
            HomeStatusAdornment.UPLOADED,
            homeStatusPresentation("complete · uploaded").adornment,
        )
        assertEquals("", homeStatusPresentation("complete · uploaded").text)
        assertEquals(
            HomeStatusAdornment.UPLOADED,
            homeStatusPresentation("imported").adornment,
        )
    }

    @Test
    fun pendingWorkUsesTheAnimatedWaitingAdornmentWithoutPendingText() {
        for (status in listOf(
            "waiting",
            "processing",
            "complete · pending upload",
            "complete · pending delivery",
            "complete · claim for cloud",
        )) {
            val presentation = homeStatusPresentation(status)
            assertEquals(status, HomeStatusAdornment.WAITING, presentation.adornment)
            assertEquals(status, "", presentation.text)
        }
        assertEquals(
            "different account",
            homeStatusPresentation("complete · different account").text,
        )
    }

    @Test
    fun aboutChangelogMarkdownIsCompactAndReadable() {
        val rendered = formatChangelogForAbout(
            "# Android Changelog\n\n## 0.5.1\n\n### Fixes\n\n- One\n- Two",
        )

        assertEquals(
            "Android Changelog\n\n0.5.1\n\nFixes\n\n• One\n• Two",
            rendered,
        )
    }
}
