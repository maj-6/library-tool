package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * The cloud read-back that lets a scanned box list its books on a phone that
 * never captured them. Its desktop counterpart is `PHONE_PROVENANCE_KEYS` in
 * tools/whl_explorer/server.py: this test names the `scan_collection_id` wire key
 * so renaming it fails on both sides.
 */
class RemoteCollectionBooksTest {

    private val owner1 = "11111111-1111-1111-1111-111111111111"
    private val owner2 = "22222222-2222-2222-2222-222222222222"
    // capture_book_metadata's parser requires uuid capture ids, matching the
    // `captures.id` uuid column the entry id is inserted into.
    private val capA = "aaaaaaaa-0000-4000-8000-000000000001"
    private val capZ = "aaaaaaaa-0000-4000-8000-00000000000f"
    private val boxA = "00000000-0000-0000-0000-00000000000a"
    private val boxB = "00000000-0000-0000-0000-00000000000b"
    private val boxC = "00000000-0000-0000-0000-00000000000c"

    private fun tempFile(): File {
        val dir = Files.createTempDirectory("remote-books").toFile()
        return File(dir, REMOTE_COLLECTION_BOOKS_FILE)
    }

    /**
     * The shape PostgREST returns for the box-listing query: FLAT aliased columns,
     * because the request projects `meta->>...` rather than the whole blob. A key
     * the capture lacks comes back as JSON null, not as an absent key.
     */
    private fun captureRow(
        id: String,
        collectionId: String? = boxA,
        title: String = "Materia Medica",
        photos: Int = 3,
        createdAt: String = "2026-07-20T11:59:45.048996+00:00",
    ): JSONObject = JSONObject()
        .put("id", id)
        .put("created_at", createdAt)
        .put("photos", JSONArray(List(photos) { "dev/$id/photo_$it.jpg" }))
        .put(CAPTURE_COLLECTION_ID_FIELD, collectionId ?: JSONObject.NULL)
        .put(CAPTURE_COLLECTION_NAME_FIELD, "Blue crate")
        .put("title", if (title.isEmpty()) JSONObject.NULL else title)
        .put("author", if (title.isEmpty()) JSONObject.NULL else "Boerhaave, H.")
        .put("year", if (title.isEmpty()) JSONObject.NULL else "1741")

    private fun collection(
        id: String,
        mergedInto: String? = null,
        deleted: Boolean = false,
    ) = BookCollection(
        id = id,
        name = "Box ${id.takeLast(1)}",
        from = "Storage",
        deleted = deleted,
        mergedInto = mergedInto,
    )

    private fun remoteBook(id: String, collectionId: String = boxA) =
        RemoteCollectionBook(
            captureId = id,
            collectionId = collectionId,
            collectionName = "Blue crate",
            title = "Materia Medica",
            author = "Boerhaave, H.",
            year = "1741",
            photoCount = 3,
            createdAt = 100L,
        )

    // --- capture row parsing ---------------------------------------------------

    @Test
    fun readsProvenanceAndPhotoCountFromACaptureRow() {
        val book = remoteCollectionBookFromCaptureJson(captureRow("abcd1234"))
        assertEquals("abcd1234", book?.captureId)
        assertEquals(boxA, book?.collectionId)
        assertEquals("Blue crate", book?.collectionName)
        assertEquals("Materia Medica", book?.title)
        assertEquals("Boerhaave, H.", book?.author)
        assertEquals("1741", book?.year)
        assertEquals(3, book?.photoCount)
    }

    @Test
    fun rejectsARowWithoutCollectionProvenance() {
        // A JSON-null collection id must not become the string "null" and file the
        // book under a phantom box.
        assertNull(remoteCollectionBookFromCaptureJson(captureRow("a", collectionId = null)))
        assertNull(remoteCollectionBookFromCaptureJson(captureRow("a", collectionId = "")))
        assertNull(
            remoteCollectionBookFromCaptureJson(
                JSONObject().put("id", "a").put("created_at", "2026-07-20T00:00:00+00:00"),
            ),
        )
        assertNull(remoteCollectionBookFromCaptureJson(captureRow("")))
    }

    @Test
    fun keepsAKeylessPhonesCaptureWithNoExtractedBibliography() {
        // A phone with no extraction API key uploads provenance and notes only.
        // The row must still list, or the box looks empty. `meta->>title` projects
        // SQL NULL, and optString would render that as the literal string "null".
        val book = remoteCollectionBookFromCaptureJson(captureRow("a", title = ""))
        assertEquals("a", book?.captureId)
        assertEquals("", book?.title)
        assertEquals("", book?.author)
        assertEquals("", book?.year)
    }

    @Test
    fun anAbsentProjectedColumnIsAlsoEmptyRatherThanNull() {
        val row = JSONObject()
            .put("id", "a")
            .put(CAPTURE_COLLECTION_ID_FIELD, boxA)
        val book = remoteCollectionBookFromCaptureJson(row)
        assertEquals("", book?.title)
        assertEquals("", book?.collectionName)
        assertEquals(0, book?.photoCount)
        assertEquals(0L, book?.createdAt)
    }

    @Test
    fun parsesPostgrestOffsetTimestampsAndSurvivesJunk() {
        // Sub-millisecond precision truncates rather than rounding or throwing.
        assertEquals(
            1784548785048L,
            parseCaptureCreatedAt("2026-07-20T11:59:45.048996+00:00"),
        )
        assertEquals(1784548785000L, parseCaptureCreatedAt("2026-07-20T11:59:45Z"))
        assertEquals(0L, parseCaptureCreatedAt("not a timestamp"))
        assertEquals(0L, parseCaptureCreatedAt(""))
    }

    // --- merge closure ---------------------------------------------------------

    @Test
    fun closureCollectsEveryMergeLoserThatResolvesToTheSurvivor() {
        // A -> B -> C(live). Asking for C must also ask for A and B, because
        // captures.meta is never rewritten by a merge.
        val records = listOf(
            collection(boxA, mergedInto = boxB),
            collection(boxB, mergedInto = boxC),
            collection(boxC),
        )
        assertEquals(setOf(boxC, boxA, boxB), collectionMergeClosure(records, boxC))
    }

    @Test
    fun closureExcludesPlainDeletesAndUnrelatedBoxes() {
        val other = "00000000-0000-0000-0000-0000000000ff"
        val records = listOf(
            collection(boxA, deleted = true),          // deleted, never merged
            collection(boxB, mergedInto = other),      // merged elsewhere
            collection(boxC),
        )
        assertEquals(setOf(boxC), collectionMergeClosure(records, boxC))
    }

    @Test
    fun closureTerminatesOnAMergeCycleAndRejectsAnEmptyId() {
        val records = listOf(
            collection(boxA, mergedInto = boxB),
            collection(boxB, mergedInto = boxA),
        )
        assertEquals(setOf(boxA, boxB), collectionMergeClosure(records, boxA))
        assertTrue(collectionMergeClosure(records, "").isEmpty())
    }

    // --- local vs remote merge -------------------------------------------------

    @Test
    fun aLocalRowAlwaysWinsOverItsCloudTwinInEitherOrder() {
        val local = CollectionInventoryItem(
            summary = remoteBook("shared").let {
                CollectionInventorySummary(
                    entryId = it.captureId,
                    collectionId = it.collectionId,
                    collectionName = it.collectionName,
                    title = "Local title",
                    author = it.author,
                    year = it.year,
                    photoCount = it.photoCount,
                    createdAt = it.createdAt,
                )
            },
            current = null,
        )
        val remote = remoteBook("shared").toInventoryItem()

        listOf(listOf(local, remote), listOf(remote, local)).forEach { input ->
            val merged = mergeCollectionBookItems(input)
            assertEquals(1, merged.size)
            assertFalse(merged.single().remote)
            assertEquals("Local title", merged.single().summary.title)
        }
    }

    @Test
    fun mergeKeepsDistinctBooksAndDropsIdlessRows() {
        val merged = mergeCollectionBookItems(
            listOf(
                remoteBook("one").toInventoryItem(),
                remoteBook("two").toInventoryItem(),
                remoteBook("").toInventoryItem(),
            ),
        )
        assertEquals(listOf("one", "two"), merged.map { it.summary.entryId })
    }

    @Test
    fun aCloudRowIsMarkedRemoteAndCarriesNoOpenableEntry() {
        val item = remoteBook("a").toInventoryItem()
        assertTrue(item.remote)
        assertNull(item.current)
        assertEquals("a", item.summary.entryId)
    }

    // --- desktop bibliography enrichment ---------------------------------------

    /** The desktop's projection as it arrives from `capture_book_metadata`. */
    private fun desktopMetadata(
        captureId: String,
        title: String? = "Desktop Title",
        author: String? = "D. Desktop",
        year: String? = "1899",
        includeBlock: Boolean = true,
    ): DesktopBookMetadata {
        val data = JSONObject()
            .put("schema", "org.whl.capture.desktop-book-metadata")
            .put("version", 1)
        if (includeBlock) {
            val biblio = JSONObject()
            title?.let { biblio.put("title", it) }
            author?.let { biblio.put("author", it) }
            year?.let { biblio.put("year", it) }
            data.put("bibliography", biblio)
        }
        return desktopBookMetadataFromJson(
            JSONObject()
                .put("capture_id", captureId)
                .put("owner_id", owner1)
                .put("book_id", "book-1")
                .put("revision", 1L)
                .put("updated_at", "2026-07-24T00:00:00+00:00")
                .put("data", data),
        )!!
    }

    @Test
    fun blankTitlesAreFilledFromTheDesktopProjection() {
        val book = remoteBook(capA).copy(title = "", author = "", year = "")
        val enriched = enrichRemoteCollectionBooks(
            listOf(book), mapOf(capA to desktopMetadata(capA)),
        ).single()
        assertEquals("Desktop Title", enriched.title)
        assertEquals("D. Desktop", enriched.author)
        assertEquals("1899", enriched.year)
    }

    @Test
    fun theCapturesOwnSnapshotWinsOverTheDesktopProjection() {
        // What the contributor saw at capture time is not overwritten by a later
        // desktop edit — same rule as the frozen collection-name snapshot.
        val enriched = enrichRemoteCollectionBooks(
            listOf(remoteBook(capA)), mapOf(capA to desktopMetadata(capA)),
        ).single()
        assertEquals("Materia Medica", enriched.title)
        assertEquals("Boerhaave, H.", enriched.author)
        assertEquals("1741", enriched.year)
    }

    @Test
    fun enrichmentFillsFieldsIndependently() {
        val book = remoteBook(capA).copy(author = "", year = "")
        val enriched = enrichRemoteCollectionBooks(
            listOf(book), mapOf(capA to desktopMetadata(capA)),
        ).single()
        assertEquals("Materia Medica", enriched.title)
        assertEquals("D. Desktop", enriched.author)
        assertEquals("1899", enriched.year)
    }

    @Test
    fun anOlderDesktopWithoutTheBlockLeavesRowsUntouched() {
        // Backward compatibility: a projection written before `bibliography`
        // existed must degrade to empty, never to a crash or a wrong value.
        val book = remoteBook(capA).copy(title = "", author = "", year = "")
        val enriched = enrichRemoteCollectionBooks(
            listOf(book), mapOf(capA to desktopMetadata(capA, includeBlock = false)),
        ).single()
        assertEquals(book, enriched)
        assertTrue(desktopMetadata(capA, includeBlock = false).bibliography.isEmpty)
    }

    @Test
    fun aPartialOrOverlongBlockDegradesPerField() {
        val book = remoteBook(capA).copy(title = "", author = "", year = "")
        val partial = enrichRemoteCollectionBooks(
            listOf(book),
            mapOf(capA to desktopMetadata(capA, author = null, year = null)),
        ).single()
        assertEquals("Desktop Title", partial.title)
        assertEquals("", partial.author)

        // strictString rejects an over-long value outright; the desktop clamps to
        // the same bound, so this only fires if the two sides drift apart.
        val overlong = enrichRemoteCollectionBooks(
            listOf(book),
            mapOf(capA to desktopMetadata(
                capA, title = "T".repeat(DESKTOP_BIBLIOGRAPHY_FIELD_MAX + 1),
            )),
        ).single()
        assertEquals("", overlong.title)
        assertEquals("D. Desktop", overlong.author)
    }

    @Test
    fun aCaptureWithNoProjectionIsLeftAlone() {
        val book = remoteBook(capA).copy(title = "", author = "", year = "")
        assertEquals(
            listOf(book),
            enrichRemoteCollectionBooks(listOf(book), emptyMap()),
        )
        // A projection for a DIFFERENT capture must never leak across rows.
        assertEquals(
            listOf(book),
            enrichRemoteCollectionBooks(listOf(book), mapOf(capZ to desktopMetadata(capZ))),
        )
    }

    // --- cache store -----------------------------------------------------------

    @Test
    fun storeRoundTripsThroughJson() {
        val target = tempFile()
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one"))))
        val store = readRemoteCollectionBooksStore(target, owner1)
        assertTrue(store.valid)
        assertEquals(1, store.byCollection.getValue(boxA).size)
        assertEquals(remoteBook("one"), store.byCollection.getValue(boxA).single())
    }

    @Test
    fun recordingOneBoxLeavesAnotherBoxesCacheAlone() {
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one")))
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(remoteBook("two", boxB)))
        RemoteCollectionBooks.record(target, owner1, boxA, emptyList())

        val store = readRemoteCollectionBooksStore(target, owner1)
        assertTrue(store.byCollection.getValue(boxA).isEmpty())
        assertEquals(listOf("two"), store.byCollection.getValue(boxB).map { it.captureId })
    }

    @Test
    fun anUnreadableSourceIsNeverOverwritten() {
        val target = tempFile()
        target.parentFile.mkdirs()
        target.writeText("{ not json")
        assertFalse(readRemoteCollectionBooksStore(target, owner1).valid)
        assertFalse(RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one"))))
        assertEquals("{ not json", target.readText())
    }

    @Test
    fun pruneDropsCachedBoxesThatNoLongerExist() {
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one")))
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(remoteBook("two", boxB)))

        assertTrue(RemoteCollectionBooks.prune(target, owner1, setOf(boxB)))
        val store = readRemoteCollectionBooksStore(target, owner1)
        assertEquals(setOf(boxB), store.byCollection.keys)
    }

    @Test
    fun anEmptyCollectionIdIsNeverRecorded() {
        val target = tempFile()
        assertFalse(RemoteCollectionBooks.record(target, owner1, "", listOf(remoteBook("one"))))
        assertFalse(target.exists())
    }

    @Test
    fun anotherAccountsCacheReadsAsEmptyRatherThanAsThisAccountsBooks() {
        // Shared handset: sign-out leaves filesDir alone, so the store must fail
        // closed on owner rather than showing U1's box contents to U2. Every other
        // account-scoped path in the app does the same.
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one")))

        val asOwner2 = readRemoteCollectionBooksStore(target, owner2)
        assertTrue(asOwner2.valid)
        assertTrue(asOwner2.byCollection.isEmpty())

        // …and U1's rows are still there for U1 until something overwrites them.
        assertEquals(
            listOf("one"),
            readRemoteCollectionBooksStore(target, owner1)
                .byCollection.getValue(boxA).map { it.captureId },
        )
    }

    @Test
    fun recordingAsANewAccountReplacesTheOldAccountsStoreWholesale() {
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one")))
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(remoteBook("two", boxB)))

        assertTrue(RemoteCollectionBooks.record(target, owner2, boxA, listOf(remoteBook("three"))))
        val store = readRemoteCollectionBooksStore(target, owner2)
        assertEquals(setOf(boxA), store.byCollection.keys)
        assertEquals(listOf("three"), store.byCollection.getValue(boxA).map { it.captureId })
        // The previous owner must not read its old rows back out of a store that
        // now belongs to someone else.
        assertTrue(readRemoteCollectionBooksStore(target, owner1).byCollection.isEmpty())
    }

    @Test
    fun anEmptyOwnerNeverRecords() {
        val target = tempFile()
        assertFalse(RemoteCollectionBooks.record(target, "", boxA, listOf(remoteBook("one"))))
        assertFalse(target.exists())
    }

    @Test
    fun pruneEvictsATombstonedBoxUsingTheKeepSetInspectCanActuallyBuild() {
        // A delete tombstones the row in place, so a keep-set of "every id still in
        // allRecords" would never evict anything. Inspect keys the keep-set off what
        // it can render instead: live boxes plus their merge closures.
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one")))
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(remoteBook("two", boxB)))

        val records = listOf(
            collection(boxA, deleted = true),   // tombstoned, still in allRecords
            collection(boxB),
        )
        val keep = records.asSequence()
            .filter { !it.deleted && it.mergedInto == null }
            .flatMapTo(mutableSetOf()) { collectionMergeClosure(records, it.id) }

        assertTrue(RemoteCollectionBooks.prune(target, owner1, keep))
        assertEquals(
            setOf(boxB),
            readRemoteCollectionBooksStore(target, owner1).byCollection.keys,
        )
    }

    @Test
    fun pruneKeepsAMergeLosersListingUnderItsSurvivor() {
        // A merged-away box's cached listing still renders under the survivor, so
        // the closure must protect it from eviction.
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook("one")))
        val records = listOf(collection(boxA, mergedInto = boxB), collection(boxB))
        val keep = records.asSequence()
            .filter { !it.deleted && it.mergedInto == null }
            .flatMapTo(mutableSetOf()) { collectionMergeClosure(records, it.id) }

        RemoteCollectionBooks.prune(target, owner1, keep)
        assertEquals(
            setOf(boxA),
            readRemoteCollectionBooksStore(target, owner1).byCollection.keys,
        )
    }

    @Test
    fun aRejectedVersionFailsClosedRatherThanReadingAsEmpty() {
        assertFalse(
            remoteCollectionBooksStoreFromJson(
                """{"version":99,"collections":{}}""",
            ).valid,
        )
        assertFalse(remoteCollectionBooksStoreFromJson("""{"collections":{}}""").valid)
        assertFalse(remoteCollectionBooksStoreFromJson("""{"version":1}""").valid)
        // A store predating the owner stamp must not read as "owned by nobody".
        assertFalse(
            remoteCollectionBooksStoreFromJson("""{"version":1,"collections":{}}""").valid,
        )
    }
}
