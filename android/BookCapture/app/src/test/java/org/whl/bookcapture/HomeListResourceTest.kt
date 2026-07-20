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

class HomeListResourceTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun collectionRowsUseAccessibleIconActionsAndVisualCurrentState() {
        val row = xml("src/main/res/layout/item_collection.xml")
        assertFalse(hasElementWithId(row, "currentMarker"))
        for ((id, icon) in listOf(
            "editCollection" to "@drawable/ic_edit",
            "deleteCollection" to "@drawable/ic_delete",
        )) {
            val button = elementById(row, id)
            assertEquals("androidx.appcompat.widget.AppCompatImageButton", button.tagName)
            assertEquals(icon, button.getAttributeNS(appNs, "srcCompat"))
            assertTrue(button.getAttributeNS(androidNs, "contentDescription").isNotEmpty())
            assertFalse(button.hasAttributeNS(androidNs, "text"))
        }

        val current = xml("src/main/res/drawable/whl_collection_current.xml")
        val strokes = current.getElementsByTagName("stroke")
        assertTrue(strokes.length >= 2)
        for (i in 0 until strokes.length) {
            val stroke = strokes.item(i) as Element
            assertEquals("1dp", stroke.getAttributeNS(androidNs, "width"))
            assertFalse(stroke.hasAttributeNS(androidNs, "dashWidth"))
        }
        val source = source("HomeActivity")
        assertTrue(source.contains("R.drawable.whl_collection_current"))
        assertTrue(source.contains("if (isCurrent) Typeface.BOLD else Typeface.NORMAL"))
        assertFalse(source.contains("collections_row_current"))
    }

    @Test
    fun collectionEditorUsesContentForValuesAndIconActions() {
        val dialog = xml("src/main/res/layout/dialog_collection.xml")
        assertEquals(
            "@string/collections_hint_name",
            elementById(dialog, "collectionName").getAttributeNS(androidNs, "hint"),
        )
        assertEquals(
            "@string/collections_hint_from",
            elementById(dialog, "collectionFrom").getAttributeNS(androidNs, "hint"),
        )
        for ((id, icon) in listOf(
            "cancelCollectionEdit" to "@drawable/ic_cancel",
            "saveCollectionEdit" to "@drawable/ic_done",
        )) {
            val button = elementById(dialog, id)
            assertEquals("androidx.appcompat.widget.AppCompatImageButton", button.tagName)
            assertEquals(icon, button.getAttributeNS(appNs, "srcCompat"))
            assertTrue(button.getAttributeNS(androidNs, "contentDescription").isNotEmpty())
        }
        val source = source("HomeActivity")
        assertTrue(source.contains("nameField.setText(existing?.name.orEmpty())"))
        assertTrue(source.contains("fromField.setText(existing?.from.orEmpty())"))
        assertTrue(source.contains("R.id.cancelCollectionEdit"))
        assertTrue(source.contains("R.id.saveCollectionEdit"))
    }

    @Test
    fun scanListHasCollapsibleGroupsCompactPreferenceAndFramedThumbnails() {
        val group = xml("src/main/res/layout/item_scan_group.xml")
        for (id in listOf("groupChevron", "groupName", "groupCount")) {
            assertNotNull(elementById(group, id))
        }

        val item = xml("src/main/res/layout/item_home.xml")
        assertEquals(
            "@drawable/whl_thumbnail_frame",
            elementById(item, "thumb").getAttributeNS(androidNs, "foreground"),
        )
        val frame = xml("src/main/res/drawable/whl_thumbnail_frame.xml")
        val stroke = frame.getElementsByTagName("stroke").item(0) as Element
        assertEquals("1dp", stroke.getAttributeNS(androidNs, "width"))
        assertEquals("@color/whl_thumbnail_border", stroke.getAttributeNS(androidNs, "color"))

        val settings = xml("src/main/res/layout/activity_settings.xml")
        assertEquals(
            "com.google.android.material.materialswitch.MaterialSwitch",
            elementById(settings, "compactScanList").tagName,
        )
        val prefs = source("Prefs")
        val settingsSource = source("SettingsActivity")
        assertTrue(prefs.contains("fun compactScanList(ctx: Context): Boolean"))
        assertTrue(prefs.contains("putBoolean(\"compact_scan_list\", compact)"))
        assertTrue(settingsSource.contains("Prefs.setCompactScanList(this, compact)"))

        val home = source("HomeActivity")
        assertTrue(home.contains("groupScansByCollection("))
        assertTrue(home.contains("initiallyExpandedScanGroup("))
        assertTrue(home.contains("Prefs.compactScanList(this)"))
        assertTrue(home.contains("e.thumbnailPhoto()?.let"))
        assertFalse(home.contains("e.photos().firstOrNull()?.let"))
    }

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()

    private fun xml(path: String): Document {
        val factory = DocumentBuilderFactory.newInstance()
        factory.isNamespaceAware = true
        return factory.newDocumentBuilder().parse(File(path))
    }

    private fun hasElementWithId(document: Document, id: String): Boolean =
        findElementById(document, id) != null

    private fun elementById(document: Document, id: String): Element =
        requireNotNull(findElementById(document, id)) { "$id is missing" }

    private fun findElementById(document: Document, id: String): Element? {
        val nodes = document.getElementsByTagName("*")
        return (0 until nodes.length)
            .map { nodes.item(it) as Element }
            .firstOrNull {
                it.getAttributeNS(androidNs, "id") in
                    listOf("@+id/$id", "@id/$id")
            }
    }
}
