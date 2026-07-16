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

class ResourceContractTest {

    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun captureActionsHaveVisibleIconsAndAccessibleLabels() {
        val layout = xml("src/main/res/layout/activity_main.xml")
        val expected = mapOf(
            "btnStart" to "@drawable/ic_camera_new",
            "btnPhoto" to "@drawable/ic_camera",
            "btnDone" to "@drawable/ic_done",
            "btnCancel" to "@drawable/ic_cancel",
        )

        for ((id, icon) in expected) {
            val action = elementById(layout, id)
            assertEquals("@style/WhlIconButton", action.getAttribute("style"))
            assertEquals(icon, action.getAttributeNS(appNs, "srcCompat"))
            assertEquals("@color/whl_icon_button_tint", action.getAttributeNS(appNs, "tint"))
            assertTrue(action.getAttributeNS(androidNs, "contentDescription").isNotBlank())
        }
    }

    @Test
    fun deepseekEditorLivesInScrollableDialogOpenedBySparkleButton() {
        val detail = xml("src/main/res/layout/activity_entry_detail.xml")
        val trigger = elementById(detail, "deepseekInstructions")
        assertEquals("@drawable/ic_sparkles", trigger.getAttributeNS(androidNs, "drawableStart"))
        assertEquals("@string/detail_deepseek_action", trigger.getAttributeNS(androidNs, "text"))
        assertTrue(trigger.getAttributeNS(androidNs, "contentDescription").isNotBlank())
        assertFalse(hasElementWithId(detail, "customInstructions"))

        val dialog = xml("src/main/res/layout/dialog_deepseek_instructions.xml")
        assertEquals("ScrollView", dialog.documentElement.tagName)
        assertNotNull(elementById(dialog, "customInstructions"))
        assertNotNull(elementById(dialog, "reprocessState"))
        assertNotNull(elementById(dialog, "resubmit"))

        val source = File("src/main/java/org/whl/bookcapture/EntryDetailActivity.kt").readText()
        assertTrue(source.contains("binding.deepseekInstructions.setOnClickListener"))
        assertTrue(source.contains("setNegativeButton(R.string.close"))
    }

    @Test
    fun launcherArtworkUsesTheSameInsetSafeLayerForEveryPresentation() {
        val safeLayer = xml("src/main/res/drawable/ic_launcher_safe_fg.xml").documentElement
        assertEquals("inset", safeLayer.tagName)
        assertEquals("@drawable/ic_launcher_fg", safeLayer.getAttributeNS(androidNs, "drawable"))
        for (edge in listOf("insetLeft", "insetTop", "insetRight", "insetBottom")) {
            assertEquals("14.5dp", safeLayer.getAttributeNS(androidNs, edge))
        }

        val adaptive = xml("src/main/res/mipmap-anydpi-v26/ic_launcher.xml").documentElement
        val foreground = adaptive.getElementsByTagName("foreground").item(0) as Element
        val monochrome = adaptive.getElementsByTagName("monochrome").item(0) as Element
        assertEquals("@drawable/ic_launcher_safe_fg", foreground.getAttributeNS(androidNs, "drawable"))
        assertEquals("@drawable/ic_launcher_safe_fg", monochrome.getAttributeNS(androidNs, "drawable"))

        val legacy = xml("src/main/res/mipmap-anydpi/ic_launcher.xml")
        val legacyItems = legacy.getElementsByTagName("item")
        assertTrue((0 until legacyItems.length)
            .map { legacyItems.item(it) as Element }
            .any { it.getAttributeNS(androidNs, "drawable") == "@drawable/ic_launcher_safe_fg" })

        val manifest = xml("src/main/AndroidManifest.xml")
        val application = manifest.getElementsByTagName("application").item(0) as Element
        assertEquals("@mipmap/ic_launcher", application.getAttributeNS(androidNs, "icon"))
        assertEquals("@mipmap/ic_launcher", application.getAttributeNS(androidNs, "roundIcon"))
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
