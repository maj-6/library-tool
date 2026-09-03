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
        assertTrue(source.contains("assessment = entry.desktopBook?.scanPriorityAssessment"))
        assertTrue(source.contains("scanPriority.accessibilityLabel"))
        assertTrue(source.contains("CaptureMetadataSyncWorker.WORK_NAME"))
        assertTrue(source.contains("CaptureMetadataSyncWorker.PULL_WORK_NAME"))
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
        assertTrue(source.contains("assessment = null"))
        assertTrue(source.contains("assessment = entry.desktopBook?.scanPriorityAssessment"))
        assertTrue(source.contains("scanPriority.accessibilityLabel"))
        val observer = source.substringAfter("activeUniqueWorkQuery(")
            .substringBefore(".observe(this)")
        assertTrue(observer.contains("CaptureMetadataSyncWorker.WORK_NAME"))
        assertTrue(observer.contains("CaptureMetadataSyncWorker.PULL_WORK_NAME"))
    }

    @Test
    fun `detail keeps textual assessment separate from legacy digitization state`() {
        val layout = File("src/main/res/layout/activity_entry_detail.xml").readText()
        val source = File("src/main/java/org/whl/bookcapture/EntryDetailActivity.kt").readText()

        assertTrue(layout.contains("android:id=\"@+id/detailBookSummary\""))
        assertEquals(
            1,
            "layout=\"@layout/view_scan_priority_indicator\"".toRegex().findAll(layout).count(),
        )
        assertTrue(layout.contains("android:background=\"@drawable/whl_inspect_panel\""))
        assertTrue(source.contains("bookView = binding.detailBookSummary"))
        assertTrue(source.contains("assessment = entry.desktopBook?.scanPriorityAssessment"))
        assertTrue(source.contains("R.string.detail_scan_priority"))
        assertTrue(source.contains("scanPriorityDetailValue("))
        assertTrue(source.contains("R.string.scan_priority_assessment_unassessed"))
        assertTrue(source.contains("R.string.scan_priority_assessment_unavailable"))
        assertTrue(source.contains("if (desktop == null) return listOf(scanPriority)"))
        assertTrue(source.contains("R.string.detail_digitization"))
        assertTrue(source.contains("R.string.scan_priority_description"))
        assertTrue(source.contains("R.string.scan_priority_unset"))
    }

    @Test
    fun `home and every inspect binder pass the textual assessment`() {
        val source = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        assertTrue(source.contains("assessment = desktop?.scanPriorityAssessment"))
        assertTrue(
            "assessment = summary.scanPriorityAssessment".toRegex()
                .findAll(source).count() >= 2,
        )
        assertTrue(source.contains("scanPriority.accessibilityLabel"))
    }

    @Test
    fun `archived captures receive book metadata but not mutable sync families`() {
        val source = File(
            "src/main/java/org/whl/bookcapture/CaptureMetadataSyncWorker.kt",
        ).readText()
        val cloud = source.substringAfter("override suspend fun doWork()")
            .substringBefore("private suspend fun syncLan(")
        assertTrue(cloud.contains("CaptureArchive.archivedIds(ctx)"))
        assertTrue(!cloud.contains("Entries.archived(ctx)"))
        assertTrue(cloud.contains("Entries.findIncludingArchive(ctx, id)"))
        assertTrue(
            cloud.indexOf("CaptureArchive.archivedIds(ctx)") <
                cloud.indexOf("Entries.findIncludingArchive(ctx, id)"),
        )
        assertTrue(cloud.contains("client.desktopBookMetadata(metadataIds)"))
        assertTrue(cloud.contains("Entries.findIncludingArchive(ctx, captureId)"))
        assertTrue(cloud.contains("if (entries.isEmpty()) return@withContext Result.success()"))
        assertTrue(cloud.contains("CollectionInventory.recordFinalized"))

        val lan = source.substringAfter("private suspend fun syncLan(")
            .substringBefore("private suspend fun applyDesktopCorrection(")
        assertTrue(lan.contains("if (pass == 0) archivedEntries else emptyList()"))
        assertTrue(lan.contains("val currentBatch = batch.filter"))
        assertTrue(lan.contains("Entries.findIncludingArchive(ctx, captureId)"))
        assertTrue(lan.contains("CollectionInventory.recordFinalized"))
        assertTrue(lan.contains("if (captureId !in entryIds) continue"))
        assertTrue(lan.contains("for (entrySnapshot in currentBatch)"))
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

    @Test
    fun `assigned priorities have labelled compact treatments that yield to selection`() {
        val layout = File("src/main/res/layout/view_scan_priority_indicator.xml").readText()
        assertTrue(layout.contains("android:layout_width=\"27dp\""))
        assertTrue(layout.contains("android:layout_height=\"18dp\""))
        assertTrue(layout.contains("android:maxLines=\"1\""))

        val binder = File(
            "src/main/java/org/whl/bookcapture/ScanPriorityIndicator.kt",
        ).readText()
        for (name in listOf("no_scan", "low", "medium", "high")) {
            assertTrue(binder.contains("R.drawable.whl_scan_priority_${name}_foreground"))
            val drawable = File(
                "src/main/res/drawable/whl_scan_priority_${name}_foreground.xml",
            ).readText()
            assertTrue(drawable.contains("android:state_activated=\"true\""))
            assertTrue(drawable.contains("@android:color/transparent"))
            assertTrue(!drawable.contains("state_selected"))
        }
        assertTrue(binder.contains("R.drawable.whl_scan_priority_no_scan_badge"))
        assertTrue(binder.contains("presentation.candidateGlyphVisible"))
        assertTrue(binder.contains("setImageResource(R.drawable.ic_scan_priority)"))
        assertTrue(binder.contains("imageTintList = null"))
        assertTrue(binder.contains("clearColorFilter()"))

        val noScanBadge = File(
            "src/main/res/drawable/whl_scan_priority_no_scan_badge.xml",
        ).readText()
        assertTrue(noScanBadge.contains("@color/whl_face_sh2"))
        assertTrue(binder.contains("R.color.whl_ink"))
    }
}
