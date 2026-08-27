package org.whl.bookcapture

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanPrioritySurfaceResourceTest {

    @Test
    fun `archive overlays priority on cover and binds classification`() {
        val layout = File("src/main/res/layout/item_archive.xml").readText()
        val source = File("src/main/java/org/whl/bookcapture/ArchiveActivity.kt").readText()

        assertTrue(layout.contains("android:id=\"@+id/archiveThumbFrame\""))
        assertTrue(layout.contains("layout=\"@layout/view_scan_priority_indicator\""))
        assertTrue(layout.contains("android:background=\"@drawable/whl_row\""))
        assertTrue(source.contains("bookView = row.root"))
        assertTrue(source.contains("candidate = entryScanCandidate(this, entry)"))
        assertTrue(source.contains("entry.desktopBook?.scanPriorityRank"))
        assertTrue(source.contains("scanPriority.accessibilityLabel"))
    }

    @Test
    fun `last book preview overlays priority and clears stale state`() {
        val layout = File("src/main/res/layout/activity_main.xml").readText()
        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()

        assertTrue(layout.contains("android:id=\"@+id/lastBookThumbFrame\""))
        assertTrue(layout.contains("layout=\"@layout/view_scan_priority_indicator\""))
        assertTrue(layout.contains("android:background=\"@drawable/whl_inspect_panel\""))
        assertTrue(source.contains("bookView = binding.lastBookPreview"))
        assertTrue(source.contains("candidate = entryScanCandidate(this, entry)"))
        assertTrue(source.contains("candidate = null"))
        assertTrue(source.contains("rank = null"))
        assertTrue(source.contains("scanPriority.accessibilityLabel"))
    }

    @Test
    fun `detail shows one header indicator and priority in digitization field`() {
        val layout = File("src/main/res/layout/activity_entry_detail.xml").readText()
        val source = File("src/main/java/org/whl/bookcapture/EntryDetailActivity.kt").readText()

        assertTrue(layout.contains("android:id=\"@+id/detailBookSummary\""))
        assertEquals(
            1,
            "layout=\"@layout/view_scan_priority_indicator\"".toRegex().findAll(layout).count(),
        )
        assertTrue(layout.contains("android:background=\"@drawable/whl_inspect_panel\""))
        assertTrue(source.contains("bookView = binding.detailBookSummary"))
        assertTrue(source.contains("R.string.scan_priority_description"))
        assertTrue(source.contains("scanPriorityPresentation("))
        assertTrue(source.contains("R.string.scan_priority_unset"))
    }

    @Test
    fun `candidate highlighting is visual metadata rather than selection state`() {
        val binder = File(
            "src/main/java/org/whl/bookcapture/ScanPriorityIndicator.kt",
        ).readText()
        assertTrue(binder.contains("R.drawable.whl_scan_candidate_foreground"))
        assertTrue(!binder.contains("bookView.isSelected"))
        for (drawable in listOf("whl_row", "whl_inspect_row", "whl_inspect_panel")) {
            assertTrue(!File("src/main/res/drawable/$drawable.xml").readText()
                .contains("state_selected"))
        }
    }
}
