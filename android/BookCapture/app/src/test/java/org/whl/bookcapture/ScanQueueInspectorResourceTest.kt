package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import org.w3c.dom.Node
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class ScanQueueInspectorResourceTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun homeHasFourAccessibleIconOnlyTabs() {
        val home = xml("src/main/res/layout/activity_home.xml")
        val expected = listOf(
            Triple("tabScans", "@drawable/ic_scans", "@string/home_tab_scans"),
            Triple(
                "tabScanQueue",
                "@drawable/ic_scan_queue",
                "@string/home_tab_scan_queue",
            ),
            Triple(
                "tabCollections",
                "@drawable/ic_collections",
                "@string/home_tab_collections",
            ),
            Triple("tabInspect", "@drawable/ic_inspect", "@string/home_tab_inspect"),
        )
        expected.forEach { (id, icon, description) ->
            val tab = elementById(home, id)
            assertEquals("com.google.android.material.button.MaterialButton", tab.tagName)
            assertEquals("@style/WhlToolbarAction", tab.getAttribute("style"))
            assertEquals("", tab.getAttributeNS(androidNs, "text"))
            assertEquals(description, tab.getAttributeNS(androidNs, "contentDescription"))
            assertEquals(icon, tab.getAttributeNS(appNs, "icon"))
            assertEquals("0dp", tab.getAttributeNS(appNs, "iconPadding"))
        }

        val themes = File("src/main/res/values/themes.xml").readText()
        val toolbarStyle = themes.substringAfter("<style name=\"WhlToolbarAction\"")
            .substringBefore("</style>")
        assertTrue(toolbarStyle.contains("android:minWidth\">48dp"))
        assertTrue(toolbarStyle.contains("android:minHeight\">48dp"))
    }

    @Test
    fun queueOwnsItsPaneSummaryListAndCaptureAction() {
        val home = xml("src/main/res/layout/activity_home.xml")
        val queuePane = elementById(home, "scanQueuePane")
        val inspectPane = elementById(home, "inspectPane")
        val queueActions = elementById(home, "scanQueueActions")
        val inspectActions = elementById(home, "inspectActions")
        val summary = elementById(home, "scanQueueSummary")
        val empty = elementById(home, "scanQueueEmpty")
        val list = elementById(home, "scanQueueList")
        val capture = elementById(home, "queueScanBook")

        assertEquals("LinearLayout", queuePane.tagName)
        assertEquals("gone", queuePane.getAttributeNS(androidNs, "visibility"))
        assertTrue(isDescendant(summary, queuePane))
        assertTrue(isDescendant(empty, queuePane))
        assertTrue(isDescendant(list, queuePane))
        assertTrue(!isDescendant(summary, inspectPane))
        assertEquals("polite", summary.getAttributeNS(androidNs, "accessibilityLiveRegion"))
        assertEquals(
            "@string/scan_queue_inspector_empty",
            empty.getAttributeNS(androidNs, "text"),
        )
        assertTrue(isDescendant(capture, queueActions))
        assertTrue(!isDescendant(capture, inspectActions))
    }

    @Test
    fun queueRowsExposeReadableStateAndOneFullSizeSemanticAction() {
        val row = xml("src/main/res/layout/item_scan_queue.xml")
        assertNotNull(elementById(row, "scanQueueRowTitle"))
        assertNotNull(elementById(row, "scanQueueRowStatus"))
        assertNotNull(elementById(row, "scanQueueRowDetail"))

        val action = elementById(row, "scanQueueRowAction")
        assertEquals("com.google.android.material.button.MaterialButton", action.tagName)
        assertEquals("48dp", action.getAttributeNS(androidNs, "layout_width"))
        assertEquals("48dp", action.getAttributeNS(androidNs, "layout_height"))
        assertEquals("", action.getAttributeNS(androidNs, "text"))
        assertEquals(
            "@string/scan_queue_review_action",
            action.getAttributeNS(androidNs, "contentDescription"),
        )

        val decorativeIcon = row.getElementsByTagName("ImageView").item(0) as Element
        assertEquals("no", decorativeIcon.getAttributeNS(androidNs, "importantForAccessibility"))
        val icon = xml("src/main/res/drawable/ic_scan_queue.xml")
        assertTrue(icon.getElementsByTagName("path").length > 0)

        val home = File(
            "src/main/java/org/whl/bookcapture/HomeActivity.kt",
        ).readText()
        assertTrue(home.contains("ScanSearchQueue.dismissLocalFailures("))
        assertTrue(home.contains("scan_queue_dismiss_failure_action"))
    }

    private fun xml(path: String): Document = DocumentBuilderFactory.newInstance().apply {
        isNamespaceAware = true
    }.newDocumentBuilder().parse(File(path))

    private fun elementById(document: Document, id: String): Element {
        val all = document.getElementsByTagName("*")
        return (0 until all.length)
            .map { all.item(it) as Element }
            .first { it.getAttributeNS(androidNs, "id") == "@+id/$id" }
    }

    private fun isDescendant(child: Node, ancestor: Node): Boolean {
        var current = child.parentNode
        while (current != null) {
            if (current == ancestor) return true
            current = current.parentNode
        }
        return false
    }
}
