package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

class CoverVisualSignatureTest {
    @Test
    fun descriptorHasExactWhlCoverV1ShapeAndBound() {
        val signature = checkNotNull(coverVisualSignature(
            36,
            48,
            IntArray(36 * 48) { 0xff777777.toInt() },
        ))
        val root = JSONObject(signature)

        assertEquals(1, root.getInt("version"))
        assertEquals("whl-cover-v1", root.getString("algorithm"))
        assertEquals(750, root.getInt("aspect_milli"))
        assertEquals(12, root.getJSONArray("hue_hist").length())
        assertEquals(16, root.getJSONArray("chroma_hist").length())
        assertEquals(144, root.getJSONArray("chroma_grid").length())
        assertEquals(48, root.getJSONArray("tone_grid").length())
        assertEquals(48, root.getJSONArray("edge_grid").length())
        assertEquals(8, root.getJSONArray("gradient_hist").length())
        assertTrue(root.getString("dhash").matches(Regex("^[0-9a-f]{16}$")))
        assertTrue(signature.toByteArray().size <= ScanSearchQueue.MAX_VISUAL_SIGNATURE_BYTES)
        assertTrue(validCoverVisualSignature(signature))
    }

    @Test
    fun emptyDistributionsAreAllZeroForPythonMatcherParity() {
        val root = JSONObject(checkNotNull(coverVisualSignature(
            48,
            64,
            IntArray(48 * 64) { 0xff777777.toInt() },
        )))

        assertEquals(List(12) { 0 }, ints(root, "hue_hist"))
        assertEquals(List(8) { 0 }, ints(root, "gradient_hist"))
        assertEquals(List(48) { 0 }, ints(root, "edge_grid"))
        assertEquals(List(48) { 128 }, ints(root, "tone_grid"))
        assertEquals("0000000000000000", root.getString("dhash"))
    }

    @Test
    fun colorAndStructureStayStableAcrossLinearExposureChange() {
        fun image(multiplier: Int): IntArray = IntArray(48 * 64) { index ->
            val x = index % 48
            val y = index / 48
            val baseR = 20 + x
            val baseG = 18 + y / 2
            val baseB = 16 + (x + y) / 4
            argb(baseR * multiplier, baseG * multiplier, baseB * multiplier)
        }
        val normal = JSONObject(checkNotNull(coverVisualSignature(48, 64, image(1))))
        val bright = JSONObject(checkNotNull(coverVisualSignature(48, 64, image(2))))

        assertEquals(ints(normal, "chroma_hist"), ints(bright, "chroma_hist"))
        assertEquals(ints(normal, "hue_hist"), ints(bright, "hue_hist"))
        val toneDrift = ints(normal, "tone_grid").zip(ints(bright, "tone_grid"))
            .maxOf { (left, right) -> abs(left - right) }
        assertTrue("exposure changed tone grid by $toneDrift", toneDrift <= 5)
        assertEquals(normal.getString("dhash"), bright.getString("dhash"))
        val chromaDrift = ints(normal, "chroma_grid").zip(ints(bright, "chroma_grid"))
            .maxOf { (left, right) -> abs(left - right) }
        assertTrue("exposure changed chroma grid by $chromaDrift", chromaDrift <= 1)
    }

    @Test
    fun validatorRejectsMissingOrOutOfRangeMatcherFields() {
        val root = JSONObject(checkNotNull(coverVisualSignature(
            48,
            64,
            IntArray(48 * 64) { argb(40, 80, 120) },
        )))
        assertFalse(validCoverVisualSignature(JSONObject(root.toString()).apply {
            remove("tone_grid")
        }.toString()))
        assertFalse(validCoverVisualSignature(JSONObject(root.toString()).apply {
            getJSONArray("edge_grid").put(0, 256)
        }.toString()))
        assertFalse(validCoverVisualSignature(JSONObject(root.toString()).apply {
            put("unexpected", true)
        }.toString()))
        assertFalse(validCoverVisualSignature(JSONObject(root.toString()).apply {
            put("hue_hist", org.json.JSONArray(List(12) { if (it == 0) 1 else 0 }))
        }.toString()))
        assertFalse(validCoverVisualSignature(JSONObject(root.toString()).apply {
            put("version", "1")
        }.toString()))
        assertFalse(validCoverVisualSignature(
            root.toString().replace("\"aspect_milli\":750", "\"aspect_milli\":750.0"),
        ))
    }

    @Test
    fun redBlueHueWrapsIntoTheMatcherHistogram() {
        val root = JSONObject(checkNotNull(coverVisualSignature(
            48,
            64,
            IntArray(48 * 64) { argb(255, 0, 255) },
        )))

        val hue = ints(root, "hue_hist")
        assertEquals(255, hue.sum())
        assertEquals(255, hue[10])
        assertTrue(validCoverVisualSignature(root.toString()))
    }

    private fun ints(root: JSONObject, field: String): List<Int> {
        val array = root.getJSONArray(field)
        return (0 until array.length()).map(array::getInt)
    }

    private fun argb(red: Int, green: Int, blue: Int): Int =
        (0xff shl 24) or (red.coerceIn(0, 255) shl 16) or
            (green.coerceIn(0, 255) shl 8) or blue.coerceIn(0, 255)
}
