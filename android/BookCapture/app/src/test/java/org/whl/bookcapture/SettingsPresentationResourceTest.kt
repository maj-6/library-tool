package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class SettingsPresentationResourceTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"

    @Test
    fun settingsUseConciseControlsWithoutHelperParagraphsOrPrefilledHints() {
        val settings = xml("src/main/res/layout/activity_settings.xml")
        val forbiddenText = setOf(
            "@string/set_compact_scan_list_note",
            "@string/set_show_ocr_regions_note",
            "@string/set_post_processing_note",
            "@string/set_sharpen_note",
            "@string/set_voice_note",
            "@string/lan_note",
            "@string/set_api_keys_note",
        )

        val nodes = settings.getElementsByTagName("*")
        for (index in 0 until nodes.length) {
            val element = nodes.item(index) as Element
            assertFalse(element.getAttributeNS(androidNs, "text") in forbiddenText)
            assertFalse(element.hasAttributeNS(androidNs, "hint"))
        }

        assertNotNull(elementById(settings, "cameraProfileLow"))
        assertFalse(hasElementWithId(settings, "postProcessingPresetSummary"))
        assertFalse(hasElementWithId(settings, "cameraDiagnostics"))
        assertFalse(hasElementWithId(settings, "apiKeysNote"))
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
