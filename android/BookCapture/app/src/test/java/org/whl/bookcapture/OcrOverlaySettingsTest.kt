package org.whl.bookcapture

import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

class OcrOverlaySettingsTest {

    @Test
    fun overlayOutlineIsAtLeastTwoPhysicalPixels() {
        assertEquals(2f, minimumOverlayStrokeWidthPx(.75f), 0f)
        assertEquals(2f, minimumOverlayStrokeWidthPx(1f), 0f)
        assertEquals(6f, minimumOverlayStrokeWidthPx(3f), 0f)
    }

    @Test
    fun displaySettingsExposeGeometryVisibilityOpacityAndLabels() {
        val layout = File("src/main/res/layout/activity_settings.xml").readText()
        for (id in listOf(
            "showOcrRegions", "ocrRegionOptions", "ocrRegionOpacity",
            "ocrRegionOpacityLabel", "showOcrRegionLabels",
        )) assertTrue(layout.contains("android:id=\"@+id/$id\""))

        val prefs = File("src/main/java/org/whl/bookcapture/Prefs.kt").readText()
        assertTrue(prefs.contains("getBoolean(\"show_ocr_regions\", true)"))
        assertTrue(prefs.contains("getInt(\"ocr_region_opacity\", 55)"))
        assertTrue(prefs.contains("getBoolean(\"show_ocr_region_labels\", false)"))
    }

    @Test
    fun ocrRequestsBlocksAndDetailsRenderOnlyPersistedGeometry() {
        val pipeline = File("src/main/java/org/whl/bookcapture/Pipeline.kt").readText()
        val detail = File("src/main/java/org/whl/bookcapture/EntryDetailActivity.kt").readText()

        assertTrue(pipeline.contains(".put(\"include_blocks\", true)"))
        assertTrue(detail.contains("descriptor.geometry.mapNotNull"))
        assertTrue(detail.contains("Prefs.showOcrRegions(this)"))
        assertTrue(detail.contains("Prefs.ocrRegionOpacityPercent(this) / 100f"))
    }

    @Test
    fun globalInstructionsReplaceTheRemovedPerBookActionForNewScans() {
        val layout = File("src/main/res/layout/activity_settings.xml").readText()
        val worker = File("src/main/java/org/whl/bookcapture/ProcessWorker.kt").readText()

        assertTrue(layout.contains("android:id=\"@+id/extractionInstructions\""))
        assertTrue(worker.contains("Prefs.extractionInstructions(applicationContext)"))
    }
}
