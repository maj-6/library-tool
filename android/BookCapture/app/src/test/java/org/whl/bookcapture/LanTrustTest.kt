package org.whl.bookcapture

import java.net.InetAddress
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LanTrustTest {
    @Test
    fun cleartextTrustIsLimitedToPrivateOrLocalAddresses() {
        for (host in listOf("10.0.0.5", "172.16.4.2", "192.168.1.20", "127.0.0.1")) {
            assertTrue(host, isPrivateLanAddress(InetAddress.getByName(host)))
        }
        assertFalse(isPrivateLanAddress(InetAddress.getByName("8.8.8.8")))
        assertTrue(isPrivateLanHost("192.168.1.20"))
        assertTrue(isPrivateLanHost("localhost"))
        assertFalse(isPrivateLanHost("example.com"))
        assertFalse(isPrivateLanHost("8.8.8.8"))
    }

    @Test
    fun pairingProbeRequiresBrandedFreshNonceEcho() {
        assertTrue(isValidPairingResponse("fresh", 200, "whl-capture", "fresh"))
        assertFalse(isValidPairingResponse("fresh", 200, "whl-capture", "stale"))
        assertFalse(isValidPairingResponse("fresh", 200, "other", "fresh"))
        assertFalse(isValidPairingResponse("fresh", 401, "whl-capture", "fresh"))
    }

    @Test
    fun captureReceiptMustBeBrandedAndMatchTheSubmittedEntry() {
        assertTrue(isValidCaptureReceipt("entry-1", 200, "whl-capture", "imported", "entry-1"))
        assertTrue(isValidCaptureReceipt("entry-1", 200, "whl-capture", "duplicate", "entry-1"))
        assertFalse(isValidCaptureReceipt("entry-1", 200, "other", "imported", "entry-1"))
        assertFalse(isValidCaptureReceipt("entry-1", 200, "whl-capture", "ok", "entry-1"))
        assertFalse(isValidCaptureReceipt("entry-1", 200, "whl-capture", "imported", "entry-2"))
    }

    @Test
    fun archiveConfirmationEnvelopeIsCaptureBoundAndPathFree() {
        val captureId = "11111111-2222-4333-8444-555555555555"
        val envelope = JSONObject()
            .put("schema", "org.whl.capture-lib-confirmation")
            .put("version", 1)
            .put("capture_id", captureId)
            .put("stream_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
            .put("revision", 1)
            .put("updated_at", "2026-07-23T12:35:00+00:00")
            .put("association", JSONObject()
                .put("schema", "org.whl.capture-lib-association")
                .put("version", 1)
                .put("capture_id", captureId)
                .put("book_id", "b-" + "a".repeat(32))
                .put("archive_sha256", "b".repeat(64))
                .put("archive_bytes", 12345)
                .put("format_version", "3.0")
                .put("state", "current")
                .put("generated_at", "2026-07-23T12:34:56+00:00")
                .put("source_revision", "sha256:" + "c".repeat(64))
                .put("source_fingerprint", "d".repeat(64)))
        val parsed = captureLibConfirmationFromJson(envelope, captureId)!!
        assertTrue(parsed.confirmed)
        assertEquals(captureId, parsed.captureId)
        assertFalse(envelope.toString().contains("path", ignoreCase = true))
        assertFalse(envelope.toString().contains("credential", ignoreCase = true))
        assertTrue(captureLibConfirmationFromJson(
            JSONObject(envelope.toString()).put("capture_id", parsed.streamId),
            captureId,
        ) == null)
    }
}
