package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.File

class CaptureBookPreviewTest {

    @Test
    fun openCaptureDoesNotDisplaceLastSubmittedBook() {
        val previous = entry("previous", sealed = true, createdAt = 100)
        val open = entry("open", sealed = false, createdAt = 200)

        assertEquals(previous, selectLastSubmittedEntry(listOf(open, previous)))
        assertNull(selectLastSubmittedEntry(listOf(open)))
    }

    @Test
    fun subsequentSubmittedCaptureReplacesPreviousBook() {
        val previous = entry("previous", sealed = true, createdAt = 100)
        val subsequent = entry("subsequent", sealed = true, createdAt = 200)

        assertEquals(subsequent, selectLastSubmittedEntry(listOf(previous, subsequent)))
    }

    @Test
    fun extrasContainEveryNonPrimaryCatalogFieldAndNoTransportMetadata() {
        val metadata = JSONObject()
            .put("title", "Primary title")
            .put("author", "Primary author")
            .put("publisher", "Root publisher")
            .put("volume", "2")
            .put("scan_collection", "Blue crate")
            .put("_capture_photo_assets", JSONObject().put("version", 1))
            .put("extra", JSONObject()
                .put("binding", "cloth")
                .put("shelf_mark", "  QA 12  ")
                .put("blank", "  ")
                .put("null_value", JSONObject.NULL))

        assertEquals(
            listOf(
                CaptureExtraField("publisher", "Publisher", "Root publisher"),
                CaptureExtraField("volume", "Volume", "2"),
                CaptureExtraField("binding", "Binding", "cloth"),
                CaptureExtraField("shelf_mark", "Shelf Mark", "QA 12"),
            ),
            captureExtraFields(metadata),
        )
    }

    @Test
    fun missingExtrasProduceNoPopupRows() {
        assertEquals(emptyList<CaptureExtraField>(), captureExtraFields(null))
        assertEquals(emptyList<CaptureExtraField>(), captureExtraFields(JSONObject().put("title", "Only")))
    }

    private fun entry(id: String, sealed: Boolean, createdAt: Long) = Entries.Entry(
        id = id,
        dir = File(id),
        sealed = sealed,
        uploaded = false,
        createdAt = createdAt,
        photoCount = 1,
        meta = null,
        cloudStatus = "",
        processing = Entries.ProcessingState(
            status = Entries.ProcessingStatus.WAITING,
            stage = Entries.ProcessingStage.WAITING,
            retryable = true,
            lastError = "",
            updatedAt = 0,
        ),
        processingRecorded = false,
    )
}
