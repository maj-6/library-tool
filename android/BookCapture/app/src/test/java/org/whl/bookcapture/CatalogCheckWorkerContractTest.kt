package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CatalogCheckWorkerContractTest {

    private val source by lazy {
        File("src/main/java/org/whl/bookcapture/ProcessWorker.kt")
            .readText()
            .replace("\r\n", "\n")
    }

    @Test
    fun openCheckUsesPureChLookupWithoutChangingEntryDecisionState() {
        val openCheck = source
            .substringAfter("private fun processOpenCatalogCheck(")
            .substringBefore("private fun catalogCheckFailure(")
        val pureLookup = source
            .substringAfter("private fun lookupChList(")
            .substringBefore("private fun searchChList(")

        assertTrue(openCheck.contains("val ch = lookupChList(ctx, metadata)"))
        assertFalse(openCheck.contains("searchChList("))
        assertFalse(openCheck.contains("ChMatchStore"))
        assertFalse(pureLookup.contains("ChMatchStore"))
    }

    @Test
    fun unavailablePhotoWorkBecomesTerminalAtTheAutomaticRetryLimit() {
        val ocrWait = source
            .substringAfter("if (missingOcr || waitingForJpeg)")
            .substringBefore("val pendingCheck =")
        val sidecarWait = source
            .substringAfter("if (!sidecar.isFile)")
            .substringBefore("val text = runCatching")

        assertTrue(ocrWait.contains("shouldRetryProcessingWork("))
        assertTrue(ocrWait.contains("catalogCheckFailure("))
        assertTrue(sidecarWait.contains("shouldRetryProcessingWork("))
        assertTrue(sidecarWait.contains("catalogCheckFailure("))
    }

    @Test
    fun unboundDurableIntentWaitsForCameraCommitInsteadOfFailing() {
        val reservationWait = source
            .substringAfter("if (pendingAtStart != null && pendingAtStart.targetAssetId == null)")
            .substringBefore("val allPhotos =")
        val openCheck = source
            .substringAfter("private fun processOpenCatalogCheck(")
            .substringBefore("val target = catalogCheckPhoto")

        assertTrue(reservationWait.contains("Entries.markWaiting("))
        assertTrue(reservationWait.contains("return DirectoryOutcome()"))
        assertTrue(openCheck.contains("if (request.targetAssetId == null)"))
        assertTrue(openCheck.contains("return DirectoryOutcome()"))
    }
}
