package org.whl.bookcapture

import androidx.work.WorkInfo
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
        assertFalse(worker.contains("Result.failure()"))
        assertTrue(worker.contains("ProcessingWorkDecision.COMPLETE_CHAIN ->"))
        assertTrue(worker.contains("finishReprocess(error)"))
        assertTrue(worker.contains("processingWorkDecision(transient, forceReprocess, runAttemptCount)"))
        assertTrue(
            worker.indexOf("finishReprocess(error)") <
                worker.indexOf("transient = transient || outcome.retry"),
        )
    }

    @Test
    fun processingRecoversInterruptedThumbnailDeletesBeforeReadingPhotos() {
        val worker = source("ProcessWorker")
        val lock = worker.indexOf("EntryOperationLocks.withLock(entryId)")
        val recovery = worker.indexOf("cleanupCommittedThumbnailDeletes(currentDir)", lock)
        val processing = worker.indexOf("processDirectory(", recovery)

        assertTrue(lock >= 0)
        assertTrue(recovery > lock)
        assertTrue(processing > recovery)
        assertTrue(worker.contains("DirectoryOutcome(\n                        retry = true,"))
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

    /** Extraction runs at temperature 0, so a book that trips a shape check
     * trips it on every attempt. A deviation that loses no value must not make
     * the record permanently "partial". */
    @Test
    fun anUnquotedYearIsKeptAndDoesNotMakeTheRecordPartial() {
        val response = JSONObject().apply {
            Pipeline.FIELDS.forEach { put(it, "") }
            put("title", "The Published Title")
            put("year", 1897)                     // a JSON number, not a string
            put("extra", JSONObject())
        }

        val parsed = Pipeline.parseExtraction(response.toString())

        assertTrue(parsed.complete)
        assertEquals("1897", parsed.metadata.getString("year"))
        // The deviation is still reported, just not as a defect.
        assertTrue(parsed.warning != null && parsed.warning!!.contains("year"))
    }

    @Test
    fun anOmittedExtraObjectIsNotAPartialExtraction() {
        val response = JSONObject().apply {
            Pipeline.FIELDS.forEach { put(it, "") }
            put("title", "The Published Title")
        }

        assertTrue(Pipeline.parseExtraction(response.toString()).complete)
    }

    @Test
    fun aMissingFieldIsStillAPartialExtraction() {
        val response = JSONObject().apply {
            Pipeline.FIELDS.filterNot { it == "author" }.forEach { put(it, "") }
            put("title", "The Published Title")
            put("extra", JSONObject())
        }

        val parsed = Pipeline.parseExtraction(response.toString())

        assertFalse(parsed.complete)
        assertTrue(parsed.warning!!.contains("author"))
    }

    @Test
    fun aStructuredValueWhereAStringBelongsIsStillAPartialExtraction() {
        // No honest scalar to keep, so the value really was lost.
        val response = JSONObject().apply {
            Pipeline.FIELDS.forEach { put(it, "") }
            put("title", "The Published Title")
            put("author", JSONObject().put("first", "Ada"))
            put("extra", JSONObject())
        }

        assertFalse(Pipeline.parseExtraction(response.toString()).complete)
    }

    @Test
    fun anExtraFieldOfTheWrongTypeIsStillAPartialExtraction() {
        val response = JSONObject().apply {
            Pipeline.FIELDS.forEach { put(it, "") }
            put("title", "The Published Title")
            put("extra", "not an object")
        }

        assertFalse(Pipeline.parseExtraction(response.toString()).complete)
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
    fun automaticAndForcedProcessingRetriesAreBounded() {
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
            runAttemptCount = MAX_FORCED_PROCESS_RETRIES - 1,
        ))
        assertFalse(shouldRetryProcessingWork(
            retryRequested = true,
            forceReprocess = true,
            runAttemptCount = MAX_FORCED_PROCESS_RETRIES,
        ))
    }

    @Test
    fun terminalProcessingOutcomesCompleteTheirSerialChain() {
        assertEquals(
            ProcessingWorkDecision.COMPLETE_CHAIN,
            processingWorkDecision(
                retryRequested = false,
                forceReprocess = true,
                runAttemptCount = 0,
            ),
        )
        assertEquals(
            ProcessingWorkDecision.RETRY,
            processingWorkDecision(
                retryRequested = true,
                forceReprocess = true,
                runAttemptCount = MAX_FORCED_PROCESS_RETRIES - 1,
            ),
        )
        assertEquals(
            ProcessingWorkDecision.COMPLETE_CHAIN,
            processingWorkDecision(
                retryRequested = true,
                forceReprocess = true,
                runAttemptCount = MAX_FORCED_PROCESS_RETRIES,
            ),
        )
        assertEquals(
            ProcessingWorkDecision.COMPLETE_CHAIN,
            processingWorkDecision(
                retryRequested = true,
                forceReprocess = false,
                runAttemptCount = MAX_AUTOMATIC_PROCESS_RETRIES,
            ),
        )
    }

    @Test
    fun strandedForcedRetryRecoveryRebuildsOnlyIncompleteOwnership() {
        for (state in listOf(
            WorkInfo.State.ENQUEUED,
            WorkInfo.State.RUNNING,
            WorkInfo.State.BLOCKED,
        )) {
            assertTrue(isUnfinishedProcessingWork(state))
        }
        for (state in listOf(
            WorkInfo.State.SUCCEEDED,
            WorkInfo.State.FAILED,
            WorkInfo.State.CANCELLED,
        )) {
            assertFalse(isUnfinishedProcessingWork(state))
        }

        assertEquals(
            emptyList<String>(),
            pendingForcedRetryRecoveryIds(
                pendingIds = listOf("entry-a", "entry-b"),
                unfinishedOwnedIds = listOf("entry-a", "entry-b"),
                hasUntaggedUnfinishedWork = false,
            ),
        )
        assertEquals(
            listOf("entry-b"),
            pendingForcedRetryRecoveryIds(
                pendingIds = listOf("entry-b", "entry-a"),
                unfinishedOwnedIds = listOf("entry-a"),
                hasUntaggedUnfinishedWork = false,
            ),
        )
        assertEquals(
            listOf("entry-a", "entry-b"),
            pendingForcedRetryRecoveryIds(
                pendingIds = listOf("entry-a", "entry-b"),
                unfinishedOwnedIds = emptyList(),
                hasUntaggedUnfinishedWork = true,
            ),
        )

        val worker = source("ProcessWorker")
        assertTrue(worker.contains("@Synchronized\n        fun resumePendingForcedRetries"))
        assertTrue(worker.contains("addTag(retryEntryTag(entryId))"))
        assertTrue(worker.contains("if (hasUntaggedUnfinished) ExistingWorkPolicy.REPLACE"))
        assertTrue(worker.contains("else ExistingWorkPolicy.APPEND_OR_REPLACE"))
    }

    @Test
    fun everyTerminalForcedOutcomeClearsItsHoldMarker() {
        val worker = source("ProcessWorker")
        val terminal = worker.substringAfter("if (forced && processingWorkDecision(")
            .substringBefore("Entries.find(ctx, entryId)?.finishReprocess(error)")

        assertTrue(terminal.contains("ProcessingWorkDecision.COMPLETE_CHAIN"))
        assertTrue(worker.contains("} else null\n                    Entries.find(ctx, entryId)?.finishReprocess(error)"))
    }

    @Test
    fun recoveredForcedWorkRechecksItsMovedDirectoryAndMarkerInsideTheEntryLock() {
        val worker = source("ProcessWorker")
        val lock = worker.indexOf("EntryOperationLocks.withLock(entryId)")
        val currentEntry = worker.indexOf("val currentEntry = Entries.find(ctx, entryId)", lock)
        val currentDir = worker.indexOf("val currentDir = currentEntry?.dir", currentEntry)
        val markerCheck = worker.indexOf("currentEntry?.reprocessPending() == true", currentDir)
        val processing = worker.indexOf("processDirectory(", markerCheck)

        assertTrue(lock >= 0)
        assertTrue(currentEntry > lock)
        assertTrue(currentDir > currentEntry)
        assertTrue(markerCheck > lock)
        assertTrue(processing > markerCheck)
        assertTrue(worker.substring(processing, processing + 200).contains("currentDir"))
    }
}
