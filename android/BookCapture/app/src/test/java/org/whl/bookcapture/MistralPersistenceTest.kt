package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class MistralPersistenceTest {

    @Test
    fun entryExposesExactBookJsonAndOrderedRawMistralResponses() {
        val dir = Files.createTempDirectory("mistral-persistence-").toFile()
        try {
            File(dir, "photo_1.jpg").writeText("jpeg")
            val meta = "{\n  \"title\": \"A Flora\",\n  \"future_field\": 7\n}"
            File(dir, "meta.json").writeText(meta)
            File(dir, "photo_1.jpg${Entries.MISTRAL_RESPONSE_SUFFIX}").writeText(
                JSONObject().put("pages", org.json.JSONArray()).put("model", "ocr").toString(),
            )
            File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE).writeText(
                JSONObject().put("choices", org.json.JSONArray()).put("model", "extract").toString(),
            )
            val entry = entry(dir)

            assertEquals(meta, entry.bookJsonText())
            val responses = entry.mistralResponses()
            assertEquals(2, responses.size)
            assertEquals(Entries.MistralResponseKind.OCR, responses[0].kind)
            assertEquals(1, responses[0].captureOrder)
            assertEquals("ocr", JSONObject(responses[0].rawJson).getString("model"))
            assertEquals(Entries.MistralResponseKind.EXTRACTION, responses[1].kind)
            assertNull(responses[1].captureOrder)
            assertEquals("extract", JSONObject(responses[1].rawJson).getString("model"))
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun absentOrBlankDiagnosticsRemainHonestEmptyStates() {
        val dir = Files.createTempDirectory("mistral-empty-").toFile()
        try {
            File(dir, "photo_1.jpg").writeText("jpeg")
            File(dir, "photo_1.jpg${Entries.MISTRAL_RESPONSE_SUFFIX}").writeText("  ")
            val entry = entry(dir)

            assertNull(entry.bookJsonText())
            assertTrue(entry.mistralResponses().isEmpty())
        } finally {
            dir.deleteRecursively()
        }
    }

    private fun entry(dir: File) = Entries.Entry(
        id = dir.name,
        dir = dir,
        sealed = true,
        uploaded = false,
        createdAt = 1L,
        photoCount = 1,
        meta = null,
        cloudStatus = "",
        processing = Entries.ProcessingState(
            Entries.ProcessingStatus.COMPLETE,
            Entries.ProcessingStage.COMPLETE,
            retryable = false,
            lastError = "",
            updatedAt = 1L,
        ),
        processingRecorded = true,
    )
}
