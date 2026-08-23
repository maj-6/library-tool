package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanWorkflowClientTest {
    private val queueId = "11111111-1111-4111-8111-111111111111"
    private val ownerId = "22222222-2222-4222-8222-222222222222"
    private val collectionId = "33333333-3333-4333-8333-333333333333"
    private val captureId = "44444444-4444-4444-8444-444444444444"

    @Test
    fun enqueueBodyCarriesIdempotencyKeyDestinationRoleAndOcr() {
        val body = scanSearchEnqueueBody(
            ScanSearchQueueItem(
                id = queueId,
                scanCollectionId = collectionId,
                photoRole = ScanSearchPhotoRole.TITLE_PAGE,
                ocrText = " A New Herbal ",
                createdAt = "2026-08-21T12:00:00Z",
            ),
        )
        assertEquals(queueId, body.getString("p_id"))
        assertEquals(queueId, body.getString("p_session_id"))
        assertEquals(collectionId, body.getString("p_scan_collection_id"))
        assertEquals("title_page", body.getString("p_photo_role"))
        assertEquals("A New Herbal", body.getString("p_ocr_text"))
        assertTrue(body.isNull("p_visual_signature"))
    }

    @Test
    fun cloudMatchedRowParsesAndIsClean() {
        val parsed = scanSearchQueueItemFromCloudJson(JSONObject().apply {
            put("id", queueId)
            put("owner_id", ownerId)
            put("scan_collection_id", collectionId)
            put("photo_role", "cover")
            put("ocr_text", "Herbal")
            put("status", "matched")
            put("candidate_capture_id", captureId)
            put("match_confidence", 1.0)
            put("match_evidence", JSONObject().put("version", 1))
            put("matched_capture_id", captureId)
            put("revision", 2)
            put("created_at", "2026-08-21T12:00:00Z")
            put("updated_at", "2026-08-21T12:01:00Z")
        })
        assertEquals(captureId, parsed?.matchedCaptureId)
        assertEquals(2L, parsed?.revision)
        assertFalse(checkNotNull(parsed).dirty)
    }

    @Test
    fun cloudProposalCarriesConfidenceEvidenceAndVisualJson() {
        val signature = checkNotNull(coverVisualSignature(
            48,
            64,
            IntArray(48 * 64) { 0xff336699.toInt() },
        ))
        val parsed = scanSearchQueueItemFromCloudJson(JSONObject().apply {
            put("id", queueId)
            put("owner_id", ownerId)
            put("session_id", ownerId)
            put("scan_collection_id", collectionId)
            put("photo_role", "cover")
            put("ocr_text", "Herbal")
            put("visual_signature", JSONObject(signature))
            put("status", "proposed")
            put("candidate_capture_id", captureId)
            put("match_confidence", .875)
            put("match_evidence", JSONObject().put("version", 1))
            put("matched_capture_id", JSONObject.NULL)
            put("revision", 2)
            put("created_at", "2026-08-21T12:00:00Z")
            put("updated_at", "2026-08-21T12:01:00Z")
        })
        assertEquals(ScanSearchStatus.PROPOSED, parsed?.status)
        assertEquals(captureId, parsed?.candidateCaptureId)
        assertEquals(.875, parsed!!.matchConfidence!!, 0.0)
        assertEquals(signature, parsed.visualSignature)

        val decision = scanSearchProposalDecisionBody(queueId, captureId)
        assertEquals(queueId, decision.getString("p_id"))
        assertEquals(captureId, decision.getString("p_capture_id"))
    }

    @Test
    fun cloudProposalAndMatchedRowsFailClosedWhenProposalEvidenceIsMissing() {
        fun base(status: String) = JSONObject().apply {
            put("id", queueId)
            put("owner_id", ownerId)
            put("session_id", queueId)
            put("scan_collection_id", collectionId)
            put("photo_role", "cover")
            put("ocr_text", "Herbal")
            put("status", status)
            put("candidate_capture_id", captureId)
            put("match_confidence", .75)
            put("match_evidence", JSONObject().put("version", 1))
            put(
                "matched_capture_id",
                if (status == "matched") captureId else JSONObject.NULL,
            )
            put("revision", 2)
            put("created_at", "2026-08-21T12:00:00Z")
            put("updated_at", "2026-08-21T12:01:00Z")
        }

        assertTrue(scanSearchQueueItemFromCloudJson(base("proposed")) != null)
        assertTrue(scanSearchQueueItemFromCloudJson(base("matched")) != null)
        assertNull(scanSearchQueueItemFromCloudJson(base("proposed").apply {
            put("match_evidence", JSONObject.NULL)
        }))
        assertNull(scanSearchQueueItemFromCloudJson(base("matched").apply {
            put("candidate_capture_id", JSONObject.NULL)
        }))
    }

    @Test
    fun liveQueuePaginationFetchesOneOverflowSentinelRow() {
        assertEquals("pending,proposed,failed", SCAN_SEARCH_LIVE_STATUS_FILTER)
        assertEquals(SCAN_SEARCH_PAGE_SIZE, scanSearchQueuePageLimit(0))
        assertEquals(2, scanSearchQueuePageLimit(ScanSearchQueue.MAX_ITEMS - 1))
        assertEquals(1, scanSearchQueuePageLimit(ScanSearchQueue.MAX_ITEMS))
        assertNull(scanSearchQueuePageLimit(ScanSearchQueue.MAX_ITEMS + 1))
    }

    @Test
    fun cloudRowFailsClosedOnFractionalOrZeroRevision() {
        val row = JSONObject().apply {
            put("id", queueId)
            put("owner_id", ownerId)
            put("scan_collection_id", collectionId)
            put("photo_role", "cover")
            put("ocr_text", "Herbal")
            put("status", "pending")
            put("matched_capture_id", JSONObject.NULL)
            put("revision", 1.5)
            put("created_at", "2026-08-21T12:00:00Z")
            put("updated_at", "2026-08-21T12:00:00Z")
        }
        assertNull(scanSearchQueueItemFromCloudJson(row))
        row.put("revision", 0)
        assertNull(scanSearchQueueItemFromCloudJson(row))
        row.put("revision", 1)
        row.put("scan_collection_id", "")
        assertNull(scanSearchQueueItemFromCloudJson(row))
    }

    @Test
    fun failedRowsCannotBeReenqueuedAsPendingWork() {
        val failed = ScanSearchQueueItem(
            id = queueId,
            ownerId = ownerId,
            scanCollectionId = collectionId,
            photoRole = ScanSearchPhotoRole.COVER,
            ocrText = "Herbal",
            status = ScanSearchStatus.FAILED,
            revision = 2,
            createdAt = "2026-08-21T12:00:00Z",
            dirty = false,
        )
        assertThrows(IllegalArgumentException::class.java) {
            scanSearchEnqueueBody(failed)
        }
    }
}
