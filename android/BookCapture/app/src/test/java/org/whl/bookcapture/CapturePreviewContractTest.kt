package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class CapturePreviewContractTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun lastSubmittedBookThumbnailHasDummyAndDecodedBitmapWiring() {
        val capture = xml("src/main/res/layout/activity_main.xml")
        val preview = elementById(capture, "lastBookPreview")
        val thumbnail = elementById(capture, "lastBookThumb")

        assertEquals(
            "androidx.appcompat.widget.AppCompatImageView",
            thumbnail.tagName,
        )
        assertEquals("@drawable/ic_launcher_safe_fg", thumbnail.getAttributeNS(appNs, "srcCompat"))
        assertEquals("@drawable/whl_thumbnail_frame", thumbnail.getAttributeNS(androidNs, "foreground"))
        assertEquals(
            "@id/buttons",
            preview.getAttributeNS(appNs, "layout_constraintBottom_toTopOf"),
        )
        assertFalse(hasElementWithId(capture, "recentList"))
        assertFalse(hasElementWithId(capture, "queueChip"))

        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        assertTrue(source.contains("selectLastSubmittedEntry(Entries.recent(this@MainActivity))"))
        assertTrue(source.contains("val photo = latest?.thumbnailDescriptor()?.displayFile"))
        assertTrue(source.contains(
            "decodeSampledOriented(photo, maxWidth = 360, maxHeight = 480)",
        ))
        assertTrue(source.contains("binding.lastBookThumb.setImageBitmap(load.bitmap)"))
        assertTrue(source.contains("binding.lastBookThumb.setImageResource(R.drawable.ic_launcher_safe_fg)"))
        assertTrue(source.contains("R.string.capture_last_book_thumbnail_description"))
    }

    @Test
    fun pageMarginGuideNeverCollapsesBelowTwoPhysicalPixels() {
        val capture = xml("src/main/res/layout/activity_main.xml")
        assertEquals(
            "org.whl.bookcapture.PageMarginOverlayView",
            elementById(capture, "pageMarginOverlay").tagName,
        )

        val source = File(
            "src/main/java/org/whl/bookcapture/PageMarginOverlayView.kt",
        ).readText()
        val frameStart = source.indexOf("private val framePaint")
        val focusStart = source.indexOf("private val focusPaint")
        assertTrue(frameStart >= 0 && focusStart > frameStart)
        val framePaint = source.substring(frameStart, focusStart)
        assertTrue(framePaint.contains("strokeWidth = maxOf(2f, 2f * density)"))
    }

    private fun xml(path: String): Document =
        DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
            .newDocumentBuilder()
            .parse(File(path))

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
