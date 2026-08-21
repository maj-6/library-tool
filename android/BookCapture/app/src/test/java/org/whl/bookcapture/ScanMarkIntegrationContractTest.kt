package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class ScanMarkIntegrationContractTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"

    @Test
    fun captureExposesButtonAndFinalOnlyVoiceMark() {
        val layout = xml("src/main/res/layout/activity_main.xml")
        val button = elementById(layout, "btnMarkScan")
        assertEquals("@string/cmd_scan", button.getAttributeNS(androidNs, "contentDescription"))

        val main = source("MainActivity")
        assertTrue(main.contains("binding.btnMarkScan.setOnClickListener { command(\"scan\") }"))
        assertTrue(main.contains("\"scan\" -> markCurrentBookForScan()"))
        assertTrue(main.contains("CaptureScanMarkStore.stageMembership(this, id)"))
        assertEquals(
            PolicyVoiceCommand.SCAN,
            StateAwareVoiceCommandPolicy.evaluate(
                "scan",
                VoiceCommandState.IDLE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
        assertNull(
            StateAwareVoiceCommandPolicy.evaluate(
                "scan",
                VoiceCommandState.IDLE,
                VoiceRecognitionStability.STABLE_PARTIAL,
            ),
        )
    }

    @Test
    fun inspectQueueUsesEphemeralMistralOcrAndDurableTextOnlyQueue() {
        val homeLayout = xml("src/main/res/layout/activity_home.xml")
        assertNotNull(elementById(homeLayout, "queueScanBook"))
        assertNotNull(elementById(homeLayout, "scanQueueSummary"))

        val home = source("HomeActivity")
        assertTrue(home.contains("scanSearchCamera.launch("))
        assertTrue(home.contains("ScanSearchQueue.enqueue("))
        assertTrue(home.contains("ScanSearchQueueSyncWorker.enqueue("))
        assertTrue(home.contains("showScanSearchMatchConfirmation(queueId, item)"))

        val scanner = source("CoverScannerActivity")
        assertTrue(scanner.contains("Pipeline.coverOcr(target, mistralKey)"))
        assertTrue(scanner.contains("discardTemp(target)"))
        assertTrue(scanner.contains("ScanSearchPhotoRole.TITLE_PAGE"))
        assertEquals("mistral-ocr-4-1", Pipeline.COVER_OCR_MODEL)

        val queue = source("ScanSearchQueue")
        assertTrue(queue.contains("val ocrText: String"))
        assertTrue(queue.contains("val photoRole: ScanSearchPhotoRole"))
        assertTrue(!queue.contains("photoPath"))
        assertTrue(!queue.contains("imageBytes"))
    }

    @Test
    fun collectionPurposeAndActiveSelectionsAreIndependent() {
        val dialog = xml("src/main/res/layout/dialog_collection.xml")
        assertNotNull(elementById(dialog, "collectionType"))
        val home = source("HomeActivity")
        assertTrue(home.contains("Prefs.setCurrentCollectionId(this, c.id)"))
        assertTrue(home.contains("Prefs.setCurrentScanCollectionId(this, c.id)"))
        assertTrue(home.contains("collectionType = collectionType"))
    }

    private fun source(name: String): String = File(
        "src/main/java/org/whl/bookcapture/$name.kt",
    ).readText()

    private fun xml(path: String) = DocumentBuilderFactory.newInstance().apply {
        isNamespaceAware = true
    }.newDocumentBuilder().parse(File(path))

    private fun elementById(document: org.w3c.dom.Document, id: String): Element {
        val all = document.getElementsByTagName("*")
        return (0 until all.length)
            .map { all.item(it) as Element }
            .first { it.getAttributeNS(androidNs, "id") == "@+id/$id" }
    }
}
