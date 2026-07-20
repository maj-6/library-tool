package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BookDiagnosticsPresentationTest {

    @Test
    fun bookTabPrettyPrintsTheExactPersistedObjectIncludingUnknownFields() {
        val raw = """{"title":"Herbarium","future":{"score":4},"tags":["old","rare"]}"""

        val content = BookDiagnosticsPresenter.from(raw, emptyList())

        assertEquals(JSONObject(raw).toString(2), content.bookJson)
        assertTrue(content.bookJson!!.contains("\"future\""))
        assertFalse(content.bookJson!!.contains("captureOrder"))
    }

    @Test
    fun mistralTabHumanizesEveryPersistedResponseWithoutUsingLegacyOcrText() {
        val content = BookDiagnosticsPresenter.from(
            bookJson = null,
            mistralResponses = listOf(
                Entries.MistralResponse(
                    kind = Entries.MistralResponseKind.OCR,
                    captureOrder = 2,
                    rawJson = """{
                        "model":"mistral-ocr-latest",
                        "pages":[{"index":0,"markdown":"# A Flora\nLondon","blocks":[]}],
                        "usage_info":{"pages_processed":1}
                    }""".trimIndent(),
                ),
                Entries.MistralResponse(
                    kind = Entries.MistralResponseKind.EXTRACTION,
                    rawJson = """{
                        "choices":[{"message":{"content":"{\"title\":\"A Flora\",\"year\":1899}"}}]
                    }""".trimIndent(),
                ),
            ),
        )

        assertEquals(2, content.mistralSections.size)
        assertEquals(2, content.mistralSections.first().captureOrder)
        val rendered = content.mistralSections.joinToString("\n") { it.humanReadableBody }
        for (persistedValue in listOf(
            "mistral-ocr-latest",
            "# A Flora",
            "London",
            "Pages processed: 1",
            "Title: A Flora",
            "Year: 1899",
        )) {
            assertTrue("missing $persistedValue", rendered.contains(persistedValue))
        }
    }

    @Test
    fun legacyIsEmptyWhileMalformedPersistedResponsesRemainVisible() {
        val legacy = BookDiagnosticsPresenter.from(null, emptyList())
        val malformed = BookDiagnosticsPresenter.from(
            null,
            listOf(Entries.MistralResponse(Entries.MistralResponseKind.OCR, 1, "not json")),
        )

        assertEquals(null, legacy.bookJson)
        assertTrue(legacy.mistralSections.isEmpty())
        assertEquals(1, malformed.mistralSections.size)
        assertFalse(malformed.mistralSections.single().validJson)
        assertEquals("not json", malformed.mistralSections.single().humanReadableBody)
    }

    @Test
    fun tokenizerDistinguishesKeysValuesNumbersAndLiteralsWithEscapes() {
        val json = """{"key":"escaped \" value","n":-12.5e+2,"ok":false,"none":null}"""
        val classified = JsonSyntaxTokenizer.tokenize(json)
            .groupBy { it.kind }
            .mapValues { (_, tokens) -> tokens.map { json.substring(it.start, it.end) } }

        assertTrue(classified.getValue(JsonSyntaxKind.KEY).contains("\"key\""))
        assertTrue(classified.getValue(JsonSyntaxKind.STRING).contains("\"escaped \\\" value\""))
        assertTrue(classified.getValue(JsonSyntaxKind.NUMBER).contains("-12.5e+2"))
        assertTrue(classified.getValue(JsonSyntaxKind.BOOLEAN).contains("false"))
        assertTrue(classified.getValue(JsonSyntaxKind.NULL).contains("null"))
        assertTrue(classified.getValue(JsonSyntaxKind.PUNCTUATION).contains("{"))
    }
}
