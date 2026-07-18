package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ProcessingContractTest {

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()

    @Test
    fun extractionRejectsEmptyOutputAndMergesWithoutErasingPriorFields() {
        val pipeline = source("Pipeline")
        val worker = source("ProcessWorker")

        assertTrue(pipeline.contains("throw InvalidExtractionError(\"Extraction returned an empty response\")"))
        assertTrue(pipeline.contains("throw InvalidExtractionError(\"Extraction returned invalid JSON\")"))
        assertTrue(pipeline.contains("throw InvalidExtractionError(\"Extraction returned no bibliographic fields\")"))
        assertTrue(pipeline.contains("internal fun mergeExtraction("))
        assertTrue(pipeline.contains("fresh.isEmpty() -> old"))
        assertFalse(pipeline.contains("catch (_: Exception) { JSONObject() }"))
        assertTrue(worker.contains(
            "if (forced && !extraction.complete) Entries.holdForProcessing(dir)"
        ))
        assertFalse(worker.contains(
            "if (!extraction.complete) Entries.holdForProcessing(dir)"
        ))
        val upload = source("UploadWorker")
        assertTrue(upload.contains("val processingCanImprove = entry != null"))
        assertTrue(upload.contains("entry.processing.retryable"))
        assertTrue(upload.contains("now - entry.createdAt < PROCESS_GRACE_MS"))
    }

    @Test
    fun eachEntryPersistsRecoverableProcessingStateAtomically() {
        val entries = source("Entries")

        for (status in listOf("waiting", "processing", "failed", "partial", "complete")) {
            assertTrue(entries.contains("(\"$status\")"))
        }
        for (field in listOf("status", "best_status", "stage", "retryable", "last_error", "updated_at")) {
            assertTrue(entries.contains(".put(\"$field\""))
        }
        assertTrue(entries.contains("atomicWrite(File(dir, PROCESSING_STATE), state.toString())"))
        assertTrue(entries.contains("Pipeline.hasPopulatedMetadata(it)"))
        assertTrue(entries.contains(".put(\"status\", requestedStatus.wireValue)"))
        assertTrue(entries.contains("current?.bestStatus == ProcessingStatus.COMPLETE"))
    }

    @Test
    fun rapidShutterDebouncesWhileBacklogsSplitIntoPerEntryWork() {
        val worker = source("ProcessWorker")

        assertTrue(worker.contains("Prefs.currentEntryId(ctx)"))
        assertTrue(worker.contains("setInitialDelay(ACTIVE_CAPTURE_IDLE_SECONDS"))
        assertTrue(worker.contains("ExistingWorkPolicy.REPLACE"))
        assertTrue(worker.contains("KEY_ENTRY_ID to requestedId"))
        assertTrue(worker.contains("BACKLOG_WORK_NAME"))
        assertTrue(worker.contains("entryIds.distinct().sorted().map { entryId"))
        assertTrue(worker.contains("KEY_ENTRY_ID to entryId"))
        assertTrue(worker.contains("ExistingWorkPolicy.APPEND_OR_REPLACE"))
        assertTrue(worker.contains("if (requestedId == null)"))
        assertTrue(worker.contains("permanent != null && forceReprocess -> Result.failure()"))
    }
}
