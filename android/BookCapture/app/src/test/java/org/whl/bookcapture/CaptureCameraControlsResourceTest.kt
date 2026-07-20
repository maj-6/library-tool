package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class CaptureCameraControlsResourceTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun previewOwnsCameraOnlySettingsFramingAndFixedSideOrientationControl() {
        val capture = xml("src/main/res/layout/activity_main.xml")
        assertFalse(hasElementWithId(capture, "btnSettings"))

        val settings = elementById(capture, "btnCameraSettings")
        assertEquals("@style/WhlIconButton", settings.getAttribute("style"))
        assertEquals(
            "@drawable/ic_camera_settings",
            settings.getAttributeNS(appNs, "srcCompat"),
        )
        assertEquals(
            "@id/preview",
            settings.getAttributeNS(appNs, "layout_constraintEnd_toEndOf"),
        )

        val orientation = elementById(capture, "btnCaptureOrientation")
        assertEquals("@style/WhlIconButton", orientation.getAttribute("style"))
        assertEquals(
            "@id/btnCameraSettings",
            orientation.getAttributeNS(appNs, "layout_constraintTop_toBottomOf"),
        )
        assertEquals(
            "@id/preview",
            orientation.getAttributeNS(appNs, "layout_constraintEnd_toEndOf"),
        )
        assertTrue(orientation.getAttributeNS(androidNs, "contentDescription").isNotBlank())

        val frame = elementById(capture, "pageMarginOverlay")
        assertEquals("org.whl.bookcapture.PageMarginOverlayView", frame.tagName)
        for (constraint in listOf(
            "layout_constraintTop_toTopOf",
            "layout_constraintBottom_toBottomOf",
            "layout_constraintStart_toStartOf",
            "layout_constraintEnd_toEndOf",
        )) assertEquals("@id/preview", frame.getAttributeNS(appNs, constraint))

        val preview = elementById(capture, "preview")
        assertEquals("fillCenter", preview.getAttributeNS(appNs, "scaleType"))
        val thumbnails = elementById(capture, "thumbs_scroll")
        assertEquals("gone", thumbnails.getAttributeNS(androidNs, "visibility"))

        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        assertTrue(source.contains("updateThumbnailStripVisibility()"))
    }

    @Test
    fun popupContainsOnlyPracticalCameraAndScanControls() {
        val dialog = xml("src/main/res/layout/dialog_capture_camera_settings.xml")
        for (id in listOf(
            "cameraFocusLock", "cameraFocusState", "cameraZoom", "cameraExposure",
            "cameraTorch", "cameraProfile", "cameraProfileLow", "cameraProfileFast",
            "cameraProfileDetail",
            "cameraSharpen",
        )) assertNotNull(elementById(dialog, id))
        for (forbidden in listOf(
            "account", "transport", "mistralKey", "deepseekKey", "checkUpdates",
        )) assertFalse(hasElementWithId(dialog, forbidden))
        assertFalse(
            File("src/main/res/layout/dialog_capture_camera_settings.xml")
                .readText()
                .contains("camera_settings_scope_note"),
        )

        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        assertTrue(source.contains("isFocusMeteringSupported"))
        assertTrue(source.contains("setZoomRatio"))
        assertTrue(source.contains("setExposureCompensationIndex"))
        assertTrue(source.contains("hasFlashUnit"))
        assertTrue(source.contains("disableAutoCancel"))
        assertFalse(source.contains("Intent(this, SettingsActivity::class.java)"))
    }

    private fun xml(path: String): Document {
        val factory = DocumentBuilderFactory.newInstance()
        factory.isNamespaceAware = true
        return factory.newDocumentBuilder().parse(File(path))
    }

    private fun hasElementWithId(document: Document, id: String): Boolean =
        findElementById(document, id) != null

    private fun elementById(document: Document, id: String): Element =
        requireNotNull(findElementById(document, id)) { "Missing view id $id" }

    private fun findElementById(document: Document, id: String): Element? {
        val nodes = document.getElementsByTagName("*")
        for (index in 0 until nodes.length) {
            val element = nodes.item(index) as Element
            if (element.getAttributeNS(androidNs, "id") == "@+id/$id") return element
        }
        return null
    }
}
