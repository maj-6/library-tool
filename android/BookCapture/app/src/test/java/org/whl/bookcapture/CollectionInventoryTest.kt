package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CollectionInventoryTest {

    private fun summary(
        id: String,
        title: String = "The Herb Book",
        createdAt: Long = 100L,
    ) = CollectionInventorySummary(
        entryId = id,
        collectionId = "00000000-0000-0000-0000-000000000001",
        collectionName = "Fungi",
        title = title,
        author = "Jane Doe",
        year = "1982",
        photoCount = 3,
        createdAt = createdAt,
    )

    private fun entry(
        id: String,
        title: String = "Current title",
        uploaded: Boolean = true,
        createdAt: Long = 200L,
        dir: File = Files.createTempDirectory("inventory-entry").toFile(),
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
        processing = Entries.ProcessingState(
            status = Entries.ProcessingStatus.COMPLETE,
            stage = Entries.ProcessingStage.COMPLETE,
            retryable = false,
            lastError = "",
            updatedAt = createdAt,
        ),
        processingRecorded = true,
        provenance = CaptureProvenance(
            collectionId = "00000000-0000-0000-0000-000000000002",
            collectionName = "Current fungi name",
            from = "Storage",
        ),
    )

    @Test
    fun currentStoreIsVersionedAndKeyedByEntryId() {
        val original = summary("entry-1")
        val encoded = collectionInventoryStoreToJson(
            CollectionInventoryStore(mapOf(original.entryId to original)),
        )

        val root = JSONObject(encoded)
        assertEquals(COLLECTION_INVENTORY_VERSION, root.getInt("version"))
        val row = root.getJSONObject("entries").getJSONObject("entry-1")
        assertFalse(row.has("id"))
        assertEquals("Fungi", row.getString("collection_name"))
        assertEquals(3, row.getInt("photo_count"))
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
            """{"version":2,"entries":{}}""",
            """{"version":1,"entries":[]}""",
            """{"version":1,"entries":{"e":{"collection_id":4}}}""",
            """{"version":1,"entries":{"e":{
                "collection_id":"c","collection_name":"Fungi","title":"t",
                "author":"a","year":"y","photo_count":-1,"created_at":1
            }}}""",
        )

        invalid.forEach { assertFalse(collectionInventoryStoreFromJson(it).valid) }
    }

    @Test
    fun versionZeroArrayMigratesToKeyedVersionOneOnNextRecord() {
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
        assertEquals(1, migrated.getInt("version"))
        assertEquals(setOf("legacy", "new"), migrated.getJSONObject("entries").keys().asSequence().toSet())
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
    fun prunePersistsTheInventoryBeforeDeletingSentFolders() {
        val source = File("src/main/java/org/whl/bookcapture/Entries.kt").readText()
        val prune = source.substringAfter("suspend fun pruneSent")
            .substringBefore("fun atomicWrite")

        assertTrue(prune.indexOf("CollectionInventory.recordFinalized") >= 0)
        assertTrue(
            prune.indexOf("CollectionInventory.recordFinalized") <
                prune.indexOf("CaptureMetadataStore.deleteIfNoUnsyncedLocalMutation"),
        )
        assertTrue(prune.contains("val latest = runCatching { load(dir) }"))
        assertTrue(prune.contains("retainLocally(latest)"))
        assertTrue(
            prune.lastIndexOf("CollectionInventory.recordFinalized") <
                prune.indexOf("CaptureMetadataStore.deleteIfNoUnsyncedLocalMutation"),
        )
    }

    private fun tempDir(): File = Files.createTempDirectory("collection-inventory").toFile()
}
