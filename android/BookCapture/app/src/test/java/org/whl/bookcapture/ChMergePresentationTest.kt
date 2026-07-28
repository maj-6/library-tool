package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChMergePresentationTest {

    private fun candidate(vararg fields: Pair<String, String>) = ChCandidate(
        key = "ch:sha-abc",
        title = "Flora Medica",
        author = "Spratt, G.",
        year = "1829",
        score = 0.91,
        fields = mapOf(*fields),
    )

    private fun preview(scan: Map<String, String>, cand: ChCandidate?) =
        ChMergePresenter.preview(scan, cand)

    @Test
    fun `ch fills a field the scan left empty`() {
        val result = preview(mapOf("title" to "Flora Medica"), candidate("publisher" to "Callow & Wilson"))
        val publisher = result.fields.single { it.key == "publisher" }
        assertEquals(FieldOrigin.ADOPTED, publisher.origin)
        assertEquals("Callow & Wilson", publisher.mergedValue)
        assertEquals(listOf("publisher"), result.adopted.map { it.key })
    }

    @Test
    fun `a blank ch value never clears a captured one`() {
        val result = preview(mapOf("publisher" to "Callow"), candidate("publisher" to ""))
        val publisher = result.fields.single { it.key == "publisher" }
        assertEquals(FieldOrigin.UNCHANGED, publisher.origin)
        assertEquals("Callow", publisher.mergedValue)
    }

    @Test
    fun `a disagreement keeps the scanned value and is reported`() {
        // CH is known-dirty; approving "same book" is not approving "overwrite me".
        val result = preview(mapOf("year" to "1829"), candidate("year" to "1830"))
        val year = result.fields.single { it.key == "year" }
        assertEquals(FieldOrigin.CONFLICT, year.origin)
        assertEquals("1829", year.mergedValue)
        assertEquals("1830", year.chValue)
        assertEquals(listOf("year"), result.conflicts.map { it.key })
    }

    @Test
    fun `cosmetic differences are not conflicts`() {
        val result = preview(
            mapOf("title" to "Flora Medica"),
            candidate("title" to "flora  medica!"),
        )
        assertEquals(FieldOrigin.UNCHANGED, result.fields.single { it.key == "title" }.origin)
    }

    @Test
    fun `fields empty on both sides are omitted entirely`() {
        val result = preview(mapOf("title" to "X"), candidate("publisher" to ""))
        assertTrue(result.fields.none { it.key == "publisher" })
    }

    @Test
    fun `identity fields lead the preview`() {
        val result = preview(
            mapOf("notes" to "n", "title" to "t"),
            candidate("price" to "400", "author" to "Spratt"),
        )
        assertEquals("title", result.fields.first().key)
        assertTrue(
            result.fields.indexOfFirst { it.key == "author" } <
                result.fields.indexOfFirst { it.key == "notes" },
        )
    }

    @Test
    fun `a merge that adds nothing is a no-op`() {
        assertTrue(preview(mapOf("title" to "X"), candidate("title" to "X")).isNoOp)
        assertFalse(preview(emptyMap(), candidate("title" to "X")).isNoOp)
        assertTrue(preview(mapOf("title" to "X"), null).isNoOp)
    }

    @Test
    fun `apply writes only adopted fields and preserves unknown keys`() {
        val meta = JSONObject()
            .put("title", "Flora Medica")
            .put("year", "1829")
            .put("future_field_we_do_not_own", "keep me")
        val result = preview(
            ChMergePresenter.scanFields(meta),
            candidate("publisher" to "Callow", "year" to "1830"),
        )
        val merged = ChMergePresenter.apply(meta, result)

        assertEquals("Callow", merged.getString("publisher"))
        // The conflicting year is NOT overwritten.
        assertEquals("1829", merged.getString("year"))
        assertEquals("keep me", merged.getString("future_field_we_do_not_own"))
        // and the original is untouched — apply works on a copy
        assertFalse(meta.has("publisher"))
    }

    @Test
    fun `apply records the durable link with what it did`() {
        val result = preview(emptyMap(), candidate("publisher" to "Callow"))
        val link = ChMergePresenter.apply(JSONObject(), result).getJSONObject("ch_match")
        assertEquals("ch:sha-abc", link.getString("key"))
        assertEquals("publisher", link.getString("adopted"))
    }

    @Test
    fun `apply on a null meta still produces a usable object`() {
        val merged = ChMergePresenter.apply(null, preview(emptyMap(), candidate("title" to "T")))
        assertEquals("T", merged.getString("title"))
    }

    @Test
    fun `scanFields flattens extra and skips transport and nested values`() {
        val meta = JSONObject()
            .put("title", "T")
            .put("_capture_photo_assets", JSONObject().put("assets", "x"))
            .put("nested", JSONObject().put("a", 1))
            .put("blank", "   ")
            .put("extra", JSONObject().put("publisher", "P").put("title", "ignored"))
        val fields = ChMergePresenter.scanFields(meta)

        assertEquals("T", fields["title"])         // top level wins over extra
        assertEquals("P", fields["publisher"])
        assertNull(fields["_capture_photo_assets"])
        assertNull(fields["nested"])
        assertNull(fields["blank"])
    }

    @Test
    fun `a null candidate yields an empty preview rather than throwing`() {
        val result = preview(mapOf("title" to "X"), null)
        assertTrue(result.fields.isEmpty())
        assertNull(result.candidate)
    }
}
