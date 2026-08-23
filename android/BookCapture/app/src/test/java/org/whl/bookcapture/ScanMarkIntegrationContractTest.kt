package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun inspectQueueUsesVoiceSessionsMistralOcrAndPhotoFreeDescriptors() {
        val homeLayout = xml("src/main/res/layout/activity_home.xml")
        assertNotNull(elementById(homeLayout, "queueScanBook"))
        assertNotNull(elementById(homeLayout, "scanQueueSummary"))

        val home = source("HomeActivity")
        assertTrue(home.contains("scanSearchCamera.launch("))
        assertTrue(home.contains("CoverScannerActivity.EXTRA_QUEUE_SESSION"))
        assertTrue(home.contains("ScanSearchQueueSyncWorker.enqueue("))
        assertTrue(home.contains("showScanSearchProposalReview(queueId, item)"))
        assertTrue(!home.contains("chooseScanSearchPhotoRole"))
        val inspectWatcher = home.substringAfter(
            "binding.inspectBookSearch.doAfterTextChanged",
        ).substringBefore("binding.inspectBookSearch.setOnEditorActionListener")
        assertTrue(inspectWatcher.contains("activeScanSearchQueueId = null"))
        assertTrue(inspectWatcher.contains("activeScanSearchProposal = null"))

        val approveFlow = home.substringAfter(
            "private fun approveScanSearchProposal(",
        ).substringBefore("private fun rejectScanSearchProposal(")
        assertTrue(approveFlow.indexOf("client.approve(") in
            0 until approveFlow.indexOf("CaptureScanMarkStore.write("))
        assertTrue(approveFlow.contains("ScanSearchQueue.acknowledge("))
        assertTrue(approveFlow.contains("refreshLiveScanSearchQueue("))
        assertFalse(approveFlow.contains("InspectBookMemberships.move("))
        assertFalse(approveFlow.contains("InspectBookMemberships.markCloud("))
        assertFalse(approveFlow.contains("ScanSearchQueue.approveProposal("))

        val rejectFlow = home.substringAfter(
            "private fun rejectScanSearchProposal(",
        ).substringBefore("private fun refreshLiveScanSearchQueue(")
        assertTrue(rejectFlow.contains("client.reject("))
        assertTrue(rejectFlow.contains("ScanSearchQueue.acknowledge("))
        assertTrue(rejectFlow.contains("refreshLiveScanSearchQueue("))
        assertFalse(rejectFlow.contains("ScanSearchQueue.rejectProposal("))

        val scanner = source("CoverScannerActivity")
        assertTrue(scanner.contains("Pipeline.coverOcr(target, mistralKey)"))
        assertTrue(scanner.contains("discardTemp(target)"))
        assertTrue(scanner.contains("ScanSearchQueue.enqueueDraft("))
        assertTrue(scanner.contains("ScanSearchQueue.routeSession("))
        assertTrue(scanner.contains("\"a\", \"b\", \"c\""))
        assertTrue(scanner.contains("ScanSearchPhotoRole.TITLE_PAGE"))
        assertTrue(scanner.contains("coverScannerHelpPanel.visibility = View.GONE"))
        assertTrue(scanner.contains("captureCover.visibility = View.GONE"))
        assertTrue(scanner.contains("R.string.scan_queue_capture_title"))
        assertEquals("mistral-ocr-4-1", Pipeline.COVER_OCR_MODEL)

        val queue = source("ScanSearchQueue")
        assertTrue(queue.contains("val ocrText: String"))
        assertTrue(queue.contains("val photoRole: ScanSearchPhotoRole"))
        assertTrue(queue.contains("val visualSignature: String"))
        assertTrue(queue.contains("val matchConfidence: Double?"))
        assertTrue(!queue.contains("photoPath"))
        assertTrue(!queue.contains("imageBytes"))

        val client = source("ScanWorkflowClient")
        assertTrue(client.contains("approve_scan_search"))
        assertTrue(client.contains("reject_scan_search"))
        assertFalse(client.contains("complete_scan_search"))

        val strings = xml("src/main/res/values/strings.xml")
        val all = strings.getElementsByTagName("string")
        (0 until all.length)
            .map { all.item(it) as Element }
            .filter { it.getAttribute("name").startsWith("scan_queue_") }
            .forEach { value ->
                val visible = value.textContent.lowercase()
                assertFalse("queue prompt names a cover: $visible", visible.contains("cover"))
                assertFalse(
                    "queue prompt names a title page: $visible",
                    visible.contains("title page"),
                )
            }
    }

    @Test
    fun collectionPurposeAndActiveSelectionsAreIndependent() {
        val dialog = xml("src/main/res/layout/dialog_collection.xml")
        assertNotNull(elementById(dialog, "collectionType"))
        val home = source("HomeActivity")
        assertTrue(home.contains("Prefs.setCurrentCollectionId(this, c.id)"))
        assertTrue(home.contains("Prefs.setCurrentScanCollectionIds(this, selections)"))
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
