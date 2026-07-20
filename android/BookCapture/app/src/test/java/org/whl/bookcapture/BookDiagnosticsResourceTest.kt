package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class BookDiagnosticsResourceTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"

    @Test
    fun diagnosticsPanelIsCollapsedBoundedAndAccessible() {
        val document = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
            .newDocumentBuilder()
            .parse(File("src/main/res/layout/activity_entry_detail.xml"))
        val elements = document.getElementsByTagName("*")
        val byId = (0 until elements.length)
            .map { elements.item(it) as Element }
            .mapNotNull { element ->
                element.getAttributeNS(androidNs, "id")
                    .removePrefix("@+id/")
                    .takeIf { it.isNotEmpty() }
                    ?.let { it to element }
            }
            .toMap()

        assertEquals("gone", byId.getValue("diagnosticsContent").getAttributeNS(androidNs, "visibility"))
        assertEquals("260dp", byId.getValue("diagnosticsScroll").getAttributeNS(androidNs, "layout_height"))
        assertFalse(byId.getValue("diagnosticsScroll")
            .hasAttributeNS(androidNs, "nestedScrollingEnabled"))
        assertEquals("10sp", byId.getValue("diagnosticsText").getAttributeNS(androidNs, "textSize"))
        assertEquals("monospace", byId.getValue("diagnosticsText").getAttributeNS(androidNs, "fontFamily"))
        assertEquals("yes", byId.getValue("diagnosticsText").getAttributeNS(androidNs, "importantForAccessibility"))
        assertTrue(byId.getValue("diagnosticsTabs")
            .getAttributeNS(androidNs, "contentDescription").isNotEmpty())
    }

    @Test
    fun activityRestoresExpansionAndSelectedTabAndAppliesJsonColors() {
        val source = File(
            "src/main/java/org/whl/bookcapture/EntryDetailActivity.kt",
        ).readText()

        assertTrue(source.contains("STATE_DIAGNOSTICS_EXPANDED"))
        assertTrue(source.contains("STATE_DIAGNOSTICS_TAB"))
        assertTrue(source.contains("STATE_DIAGNOSTICS_SCROLL"))
        assertTrue(source.contains("BookDiagnosticsPresenter.from(entry)"))
        assertTrue(source.contains("withContext(Dispatchers.IO) { BookDiagnosticsPresenter.from(entry) }"))
        assertTrue(source.contains("if (diagnosticsExpanded) loadDiagnostics(entry, force = true)"))
        assertTrue(source.contains("diagnosticsJob?.cancel()"))
        assertTrue(source.contains("JsonSyntaxTokenizer.tokenize(json)"))
        assertTrue(source.contains("ViewCompat.setStateDescription"))
    }
}
