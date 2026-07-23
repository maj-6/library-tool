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

class InspectResourceContractTest {

    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun homeHasAnIconInspectTabAndDedicatedCollectionOverview() {
        val home = xml("src/main/res/layout/activity_home.xml")
        val inspectTab = elementById(home, "tabInspect")

        assertEquals("com.google.android.material.button.MaterialButton", inspectTab.tagName)
        assertEquals("@style/WhlToolbarAction", inspectTab.getAttribute("style"))
        assertEquals("@string/home_tab_inspect", inspectTab.getAttributeNS(androidNs, "text"))
        assertEquals("@drawable/ic_inspect", inspectTab.getAttributeNS(appNs, "icon"))

        assertEquals("LinearLayout", elementById(home, "inspectPane").tagName)
        assertNotNull(elementById(home, "inspectSummary"))
        assertEquals(
            "HorizontalScrollView",
            elementById(home, "inspectCollectionScroll").tagName,
        )
        assertEquals("LinearLayout", elementById(home, "inspectCollectionChips").tagName)
        assertNotNull(elementById(home, "inspectCollectionName"))
        assertNotNull(elementById(home, "inspectCollectionMeta"))
        assertNotNull(elementById(home, "inspectBooks"))

        val source = source("HomeActivity")
        assertTrue(source.contains("private enum class HomeTab { SCANS, COLLECTIONS, INSPECT }"))
        assertTrue(source.contains("binding.tabInspect.setOnClickListener { showTab(HomeTab.INSPECT) }"))
        assertTrue(source.contains("binding.inspectPane.visibility = if (tab == HomeTab.INSPECT)"))
    }

    @Test
    fun inspectViewModesAreRequiredSingleSelectionControls() {
        val home = xml("src/main/res/layout/activity_home.xml")
        val group = elementById(home, "inspectViewModes")
        assertEquals(
            "com.google.android.material.button.MaterialButtonToggleGroup",
            group.tagName,
        )
        assertEquals("true", group.getAttributeNS(appNs, "singleSelection"))
        assertEquals("true", group.getAttributeNS(appNs, "selectionRequired"))

        val expected = mapOf(
            "inspectModeTiles" to ("@string/inspect_mode_tiles" to "@drawable/ic_view_tiles"),
            "inspectModeContent" to ("@string/inspect_mode_content" to "@drawable/ic_view_content"),
            "inspectModeIcons" to ("@string/inspect_mode_icons" to "@drawable/ic_view_icons"),
        )
        expected.forEach { (id, labels) ->
            val button = elementById(home, id)
            assertEquals("com.google.android.material.button.MaterialButton", button.tagName)
            assertEquals(labels.first, button.getAttributeNS(androidNs, "text"))
            assertEquals(labels.second, button.getAttributeNS(appNs, "icon"))
        }
    }

    @Test
    fun tagEditorAndInspectSelectionAreAccessible() {
        val dialog = xml("src/main/res/layout/dialog_collection.xml")
        assertTrue(elements(dialog, "TextView").any {
            it.getAttributeNS(androidNs, "labelFor") == "@id/collectionTagId"
        })

        val source = source("HomeActivity")
        assertTrue(source.contains("button.isSelected = on"))
        assertTrue(source.contains("chip.isSelected = isSelected"))
        assertTrue(source.contains("ViewCompat.setStateDescription"))
        assertTrue(source.contains("R.string.selection_selected_state"))
    }

    @Test
    fun eachViewModeHasADistinctBookLayoutAndAccessibleDetails() {
        val layouts = listOf(
            "src/main/res/layout/item_inspect_tile.xml",
            "src/main/res/layout/item_inspect_content.xml",
            "src/main/res/layout/item_inspect_icon.xml",
        ).map(::xml)

        val signatures = mutableSetOf<String>()
        layouts.forEach { layout ->
            val thumbnail = elementById(layout, "inspectThumb")
            assertEquals("ImageView", thumbnail.tagName)
            assertTrue(thumbnail.getAttributeNS(androidNs, "contentDescription").isNotBlank())
            assertNotNull(elementById(layout, "inspectTitle"))
            assertNotNull(elementById(layout, "inspectSubtitle"))

            val root = layout.documentElement
            signatures += listOf(
                root.getAttributeNS(androidNs, "orientation"),
                root.getAttributeNS(androidNs, "minHeight"),
                root.getAttributeNS(androidNs, "background"),
            ).joinToString("|")
        }
        assertEquals("Tiles, Content, and Icons must remain visually distinct", 3, signatures.size)

        val detail = elementById(layouts[1], "inspectOpen")
        assertEquals("androidx.appcompat.widget.AppCompatImageButton", detail.tagName)
        assertEquals("@string/home_open_details", detail.getAttributeNS(androidNs, "contentDescription"))

        val source = source("HomeActivity")
        assertTrue(source.contains("view.setOnClickListener { open() }"))
        assertTrue(source.contains("view.findViewById<View>(R.id.inspectOpen)?.apply"))
        assertTrue(source.contains("if (item.current != null) openEntryDetails(summary.entryId)"))
    }

    @Test
    fun scanBoxLaunchesThePrivateQrResultFlow() {
        val home = xml("src/main/res/layout/activity_home.xml")
        val scan = elementById(home, "scanBox")
        assertEquals("com.google.android.material.button.MaterialButton", scan.tagName)
        assertEquals("@string/inspect_scan_box", scan.getAttributeNS(androidNs, "text"))
        assertEquals("@drawable/ic_qr_scan", scan.getAttributeNS(appNs, "icon"))

        val homeSource = source("HomeActivity")
        assertTrue(homeSource.contains("binding.scanBox.setOnClickListener"))
        assertTrue(homeSource.contains("qrScanner.launch(Intent(this, QrScannerActivity::class.java))"))
        assertTrue(homeSource.contains("binding.scanBox.visibility = if (tab == HomeTab.INSPECT)"))

        val manifest = xml("src/main/AndroidManifest.xml")
        val scannerActivity = elements(manifest, "activity").first {
            it.getAttributeNS(androidNs, "name") == ".QrScannerActivity"
        }
        assertEquals("false", scannerActivity.getAttributeNS(androidNs, "exported"))
        assertEquals(0, scannerActivity.getElementsByTagName("intent-filter").length)
    }

    @Test
    fun qrScreenHasAccessibleHelpAndCloseControls() {
        val layout = xml("src/main/res/layout/activity_qr_scanner.xml")
        assertEquals(
            "@string/qr_scanner_help",
            elementById(layout, "qrPreview").getAttributeNS(androidNs, "contentDescription"),
        )

        val helpLabels = elements(layout, "TextView").filter {
            it.getAttributeNS(androidNs, "text") == "@string/qr_scanner_help"
        }
        assertTrue("The scanner instructions must be exposed as real text", helpLabels.isNotEmpty())

        val close = elementById(layout, "closeQrScanner")
        assertEquals("androidx.appcompat.widget.AppCompatImageButton", close.tagName)
        assertEquals("@string/qr_scanner_close", close.getAttributeNS(androidNs, "contentDescription"))
        assertEquals("@drawable/ic_cancel", close.getAttributeNS(appNs, "srcCompat"))
    }

    @Test
    fun qrPayloadIsReturnedThroughActivityResultAndMatchedOnlyAsATagId() {
        val scanner = source("QrScannerActivity")
        assertTrue(scanner.contains("ActivityResultContracts.RequestPermission()"))
        assertTrue(scanner.contains(".setBarcodeFormats(Barcode.FORMAT_QR_CODE)"))
        assertTrue(scanner.contains("setResult(RESULT_OK, Intent().putExtra(EXTRA_TAG_ID, raw))"))
        assertFalse(scanner.contains("Intent.ACTION_VIEW"))
        assertFalse(scanner.contains("Uri.parse"))
        assertFalse(scanner.contains("startActivity("))

        val home = source("HomeActivity")
        val callback = home.substringAfter("private val qrScanner = registerForActivityResult(")
            .substringBefore("override fun onCreate")
        assertTrue(callback.contains("ActivityResultContracts.StartActivityForResult()"))
        assertTrue(callback.contains("getStringExtra(QrScannerActivity.EXTRA_TAG_ID)"))
        assertTrue(callback.contains("findCollectionByTagId(Collections.allRecords(this), raw.orEmpty())"))
        assertFalse(callback.contains("Intent.ACTION_VIEW"))
        assertFalse(callback.contains("Uri.parse"))
        assertFalse(callback.contains("UUID"))
        assertFalse(callback.contains("it.id == raw"))
    }

    @Test
    fun viewModeIsADevicePreferenceAndInspectReadsDurableInventory() {
        val prefs = source("Prefs")
        assertTrue(prefs.contains("fun inspectViewMode(ctx: Context): String"))
        assertTrue(prefs.contains("str(ctx, \"inspect_view_mode\").ifEmpty { \"tiles\" }"))
        assertTrue(prefs.contains("fun setInspectViewMode(ctx: Context, mode: String)"))
        assertTrue(prefs.contains("put(ctx, \"inspect_view_mode\" to mode.trim().lowercase())"))

        val home = source("HomeActivity")
        assertTrue(home.contains("Prefs.inspectViewMode(this)"))
        assertTrue(home.contains("Prefs.setInspectViewMode(this, inspectViewMode.wireValue)"))
        for (layout in listOf("item_inspect_tile", "item_inspect_content", "item_inspect_icon")) {
            assertTrue(home.contains("R.layout.$layout"))
        }

        val refreshInspect = home.substringAfter("private fun refreshInspect()")
            .substringBefore("private fun renderInspectBooks")
        assertTrue(refreshInspect.contains("CollectionInventory.items(this)"))
        assertFalse(refreshInspect.contains("Entries.recent(this)"))

        val inventory = source("CollectionInventory")
        assertTrue(inventory.contains("File(ctx.filesDir, COLLECTION_INVENTORY_FILE)"))
        assertTrue(inventory.contains("mergeCollectionInventory(read(ctx).summaries.values, Entries.recent(ctx))"))

        val entries = source("Entries")
        val pruning = entries.substringAfter("suspend fun pruneSent")
            .substringBefore("fun atomicWrite")
        assertTrue(pruning.contains("CollectionInventory.recordFinalized"))
        assertTrue(
            pruning.indexOf("CollectionInventory.recordFinalized") <
                pruning.indexOf("CaptureMetadataStore.deleteIfNoUnsyncedLocalMutation"),
        )
    }

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()

    private fun xml(path: String): Document {
        val factory = DocumentBuilderFactory.newInstance()
        factory.isNamespaceAware = true
        return factory.newDocumentBuilder().parse(File(path))
    }

    private fun elementById(document: Document, id: String): Element =
        requireNotNull(elements(document, "*").firstOrNull {
            it.getAttributeNS(androidNs, "id") in listOf("@+id/$id", "@id/$id")
        }) { "Missing view id $id" }

    private fun elements(document: Document, tag: String): List<Element> {
        val nodes = document.getElementsByTagName(tag)
        return (0 until nodes.length).map { nodes.item(it) as Element }
    }
}
