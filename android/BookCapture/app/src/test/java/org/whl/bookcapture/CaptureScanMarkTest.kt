package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.Instant

class CaptureScanMarkTest {
    private val source = "11111111-1111-4111-8111-111111111111"
    private val destination = "22222222-2222-4222-8222-222222222222"

    @Test
    fun sidecarRoundTripsAndProjectsBoundedAuditMetadata() {
        val dir = Files.createTempDirectory("capture-scan-mark").toFile()
        val at = Instant.parse("2026-08-21T12:34:56Z")

        assertTrue(CaptureScanMarkStore.write(dir, source.uppercase(), destination, at))
        assertEquals(
            CaptureScanMark(source, destination, at.toString()),
            CaptureScanMarkStore.read(dir),
        )

        val meta = CaptureScanMarkStore.attachToMeta(JSONObject(), dir)
        assertTrue(meta.getBoolean("scan_marked"))
        assertEquals(source, meta.getString("scan_source_collection_id"))
        assertEquals(destination, meta.getString("scan_destination_collection_id"))
        assertEquals(at.toString(), meta.getString("scan_marked_at"))
    }

    @Test
    fun malformedOrSameCollectionMarksFailClosed() {
        val dir = Files.createTempDirectory("capture-scan-mark-bad").toFile()
        assertFalse(CaptureScanMarkStore.write(dir, source, source))
        assertFalse(File(dir, CaptureScanMarkStore.FILE_NAME).exists())

        File(dir, CaptureScanMarkStore.FILE_NAME).writeText(
            """{"version":1.0,"source_collection_id":"$source","scan_collection_id":"$destination","marked_at":"2026-08-21T12:34:56Z"}""",
        )
        assertNull(CaptureScanMarkStore.read(dir))
    }

    @Test
    fun clearingActiveMarkIsIdempotent() {
        val dir = Files.createTempDirectory("capture-scan-mark-clear").toFile()
        assertTrue(CaptureScanMarkStore.write(dir, source, destination))

        assertTrue(CaptureScanMarkStore.clear(dir))
        assertNull(CaptureScanMarkStore.read(dir))
        assertTrue(CaptureScanMarkStore.clear(dir))
    }
}
