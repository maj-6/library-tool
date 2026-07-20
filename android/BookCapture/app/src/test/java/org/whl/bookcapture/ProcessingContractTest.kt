package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ProcessingContractTest {

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()
            .replace("\r\n", "\n")

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
        assertTrue(worker.contains("shouldRetryProcessingWork(\n                transient,"))
    }

    @Test
    fun spineTitleIsRequestedParsedMergedAndCountedAsMetadata() {
        assertTrue("spine_title" in Pipeline.FIELDS)
        val response = JSONObject().apply {
            Pipeline.FIELDS.forEach { put(it, "") }
            put("title", "The Published Title")
            put("spine_title", "Short Spine Title")
            put("extra", JSONObject())
        }

        val parsed = Pipeline.parseExtraction(response.toString())
        assertTrue(parsed.complete)
        assertEquals("The Published Title", parsed.metadata.getString("title"))
        assertEquals("Short Spine Title", parsed.metadata.getString("spine_title"))

        val merged = Pipeline.mergeExtraction(
            JSONObject().put("spine_title", "Earlier Spine Title"),
            JSONObject().put("spine_title", ""),
        )
        assertEquals("Earlier Spine Title", merged.getString("spine_title"))
        assertTrue(Pipeline.hasPopulatedMetadata(JSONObject().put("spine_title", "Only spine text")))

        val pipeline = source("Pipeline")
        assertTrue(pipeline.contains("only when it differs materially"))
        assertTrue(pipeline.contains("absent or equivalent"))
    }

    @Test
    fun acceptedMetadataFreezesPostProcessingIntentBeforeCompletion() {
        val worker = source("ProcessWorker")
        val roleSuggestions = worker.indexOf("PhotoAssetStore.applyBibliographicSuggestions(dir, merged)")
        val processingRequest = worker.indexOf("requestPostProcessing(ctx, dir, merged)")
        val completion = worker.indexOf("if (extraction.complete)", processingRequest)

        assertTrue(roleSuggestions >= 0)
        assertTrue(processingRequest > roleSuggestions)
        assertTrue(completion > processingRequest)
        assertTrue(worker.contains("Prefs.postProcessingProfile(ctx, publicationYear)"))
        assertTrue(worker.contains("PhotoAssetStore.requestProcessing(dir, asset.assetId, profile)"))
    }

    @Test
    fun successfulMistralResponsesArePersistedBeforeTheirCommitMarkers() {
        val pipeline = source("Pipeline")
        val worker = source("ProcessWorker")
        val entries = source("Entries")

        assertTrue(pipeline.contains("val providerResponse: String"))
        assertTrue(entries.contains("MISTRAL_RESPONSE_SUFFIX"))
        assertTrue(entries.contains("fun bookJsonText(): String?"))
        assertTrue(entries.contains("fun mistralResponses(): List<MistralResponse>"))
        assertTrue(worker.indexOf("result.providerResponse") < worker.indexOf("Entries.atomicWrite(sidecar"))
        assertTrue(worker.contains("extraction.provider == \"mistral\""))
        assertTrue(worker.contains("mistralExtraction.delete()"))
    }

    @Test
    fun automaticBacklogRetriesAreBoundedWithoutBoundingExplicitReprocess() {
        assertFalse(shouldRetryProcessingWork(
            retryRequested = false,
            forceReprocess = false,
            runAttemptCount = 0,
        ))
        assertTrue(shouldRetryProcessingWork(
            retryRequested = true,
            forceReprocess = false,
            runAttemptCount = 0,
        ))
        assertTrue(shouldRetryProcessingWork(
            retryRequested = true,
            forceReprocess = false,
            runAttemptCount = MAX_AUTOMATIC_PROCESS_RETRIES - 1,
        ))
        assertFalse(shouldRetryProcessingWork(
            retryRequested = true,
            forceReprocess = false,
            runAttemptCount = MAX_AUTOMATIC_PROCESS_RETRIES,
        ))
        assertTrue(shouldRetryProcessingWork(
            retryRequested = true,
            forceReprocess = true,
            runAttemptCount = 1_000,
        ))
    }
}
