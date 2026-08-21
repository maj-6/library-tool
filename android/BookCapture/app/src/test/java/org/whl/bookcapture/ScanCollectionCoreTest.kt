package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest

class ScanCollectionCoreTest {
    private val owner = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    private val captureId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    private val sourceId = "11111111-1111-4111-8111-111111111111"
    private val scanId = "22222222-2222-4222-8222-222222222222"
    private val otherCaptureId = "33333333-3333-4333-8333-333333333333"

    @Test
    fun collectionTypesRoundTripAndResolveIndependentSelections() {
        val capture = BookCollection(sourceId, "Shelf A", "Office")
        val scan = BookCollection(
            scanId,
            "Digitize next",
            "Scan room",
            collectionType = CollectionType.SCAN,
        )
        val parsed = collectionsFromJson(collectionsToJson(listOf(capture, scan)))

        assertEquals(CollectionType.CAPTURE, parsed[0].collectionType)
        assertEquals(CollectionType.SCAN, parsed[1].collectionType)
        assertEquals(capture, resolveCurrentCollection(parsed, sourceId))
        assertEquals(
            scan,
            resolveCurrentCollection(parsed, scanId, CollectionType.SCAN),
        )
        // A pointer from the other collection role is ignored; the usual
        // single-choice fallback still applies within the requested role.
        assertEquals(capture, resolveCurrentCollection(parsed, scanId, CollectionType.CAPTURE))
        assertEquals("scan", CollectionType.SCAN.wireValue)
        assertEquals(CollectionType.SCAN, CollectionType.fromWire(" SCAN "))
    }

    @Test
    fun legacyCollectionsDefaultToCaptureAndCreationFixesType() {
        val legacy = collectionStoreFromJson(
            """{"version":4,"collections":[{
                "id":"$sourceId","name":"Shelf A","from":"Office","tag_id":"SHELF_A_1"
            }]}""".trimIndent(),
        )
        assertTrue(legacy.valid)
        assertEquals(CollectionType.CAPTURE, legacy.collections.single().collectionType)

        val added = addCollection(
            legacy.collections,
            "Digitize next",
            "Scan room",
            id = scanId,
            collectionType = CollectionType.SCAN,
        ).collections!!.last()
        assertEquals(CollectionType.SCAN, added.collectionType)

        val edited = updateCollection(
            legacy.collections + added,
            scanId,
            "Digitize soon",
            "Scan room",
        ).collections!!.first { it.id == scanId }
        assertEquals(CollectionType.SCAN, edited.collectionType)
    }

    @Test
    fun versionFourShadowRebasesAcrossTypeFormatMigration() {
        val oldCanonical = listOf(
            sourceId,
            "Shelf A",
            "Office",
            "false",
            "",
            "tag:SHELF_A_1",
        ).joinToString("\u0000")
        val oldHash = MessageDigest.getInstance("SHA-256")
            .digest(oldCanonical.toByteArray())
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }
        val parsed = collectionStoreFromJson(
            """{"version":4,"collections":[{
                "id":"$sourceId","name":"Shelf A","from":"Office",
                "tag_id":"SHELF_A_1","updated_at":"2026-08-21T12:00:00Z"
            }],"sync_shadow":{"$sourceId":{
                "hash":"$oldHash","updated_at":"2026-08-21T12:00:00Z"
            }},"sync_dirty":[]}""".trimIndent(),
        )

        assertTrue(parsed.valid)
        assertEquals(
            collectionContentHash(parsed.collections.single()),
            parsed.shadow.getValue(sourceId).hash,
        )
        assertTrue(parsed.dirty.isEmpty())
    }

    @Test
    fun cloudInsertIncludesImmutableTypeWhilePatchOmitsIt() {
        val scan = BookCollection(
            scanId,
            "Digitize next",
            "Scan room",
            collectionType = CollectionType.SCAN,
        )
        val insert = collectionCloudBody(
            scan,
            ownerId = owner,
            includeUpdatedAt = false,
            includeCollectionType = true,
        )
        val patch = collectionCloudBody(scan)

        assertEquals("scan", insert.getString("collection_type"))
        assertFalse(patch.has("collection_type"))
        assertEquals(
            CollectionType.SCAN,
            cloudCollectionFromJson(
                JSONObject()
                    .put("id", scanId)
                    .put("name", "Digitize next")
                    .put("from_place", "Scan room")
                    .put("tag_id", "DIGITIZE_NEXT_1")
                    .put("collection_type", "scan"),
            )!!.collectionType,
        )
    }

    @Test
    fun inventoryProjectionCarriesActiveScanAuditAndPresentsCandidate() {
        val row = JSONObject()
            .put("id", captureId)
            .put("created_by", owner)
            .put("created_at", "2026-08-21T12:00:00+00:00")
            .put(CAPTURE_ORIGINAL_COLLECTION_ID_FIELD, sourceId)
            .put(CAPTURE_COLLECTION_ID_FIELD, scanId)
            .put(CAPTURE_COLLECTION_NAME_FIELD, "Digitize next")
            .put("title", "Herbal")
            .put("author", "A. Author")
            .put("year", "1901")
            .put(CAPTURE_COLLECTION_PHOTO_COUNT_FIELD, 2)
            .put(CAPTURE_COLLECTION_REMOVED_FIELD, false)
            .put(CAPTURE_COLLECTION_REVISION_FIELD, 4)
            .put(CAPTURE_COLLECTION_TYPE_FIELD, "scan")
            .put(CAPTURE_SCAN_MARKED_FIELD, true)
            .put(CAPTURE_SCAN_SOURCE_COLLECTION_ID_FIELD, sourceId)
            .put(CAPTURE_SCAN_DESTINATION_COLLECTION_ID_FIELD, scanId)
            .put(CAPTURE_SCAN_REVISION_FIELD, 7)

        val book = remoteCollectionBookFromCaptureJson(row, owner)!!
        assertEquals(CollectionType.SCAN, book.collectionType)
        assertTrue(book.scanMarked)
        assertEquals(sourceId, book.scanSourceCollectionId)
        assertEquals(scanId, book.scanDestinationCollectionId)
        assertEquals(7L, book.scanRevision)
        assertTrue(book.digitizationCandidate)

        val store = RemoteCollectionBooksStore(
            mapOf(scanId to listOf(book)),
            owner = owner,
        )
        assertEquals(store, remoteCollectionBooksStoreFromJson(
            remoteCollectionBooksStoreToJson(store),
        ))
        assertTrue(book.toInventoryItem().summary.digitizationCandidate)
    }

    @Test
    fun optimisticMoveToScanUpdatesCachedPresentationAndPreservesSourceAudit() {
        val target = File(Files.createTempDirectory("scan-cache").toFile(), "cache.json")
        val original = RemoteCollectionBook(
            captureId = captureId,
            originalCollectionId = sourceId,
            collectionId = sourceId,
            removed = false,
            membershipRevision = 2L,
            collectionName = "Shelf A",
            title = "Herbal",
            author = "",
            year = "",
            photoCount = 1,
            createdAt = 1L,
        )
        assertTrue(saveRemoteCollectionBooksStore(
            target,
            RemoteCollectionBooksStore(mapOf(sourceId to listOf(original)), owner = owner),
        ))

        assertTrue(RemoteCollectionBooks.applyMembershipMutation(
            target,
            owner,
            listOf(captureId),
            scanId,
            "Digitize next",
            removed = false,
            collectionType = CollectionType.SCAN,
        ))
        val marked = readRemoteCollectionBooksStore(target, owner)
            .byCollection.getValue(sourceId).single()
        assertEquals(scanId, marked.collectionId)
        assertEquals(CollectionType.SCAN, marked.collectionType)
        assertTrue(marked.scanMarked)
        assertEquals(sourceId, marked.scanSourceCollectionId)
        assertEquals(scanId, marked.scanDestinationCollectionId)
        assertEquals(1L, marked.scanRevision)
        assertTrue(marked.digitizationCandidate)

        assertTrue(RemoteCollectionBooks.applyMembershipMutation(
            target,
            owner,
            listOf(captureId),
            otherCaptureId,
            "Shelf B",
            removed = false,
            collectionType = CollectionType.CAPTURE,
        ))
        val returned = readRemoteCollectionBooksStore(target, owner)
            .byCollection.getValue(sourceId).single()
        assertFalse(returned.scanMarked)
        assertEquals(sourceId, returned.scanSourceCollectionId)
        assertEquals(scanId, returned.scanDestinationCollectionId)
        assertEquals(2L, returned.scanRevision)
    }

    @Test
    fun localInventoryCacheRoundTripsScanState() {
        val summary = CollectionInventorySummary(
            entryId = captureId,
            collectionId = scanId,
            collectionName = "Digitize next",
            title = "Herbal",
            author = "",
            year = "",
            photoCount = 1,
            createdAt = 1L,
            collectionType = CollectionType.SCAN,
            scanMarked = true,
            scanSourceCollectionId = sourceId,
            scanDestinationCollectionId = scanId,
            scanRevision = 3L,
        )
        val store = CollectionInventoryStore(mapOf(captureId to summary))
        assertEquals(
            store,
            collectionInventoryStoreFromJson(collectionInventoryStoreToJson(store)),
        )
        assertTrue(summary.digitizationCandidate)
    }
}
