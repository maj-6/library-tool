package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CorrectionsReleaseFixtureTest {
    private fun fixture(): JSONObject {
        val stream = javaClass.classLoader
            ?.getResourceAsStream("corrections_release_fixture.json")
        assertNotNull("shared Corrections release fixture is packaged", stream)
        return stream!!.bufferedReader(Charsets.UTF_8).use {
            JSONObject(it.readText())
        }
    }

    @Test
    fun generatedDesktopConfirmationParsesAndPresentsAnAccessibleMarker() {
        val fixture = fixture()
        val captureId = fixture.getString("capture_id")
        val markerFixture = fixture.getJSONObject("android_marker")
        val raw = fixture.getJSONObject("lib_confirmation")
        val associationWire = raw.getJSONObject("association")
        val parsed = captureLibConfirmationFromJson(raw, captureId)

        assertNotNull("desktop LAN confirmation is Android-compatible", parsed)
        val confirmation = requireNotNull(parsed)
        assertEquals(raw.toString(), confirmation.toJson().toString())
        assertEquals(fixture.getString("book_id"), confirmation.association.bookId)
        assertEquals(
            associationWire.getLong("archive_bytes"),
            confirmation.association.archiveBytes,
        )
        assertEquals(
            associationWire.getString("archive_sha256"),
            confirmation.association.archiveSha256,
        )
        assertEquals(
            associationWire.getString("source_fingerprint"),
            confirmation.association.sourceFingerprint,
        )
        assertEquals(CaptureLibAssociationState.CURRENT, confirmation.association.state)
        assertTrue(confirmation.confirmed)

        val marker = captureLibMarkerPresentation(
            confirmation,
            markerFixture.getString("accessibility_label"),
        )
        assertEquals(markerFixture.getBoolean("visible"), marker.visible)
        assertEquals(
            markerFixture.getString("accessibility_label"),
            marker.accessibilityLabel,
        )

        val stale = confirmation.copy(
            association = confirmation.association.copy(
                state = CaptureLibAssociationState.STALE,
            ),
        )
        val staleMarker = captureLibMarkerPresentation(
            stale,
            markerFixture.getString("accessibility_label"),
        )
        assertFalse(staleMarker.visible)
        assertEquals("", staleMarker.accessibilityLabel)
    }

    @Test
    fun exactCloudRowReachesTheSameConfirmedMarkerState() {
        val fixture = fixture()
        val row = fixture.getJSONObject("cloud_row")
        val markerFixture = fixture.getJSONObject("android_marker")
        val imported = captureImportStateFromJson(
            row,
            fixture.getString("stream_id"),
        )

        assertNotNull("desktop cloud association is Android-compatible", imported)
        val state = requireNotNull(imported)
        val confirmation = state.confirmation
        assertNotNull(confirmation)
        assertEquals("imported", state.status)
        assertEquals(
            fixture.getJSONObject("lib_confirmation")
                .getJSONObject("association").toString(),
            requireNotNull(confirmation).association.toJson().toString(),
        )

        val marker = captureLibMarkerPresentation(
            confirmation,
            markerFixture.getString("accessibility_label"),
        )
        assertTrue(marker.visible)
        assertEquals(
            markerFixture.getString("accessibility_label"),
            marker.accessibilityLabel,
        )
    }
}
