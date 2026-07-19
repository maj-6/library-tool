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
    fun singleEntryDiscardRequiresExplicitConfirmation() {
        val source = File("src/main/java/org/whl/bookcapture/EntryDetailActivity.kt").readText()

        assertTrue(source.contains("binding.discard.setOnClickListener { showDiscardConfirmation(entry) }"))
        assertTrue(source.contains("MaterialAlertDialogBuilder(this)"))
        assertTrue(source.contains("setNegativeButton(android.R.string.cancel, null)"))
        assertTrue(source.contains("setPositiveButton(R.string.detail_discard_confirm)"))
        assertTrue(source.contains("setTextColor(getColor(R.color.whl_red))"))
    }

    @Test
    fun secondaryScreensExposeVisibleUpNavigation() {
        for (path in listOf(
            "src/main/res/layout/activity_settings.xml",
            "src/main/res/layout/activity_entry_detail.xml",
        )) {
            val toolbar = elementById(xml(path), "toolbar")
            assertEquals(
                "com.google.android.material.appbar.MaterialToolbar",
                toolbar.tagName,
            )
            assertEquals(
                "@drawable/ic_arrow_back",
                toolbar.getAttributeNS(appNs, "navigationIcon"),
            )
            assertEquals(
                "@string/navigate_up",
                toolbar.getAttributeNS(appNs, "navigationContentDescription"),
            )
        }
    }

    @Test
    fun chromeTextActionsUseSemanticMaterialButtons() {
        val expected = mapOf(
            "src/main/res/layout/activity_home.xml" to
                listOf(
                    "btnSelect", "deleteSelected", "cancelSelection",
                    "tabScans", "tabCollections",
                ),
            "src/main/res/layout/item_collection.xml" to
                listOf("editCollection", "deleteCollection"),
            "src/main/res/layout/activity_main.xml" to
                listOf("queueChip", "btnSettings"),
        )
        for ((path, ids) in expected) {
            val layout = xml(path)
            for (id in ids) {
                val action = elementById(layout, id)
                assertEquals(
                    "com.google.android.material.button.MaterialButton",
                    action.tagName,
                )
                assertEquals("@style/WhlToolbarAction", action.getAttribute("style"))
            }
        }
    }

    /**
     * A book scan must never begin without a collection behind it. There are two
     * ways in — the Home button and the spoken word "start" — and gating only
     * the first would leave provenance optional for anyone already standing on
     * the camera screen.
     */
    @Test
    fun everyRouteIntoCaptureRequiresACollection() {
        val home = xml("src/main/res/layout/activity_home.xml")
        assertNotNull(elementById(home, "collectionsList"))
        assertNotNull(elementById(home, "collectionBar"))
        assertNotNull(elementById(home, "newCollection"))

        val homeSource = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val gate = homeSource.indexOf("if (!resuming && Collections.current(this) == null)")
        val launch = homeSource.indexOf("Intent(this, MainActivity::class.java)")
        assertTrue("Home must check for a collection", gate >= 0)
        assertTrue("the check must precede the camera launch", gate < launch)
        // ...but an already-open capture keeps its way back to the camera, or a
        // half-photographed book could be neither sealed nor discarded.
        val resuming = homeSource.indexOf("val resuming = Prefs.currentEntryId(this) != null")
        assertTrue("the gate must exempt a capture already in progress", resuming in 0 until gate)

        val capture = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        assertTrue(capture.contains("val collection = Collections.current(this)"))
        assertTrue(capture.contains("session.start(collection)"))

        // start() takes the collection rather than reading it back from Prefs,
        // so the requirement is enforced by the type, not by remembering to ask.
        val session = File("src/main/java/org/whl/bookcapture/CaptureSession.kt").readText()
        assertTrue(session.contains("fun start(collection: BookCollection): String"))
        assertFalse(session.contains("fun start(): String"))
    }

    /**
     * Home's chrome is the app mark plus the two tabs. Settings moved into a
     * menu behind the mark, so Home must carry no gear of its own — the capture
     * screen keeps its own, which is why this checks the file and not the app.
     */
    @Test
    fun homeReachesSettingsThroughTheAppMarkNotAGear() {
        val home = xml("src/main/res/layout/activity_home.xml")
        assertNotNull(elementById(home, "appMenu"))
        assertFalse(hasElementWithId(home, "btnSettings"))
        assertFalse(
            "the wordmark and the separate tab strip were folded into the toolbar",
            hasElementWithId(home, "tabBar"),
        )

        val menu = xml("src/main/res/menu/home_app_menu.xml")
        for (id in listOf("menuSettings", "menuAbout", "menuCheckUpdates")) {
            assertNotNull("app menu is missing $id", elementById(menu, id))
        }

        val source = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        assertTrue(source.contains("binding.appMenu.setOnClickListener { showAppMenu() }"))
        assertTrue(source.contains("R.id.menuSettings ->"))
        assertTrue(source.contains("SettingsActivity::class.java"))
    }

    @Test
    fun voiceIsOptionalAndDiscardRequiresConfirmation() {
        val manifest = xml("src/main/AndroidManifest.xml")
        val features = manifest.getElementsByTagName("uses-feature")
        val microphone = (0 until features.length)
            .map { features.item(it) as Element }
            .first { it.getAttributeNS(androidNs, "name") == "android.hardware.microphone" }
        assertEquals("false", microphone.getAttributeNS(androidNs, "required"))

        val settings = xml("src/main/res/layout/activity_settings.xml")
        assertNotNull(elementById(settings, "voiceControl"))

        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        assertTrue(source.contains("arrayOf(Manifest.permission.CAMERA)"))
        assertTrue(source.contains("arrayOf(Manifest.permission.RECORD_AUDIO)"))
        assertTrue(source.contains("if (word == \"cancel\" && session.active && !discardConfirmed)"))
        assertTrue(source.contains("MaterialAlertDialogBuilder(this)"))
    }

    @Test
    fun localCaptureIsAvailableWithoutPublicSignup() {
        val login = xml("src/main/res/layout/activity_login.xml")
        assertNotNull(elementById(login, "continueLocal"))
        assertFalse(hasElementWithId(login, "register"))

        val home = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val capture = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val auth = File("src/main/java/org/whl/bookcapture/Auth.kt").readText()
        assertFalse(home.contains("if (!Auth.signedIn(this))"))
        assertFalse(capture.contains("if (!Auth.signedIn(this))"))
        assertFalse(auth.contains("session(ctx, \"signup\""))
        assertTrue(home.contains("Prefs.transport(this) != \"cloud\""))
        assertTrue(capture.contains("Prefs.transport(this) != \"cloud\""))
    }

    @Test
    fun acceptedCameraWritesSurviveActivityRecreation() {
        val main = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val session = File("src/main/java/org/whl/bookcapture/CaptureSession.kt").readText()
        assertTrue(main.contains("if (!captureQueue.busy) discardAllCaptureRequests()"))
        assertTrue(main.contains("finishAfterAcceptedCaptures"))
        assertTrue(session.contains("ActiveCaptureWrites.register"))
        assertTrue(session.contains("filterNot(ActiveCaptureWrites::isActive)"))
        assertTrue(session.contains("fun refreshPhotoCount()"))

        val manifest = xml("src/main/AndroidManifest.xml")
        val activities = manifest.getElementsByTagName("activity")
        val camera = (0 until activities.length)
            .map { activities.item(it) as Element }
            .first { it.getAttributeNS(androidNs, "name") == ".MainActivity" }
        assertTrue(camera.getAttributeNS(androidNs, "configChanges").contains("uiMode"))
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
