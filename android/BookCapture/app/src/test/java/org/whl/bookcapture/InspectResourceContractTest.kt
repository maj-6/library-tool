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
    fun inspectLongPressBuildsAnAccessibleMoveDeleteSelection() {
        val source = source("HomeActivity")
        val binding = source.substringAfter("private fun bindInspectBook(")
            .substringBefore("private val inspectActionModeCallback")
        assertTrue(binding.contains("view.setOnLongClickListener"))
        assertTrue(binding.contains("selectInspectBook(entryId)"))
        assertTrue(binding.contains("if (inspectActionMode != null) toggleInspectSelection(entryId)"))
        assertTrue(binding.contains("AccessibilityActionCompat.ACTION_LONG_CLICK"))
        assertTrue(binding.contains("R.string.inspect_select_book"))

        val actionMode = source.substringAfter("private val inspectActionModeCallback")
            .substringBefore("private fun mutateInspectSelection(")
        assertTrue(actionMode.contains("menu.add(Menu.NONE, MENU_INSPECT_MOVE"))
        assertTrue(actionMode.contains("menu.add(Menu.NONE, MENU_INSPECT_DELETE"))
        assertTrue(actionMode.contains("showInspectMoveDialog()"))
        assertTrue(actionMode.contains("showInspectDeleteConfirmation()"))
        assertTrue(actionMode.contains("startSupportActionMode(inspectActionModeCallback)"))
        assertTrue(actionMode.contains("view.isActivated = selected"))
        assertTrue(actionMode.contains("ViewCompat.setStateDescription("))
        assertTrue(actionMode.contains("binding.inspectBooks.announceForAccessibility("))

        assertTrue(source.contains("outState.putStringArrayList("))
        assertTrue(source.contains("STATE_INSPECT_SELECTED_IDS"))
    }

    @Test
    fun contentModeIsAnInlineTextTableWithOnlyADecorativeCoverSwatch() {
        val layout = xml("src/main/res/layout/item_inspect_content.xml")
        val root = layout.documentElement

        assertEquals("horizontal", root.getAttributeNS(androidNs, "orientation"))
        assertFalse(elements(layout, "*").any {
            it.tagName == "ImageView" || it.tagName.endsWith("ImageButton")
        })

        val swatch = elementById(layout, "inspectCoverSwatch")
        assertEquals("View", swatch.tagName)
        assertEquals("8dp", swatch.getAttributeNS(androidNs, "layout_width"))
        assertEquals("26dp", swatch.getAttributeNS(androidNs, "layout_height"))
        assertEquals("no", swatch.getAttributeNS(androidNs, "importantForAccessibility"))
        assertTrue(swatch.getAttributeNS(androidNs, "contentDescription").isEmpty())

        listOf("inspectTitle", "inspectAuthor", "inspectYear").forEach { id ->
            val field = elementById(layout, id)
            assertEquals("TextView", field.tagName)
            assertTrue("$id must be an inline cell", field.parentNode === root)
            assertEquals("1", field.getAttributeNS(androidNs, "maxLines"))
            assertEquals("end", field.getAttributeNS(androidNs, "ellipsize"))
            assertEquals("false", field.getAttributeNS(androidNs, "includeFontPadding"))
        }
        assertEquals("0dp", elementById(layout, "inspectTitle")
            .getAttributeNS(androidNs, "layout_width"))
        assertTrue(elementById(layout, "inspectTitle")
            .getAttributeNS(androidNs, "layout_weight").isNotEmpty())
        assertEquals("0dp", elementById(layout, "inspectAuthor")
            .getAttributeNS(androidNs, "layout_width"))
        assertTrue(elementById(layout, "inspectAuthor")
            .getAttributeNS(androidNs, "layout_weight").isNotEmpty())
        assertEquals("44dp", elementById(layout, "inspectYear")
            .getAttributeNS(androidNs, "layout_width"))
    }

    @Test
    fun tileModeKeepsTheCompactBookMetrics() {
        val layout = xml("src/main/res/layout/item_inspect_tile.xml")
        val root = layout.documentElement
        assertEquals("2dp", root.getAttributeNS(androidNs, "layout_margin"))
        assertEquals("72dp", root.getAttributeNS(androidNs, "minHeight"))
        assertEquals("4dp", root.getAttributeNS(androidNs, "paddingTop"))
        assertEquals("4dp", root.getAttributeNS(androidNs, "paddingBottom"))

        val thumbnail = elementById(layout, "inspectThumb")
        assertEquals("ImageView", thumbnail.tagName)
        assertEquals("match_parent", thumbnail.getAttributeNS(androidNs, "layout_width"))
        assertEquals("match_parent", thumbnail.getAttributeNS(androidNs, "layout_height"))
        val thumbnailFrame = thumbnail.parentNode as Element
        assertEquals("FrameLayout", thumbnailFrame.tagName)
        assertEquals("42dp", thumbnailFrame.getAttributeNS(androidNs, "layout_width"))
        assertEquals("58dp", thumbnailFrame.getAttributeNS(androidNs, "layout_height"))
        assertEquals("6dp", thumbnailFrame.getAttributeNS(androidNs, "layout_marginEnd"))
        assertEquals("no", thumbnail.getAttributeNS(androidNs, "importantForAccessibility"))
        assertTrue(thumbnail.getAttributeNS(androidNs, "contentDescription").isEmpty())
        assertTrue(File("src/main/res/layout/item_inspect_tile.xml").readText()
            .contains("@layout/view_scan_priority_indicator"))

        val title = elementById(layout, "inspectTitle")
        val subtitle = elementById(layout, "inspectSubtitle")
        listOf(title, subtitle).forEach { text ->
            assertEquals("false", text.getAttributeNS(androidNs, "includeFontPadding"))
            assertEquals("0.94", text.getAttributeNS(androidNs, "lineSpacingMultiplier"))
        }
        assertEquals("11sp", title.getAttributeNS(androidNs, "textSize"))
        assertEquals("10sp", subtitle.getAttributeNS(androidNs, "textSize"))
        assertEquals("1dp", subtitle.getAttributeNS(androidNs, "layout_marginTop"))
    }

    @Test
    fun iconModeUsesNormalizedContentAndFixedTextSlots() {
        val layout = xml("src/main/res/layout/item_inspect_icon.xml")
        val root = layout.documentElement
        assertEquals("wrap_content", root.getAttributeNS(androidNs, "layout_height"))
        assertEquals("116dp", root.getAttributeNS(androidNs, "minHeight"))

        val thumbnail = elementById(layout, "inspectThumb")
        assertEquals("ImageView", thumbnail.tagName)
        assertEquals("match_parent", thumbnail.getAttributeNS(androidNs, "layout_width"))
        assertEquals("match_parent", thumbnail.getAttributeNS(androidNs, "layout_height"))
        val thumbnailFrame = thumbnail.parentNode as Element
        assertEquals("FrameLayout", thumbnailFrame.tagName)
        assertEquals("50dp", thumbnailFrame.getAttributeNS(androidNs, "layout_width"))
        assertEquals("68dp", thumbnailFrame.getAttributeNS(androidNs, "layout_height"))
        assertEquals("no", thumbnail.getAttributeNS(androidNs, "importantForAccessibility"))
        assertTrue(thumbnail.getAttributeNS(androidNs, "contentDescription").isEmpty())
        assertTrue(File("src/main/res/layout/item_inspect_icon.xml").readText()
            .contains("@layout/view_scan_priority_indicator"))

        val title = elementById(layout, "inspectTitle")
        val subtitle = elementById(layout, "inspectSubtitle")
        assertEquals("2", title.getAttributeNS(androidNs, "lines"))
        assertEquals("1", subtitle.getAttributeNS(androidNs, "lines"))
        assertEquals("false", title.getAttributeNS(androidNs, "includeFontPadding"))
        assertEquals("false", subtitle.getAttributeNS(androidNs, "includeFontPadding"))
        assertEquals("0.94", title.getAttributeNS(androidNs, "lineSpacingMultiplier"))
        assertEquals("10sp", subtitle.getAttributeNS(androidNs, "textSize"))
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
        assertTrue(
            homeSource.contains(
                "binding.inspectActions.visibility = if (tab == HomeTab.INSPECT)",
            ),
        )

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
        val preview = elementById(layout, "qrPreview")
        assertEquals("no", preview.getAttributeNS(androidNs, "importantForAccessibility"))
        assertTrue(preview.getAttributeNS(androidNs, "contentDescription").isEmpty())

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
        val normalizedCallback = callback.filterNot(Char::isWhitespace)
        assertTrue(callback.contains("ActivityResultContracts.StartActivityForResult()"))
        assertTrue(callback.contains("getStringExtra(QrScannerActivity.EXTRA_TAG_ID)"))
        assertTrue(callback.contains("withContext(Dispatchers.IO)"))
        assertTrue(
            normalizedCallback.contains(
                "findCollectionByTagId(Collections.allRecords(this@HomeActivity),raw.orEmpty())",
            ),
        )
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
        assertTrue(refreshInspect.contains("loadHomeSnapshot"))
        assertTrue(refreshInspect.contains("CollectionInventory.items(this@HomeActivity)"))
        assertFalse(refreshInspect.contains("Entries.recent(this)"))

        val inventory = source("CollectionInventory")
        assertTrue(inventory.contains("File(ctx.filesDir, COLLECTION_INVENTORY_FILE)"))
        assertTrue(inventory.contains("mergeCollectionInventory(read(ctx).summaries.values, Entries.recent(ctx))"))

        val entries = source("Entries")
        val pruning = entries.substringAfter("suspend fun pruneSent")
            .substringBefore("fun atomicWrite")
        assertTrue(pruning.contains("CollectionInventory.recordFinalized"))
        // Retention now archives rather than deletes, but the ordering
        // invariant is unchanged: the photo-free Inspect summary must be
        // durable before any browsing media leaves sent/.
        assertTrue(
            pruning.indexOf("CollectionInventory.recordFinalized") <
                pruning.indexOf("CaptureMetadataStore.archiveIfNoUnsyncedLocalMutation"),
        )
        assertFalse(pruning.contains("deleteIfNoUnsyncedLocalMutation"))
    }

    @Test
    fun lookupIntegrationKeepsFreshCloudAuthorityAndRetiresStaleLoads() {
        val home = source("HomeActivity")
        val lookupMerge = home.substringAfter("private fun inspectLookupRenderItems(")
            .substringBefore("private fun showInspectLookupCollection(")
        assertTrue(lookupMerge.contains("remote.collectionId to existing.second.copy"))
        assertTrue(lookupMerge.contains("val book = mergeInspectLookupBookSources("))
        val mergedBookArguments = lookupMerge
            .substringAfter("val book = mergeInspectLookupBookSources(")
            .substringBefore("val collection =")
        assertFalse(mergedBookArguments.contains("inspect_book_untitled"))

        val contentRefresh = home.substringAfter("private fun refreshContentTab()")
            .substringBefore("private fun scheduleWorkerRefresh(")
        assertTrue(contentRefresh.contains("invalidateRemoteInspectLookup()"))
        val mutation = home.substringAfter("private fun mutateInspectSelection(")
            .substringBefore("override fun onDestroy()")
        assertTrue(mutation.contains("invalidateRemoteInspectLookup()"))
        val remoteBoxRefresh = home.substringAfter("private fun ensureRemoteBoxListing(")
            .substringBefore("private fun releaseDynamicThumbnails()")
        assertTrue(remoteBoxRefresh.contains("invalidateRemoteInspectLookup()"))

        val completion = home.substringAfter("when (inspectLookupCloudCompletion(")
            .substringBefore("inspectLookupCloudLoading = false")
        assertTrue(completion.contains("IGNORE_RETIRED_GENERATION -> return@launch"))
        assertTrue(completion.contains("RESET_STALE_OWNER"))
        assertTrue(completion.contains("resetRemoteInspectLookup(currentOwner)"))

        val strings = xml("src/main/res/values/strings.xml")
        assertTrue(elements(strings, "plurals").any {
            it.getAttribute("name") == "inspect_lookup_matches_cloud_unavailable"
        })
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
