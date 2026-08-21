package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CollectionInventoryTest {

    private fun summary(
        id: String,
        title: String = "The Herb Book",
        createdAt: Long = 100L,
        digitizationCandidateClassification: Boolean? = null,
        scanPriority: Int? = null,
    ) = CollectionInventorySummary(
        entryId = id,
        collectionId = "00000000-0000-0000-0000-000000000001",
        collectionName = "Fungi",
        title = title,
        author = "Jane Doe",
        year = "1982",
        photoCount = 3,
        createdAt = createdAt,
        digitizationCandidateClassification = digitizationCandidateClassification,
        scanPriority = scanPriority,
    )

    private fun entry(
        id: String,
        title: String = "Current title",
        uploaded: Boolean = true,
        createdAt: Long = 200L,
        dir: File = Files.createTempDirectory("inventory-entry").toFile(),
        deliveryTransport: String = "cloud",
        cloudOwnerId: String = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        desktopBook: DesktopBookMetadata? = null,
    ) = Entries.Entry(
        id = id,
        dir = dir,
        sealed = true,
        uploaded = uploaded,
        createdAt = createdAt,
        photoCount = 2,
        meta = JSONObject()
            .put("title", title)
            .put("author", "Current author")
            .put("year", "2026"),
        cloudStatus = "imported",
        deliveryTransport = deliveryTransport,
        cloudOwnerId = cloudOwnerId,
        processing = Entries.ProcessingState(
            status = Entries.ProcessingStatus.COMPLETE,
            stage = Entries.ProcessingStage.COMPLETE,
            retryable = false,
            lastError = "",
            updatedAt = createdAt,
        ),
        processingRecorded = true,
        desktopBook = desktopBook,
        provenance = CaptureProvenance(
            collectionId = "00000000-0000-0000-0000-000000000002",
            collectionName = "Current fungi name",
            from = "Storage",
        ),
    )

    private fun desktopMetadata(
        captureId: String,
        candidate: Boolean,
        priority: Int? = null,
    ): DesktopBookMetadata {
        val data = JSONObject().put("digitization_candidate", candidate)
        priority?.let { data.put("scan_priority", it) }
        return desktopBookMetadataFromJson(
            JSONObject()
                .put("capture_id", captureId)
                .put("owner_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
                .put("book_id", "manual:book-1")
                .put("revision", 1L)
                .put("updated_at", "2026-08-21T12:00:00Z")
                .put("data", data),
        )!!
    }

    @Test
    fun currentStoreIsVersionedAndKeyedByEntryId() {
        val original = summary(
            "entry-1",
            digitizationCandidateClassification = true,
            scanPriority = 3,
        )
        val encoded = collectionInventoryStoreToJson(
            CollectionInventoryStore(mapOf(original.entryId to original)),
        )

        val root = JSONObject(encoded)
        assertEquals(COLLECTION_INVENTORY_VERSION, root.getInt("version"))
        val row = root.getJSONObject("entries").getJSONObject("entry-1")
        assertFalse(row.has("id"))
        assertEquals("Fungi", row.getString("collection_name"))
        assertEquals(3, row.getInt("photo_count"))
        assertEquals("", row.getString("delivery_transport"))
        assertEquals("", row.getString("cloud_owner_id"))
        assertTrue(row.getBoolean("digitization_candidate"))
        assertEquals(3, row.getInt("scan_priority"))
        assertEquals(
            mapOf(original.entryId to original),
            collectionInventoryStoreFromJson(encoded).summaries,
        )
    }

    @Test
    fun absentStoreStartsEmptyAndIsCreatedOnFirstFinalizedEntry() {
        val target = File(tempDir(), COLLECTION_INVENTORY_FILE)
        val absent = readCollectionInventoryStore(target)

        assertTrue(absent.valid)
        assertTrue(absent.summaries.isEmpty())
        assertTrue(CollectionInventory.recordFinalized(target, listOf(entry("entry-1"))))
        assertTrue(target.isFile)
        assertEquals(setOf("entry-1"), readCollectionInventoryStore(target).summaries.keys)
    }

    @Test
    fun corruptSourceIsReadableAsInvalidButNeverOverwritten() {
        val target = File(tempDir(), COLLECTION_INVENTORY_FILE)
        val corrupt = "{\"version\":1,\"entries\":"
        target.writeText(corrupt)

        assertFalse(readCollectionInventoryStore(target).valid)
        assertFalse(CollectionInventory.recordFinalized(target, listOf(entry("entry-1"))))
        assertEquals(corrupt, target.readText())
        assertFalse(
            saveCollectionInventoryStore(
                target,
                CollectionInventoryStore(mapOf(), valid = false),
            ),
        )
        assertEquals(corrupt, target.readText())
    }

    @Test
    fun malformedOrUnknownSchemasAreNotWritableStores() {
        val invalid = listOf(
            "{}",
            """{"version":5,"entries":{}}""",
            """{"version":1,"entries":[]}""",
            """{"version":1,"entries":{"e":{"collection_id":4}}}""",
            """{"version":1,"entries":{"e":{
                "collection_id":"c","collection_name":"Fungi","title":"t",
                "author":"a","year":"y","photo_count":-1,"created_at":1
            }}}""",
            """{"version":3,"entries":{"e":{
                "collection_id":"c","collection_name":"Fungi","title":"t",
                "author":"a","year":"y","photo_count":1,"created_at":1,
                "delivery_transport":"lan","cloud_owner_id":"account-a"
            }}}""",
        )

        invalid.forEach { assertFalse(collectionInventoryStoreFromJson(it).valid) }
    }

    @Test
    fun currentScanMetadataRejectsMalformedTypesRangesAndOrphanedPriorities() {
        val valid = collectionInventoryStoreToJson(
            CollectionInventoryStore(
                mapOf("entry-1" to summary(
                    "entry-1",
                    digitizationCandidateClassification = true,
                    scanPriority = 1,
                )),
            ),
        )
        val invalidValues = listOf(
            "true" to 1,
            true to 0,
            true to 6,
            true to 1.5,
            true to "1",
            true to true,
            false to 1,
            JSONObject.NULL to 1,
        )
        invalidValues.forEach { (candidate, priority) ->
            val root = JSONObject(valid)
            val row = root.getJSONObject("entries").getJSONObject("entry-1")
            row.put("digitization_candidate", candidate)
            row.put("scan_priority", priority)
            assertFalse(collectionInventoryStoreFromJson(root.toString()).valid)
        }
        assertFalse(collectionInventoryStoreFromJson(
            valid.replace("\"scan_priority\":1", "\"scan_priority\":1.0"),
        ).valid)

        listOf(
            summary(
                "not-candidate",
                digitizationCandidateClassification = false,
                scanPriority = 1,
            ),
            summary(
                "orphaned",
                digitizationCandidateClassification = null,
                scanPriority = 1,
            ),
            summary(
                "out-of-range",
                digitizationCandidateClassification = true,
                scanPriority = 6,
            ),
        ).forEach { invalid ->
            assertThrows(IllegalArgumentException::class.java) {
                collectionInventoryStoreToJson(
                    CollectionInventoryStore(mapOf(invalid.entryId to invalid)),
                )
            }
        }
    }

    @Test
    fun explicitCandidateClearRoundTripsWithoutAPriority() {
        val cleared = summary(
            "entry-1",
            digitizationCandidateClassification = false,
        )
        val encoded = collectionInventoryStoreToJson(
            CollectionInventoryStore(mapOf(cleared.entryId to cleared)),
        )
        val row = JSONObject(encoded).getJSONObject("entries").getJSONObject("entry-1")

        assertFalse(row.getBoolean("digitization_candidate"))
        assertTrue(row.isNull("scan_priority"))
        assertEquals(
            cleared,
            collectionInventoryStoreFromJson(encoded).summaries.getValue("entry-1"),
        )
    }

    @Test
    fun versionZeroArrayMigratesToTheCurrentKeyedVersionOnNextRecord() {
        val target = File(tempDir(), COLLECTION_INVENTORY_FILE)
        target.writeText(
            """{"version":0,"entries":[{
                "id":"legacy","collection_id":"old-c","collection_name":"Old crate",
                "title":"Old book","author":"A","year":"1901",
                "photo_count":1,"created_at":10
            }]}""".trimIndent(),
        )

        val legacy = readCollectionInventoryStore(target)
        assertTrue(legacy.valid)
        assertEquals("Old book", legacy.summaries.getValue("legacy").title)
        assertTrue(CollectionInventory.recordFinalized(target, listOf(entry("new"))))

        val migrated = JSONObject(target.readText())
        assertEquals(COLLECTION_INVENTORY_VERSION, migrated.getInt("version"))
        assertEquals(setOf("legacy", "new"), migrated.getJSONObject("entries").keys().asSequence().toSet())
        assertEquals(
            "",
            readCollectionInventoryStore(target).summaries.getValue("legacy").deliveryTransport,
        )
        assertEquals(
            "",
            readCollectionInventoryStore(target).summaries.getValue("legacy").cloudOwnerId,
        )
    }

    @Test
    fun versionOneKeyedRowsRemainReadableWithUnknownDeliveryTransport() {
        val legacy = """{"version":1,"entries":{"legacy":{
            "collection_id":"old-c","collection_name":"Old crate",
            "title":"Old book","author":"A","year":"1901",
            "photo_count":1,"created_at":10
        }}}""".trimIndent()

        val parsed = collectionInventoryStoreFromJson(legacy)

        assertTrue(parsed.valid)
        assertEquals("", parsed.summaries.getValue("legacy").deliveryTransport)
        assertEquals("", parsed.summaries.getValue("legacy").cloudOwnerId)
        assertEquals(1, parsed.sourceVersion)
    }

    @Test
    fun versionTwoCloudRowsRemainReadableWithUnknownOwner() {
        val legacy = """{"version":2,"entries":{"legacy":{
            "collection_id":"old-c","collection_name":"Old crate",
            "title":"Old book","author":"A","year":"1901",
            "photo_count":1,"created_at":10,"delivery_transport":"cloud"
        }}}""".trimIndent()

        val parsed = collectionInventoryStoreFromJson(legacy)

        assertTrue(parsed.valid)
        assertEquals("cloud", parsed.summaries.getValue("legacy").deliveryTransport)
        assertEquals("", parsed.summaries.getValue("legacy").cloudOwnerId)
        assertEquals(2, parsed.sourceVersion)
    }

    @Test
    fun versionThreeRowsRemainReadableWithUnknownScanClassification() {
        val legacy = """{"version":3,"entries":{"legacy":{
            "collection_id":"old-c","collection_name":"Old crate",
            "title":"Old book","author":"A","year":"1901",
            "photo_count":1,"created_at":10,"delivery_transport":"cloud",
            "cloud_owner_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        }}}""".trimIndent()

        val parsed = collectionInventoryStoreFromJson(legacy)
        val summary = parsed.summaries.getValue("legacy")

        assertTrue(parsed.valid)
        assertNull(summary.digitizationCandidateClassification)
        assertFalse(summary.digitizationCandidate)
        assertNull(summary.scanPriority)
        assertEquals(3, parsed.sourceVersion)
    }

    @Test
    fun cloudDeliveryOwnerUsesLegacyCreatorAndFailsClosedOnConflict() {
        val ownerA = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        val manifest = JSONObject()
            .put("delivery_transport", "cloud")
            .put("creator", JSONObject()
                .put("kind", Prefs.CREATOR_ACCOUNT)
                .put("id", ownerA))

        assertEquals(ownerA, cloudOwnerIdFromDeliveryManifest(manifest))
        assertEquals(
            ownerA,
            cloudOwnerIdFromDeliveryManifest(
                JSONObject(manifest.toString()).put(
                    CLOUD_OWNER_MANIFEST_KEY,
                    ownerA.uppercase(),
                ),
            ),
        )
        assertEquals(
            "",
            cloudOwnerIdFromDeliveryManifest(
                JSONObject(manifest.toString()).put(
                    CLOUD_OWNER_MANIFEST_KEY,
                    "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                ),
            ),
        )
        assertEquals(
            "",
            cloudOwnerIdFromDeliveryManifest(
                JSONObject(manifest.toString()).put("delivery_transport", "lan"),
            ),
        )
    }

    @Test
    fun versionThreeOwnersAreUuidCheckedAndCanonicalized() {
        val uppercaseOwner = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
        val encoded = collectionInventoryStoreToJson(
            CollectionInventoryStore(
                mapOf("entry-1" to summary("entry-1").copy(
                    deliveryTransport = "cloud",
                    cloudOwnerId = uppercaseOwner,
                )),
            ),
        )

        val row = JSONObject(encoded).getJSONObject("entries").getJSONObject("entry-1")
        assertEquals(uppercaseOwner.lowercase(), row.getString("cloud_owner_id"))
        assertEquals(
            uppercaseOwner.lowercase(),
            collectionInventoryStoreFromJson(encoded).summaries.getValue("entry-1").cloudOwnerId,
        )
        assertFalse(
            collectionInventoryStoreFromJson(
                encoded.replace(uppercaseOwner.lowercase(), "not-a-uuid"),
            ).valid,
        )
    }

    @Test
    fun finalizedSummaryRetainsDesktopCandidateClassificationAndPriority() {
        val captureId = "00000000-0000-4000-8000-000000000001"
        val candidate = collectionInventorySummary(
            entry(
                captureId,
                desktopBook = desktopMetadata(captureId, candidate = true, priority = 5),
            ),
        )
        assertEquals(true, candidate.digitizationCandidateClassification)
        assertTrue(candidate.digitizationCandidate)
        assertEquals(5, candidate.scanPriority)

        val cleared = collectionInventorySummary(
            entry(
                captureId,
                desktopBook = desktopMetadata(captureId, candidate = false, priority = 1),
            ),
        )
        assertEquals(false, cleared.digitizationCandidateClassification)
        assertFalse(cleared.digitizationCandidate)
        assertNull(cleared.scanPriority)
    }

    @Test
    fun mergeHasNoDuplicatesAndCurrentEntryWinsEveryDisplayedField() {
        val stale = summary("same", title = "Stale title", createdAt = 1L)
        val durableOnly = summary("durable", title = "Durable title", createdAt = 150L)
        val current = entry("same", title = "Fresh title", createdAt = 200L)

        val merged = mergeCollectionInventory(listOf(stale, durableOnly, stale), listOf(current))

        assertEquals(listOf("same", "durable"), merged.map { it.summary.entryId })
        val winning = merged.first()
        assertSame(current, winning.current)
        assertEquals("Fresh title", winning.summary.title)
        assertEquals("Current author", winning.summary.author)
        assertEquals("Current fungi name", winning.summary.collectionName)
        assertEquals(2, winning.summary.photoCount)
        assertEquals("cloud", winning.summary.deliveryTransport)
        assertEquals(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            winning.summary.cloudOwnerId,
        )
        assertNull(merged.last().current)
    }

    @Test
    fun persistenceKeepsOnlyPhotoCountAndNeverCopiesMedia() {
        val root = tempDir()
        val entryDir = File(root, "sent-entry").apply { mkdirs() }
        val originalPhoto = File(entryDir, "photo_1.jpg").apply { writeBytes(byteArrayOf(1, 2, 3)) }
        val target = File(root, COLLECTION_INVENTORY_FILE)

        assertTrue(CollectionInventory.recordFinalized(target, listOf(entry("sent-entry", dir = entryDir))))

        val row = JSONObject(target.readText()).getJSONObject("entries").getJSONObject("sent-entry")
        assertEquals(2, row.getInt("photo_count"))
        assertEquals("cloud", row.getString("delivery_transport"))
        assertEquals(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            row.getString("cloud_owner_id"),
        )
        assertFalse(row.has("photos"))
        assertFalse(target.readText().contains(originalPhoto.absolutePath))
        assertEquals(
            listOf(originalPhoto.canonicalFile),
            root.walkTopDown().filter { it.isFile && it.extension == "jpg" }
                .map { it.canonicalFile }.toList(),
        )
    }

    @Test
    fun unsentEntriesAreNotArchived() {
        val target = File(tempDir(), COLLECTION_INVENTORY_FILE)

        assertTrue(CollectionInventory.recordFinalized(target, listOf(entry("queue", uploaded = false))))

        assertTrue(readCollectionInventoryStore(target).summaries.isEmpty())
    }

    @Test
    fun aLaterFinalizedSnapshotReplacesStaleMetadataForTheSameEntry() {
        val target = File(tempDir(), COLLECTION_INVENTORY_FILE)

        assertTrue(CollectionInventory.recordFinalized(target, listOf(entry("same", title = "Old"))))
        assertTrue(CollectionInventory.recordFinalized(target, listOf(entry("same", title = "New"))))

        val stored = readCollectionInventoryStore(target).summaries
        assertEquals(setOf("same"), stored.keys)
        assertEquals("New", stored.getValue("same").title)
    }

    @Test
    fun prunePersistsTheInventoryBeforeArchivingSentFolders() {
        val source = File("src/main/java/org/whl/bookcapture/Entries.kt").readText()
        val prune = source.substringAfter("suspend fun pruneSent")
            .substringBefore("fun atomicWrite")

        assertTrue(prune.indexOf("CollectionInventory.recordFinalized") >= 0)
        assertTrue(
            prune.indexOf("CollectionInventory.recordFinalized") <
                prune.indexOf("CaptureMetadataStore.archiveIfNoUnsyncedLocalMutation"),
        )
        assertTrue(prune.contains("val latest = runCatching { load(dir) }"))
        assertTrue(prune.contains("retainLocally(latest)"))
        assertTrue(
            prune.lastIndexOf("CollectionInventory.recordFinalized") <
                prune.indexOf("CaptureMetadataStore.archiveIfNoUnsyncedLocalMutation"),
        )
        // Retention must never reach a destructive call. A capture displaced
        // from the browsing list is moved to archive/, not erased.
        assertFalse(prune.contains("deleteIfNoUnsyncedLocalMutation"))
        assertFalse(prune.contains("deleteRecursively"))
    }

    private fun tempDir(): File = Files.createTempDirectory("collection-inventory").toFile()
}
