package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class ScanSearchQueueTest {
    private val queueId = "11111111-1111-4111-8111-111111111111"
    private val collectionId = "22222222-2222-4222-8222-222222222222"
    private val captureId = "33333333-3333-4333-8333-333333333333"
    private val ownerId = "44444444-4444-4444-8444-444444444444"
    private val otherOwnerId = "55555555-5555-4555-8555-555555555555"

    @Test
    fun strictVersionedQueueRoundTrips() {
        val item = ScanSearchQueueItem(
            id = queueId,
            scanCollectionId = collectionId,
            photoRole = ScanSearchPhotoRole.TITLE_PAGE,
            ocrText = "A New Herbal\nJohn Gerard",
            createdAt = "2026-08-21T12:00:00Z",
        )
        val store = ScanSearchQueueStore(listOf(item))
        val encoded = scanSearchQueueStoreToJson(store)
        assertEquals(1, JSONObject(encoded).getInt("version"))
        assertEquals(store, scanSearchQueueStoreFromJson(encoded))
    }

    @Test
    fun matchedItemsRequireACanonicalCaptureAndPendingItemsCannotCarryOne() {
        val base = """{"version":1,"items":[{
            "id":"$queueId","owner_id":"","scan_collection_id":"$collectionId",
            "photo_role":"cover","ocr_text":"Herbal","status":"pending",
            "matched_capture_id":"$captureId","revision":0,
            "created_at":"2026-08-21T12:00:00Z","updated_at":"2026-08-21T12:00:00Z",
            "dirty":true}]}""".trimIndent()
        assertFalse(scanSearchQueueStoreFromJson(base).valid)
        assertTrue(
            scanSearchQueueStoreFromJson(
                base.replace("\"pending\"", "\"matched\""),
            ).valid,
        )
    }

    @Test
    fun corruptExistingQueueIsNeverOverwritten() {
        val target = File(Files.createTempDirectory("scan-search-queue").toFile(), "queue.json")
        target.writeText("not json")
        val store = readScanSearchQueueStore(target)
        assertFalse(store.valid)
        assertFalse(saveScanSearchQueueStore(target, store.copy(items = emptyList())))
        assertEquals("not json", target.readText())
    }

    @Test
    fun localStatusesExactlyMatchTheCloudContract() {
        assertEquals(ScanSearchStatus.PENDING, ScanSearchStatus.fromWire("pending"))
        assertEquals(ScanSearchStatus.MATCHED, ScanSearchStatus.fromWire("matched"))
        assertEquals(ScanSearchStatus.FAILED, ScanSearchStatus.fromWire("failed"))
        assertNull(ScanSearchStatus.fromWire("review"))
    }

    @Test
    fun normalizationRejectsOversizedTextAndLocallyDirtyFailure() {
        val base = item(ownerId = ownerId)
        assertNull(
            normalizedScanSearchQueueItem(
                base.copy(ocrText = "x".repeat(ScanSearchQueue.MAX_OCR_CHARS + 1)),
            ),
        )
        assertNull(
            normalizedScanSearchQueueItem(
                base.copy(status = ScanSearchStatus.FAILED, dirty = true),
            ),
        )
        assertEquals(
            ScanSearchStatus.FAILED,
            normalizedScanSearchQueueItem(
                base.copy(status = ScanSearchStatus.FAILED, dirty = false),
            )?.status,
        )
    }

    @Test
    fun ownerScopedReadNeverReturnsAnotherAccountsRows() {
        val mine = item(ownerId = ownerId)
        val other = item(id = captureId, ownerId = otherOwnerId)
        assertEquals(
            listOf(mine),
            ownerScopedScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine, other)),
                ownerId,
            ).items,
        )
        assertFalse(
            ownerScopedScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine)),
                "not-a-uuid",
            ).valid,
        )
    }

    @Test
    fun cloudMergePreservesForeignRowsAndNewerDirtyLocalIntent() {
        val mine = item(ownerId = ownerId, revision = 1).copy(dirty = false)
        val other = item(id = captureId, ownerId = otherOwnerId)
        val cloud = mine.copy(
            status = ScanSearchStatus.FAILED,
            revision = 2,
            dirty = false,
        )
        val dirty = mine.copy(
            status = ScanSearchStatus.MATCHED,
            matchedCaptureId = captureId,
            dirty = true,
        )

        assertEquals(
            setOf(other, cloud),
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine, other)),
                ownerId,
                listOf(cloud),
            )?.items?.toSet(),
        )
        assertEquals(
            dirty,
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(dirty, other)),
                ownerId,
                listOf(cloud),
            )?.items?.first { it.ownerId == ownerId },
        )
        assertNull(
            mergeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(mine)),
                ownerId,
                listOf(cloud.copy(ownerId = otherOwnerId)),
            ),
        )
    }

    @Test
    fun acknowledgementIsIdempotentAndCannotRewriteIdentityOrCommittedMatch() {
        val pending = item(ownerId = ownerId)
        val accepted = pending.copy(revision = 1, dirty = false)
        assertEquals(
            accepted,
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(pending)),
                ownerId,
                pending,
                accepted,
            )?.items?.single(),
        )
        assertNull(
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(pending)),
                ownerId,
                pending,
                accepted.copy(id = captureId),
            ),
        )

        val matched = pending.copy(
            status = ScanSearchStatus.MATCHED,
            matchedCaptureId = captureId,
            revision = 1,
        )
        assertNull(
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(matched)),
                ownerId,
                matched,
                accepted.copy(revision = 2),
            ),
        )
        val concurrent = pending.copy(ocrText = "new local text")
        assertEquals(
            ScanSearchQueueStore(listOf(concurrent)),
            acknowledgeScanSearchQueueStore(
                ScanSearchQueueStore(listOf(concurrent)),
                ownerId,
                pending,
                accepted,
            ),
        )
    }

    private fun item(
        id: String = queueId,
        ownerId: String,
        revision: Long = 0,
    ) = ScanSearchQueueItem(
        id = id,
        ownerId = ownerId,
        scanCollectionId = collectionId,
        photoRole = ScanSearchPhotoRole.COVER,
        ocrText = "Herbal",
        revision = revision,
        createdAt = "2026-08-21T12:00:00Z",
    )
}
