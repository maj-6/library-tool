package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * The row label and the empty-sync message are the two sentences that told the
 * user their captures had failed to sync when in fact the pages were already
 * delivered and only the phone's OCR had failed. Both are pinned here.
 */
class CaptureStatusLabelTest {

    private fun label(
        uploaded: Boolean,
        status: Entries.ProcessingStatus,
        bestStatus: Entries.ProcessingStatus? = null,
        sealed: Boolean = true,
        processingRecorded: Boolean = true,
        hasMeta: Boolean = false,
        importOutcome: String? = null,
    ) = Entries.captureStatusLabel(
        uploaded = uploaded,
        sealed = sealed,
        processingRecorded = processingRecorded,
        hasMeta = hasMeta,
        status = status,
        bestStatus = bestStatus,
        importOutcome = importOutcome,
        pendingLabel = { "complete · pending upload" },
    )

    @Test
    fun aDeliveredCaptureWhoseOcrFailedDoesNotSayFailed() {
        // The exact case behind the report: a rejected Mistral key marks the
        // capture FAILED, but the photos shipped. Saying "failed" made a
        // successful delivery read as a sync failure.
        val text = label(uploaded = true, status = Entries.ProcessingStatus.FAILED)

        assertTrue(text, text.startsWith("complete · uploaded"))
        assertEquals("complete · uploaded · no details read", text)
    }

    @Test
    fun aDeliveredAndImportedCaptureWhoseOcrFailedStillReadsAsImported() {
        assertEquals(
            "complete · imported · no details read",
            label(
                uploaded = true,
                status = Entries.ProcessingStatus.FAILED,
                importOutcome = "imported",
            ),
        )
    }

    @Test
    fun aCloudSideFailureStillOutranksEverything() {
        // A void or failed import is a real delivery problem and must not be
        // softened into "complete".
        assertEquals(
            "import failed",
            label(
                uploaded = true,
                status = Entries.ProcessingStatus.COMPLETE,
                importOutcome = "import failed",
            ),
        )
    }

    @Test
    fun anUndeliveredFailureStillSaysFailed() {
        assertEquals(
            "failed",
            label(uploaded = false, status = Entries.ProcessingStatus.FAILED),
        )
    }

    @Test
    fun aFailedRetryOverGoodMetadataDoesNotDiscardTheEarlierSuccess() {
        assertEquals(
            "complete · retry failed",
            label(
                uploaded = false,
                status = Entries.ProcessingStatus.FAILED,
                bestStatus = Entries.ProcessingStatus.COMPLETE,
                hasMeta = true,
            ),
        )
    }

    @Test
    fun aDeliveredCaptureThatOnceExtractedCleanlyCarriesNoQualifier() {
        assertEquals(
            "complete · uploaded",
            label(
                uploaded = true,
                status = Entries.ProcessingStatus.FAILED,
                bestStatus = Entries.ProcessingStatus.COMPLETE,
                hasMeta = true,
            ),
        )
    }

    @Test
    fun aDeliveredPartialSaysWhatIsMissingRatherThanPartial() {
        assertEquals(
            "complete · uploaded · some details missing",
            label(
                uploaded = true,
                status = Entries.ProcessingStatus.PARTIAL,
                hasMeta = true,
            ),
        )
    }

    @Test
    fun aCleanDeliveredCaptureIsUnchanged() {
        assertEquals(
            "complete · uploaded",
            label(uploaded = true, status = Entries.ProcessingStatus.COMPLETE, hasMeta = true),
        )
    }

    @Test
    fun legacySentEntriesWithoutProcessingStateKeepTheirCompactLabel() {
        assertEquals(
            "uploaded",
            label(
                uploaded = true,
                status = Entries.ProcessingStatus.WAITING,
                processingRecorded = false,
            ),
        )
        assertEquals(
            "imported",
            label(
                uploaded = true,
                status = Entries.ProcessingStatus.WAITING,
                processingRecorded = false,
                importOutcome = "imported",
            ),
        )
    }

    @Test
    fun anOpenCaptureStillReportsCapturing() {
        assertEquals(
            "capturing · waiting",
            label(
                uploaded = false,
                status = Entries.ProcessingStatus.WAITING,
                sealed = false,
            ),
        )
    }

    @Test
    fun theDeliveredAdornmentSurvivesAQualifier() {
        // The row still shows the delivered mark; the qualifier becomes the
        // visible text rather than replacing the mark.
        val presented = homeStatusPresentation("complete · uploaded · no details read")

        assertEquals(HomeStatusAdornment.UPLOADED, presented.adornment)
        assertEquals("no details read", presented.text)
        assertEquals("uploaded · no details read", presented.accessibilityLabel)
    }

    @Test
    fun aPlainDeliveredRowStillShowsOnlyTheMark() {
        val presented = homeStatusPresentation("complete · uploaded")

        assertEquals(HomeStatusAdornment.UPLOADED, presented.adornment)
        assertEquals("", presented.text)
    }

    @Test
    fun anOpenCaptureRowIsUnaffectedByTheQualifierSplit() {
        val presented = homeStatusPresentation("capturing · waiting")

        assertEquals(HomeStatusAdornment.WAITING, presented.adornment)
        assertEquals("capturing", presented.text)
    }

    // --- why a sync press found nothing ---------------------------------------

    @Test
    fun everythingDeliveredIsNotTheSameAsNothingToSync() {
        assertEquals(
            CaptureSyncEmptyReason.ALL_DELIVERED,
            captureSyncEmptyReason(
                requestedCount = 0,
                pendingReviewChanges = false,
                liveCaptureOpen = false,
                deliveredCount = 12,
                deliveredNeedingAttention = 0,
            ),
        )
    }

    @Test
    fun deliveredCapturesNeedingOcrGetTheirOwnAnswer() {
        assertEquals(
            CaptureSyncEmptyReason.DELIVERED_WITH_PROCESSING_ISSUES,
            captureSyncEmptyReason(
                requestedCount = 0,
                pendingReviewChanges = false,
                liveCaptureOpen = false,
                deliveredCount = 12,
                deliveredNeedingAttention = 9,
            ),
        )
    }

    @Test
    fun aQueuedReviewEditStillTakesPrecedence() {
        assertEquals(
            CaptureSyncEmptyReason.REVIEW_QUEUED,
            captureSyncEmptyReason(
                requestedCount = 0,
                pendingReviewChanges = true,
                liveCaptureOpen = false,
                deliveredCount = 12,
                deliveredNeedingAttention = 9,
            ),
        )
    }

    @Test
    fun aNonEmptyBatchHasNoEmptyReason() {
        assertEquals(
            CaptureSyncEmptyReason.NOTHING,
            captureSyncEmptyReason(
                requestedCount = 3,
                pendingReviewChanges = true,
                liveCaptureOpen = true,
                deliveredCount = 12,
                deliveredNeedingAttention = 9,
            ),
        )
    }

    @Test
    fun aFreshInstallWithNothingAtAllStillSaysNothing() {
        assertEquals(
            CaptureSyncEmptyReason.NOTHING,
            captureSyncEmptyReason(
                requestedCount = 0,
                pendingReviewChanges = false,
                liveCaptureOpen = false,
                deliveredCount = 0,
                deliveredNeedingAttention = 0,
            ),
        )
    }

    // --- an explicit retry must be able to undo a terminal blank-OCR verdict ---

    @Test
    fun requestingAReprocessClearsTheEmptySidecarsThatPinTheFailure() {
        val root = Files.createTempDirectory("whl-reprocess-").toFile()
        try {
            val dir = File(root, "entry-1").apply { mkdirs() }
            File(dir, "photo_1.jpg").writeText("page")
            File(dir, "photo_2.jpg").writeText("page")
            File(dir, "manifest.json").writeText("{}")
            // photo_1 OCR'd to nothing; photo_2 produced real text.
            File(dir, "photo_1.jpg.txt").writeText("")
            File(dir, "photo_2.jpg.txt").writeText("real text")
            val entry = Entries.Entry(
                id = "entry-1",
                dir = dir,
                sealed = true,
                uploaded = false,
                createdAt = 1L,
                photoCount = 2,
                meta = null,
                cloudStatus = "",
                processing = Entries.ProcessingState(
                    status = Entries.ProcessingStatus.FAILED,
                    stage = Entries.ProcessingStage.EXTRACTION,
                    retryable = false,
                    lastError = "Extraction: OCR returned no text",
                    updatedAt = 1L,
                ),
                processingRecorded = true,
            )

            assertTrue(entry.requestReprocess())

            assertTrue(
                "an empty sidecar is the commit marker that skips the page forever",
                !File(dir, "photo_1.jpg.txt").exists(),
            )
            assertEquals("real text", File(dir, "photo_2.jpg.txt").readText())
            assertTrue(entry.reprocessPending())
        } finally {
            root.deleteRecursively()
        }
    }
}
