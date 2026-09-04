package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.IOException
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
    private val capM = "aaaaaaaa-0000-4000-8000-000000000008"
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
        originalCollectionId: String? = collectionId,
        title: String = "Materia Medica",
        photos: Int = 3,
        createdAt: String = "2026-07-20T11:59:45.048996+00:00",
        owner: String = owner1,
        removed: Boolean = false,
        membershipRevision: Long = 0L,
    ): JSONObject = JSONObject()
        .put("id", id)
        .put("created_by", owner)
        .put("created_at", createdAt)
        .put(CAPTURE_ORIGINAL_COLLECTION_ID_FIELD, originalCollectionId ?: JSONObject.NULL)
        .put(CAPTURE_COLLECTION_ID_FIELD, collectionId ?: JSONObject.NULL)
        .put(CAPTURE_COLLECTION_NAME_FIELD, "Blue crate")
        .put(CAPTURE_COLLECTION_PHOTO_COUNT_FIELD, photos)
        .put(CAPTURE_COLLECTION_REMOVED_FIELD, removed)
        .put(CAPTURE_COLLECTION_REVISION_FIELD, membershipRevision)
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

    private fun remoteBook(
        id: String,
        collectionId: String = boxA,
        digitizationCandidateClassification: Boolean? = null,
        scanPriorityRank: Int? = null,
        scanPriorityAssessment: ScanPriorityAssessment? = null,
        scanPriorityAssessmentKnown: Boolean = false,
        scanPriorityRevision: Long = 0L,
        scanPriorityUpdatedAt: String = "",
    ) =
        RemoteCollectionBook(
            captureId = id,
            originalCollectionId = collectionId,
            collectionId = collectionId,
            removed = false,
            membershipRevision = 0L,
            collectionName = "Blue crate",
            title = "Materia Medica",
            author = "Boerhaave, H.",
            year = "1741",
            photoCount = 3,
            createdAt = 100L,
            digitizationCandidateClassification = digitizationCandidateClassification,
            scanPriorityRank = scanPriorityRank,
            scanPriorityAssessment = scanPriorityAssessment,
            scanPriorityAssessmentKnown = scanPriorityAssessmentKnown,
            scanPriorityRevision = scanPriorityRevision,
            scanPriorityUpdatedAt = scanPriorityUpdatedAt,
        )

    private fun liveEntry(id: String): Entries.Entry = Entries.Entry(
        id = id,
        dir = Files.createTempDirectory("remote-merge-live").toFile(),
        sealed = true,
        uploaded = true,
        createdAt = 100L,
        photoCount = 7,
        meta = JSONObject().put("title", "Local title"),
        cloudStatus = "imported",
        processing = Entries.ProcessingState(
            status = Entries.ProcessingStatus.COMPLETE,
            stage = Entries.ProcessingStage.COMPLETE,
            retryable = false,
            lastError = "",
            updatedAt = 100L,
        ),
        processingRecorded = true,
    )

    private fun mutationRow(
        id: String,
        collectionId: String = boxB,
        removed: Boolean = false,
        revision: Long = 1L,
    ) = JSONObject()
        .put("capture_id", id)
        .put("collection_id", collectionId)
        .put("removed", removed)
        .put("membership_revision", revision)

    // --- capture row parsing ---------------------------------------------------

    @Test
    fun readsProvenanceAndPhotoCountFromACaptureRow() {
        val book = remoteCollectionBookFromCaptureJson(captureRow("abcd1234"))
        assertEquals("abcd1234", book?.captureId)
        assertEquals(boxA, book?.originalCollectionId)
        assertEquals(boxA, book?.collectionId)
        assertFalse(book?.removed ?: true)
        assertEquals(0L, book?.membershipRevision)
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
            .put(CAPTURE_ORIGINAL_COLLECTION_ID_FIELD, boxA)
            .put(CAPTURE_COLLECTION_ID_FIELD, boxA)
            .put(CAPTURE_COLLECTION_PHOTO_COUNT_FIELD, 0)
            .put(CAPTURE_COLLECTION_REMOVED_FIELD, false)
            .put(CAPTURE_COLLECTION_REVISION_FIELD, 0L)
        val book = remoteCollectionBookFromCaptureJson(row)
        assertEquals("", book?.title)
        assertEquals("", book?.collectionName)
        assertEquals(0, book?.photoCount)
        assertEquals(0L, book?.createdAt)
    }

    @Test
    fun preservesOriginalMembershipAndParsesMovedRemovalTombstones() {
        val book = remoteCollectionBookFromCaptureJson(
            captureRow(
                capA,
                collectionId = boxB,
                originalCollectionId = boxA,
                removed = true,
                membershipRevision = 7L,
            ),
        )!!
        assertEquals(boxA, book.originalCollectionId)
        assertEquals(boxB, book.collectionId)
        assertTrue(book.removed)
        assertEquals(7L, book.membershipRevision)
    }

    @Test
    fun rejectsMalformedMembershipState() {
        val missingRemoval = JSONObject(captureRow(capA).toString()).apply {
            remove(CAPTURE_COLLECTION_REMOVED_FIELD)
        }
        val fractionalRevision = JSONObject(captureRow(capA).toString()).apply {
            put(CAPTURE_COLLECTION_REVISION_FIELD, 1.5)
        }
        assertNull(remoteCollectionBookFromCaptureJson(missingRemoval))
        assertNull(remoteCollectionBookFromCaptureJson(fractionalRevision))
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

    @Test
    fun ownerScopedParserRejectsMissingOrForeignOwners() {
        val missingOwner = JSONObject(captureRow(capA).toString()).apply {
            remove("created_by")
        }
        assertNull(
            remoteCollectionBookFromCaptureJson(
                missingOwner,
                owner1,
            ),
        )
        assertNull(remoteCollectionBookFromCaptureJson(captureRow(capA, owner = owner2), owner1))
        assertEquals(capA, remoteCollectionBookFromCaptureJson(captureRow(capA), owner1)?.captureId)
    }

    @Test
    fun cloudCollectionPagesContinueAfterShortPagesUntilAnEmptyPage() {
        val cursors = mutableListOf<String?>()
        val books = collectRemoteCollectionBookPages(
            expectedOwnerId = owner1,
            expectedCollectionIds = setOf(boxA),
        ) { afterId ->
            cursors += afterId
            when (afterId) {
                null -> JSONArray().put(captureRow(capA))
                capA -> JSONArray().put(captureRow(capM))
                capM -> JSONArray().put(captureRow(capZ))
                else -> JSONArray()
            }
        }

        assertEquals(listOf(capA, capM, capZ), books.map { it.captureId })
        assertEquals(listOf(null, capA, capM, capZ), cursors)
    }

    @Test
    fun requestedOriginalBoxReceivesMovedTombstonesForCacheInvalidation() {
        val books = collectRemoteCollectionBookPages(owner1, setOf(boxA)) { afterId ->
            if (afterId != null) JSONArray()
            else JSONArray().put(
                captureRow(
                    capA,
                    collectionId = boxB,
                    originalCollectionId = boxA,
                    removed = true,
                    membershipRevision = 2L,
                ),
            )
        }
        assertEquals(1, books.size)
        assertEquals(boxB, books.single().collectionId)
        assertTrue(books.single().removed)
    }

    @Test
    fun cloudCollectionPagesFailClosedOnForeignOrNonadvancingRows() {
        assertThrows(IOException::class.java) {
            collectRemoteCollectionBookPages(owner1, setOf(boxA)) {
                JSONArray().put(captureRow(capA, owner = owner2))
            }
        }
        assertThrows(IOException::class.java) {
            collectRemoteCollectionBookPages(owner1, setOf(boxA)) { afterId ->
                if (afterId == null) JSONArray().put(captureRow(capA))
                else JSONArray().put(captureRow(capA))
            }
        }
        assertThrows(IOException::class.java) {
            collectRemoteCollectionBookPages(owner1, setOf(boxA)) {
                JSONArray().put(captureRow(capA, collectionId = boxB))
            }
        }
    }

    @Test
    fun boxQueryPinsOwnerProjectionAndStableIdCursor() {
        val first = captureCollectionBooksPath(owner1, setOf(boxB, boxA))
        assertEquals(
            "/rest/v1/capture_collection_inventory?created_by=eq.$owner1" +
                "&or=($CAPTURE_ORIGINAL_COLLECTION_ID_FIELD.in.($boxA,$boxB)," +
                "$CAPTURE_COLLECTION_ID_FIELD.in.($boxA,$boxB))" +
                "&select=id,created_by,created_at" +
                ",$CAPTURE_ORIGINAL_COLLECTION_ID_FIELD" +
                ",$CAPTURE_COLLECTION_ID_FIELD" +
                ",$CAPTURE_COLLECTION_NAME_FIELD" +
                ",title,author,year" +
                ",$CAPTURE_COLLECTION_PHOTO_COUNT_FIELD" +
                ",$CAPTURE_COLLECTION_REMOVED_FIELD" +
                ",$CAPTURE_COLLECTION_REVISION_FIELD" +
                ",$CAPTURE_COLLECTION_TYPE_FIELD" +
                ",$CAPTURE_SCAN_MARKED_FIELD" +
                ",$CAPTURE_SCAN_SOURCE_COLLECTION_ID_FIELD" +
                ",$CAPTURE_SCAN_DESTINATION_COLLECTION_ID_FIELD" +
                ",$CAPTURE_SCAN_REVISION_FIELD" +
                "&order=id.asc&limit=$REMOTE_COLLECTION_BOOKS_PAGE_SIZE",
            first,
        )

        val next = captureCollectionBooksPath(owner1, setOf(boxA), afterId = capA)
        assertEquals(
            "/rest/v1/capture_collection_inventory?created_by=eq.$owner1" +
                "&or=($CAPTURE_ORIGINAL_COLLECTION_ID_FIELD.in.($boxA)," +
                "$CAPTURE_COLLECTION_ID_FIELD.in.($boxA))" +
                "&select=id,created_by,created_at" +
                ",$CAPTURE_ORIGINAL_COLLECTION_ID_FIELD" +
                ",$CAPTURE_COLLECTION_ID_FIELD" +
                ",$CAPTURE_COLLECTION_NAME_FIELD" +
                ",title,author,year" +
                ",$CAPTURE_COLLECTION_PHOTO_COUNT_FIELD" +
                ",$CAPTURE_COLLECTION_REMOVED_FIELD" +
                ",$CAPTURE_COLLECTION_REVISION_FIELD" +
                ",$CAPTURE_COLLECTION_TYPE_FIELD" +
                ",$CAPTURE_SCAN_MARKED_FIELD" +
                ",$CAPTURE_SCAN_SOURCE_COLLECTION_ID_FIELD" +
                ",$CAPTURE_SCAN_DESTINATION_COLLECTION_ID_FIELD" +
                ",$CAPTURE_SCAN_REVISION_FIELD" +
                "&order=id.asc&limit=$REMOTE_COLLECTION_BOOKS_PAGE_SIZE&id=gt.$capA",
            next,
        )
        assertFalse(first.contains("removed=eq.false"))
    }

    @Test
    fun membershipMutationBodyIsBoundedNormalizedAndRpcShaped() {
        val body = captureCollectionMutationBody(
            listOf(capZ.uppercase(), capA, capA),
            boxB.uppercase(),
            removed = true,
        )
        assertEquals(
            listOf(capZ, capA),
            body.getJSONArray("p_capture_ids").let { ids ->
                (0 until ids.length()).map(ids::getString)
            },
        )
        assertEquals(boxB, body.getString("p_collection_id"))
        assertTrue(body.getBoolean("p_removed"))
        assertThrows(IllegalArgumentException::class.java) {
            captureCollectionMutationBody(listOf("not-a-uuid"), boxB, false)
        }
        assertThrows(IllegalArgumentException::class.java) {
            captureCollectionMutationBody(
                (0..CAPTURE_COLLECTION_MUTATION_MAX_IDS).map { index ->
                    "aaaaaaaa-0000-4000-8000-${index.toString().padStart(12, '0')}"
                },
                boxB,
                false,
            )
        }
    }

    @Test
    fun membershipMutationResponseMustRepresentTheCompleteRequestedSet() {
        val expected = setOf(capA, capZ)
        val complete = JSONArray()
            .put(mutationRow(capA, removed = true, revision = 4L))
            .put(mutationRow(capZ, removed = true, revision = 9L))
        assertEquals(
            expected,
            captureCollectionMutationResultFromJson(complete, expected, boxB, true),
        )
        assertNull(
            captureCollectionMutationResultFromJson(
                JSONArray().put(mutationRow(capA, removed = true)),
                expected,
                boxB,
                true,
            ),
        )
        assertNull(
            captureCollectionMutationResultFromJson(
                JSONArray()
                    .put(mutationRow(capA, removed = true))
                    .put(mutationRow(capA, removed = true)),
                expected,
                boxB,
                true,
            ),
        )
        assertNull(
            captureCollectionMutationResultFromJson(
                JSONArray()
                    .put(mutationRow(capA, removed = true))
                    .put(mutationRow(capZ, collectionId = boxC, removed = true)),
                expected,
                boxB,
                true,
            ),
        )
    }

    @Test
    fun cloudFetchTrackerRearmsByOwnerAndRejectsLateResponses() {
        val tracker = RemoteCollectionFetchTracker()
        val original = requireNotNull(tracker.begin(owner1, boxA))
        assertNull(tracker.begin(owner1, boxA))
        assertTrue(tracker.begin(owner2, boxA) != null)

        tracker.rearm(owner1)
        val refreshed = requireNotNull(tracker.begin(owner1, boxA))
        assertFalse(tracker.isCurrent(original))
        assertTrue(tracker.isCurrent(refreshed))
        tracker.finish(original, landed = false)
        assertTrue(tracker.isCurrent(refreshed))
        tracker.finish(refreshed, landed = true)
        assertNull(tracker.begin(owner1, boxA))

        tracker.rearm(owner1)
        val failed = requireNotNull(tracker.begin(owner1, boxA))
        tracker.finish(failed, landed = false)
        assertTrue(tracker.begin(owner1, boxA) != null)
    }

    @Test
    fun staleFetchGenerationCannotReplaceTheCurrentCache() {
        val target = tempFile()
        val tracker = RemoteCollectionFetchTracker()
        val original = requireNotNull(tracker.begin(owner1, boxA))
        assertTrue(
            RemoteCollectionBooks.record(
                target,
                owner1,
                boxA,
                listOf(remoteBook("baseline")),
            ),
        )

        tracker.rearm(owner1)
        val refreshed = requireNotNull(tracker.begin(owner1, boxA))
        assertFalse(
            RemoteCollectionBooks.record(
                target,
                owner1,
                boxA,
                listOf(remoteBook("stale")),
            ) { tracker.isCurrent(original) },
        )
        assertEquals(
            listOf("baseline"),
            readRemoteCollectionBooksStore(target, owner1)
                .byCollection.getValue(boxA).map { it.captureId },
        )
        assertTrue(
            RemoteCollectionBooks.record(
                target,
                owner1,
                boxA,
                listOf(remoteBook("fresh")),
            ) { tracker.isCurrent(refreshed) },
        )
        assertEquals(
            listOf("fresh"),
            readRemoteCollectionBooksStore(target, owner1)
                .byCollection.getValue(boxA).map { it.captureId },
        )
    }

    @Test
    fun cacheCommitDropsOnlyExplicitlyObsoleteCollectionKeys() {
        val target = tempFile()
        assertTrue(
            RemoteCollectionBooks.record(
                target,
                owner1,
                boxA,
                listOf(remoteBook("box-a")),
            ),
        )
        assertTrue(
            RemoteCollectionBooks.record(
                target,
                owner1,
                boxB,
                listOf(remoteBook("box-b", boxB)),
            ),
        )

        assertTrue(
            RemoteCollectionBooks.record(
                target,
                owner1,
                boxC,
                listOf(remoteBook("box-c", boxC)),
                discardCollectionIds = setOf(boxA),
            ),
        )
        val stored = readRemoteCollectionBooksStore(target, owner1)
        assertEquals(setOf(boxB, boxC), stored.byCollection.keys)
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
    fun aLocalRowAlwaysWinsWhileUnknownScanMetadataIsFilledInEitherOrder() {
        val live = liveEntry("shared")
        val local = CollectionInventoryItem(
            summary = remoteBook("shared").let {
                CollectionInventorySummary(
                    entryId = it.captureId,
                    collectionId = it.collectionId,
                    collectionName = it.collectionName,
                    title = "Local title",
                    author = it.author,
                    year = it.year,
                    photoCount = live.photoCount,
                    createdAt = it.createdAt,
                )
            },
            current = live,
        )
        val remote = remoteBook(
            "shared",
            digitizationCandidateClassification = true,
            scanPriorityRank = 2,
        ).toInventoryItem()

        listOf(listOf(local, remote), listOf(remote, local)).forEach { input ->
            val merged = mergeCollectionBookItems(input)
            assertEquals(1, merged.size)
            assertFalse(merged.single().remote)
            assertEquals("Local title", merged.single().summary.title)
            assertEquals(7, merged.single().summary.photoCount)
            assertTrue(merged.single().current === live)
            assertEquals(true, merged.single().summary.digitizationCandidateClassification)
            assertEquals(2, merged.single().summary.scanPriorityRank)
        }
    }

    @Test
    fun explicitLocalClassificationWinsAndOnlyAnUnknownPriorityIsFilled() {
        val remote = remoteBook(
            "shared",
            digitizationCandidateClassification = true,
            scanPriorityRank = 2,
        ).toInventoryItem()
        fun local(candidate: Boolean?, priority: Int?) = CollectionInventoryItem(
            summary = remote.summary.copy(
                title = "Local title",
                digitizationCandidateClassification = candidate,
                scanPriorityRank = priority,
            ),
            current = liveEntry("shared"),
            remote = false,
        )

        val explicitlyCleared = mergeCollectionBookItems(
            listOf(local(candidate = false, priority = null), remote),
        ).single()
        assertEquals(false, explicitlyCleared.summary.digitizationCandidateClassification)
        assertNull(explicitlyCleared.summary.scanPriorityRank)

        val locallyPrioritized = mergeCollectionBookItems(
            listOf(local(candidate = true, priority = 4), remote),
        ).single()
        assertEquals(true, locallyPrioritized.summary.digitizationCandidateClassification)
        assertEquals(4, locallyPrioritized.summary.scanPriorityRank)

        val legacyPriority = mergeCollectionBookItems(
            listOf(local(candidate = true, priority = null), remote),
        ).single()
        assertEquals(true, legacyPriority.summary.digitizationCandidateClassification)
        assertEquals(2, legacyPriority.summary.scanPriorityRank)
    }

    @Test
    fun newerRemoteAssessmentReplacesStaleLocalSnapshotIncludingExplicitClear() {
        fun local(
            priority: ScanPriorityAssessment?,
            revision: Long,
            updatedAt: String = "2026-07-24T00:00:00Z",
        ) = CollectionInventoryItem(
            summary = remoteBook(
                "shared",
                scanPriorityAssessment = priority,
                scanPriorityAssessmentKnown = true,
                scanPriorityRevision = revision,
                scanPriorityUpdatedAt = updatedAt,
            ).toInventoryItem().summary.copy(title = "Local title"),
            current = liveEntry("shared"),
            remote = false,
        )
        val medium = remoteBook(
            "shared",
            scanPriorityAssessment = ScanPriorityAssessment.MEDIUM,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 2L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:01Z",
        ).toInventoryItem()

        val updated = mergeCollectionBookItems(
            listOf(local(ScanPriorityAssessment.HIGH, 1L), medium),
        ).single()
        assertFalse(updated.remote)
        assertEquals("Local title", updated.summary.title)
        assertEquals(ScanPriorityAssessment.MEDIUM, updated.summary.scanPriorityAssessment)
        assertEquals(2L, updated.summary.scanPriorityRevision)

        val clear = remoteBook(
            "shared",
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 3L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:02Z",
        ).toInventoryItem()
        val cleared = mergeCollectionBookItems(
            listOf(local(ScanPriorityAssessment.HIGH, 1L), clear),
        ).single()
        assertNull(cleared.summary.scanPriorityAssessment)
        assertTrue(cleared.summary.scanPriorityAssessmentKnown)
        assertEquals(3L, cleared.summary.scanPriorityRevision)
    }

    @Test
    fun mergeUsesTimestampToRecognizeARecreatedPriorityLedger() {
        val oldLocal = remoteBook(
            "shared",
            scanPriorityAssessment = ScanPriorityAssessment.HIGH,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 9L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
        ).toInventoryItem().copy(current = liveEntry("shared"), remote = false)
        val resetRemote = remoteBook(
            "shared",
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 1L,
            scanPriorityUpdatedAt = "2026-07-24T00:01:00Z",
        ).toInventoryItem()

        val merged = mergeCollectionBookItems(listOf(oldLocal, resetRemote)).single()

        assertFalse(merged.remote)
        assertNull(merged.summary.scanPriorityAssessment)
        assertTrue(merged.summary.scanPriorityAssessmentKnown)
        assertEquals(1L, merged.summary.scanPriorityRevision)
        assertEquals("2026-07-24T00:01:00Z", merged.summary.scanPriorityUpdatedAt)
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
        val item = remoteBook(
            "a",
            digitizationCandidateClassification = true,
            scanPriorityRank = 5,
        ).toInventoryItem()
        assertTrue(item.remote)
        assertNull(item.current)
        assertEquals("a", item.summary.entryId)
        assertEquals(true, item.summary.digitizationCandidateClassification)
        assertEquals(5, item.summary.scanPriorityRank)
    }

    // --- desktop bibliography enrichment ---------------------------------------

    /** The desktop's projection as it arrives from `capture_book_metadata`. */
    private fun desktopMetadata(
        captureId: String,
        title: String? = "Desktop Title",
        author: String? = "D. Desktop",
        year: String? = "1899",
        includeBlock: Boolean = true,
        candidate: Boolean? = null,
        priority: Int? = null,
        assessment: String? = null,
        includeAssessment: Boolean = false,
        revision: Long = 1L,
        updatedAt: String = "2026-07-24T00:00:00+00:00",
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
        candidate?.let { data.put("digitization_candidate", it) }
        priority?.let { data.put("scan_priority_rank", it) }
        if (includeAssessment) data.put("scan_priority", assessment ?: JSONObject.NULL)
        return desktopBookMetadataFromJson(
            JSONObject()
                .put("capture_id", captureId)
                .put("owner_id", owner1)
                .put("book_id", "book-1")
                .put("revision", revision)
                .put("updated_at", updatedAt)
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
    fun enrichmentCarriesExplicitCandidateClassificationAndBoundedPriority() {
        val candidate = enrichRemoteCollectionBooks(
            listOf(remoteBook(capA)),
            mapOf(capA to desktopMetadata(capA, candidate = true, priority = 2)),
        ).single()
        assertEquals(true, candidate.digitizationCandidateClassification)
        assertTrue(candidate.digitizationCandidate)
        assertEquals(2, candidate.scanPriorityRank)

        val cleared = enrichRemoteCollectionBooks(
            listOf(remoteBook(
                capA,
                digitizationCandidateClassification = true,
                scanPriorityRank = 1,
            )),
            mapOf(capA to desktopMetadata(capA, candidate = false, priority = 1)),
        ).single()
        assertEquals(false, cleared.digitizationCandidateClassification)
        assertFalse(cleared.digitizationCandidate)
        assertNull(cleared.scanPriorityRank)
    }

    @Test
    fun enrichmentCarriesCanonicalAssessmentIndependentlyOfCandidate() {
        val assigned = enrichRemoteCollectionBooks(
            listOf(remoteBook(capA)),
            mapOf(capA to desktopMetadata(
                capA,
                candidate = false,
                assessment = "High",
                includeAssessment = true,
                revision = 9L,
            )),
        ).single()
        assertEquals(ScanPriorityAssessment.HIGH, assigned.scanPriorityAssessment)
        assertTrue(assigned.scanPriorityAssessmentKnown)
        assertEquals(9L, assigned.scanPriorityRevision)
        assertEquals("2026-07-24T00:00:00+00:00", assigned.scanPriorityUpdatedAt)
        assertEquals(false, assigned.digitizationCandidateClassification)

        val unassessed = enrichRemoteCollectionBooks(
            listOf(assigned),
            mapOf(capA to desktopMetadata(
                capA,
                includeAssessment = true,
                revision = 10L,
            )),
        ).single()
        assertNull(unassessed.scanPriorityAssessment)
        assertTrue(unassessed.scanPriorityAssessmentKnown)
        assertEquals(10L, unassessed.scanPriorityRevision)
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
        val expected = remoteBook(
            "one",
            digitizationCandidateClassification = true,
            scanPriorityRank = 4,
            scanPriorityAssessment = ScanPriorityAssessment.MEDIUM,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 8L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
        )
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(expected)))
        val store = readRemoteCollectionBooksStore(target, owner1)
        assertTrue(store.valid)
        assertEquals(1, store.byCollection.getValue(boxA).size)
        assertEquals(expected, store.byCollection.getValue(boxA).single())
        val row = JSONObject(target.readText())
            .getJSONObject("collections").getJSONArray(boxA).getJSONObject(0)
        assertEquals(4, row.getInt("scan_priority_rank"))
        assertEquals("Medium", row.getString("scan_priority"))
        assertTrue(row.getBoolean("scan_priority_known"))
        assertEquals(8L, row.getLong("scan_priority_revision"))
        assertEquals("2026-07-24T00:00:00Z", row.getString("scan_priority_updated_at"))
    }

    @Test
    fun failedMetadataLookupRetainsCachedAssessmentAndNewerExplicitClearWins() {
        val target = tempFile()
        val assigned = remoteBook(
            capA,
            scanPriorityAssessment = ScanPriorityAssessment.HIGH,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 5L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
        )
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(assigned)))

        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook(capA))))
        val retained = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.getValue(boxA).single()
        assertEquals(ScanPriorityAssessment.HIGH, retained.scanPriorityAssessment)
        assertTrue(retained.scanPriorityAssessmentKnown)
        assertEquals(5L, retained.scanPriorityRevision)
        assertEquals("2026-07-24T00:00:00Z", retained.scanPriorityUpdatedAt)

        val cleared = remoteBook(
            capA,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 6L,
            scanPriorityUpdatedAt = "2026-07-24T00:01:00Z",
        )
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(cleared)))
        val current = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.getValue(boxA).single()
        assertNull(current.scanPriorityAssessment)
        assertTrue(current.scanPriorityAssessmentKnown)
        assertEquals(6L, current.scanPriorityRevision)
        assertEquals("2026-07-24T00:01:00Z", current.scanPriorityUpdatedAt)
    }

    @Test
    fun newerTimestampAllowsARecreatedLowerRevisionLedgerToClearCachedPriority() {
        val target = tempFile()
        val assigned = remoteBook(
            capA,
            scanPriorityAssessment = ScanPriorityAssessment.HIGH,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 9L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
        )
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(assigned)))

        val recreated = remoteBook(
            capA,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 1L,
            scanPriorityUpdatedAt = "2026-07-24T00:01:00Z",
        )
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(recreated)))

        val current = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.getValue(boxA).single()
        assertNull(current.scanPriorityAssessment)
        assertEquals(1L, current.scanPriorityRevision)
        assertEquals("2026-07-24T00:01:00Z", current.scanPriorityUpdatedAt)
    }

    @Test
    fun cachedCopiesSelectTheNewerLedgerResetBeforeRetainingAfterFailedLookup() {
        val target = tempFile()
        val oldLedger = remoteBook(
            capA,
            collectionId = boxA,
            scanPriorityAssessment = ScanPriorityAssessment.HIGH,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 9L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
        )
        val resetLedger = remoteBook(
            capA,
            collectionId = boxB,
            scanPriorityAssessment = ScanPriorityAssessment.LOW,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 1L,
            scanPriorityUpdatedAt = "2026-07-24T00:01:00Z",
        )
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxA, listOf(oldLedger)))
        assertTrue(RemoteCollectionBooks.record(target, owner1, boxB, listOf(resetLedger)))

        assertTrue(RemoteCollectionBooks.record(
            target,
            owner1,
            boxC,
            listOf(remoteBook(capA, collectionId = boxC)),
        ))
        val retained = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.getValue(boxC).single()
        assertEquals(ScanPriorityAssessment.LOW, retained.scanPriorityAssessment)
        assertEquals(1L, retained.scanPriorityRevision)
        assertEquals("2026-07-24T00:01:00Z", retained.scanPriorityUpdatedAt)
    }

    @Test
    fun cachedCopiesSelectTheNewerLedgerResetRegardlessOfCollectionOrder() {
        val target = tempFile()
        val resetLedger = remoteBook(
            capA,
            collectionId = boxA,
            scanPriorityAssessment = ScanPriorityAssessment.LOW,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 1L,
            scanPriorityUpdatedAt = "2026-07-24T00:01:00Z",
        )
        val oldLedger = remoteBook(
            capA,
            collectionId = boxB,
            scanPriorityAssessment = ScanPriorityAssessment.HIGH,
            scanPriorityAssessmentKnown = true,
            scanPriorityRevision = 9L,
            scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
        )
        assertTrue(saveRemoteCollectionBooksStore(
            target,
            RemoteCollectionBooksStore(
                byCollection = mapOf(
                    boxA to listOf(resetLedger),
                    boxB to listOf(oldLedger),
                ),
                owner = owner1,
            ),
        ))

        assertTrue(RemoteCollectionBooks.record(
            target,
            owner1,
            boxC,
            listOf(remoteBook(capA, collectionId = boxC)),
        ))
        val retained = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.getValue(boxC).single()
        assertEquals(ScanPriorityAssessment.LOW, retained.scanPriorityAssessment)
        assertEquals(1L, retained.scanPriorityRevision)
        assertEquals("2026-07-24T00:01:00Z", retained.scanPriorityUpdatedAt)
    }

    @Test
    fun legacyCachePriorityOnlyFallsBackForAnActualInteger() {
        val encoded = remoteCollectionBooksStoreToJson(
            RemoteCollectionBooksStore(
                byCollection = mapOf(boxA to listOf(remoteBook(
                    capA,
                    digitizationCandidateClassification = true,
                    scanPriorityRank = 2,
                ))),
                owner = owner1,
            ),
        )

        val legacyNumeric = JSONObject(encoded)
        legacyNumeric.getJSONObject("collections").getJSONArray(boxA).getJSONObject(0).apply {
            remove("scan_priority_rank")
            put("scan_priority", 4)
        }
        val migrated = remoteCollectionBooksStoreFromJson(legacyNumeric.toString())
        assertTrue(migrated.valid)
        assertEquals(4, migrated.byCollection.getValue(boxA).single().scanPriorityRank)

        listOf("4", "High", "Medium", "Low", "n/s (no scan)").forEach { label ->
            val textual = JSONObject(encoded)
            textual.getJSONObject("collections").getJSONArray(boxA).getJSONObject(0).apply {
                remove("scan_priority_rank")
                put("scan_priority", label)
            }
            val parsed = remoteCollectionBooksStoreFromJson(textual.toString())
            assertTrue(parsed.valid)
            assertNull(parsed.byCollection.getValue(boxA).single().scanPriorityRank)
        }

        val both = JSONObject(encoded)
        both.getJSONObject("collections").getJSONArray(boxA).getJSONObject(0)
            .put("scan_priority", 5)
        assertEquals(
            2,
            remoteCollectionBooksStoreFromJson(both.toString())
                .byCollection.getValue(boxA).single().scanPriorityRank,
        )
    }

    @Test
    fun versionOneCacheMigratesToUnmodifiedOriginalMembership() {
        val legacy = JSONObject()
            .put("version", 1)
            .put("owner", owner1)
            .put(
                "collections",
                JSONObject().put(
                    boxA,
                    JSONArray().put(
                        JSONObject()
                            .put("capture_id", capA)
                            .put("collection_name", "Blue crate")
                            .put("title", "Materia Medica")
                            .put("author", "Boerhaave, H.")
                            .put("year", "1741")
                            .put("photo_count", 3)
                            .put("created_at", 100L),
                    ),
                ),
            )
        val book = remoteCollectionBooksStoreFromJson(legacy.toString())
            .byCollection.getValue(boxA).single()
        assertEquals(boxA, book.originalCollectionId)
        assertEquals(boxA, book.collectionId)
        assertFalse(book.removed)
        assertEquals(0L, book.membershipRevision)
        assertNull(book.digitizationCandidateClassification)
        assertNull(book.scanPriorityRank)
    }

    @Test
    fun versionTwoCacheRemainsReadableWithUnknownScanClassification() {
        val root = JSONObject(remoteCollectionBooksStoreToJson(
            RemoteCollectionBooksStore(
                byCollection = mapOf(boxA to listOf(remoteBook(capA))),
                owner = owner1,
            ),
        )).put("version", 2)
        val row = root.getJSONObject("collections").getJSONArray(boxA).getJSONObject(0)
        row.remove("digitization_candidate")
        row.remove("scan_priority_rank")

        val parsed = remoteCollectionBooksStoreFromJson(root.toString())
        val book = parsed.byCollection.getValue(boxA).single()

        assertTrue(parsed.valid)
        assertNull(book.digitizationCandidateClassification)
        assertFalse(book.digitizationCandidate)
        assertNull(book.scanPriorityRank)
    }

    @Test
    fun versionFivePriorityCacheMigratesWithAnUnknownTimestamp() {
        val root = JSONObject(remoteCollectionBooksStoreToJson(
            RemoteCollectionBooksStore(
                byCollection = mapOf(boxA to listOf(remoteBook(
                    capA,
                    scanPriorityAssessment = ScanPriorityAssessment.MEDIUM,
                    scanPriorityAssessmentKnown = true,
                    scanPriorityRevision = 7L,
                    scanPriorityUpdatedAt = "2026-07-24T00:00:00Z",
                ))),
                owner = owner1,
            ),
        )).put("version", 5)
        root.getJSONObject("collections").getJSONArray(boxA).getJSONObject(0)
            .remove("scan_priority_updated_at")

        val parsed = remoteCollectionBooksStoreFromJson(root.toString())
        val book = parsed.byCollection.getValue(boxA).single()

        assertTrue(parsed.valid)
        assertEquals(ScanPriorityAssessment.MEDIUM, book.scanPriorityAssessment)
        assertEquals(7L, book.scanPriorityRevision)
        assertEquals("", book.scanPriorityUpdatedAt)
    }

    @Test
    fun currentCacheRejectsMalformedTypesRangesAndOrphanedPriorities() {
        val valid = remoteCollectionBooksStoreToJson(
            RemoteCollectionBooksStore(
                byCollection = mapOf(boxA to listOf(remoteBook(
                    capA,
                    digitizationCandidateClassification = true,
                    scanPriorityRank = 1,
                ))),
                owner = owner1,
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
            val row = root.getJSONObject("collections").getJSONArray(boxA).getJSONObject(0)
            row.put("digitization_candidate", candidate)
            row.put("scan_priority_rank", priority)
            assertFalse(remoteCollectionBooksStoreFromJson(root.toString()).valid)
        }
        assertFalse(remoteCollectionBooksStoreFromJson(
            valid.replace("\"scan_priority_rank\":1", "\"scan_priority_rank\":1.0"),
        ).valid)

        listOf(
            remoteBook(
                "not-candidate",
                digitizationCandidateClassification = false,
                scanPriorityRank = 1,
            ),
            remoteBook(
                "orphaned",
                digitizationCandidateClassification = null,
                scanPriorityRank = 1,
            ),
            remoteBook(
                "out-of-range",
                digitizationCandidateClassification = true,
                scanPriorityRank = 6,
            ),
        ).forEach { invalid ->
            assertThrows(IllegalArgumentException::class.java) {
                remoteCollectionBooksStoreToJson(
                    RemoteCollectionBooksStore(
                        byCollection = mapOf(boxA to listOf(invalid)),
                        owner = owner1,
                    ),
                )
            }
        }
    }

    @Test
    fun acceptedMembershipMutationUpdatesEveryCachedOccurrenceMonotonically() {
        val target = tempFile()
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(remoteBook(capA)))
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(remoteBook(capA)))

        assertTrue(
            RemoteCollectionBooks.applyMembershipMutation(
                target,
                owner1,
                setOf(capA),
                boxC,
                "Green crate",
                removed = false,
            ),
        )
        var copies = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten()
        assertEquals(2, copies.size)
        assertTrue(copies.all { it.originalCollectionId == boxA })
        assertTrue(copies.all { it.collectionId == boxC && !it.removed })
        assertTrue(copies.all { it.membershipRevision == 1L })

        // An idempotent retry mirrors the RPC and does not manufacture a newer
        // revision than the server returned for the unchanged state.
        RemoteCollectionBooks.applyMembershipMutation(
            target, owner1, setOf(capA), boxC, "Green crate", removed = false,
        )
        copies = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten()
        assertTrue(copies.all { it.membershipRevision == 1L })

        RemoteCollectionBooks.applyMembershipMutation(
            target, owner1, setOf(capA), boxC, "Green crate", removed = true,
        )
        copies = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten()
        assertTrue(copies.all { it.removed && it.membershipRevision == 2L })
    }

    @Test
    fun authoritativeEmptyEffectiveBoxSuppressesEveryStaleCopyUntilNewerStateArrives() {
        val target = tempFile()
        val movedToB = remoteBook(capA, boxA).copy(
            collectionId = boxB,
            membershipRevision = 1L,
        )
        RemoteCollectionBooks.record(target, owner1, boxA, listOf(movedToB))
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(movedToB))

        val suppressed = RemoteCollectionBooks.recordAuthoritative(
            target = target,
            owner = owner1,
            collectionId = boxB,
            books = emptyList(),
            queriedCollectionIds = setOf(boxB),
        )

        assertEquals(setOf(capA), suppressed?.acknowledgedCaptureIds)
        var copies = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten().filter { it.captureId == capA }
        assertTrue(copies.isNotEmpty())
        assertTrue(copies.all { it.removed && it.membershipRevision == 1L })

        // A second empty refresh must retain the synthetic suppression row.
        RemoteCollectionBooks.recordAuthoritative(
            target = target,
            owner = owner1,
            collectionId = boxB,
            books = emptyList(),
            queriedCollectionIds = setOf(boxB),
        )
        copies = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten().filter { it.captureId == capA }
        assertTrue(copies.isNotEmpty())
        assertTrue(copies.all { it.removed && it.membershipRevision == 1L })

        val movedToC = movedToB.copy(
            collectionId = boxC,
            removed = false,
            membershipRevision = 2L,
        )
        RemoteCollectionBooks.recordAuthoritative(
            target = target,
            owner = owner1,
            collectionId = boxA,
            books = listOf(movedToC),
            queriedCollectionIds = setOf(boxA),
        )
        copies = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten().filter { it.captureId == capA }
        assertEquals(boxC, copies.maxBy { it.membershipRevision }.collectionId)
        assertFalse(copies.maxBy { it.membershipRevision }.removed)
    }

    @Test
    fun sourceRefreshKeepsALocallyAcceptedMoveThatAlreadyLeftItsQueryClosure() {
        val target = tempFile()
        val originallyA = remoteBook(capA, boxB).copy(
            originalCollectionId = boxA,
            collectionId = boxB,
            membershipRevision = 1L,
        )
        RemoteCollectionBooks.record(target, owner1, boxB, listOf(originallyA))
        assertTrue(
            RemoteCollectionBooks.applyMembershipMutation(
                target = target,
                owner = owner1,
                ids = setOf(capA),
                collectionId = boxC,
                collectionName = "Box C",
                removed = false,
            ),
        )

        RemoteCollectionBooks.recordAuthoritative(
            target = target,
            owner = owner1,
            collectionId = boxB,
            books = emptyList(),
            queriedCollectionIds = setOf(boxB),
        )

        val retained = readRemoteCollectionBooksStore(target, owner1)
            .byCollection.values.flatten().single { it.captureId == capA }
        assertEquals(boxC, retained.collectionId)
        assertFalse(retained.removed)
        assertEquals(2L, retained.membershipRevision)
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

    @Test
    fun inspectCloudCacheCommitIsPinnedToTheRequestOwnerAndFetchGeneration() {
        val source = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
            .substringAfter("private fun ensureRemoteBoxListing")

        assertTrue(source.contains("remoteBoxFetches.begin(owner, collectionId)"))
        assertTrue(source.contains("commitIf = {"))
        assertTrue(source.contains("remoteBoxFetches.isCurrent(ticket)"))
        assertTrue(source.contains("Prefs.userId(this@HomeActivity) == owner"))
        assertTrue(source.contains("discardCollectionIds = records.asSequence()"))
        assertTrue(source.contains("owner = owner"))
    }
}
