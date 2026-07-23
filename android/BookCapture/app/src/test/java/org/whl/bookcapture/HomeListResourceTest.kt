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
            "androidx.appcompat.widget.AppCompatSpinner",
            elementById(dialog, "collectionParent").tagName,
        )
        assertFalse(elementById(dialog, "collectionName").hasAttributeNS(androidNs, "hint"))
        assertFalse(elementById(dialog, "collectionFrom").hasAttributeNS(androidNs, "hint"))
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
        assertFalse(source.contains("nameField.hint ="))
        assertFalse(source.contains("fromField.hint ="))
        assertTrue(source.contains("collectionParentCandidates(collections, collectionId)"))
        assertTrue(source.contains("parentId = parentId"))
        assertTrue(source.contains("R.id.cancelCollectionEdit"))
        assertTrue(source.contains("R.id.saveCollectionEdit"))
        assertTrue(source.contains("val isRetiredConflict = existing?.deleted == true"))
        assertTrue(source.contains("Collections.retagRetired(this, checkNotNull(existing).id"))
        assertTrue(source.contains("R.string.collections_retag_retired_title"))
        assertTrue(source.contains("delete.visibility = View.GONE"))
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
        assertFalse(home.contains("thumbnail = entry.thumbnailDescriptor()"))
        assertTrue(home.contains("ThumbnailRequest("))
        assertTrue(home.contains("request.entry.thumbnailDescriptor()"))
        assertTrue(home.contains("val loadContext = currentCoroutineContext()"))
        assertTrue(home.contains("loadContext.ensureActive()"))
        assertTrue(home.contains("startThumbnailLoading(thumbs, HomeTab.SCANS)"))
        assertTrue(home.contains("descriptor.postProcessingPending"))
        assertTrue(home.contains("softenPendingThumbnail(decoded)"))
        assertFalse(home.contains("e.photos().firstOrNull()?.let"))
    }

    @Test
    fun homeToolbarContainsOnlyIconTabsAndScanRowsOpenOrMarkAttention() {
        val home = xml("src/main/res/layout/activity_home.xml")
        assertFalse(hasElementWithId(home, "btnSelect"))
        assertFalse(hasElementWithId(home, "selectionBar"))
        val appMenu = elementById(home, "appMenu")
        assertEquals("56dp", appMenu.getAttributeNS(androidNs, "layout_width"))
        assertEquals("56dp", appMenu.getAttributeNS(androidNs, "layout_height"))
        assertEquals("@drawable/ic_app_mark", appMenu.getAttributeNS(androidNs, "src"))
        for ((id, icon) in listOf(
            "tabScans" to "@drawable/ic_scans",
            "tabCollections" to "@drawable/ic_collections",
            "tabInspect" to "@drawable/ic_inspect",
        )) {
            val tab = elementById(home, id)
            assertEquals(icon, tab.getAttributeNS(appNs, "icon"))
            assertEquals("false", tab.getAttributeNS(androidNs, "textAllCaps"))
            assertEquals("@font/roboto_slab", tab.getAttributeNS(androidNs, "fontFamily"))
        }
        val source = source("HomeActivity")
        assertTrue(source.contains("val openBook = {"))
        assertTrue(source.contains("openEntryDetails(e.id)"))
        assertTrue(source.contains("row.setOnClickListener { openBook() }"))
        assertTrue(source.contains("row.setOnLongClickListener"))
        assertTrue(source.contains("showEntryAttentionDialog(this, e.id)"))
        assertTrue(source.contains("configureScanRowAccessibility(row, openBook, markAttention)"))
        assertTrue(source.contains("AccessibilityActionCompat.ACTION_LONG_CLICK"))
        assertTrue(source.contains("R.string.home_mark_needs_attention"))
        assertTrue(source.contains("copyrightView.setOnLongClickListener"))
        assertTrue(source.contains("showEntryAttentionDialog(this, entry.id)"))
        assertFalse(source.contains("sections.joinToString(\"\\n\\n\").take(24_000)"))
        assertTrue(source.contains("R.plurals.copyright_records_omitted"))
        assertFalse(source.contains("selectSingle(e.id)"))
        assertFalse(source.contains("toggleAdditiveSelection(e.id)"))
        assertFalse(source.contains("STATE_SELECTION_MODE"))

        val scanRow = xml("src/main/res/layout/item_home.xml")
        assertFalse(hasElementWithId(scanRow, "selected"))
        assertFalse(hasElementWithId(scanRow, "openDetails"))
        for (id in listOf(
            "copyrightStatus", "whlAvailability", "internetArchiveAvailability",
            "scanStatus", "remarksStatus", "attentionStatus",
        )) assertNotNull(elementById(scanRow, id))

        val plate = xml("src/main/res/drawable/whl_icon_plate.xml")
        assertEquals(0, plate.getElementsByTagName("stroke").length)
        val solids = plate.getElementsByTagName("solid")
        assertTrue((0 until solids.length)
            .map { solids.item(it) as Element }
            .any { it.getAttributeNS(androidNs, "color") == "@color/whl_green" })

        val strings = File("src/main/res/values/strings.xml").readText()
        assertTrue(strings.contains("<string name=\"app_name\">Library Tool Capture</string>"))
        assertTrue(strings.contains("<string name=\"home_new_scan\">New scan</string>"))
        val newScan = elementById(home, "newScan")
        assertEquals("com.google.android.material.button.MaterialButton", newScan.tagName)
        assertEquals("@drawable/ic_camera_new", newScan.getAttributeNS(appNs, "icon"))
        assertEquals("textStart", newScan.getAttributeNS(appNs, "iconGravity"))
        val sync = elementById(home, "syncCaptures")
        assertEquals("com.google.android.material.button.MaterialButton", sync.tagName)
        assertEquals("@drawable/ic_sync_upload", sync.getAttributeNS(appNs, "icon"))
        assertTrue(source.contains("UploadWorker.enqueueExplicitSync(this)"))
        assertTrue(source.contains("CaptureMetadataSyncWorker.enqueueExplicitSync(this)"))
        val syncSource = source.substringAfter("private fun syncCaptures()")
            .substringBefore("private fun emphasizeTab")
        val normalizedSyncSource = syncSource.filterNot(Char::isWhitespace)
        for (resource in listOf(
            "home_sync_sign_in", "home_sync_none", "home_sync_queued", "home_sync_running",
            "home_sync_captures", "home_sync_complete", "home_sync_partial", "home_sync_failed",
        )) {
            // Some messages share one RemoteUiCatalog call and select the
            // resource dynamically (for example, no captures vs review-only
            // changes). Assert catalog use and the complete resource contract
            // without coupling this test to that harmless expression shape.
            assertTrue(normalizedSyncSource.contains("R.string.$resource"))
        }
        assertTrue(normalizedSyncSource.contains("RemoteUiCatalog.text("))
        assertTrue(source.contains("R.string.home_sync_review_queued"))
        assertEquals("polite", sync.getAttributeNS(androidNs, "accessibilityLiveRegion"))
        assertTrue(source.contains("binding.syncCaptures.announceForAccessibility(message)"))

        val copyrightButton = elementById(scanRow, "copyrightStatus")
        assertEquals("@style/WhlMetadataIconButton", copyrightButton.getAttribute("style"))
        assertEquals(
            "48dp",
            elementById(scanRow, "desktopMetadataIcons")
                .getAttributeNS(androidNs, "layout_height"),
        )
        val status = elementById(scanRow, "state")
        assertEquals("0dp", status.getAttributeNS(androidNs, "layout_width"))
        assertEquals("1", status.getAttributeNS(androidNs, "layout_weight"))
        assertEquals("end", status.getAttributeNS(androidNs, "ellipsize"))
        assertEquals("1", status.getAttributeNS(androidNs, "maxLines"))
        val metadataStyle = File("src/main/res/values/themes.xml").readText()
            .substringAfter("<style name=\"WhlMetadataIconButton\"")
            .substringBefore("</style>")
        assertTrue(metadataStyle.contains("android:layout_width\">48dp"))
        assertTrue(metadataStyle.contains("android:layout_height\">48dp"))
        assertTrue(metadataStyle.contains("android:padding\">14dp"))
    }

    @Test
    fun aboutDialogHasFullHeightGreenIconDocumentationAndScrollableChangelog() {
        val dialog = xml("src/main/res/layout/dialog_about.xml")
        val icon = elementById(dialog, "aboutIcon")
        assertEquals("64dp", icon.getAttributeNS(androidNs, "layout_width"))
        assertEquals("64dp", icon.getAttributeNS(androidNs, "layout_height"))
        assertEquals("@drawable/whl_icon_plate", icon.getAttributeNS(androidNs, "background"))
        assertEquals("@drawable/ic_app_mark", icon.getAttributeNS(androidNs, "src"))
        assertNotNull(elementById(dialog, "aboutTitle"))
        assertNotNull(elementById(dialog, "aboutVersion"))
        assertNotNull(elementById(dialog, "aboutDescription"))
        assertEquals(
            "184dp",
            elementById(dialog, "aboutChangelogScroll")
                .getAttributeNS(androidNs, "layout_height"),
        )
        assertTrue(File("src/release/res/raw/android_changelog.md").isFile)

        val strings = File("src/main/res/values/strings.xml").readText()
        assertTrue(strings.contains("https://maj-6.github.io/library-tool/docs.html#capture"))
        assertTrue(strings.contains("Changelog not included for this version"))
        val source = source("HomeActivity")
        assertTrue(source.contains("if (BuildConfig.DEBUG)"))
        assertTrue(source.contains("LinkMovementMethod.getInstance()"))
    }

    @Test
    fun menuProvidesSignOutAndKeepsAboutLastBehindAGroupSeparator() {
        val menu = xml("src/main/res/menu/home_app_menu.xml")
        assertNotNull(elementById(menu, "menuSignOut"))
        val itemIds = (0 until menu.getElementsByTagName("item").length)
            .map { menu.getElementsByTagName("item").item(it) as Element }
            .map { it.getAttributeNS(androidNs, "id") }
        assertEquals("@+id/menuAbout", itemIds.last())
        val source = source("HomeActivity")
        assertTrue(source.contains("MenuCompat.setGroupDividerEnabled(menu.menu, true)"))
        assertTrue(source.contains("Auth.signOut(this@HomeActivity)"))
    }

    @Test
    fun pendingAndUploadedStatusesUseCompactVisualIndicators() {
        val item = xml("src/main/res/layout/item_home.xml")
        val waiting = elementById(item, "waitingIndicator")
        assertEquals("true", waiting.getAttributeNS(androidNs, "indeterminate"))
        assertEquals("gone", waiting.getAttributeNS(androidNs, "visibility"))
        assertEquals(
            "@drawable/ic_cloud_done",
            elementById(item, "stateIcon").getAttributeNS(androidNs, "src"),
        )
        assertEquals(
            "false",
            elementById(item, "state").getAttributeNS(androidNs, "textAllCaps"),
        )
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
