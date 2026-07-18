package org.whl.bookcapture

import java.net.InetAddress
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
}
