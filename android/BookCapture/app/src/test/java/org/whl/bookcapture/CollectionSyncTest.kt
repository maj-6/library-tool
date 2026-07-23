package org.whl.bookcapture

import androidx.work.ExistingWorkPolicy
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

class CollectionSyncTest {

    private fun row(
        name: String,
        updatedAt: String,
        deleted: Boolean = false,
        from: String = "Storage",
        id: String = "00000000-0000-0000-0000-000000000001",
        mergedInto: String? = null,
        parentId: String? = null,
    ) = BookCollection(
        id = id,
        name = name,
        from = from,
        updatedAt = updatedAt,
        deleted = deleted,
        mergedInto = mergedInto,
        parentId = parentId,
    )

    private fun shadowOf(row: BookCollection) = mapOf(
        row.id to CollectionSyncShadow(collectionContentHash(row), row.updatedAt),
    )

    @Test
    fun oneSidedLocalEditPushesEvenWhenPhoneClockIsBehind() {
        val baseline = row("Blue crate", "2026-07-19T12:00:00Z")
        val local = baseline.copy(name = "Blue crate A", updatedAt = "2026-07-19T11:00:00Z")

        val merge = mergeCollections(
            listOf(local), listOf(baseline), shadowOf(baseline), setOf(local.id),
        )

        assertEquals(listOf(local), merge.collections)
        assertEquals(listOf(local), merge.writes.map { it.row })
        assertEquals(baseline.updatedAt, merge.writes.single().expectedCloudUpdatedAt)
        assertEquals(
            "2026-07-19T12:00:00.001Z",
            collectionPatchTimestamp(baseline.updatedAt),
        )
    }

    @Test
    fun successfulPatchDoesNotPublishAFuturePhoneClock() {
        val baseline = "2026-07-19T12:00:00Z"
        assertEquals(
            "2026-07-19T12:00:00.001Z",
            collectionPatchTimestamp(baseline),
        )
    }

    @Test
    fun oneSidedCloudEditPullsEvenWhenPhoneClockWasAhead() {
        val baseline = row("Blue crate", "2026-07-19T12:00:00Z")
        val local = baseline.copy(updatedAt = "2026-07-20T12:00:00Z")
        val cloud = baseline.copy(name = "Blue crate A", updatedAt = "2026-07-19T13:00:00Z")

        val merge = mergeCollections(listOf(local), listOf(cloud), shadowOf(baseline))

        assertEquals(listOf(cloud), merge.collections)
        assertTrue(merge.writes.isEmpty())
    }

    @Test
    fun deleteBeatsAnOlderConcurrentRename() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        val localDelete = baseline.copy(deleted = true, updatedAt = "2026-07-19T12:00:00Z")
        val cloudRename = baseline.copy(name = "Blue crate A", updatedAt = "2026-07-19T11:00:00Z")

        val merge = mergeCollections(
            listOf(localDelete),
            listOf(cloudRename),
            shadowOf(baseline),
            setOf(localDelete.id),
        )

        assertTrue(merge.collections.single().deleted)
        assertEquals(localDelete, merge.writes.single().row)
    }

    @Test
    fun newerConcurrentRenameBeatsAnOlderDelete() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        val localRename = baseline.copy(name = "Blue crate A", updatedAt = "2026-07-19T13:00:00Z")
        val cloudDelete = baseline.copy(deleted = true, updatedAt = "2026-07-19T12:00:00Z")

        val merge = mergeCollections(
            listOf(localRename),
            listOf(cloudDelete),
            shadowOf(baseline),
            setOf(localRename.id),
        )

        assertFalse(merge.collections.single().deleted)
        assertEquals("Blue crate A", merge.collections.single().name)
        assertEquals(localRename, merge.writes.single().row)
    }

    @Test
    fun newerCloudDeleteWinsAndRemainsALocalTombstone() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        val localRename = baseline.copy(name = "Blue crate A", updatedAt = "2026-07-19T11:00:00Z")
        val cloudDelete = baseline.copy(deleted = true, updatedAt = "2026-07-19T12:00:00Z")

        val merge = mergeCollections(
            listOf(localRename),
            listOf(cloudDelete),
            shadowOf(baseline),
            setOf(localRename.id),
        )

        assertTrue(merge.collections.single().deleted)
        assertTrue(merge.writes.isEmpty())
        assertEquals(collectionContentHash(cloudDelete), merge.shadow[baseline.id]?.hash)
    }

    @Test
    fun newerLocalRevertToBaselineContentStillWinsLww() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        // The phone renamed A -> B -> A while offline. The final content hash
        // matches the baseline, but its revision is newer than the cloud edit.
        val localRevert = baseline.copy(updatedAt = "2026-07-19T13:00:00Z")
        val cloudRename = baseline.copy(
            name = "Blue crate C",
            updatedAt = "2026-07-19T12:00:00Z",
        )

        val merge = mergeCollections(
            listOf(localRevert),
            listOf(cloudRename),
            shadowOf(baseline),
            setOf(localRevert.id),
        )

        assertEquals("Blue crate", merge.collections.single().name)
        assertEquals(localRevert, merge.writes.single().row)
    }

    @Test
    fun dirtyRevertAgainstUnchangedCloudAdvancesItsCausalRevisionOnce() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        // The phone changed A -> B -> A while offline. Equal content must not
        // erase that intent before the cloud revision has advanced, even when
        // the phone clock is behind the server.
        val localRevert = baseline.copy(updatedAt = "2020-07-19T12:00:00Z")
        val unchangedCloud = baseline.copy(updatedAt = "2026-07-19T10:00:00+00:00")

        val first = mergeCollections(
            listOf(localRevert),
            listOf(unchangedCloud),
            shadowOf(baseline),
            setOf(localRevert.id),
        )

        assertEquals(localRevert, first.collections.single())
        assertEquals(localRevert, first.writes.single().row)
        assertEquals(unchangedCloud.updatedAt, first.writes.single().expectedCloudUpdatedAt)
        assertEquals(setOf(localRevert.id), first.dirty)

        val accepted = localRevert.copy(updatedAt = "2026-07-19T10:00:00.001Z")
        val acknowledged = acknowledgeCollectionWrite(
            CollectionStore(first.collections, first.shadow, first.dirty),
            first.writes.single().row,
            accepted,
        )
        val second = mergeCollections(
            acknowledged.collections,
            listOf(accepted),
            acknowledged.shadow,
            acknowledged.dirty,
        )

        assertTrue(second.writes.isEmpty())
        assertTrue(second.dirty.isEmpty())
        assertEquals(accepted, second.collections.single())
    }

    @Test
    fun crashAfterEqualContentPatchDoesNotRepeatItWithAFuturePhoneClock() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        val localRevert = baseline.copy(updatedAt = "2099-07-19T12:00:00Z")
        // The prior CAS succeeded, but the process died before local
        // acknowledgement. Cloud content equals local while its revision has
        // advanced beyond the persisted shadow.
        val accepted = localRevert.copy(updatedAt = "2026-07-19T10:00:00.001Z")

        val recovered = mergeCollections(
            listOf(localRevert),
            listOf(accepted),
            shadowOf(baseline),
            setOf(localRevert.id),
        )

        assertTrue(recovered.writes.isEmpty())
        assertTrue(recovered.dirty.isEmpty())
        assertEquals(accepted, recovered.collections.single())
        assertEquals(accepted.updatedAt, recovered.shadow[accepted.id]?.updatedAt)
    }

    @Test
    fun newerCloudRevertToBaselineContentStillWinsLww() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        val localRename = baseline.copy(
            name = "Blue crate B",
            updatedAt = "2026-07-19T11:00:00Z",
        )
        // The cloud renamed A -> C -> A. Its content hash is back at the
        // baseline, but its revision is the newest concurrent write.
        val cloudRevert = baseline.copy(updatedAt = "2026-07-19T12:00:00Z")

        val merge = mergeCollections(
            listOf(localRename),
            listOf(cloudRevert),
            shadowOf(baseline),
            setOf(localRename.id),
        )

        assertEquals("Blue crate", merge.collections.single().name)
        assertTrue(merge.writes.isEmpty())
        assertEquals(cloudRevert, merge.collections.single())
    }

    @Test
    fun signedOutLegacyRowPushesOnceThenHydratesWithoutChangingIdentity() {
        val local = row("Blue crate", updatedAt = "")
        val first = mergeCollections(listOf(local), emptyList(), emptyMap())
        assertEquals(local.id, first.writes.single().row.id)
        assertEquals(null, first.writes.single().expectedCloudUpdatedAt)

        // This is also the crash-after-POST case: no local shadow was advanced,
        // but the next GET sees the same UUID/content with a server timestamp.
        val inserted = local.copy(updatedAt = "2026-07-19T12:00:00.123456Z")
        val second = mergeCollections(listOf(local), listOf(inserted), emptyMap())
        assertTrue(second.writes.isEmpty())
        assertEquals(local.id, second.collections.single().id)
        assertEquals(inserted.updatedAt, second.collections.single().updatedAt)

        val third = mergeCollections(
            second.collections, listOf(inserted), second.shadow, second.dirty,
        )
        assertTrue(third.writes.isEmpty())
        assertEquals(1, third.collections.size)
    }

    @Test
    fun successfulWriteBecomesBaselineWithoutOverwritingAnEditMadeInFlight() {
        val sent = row("Blue crate", "2099-01-01T00:00:00Z")
        val editedDuringHttp = sent.copy(
            name = "Blue crate A",
            // Deliberately behind the server clock: shadow, not raw time, must
            // establish that this is the one-sided follow-up edit.
            updatedAt = "2020-01-01T00:00:00Z",
        )
        val accepted = sent.copy(updatedAt = "2026-07-19T12:00:00Z")
        val acknowledged = acknowledgeCollectionWrite(
            CollectionStore(listOf(editedDuringHttp), dirty = setOf(sent.id)),
            sent,
            accepted,
        )

        assertEquals(editedDuringHttp.name, acknowledged.collections.single().name)
        assertEquals("2026-07-19T12:00:00.001Z", acknowledged.collections.single().updatedAt)
        val next = mergeCollections(
            acknowledged.collections, listOf(accepted), acknowledged.shadow, acknowledged.dirty,
        )
        assertEquals(editedDuringHttp.name, next.writes.single().row.name)
    }

    @Test
    fun successfulWriteHydratesTimestampWhenLocalContentDidNotMove() {
        val sent = row("Blue crate", "2099-01-01T00:00:00Z")
        val accepted = sent.copy(updatedAt = "2026-07-19T12:00:00Z")
        val acknowledged = acknowledgeCollectionWrite(
            CollectionStore(listOf(sent), dirty = setOf(sent.id)),
            sent,
            accepted,
        )
        assertEquals(accepted, acknowledged.collections.single())
        assertEquals(accepted.updatedAt, acknowledged.shadow[sent.id]?.updatedAt)
        assertTrue(acknowledged.dirty.isEmpty())
    }

    @Test
    fun equalContentEditedAwayAndBackDuringHttpRemainsDirty() {
        val sent = row("Blue crate B", "2026-07-19T11:00:00Z")
        // The UI changed B -> C -> B while the request was in flight. Content
        // equals the sent row again, but the later revision is still intent.
        val revertedDuringHttp = sent.copy(updatedAt = "2026-07-19T13:00:00Z")
        val accepted = sent.copy(updatedAt = "2026-07-19T11:00:00.001Z")

        val acknowledged = acknowledgeCollectionWrite(
            CollectionStore(listOf(revertedDuringHttp), dirty = setOf(sent.id)),
            sent,
            accepted,
        )

        assertEquals("Blue crate B", acknowledged.collections.single().name)
        assertEquals(
            "2026-07-19T11:00:00.002Z",
            acknowledged.collections.single().updatedAt,
        )
        assertEquals(setOf(sent.id), acknowledged.dirty)
        assertEquals(accepted.updatedAt, acknowledged.shadow[sent.id]?.updatedAt)
    }

    @Test
    fun transientTokenRefreshFailureRetriesOnlyForTheSameLiveSession() {
        assertTrue(retryCollectionSyncAfterTokenFailure(true, "user-1", "user-1"))
        assertFalse(retryCollectionSyncAfterTokenFailure(false, "user-1", "user-1"))
        assertFalse(retryCollectionSyncAfterTokenFailure(true, "user-2", "user-1"))
    }

    @Test
    fun untouchedVersionOneInsertOmitsTimestampSoServerDefaultCanHydrate() {
        val body = collectionCloudBody(
            row("Blue crate", updatedAt = ""),
            ownerId = "user-1",
            includeUpdatedAt = false,
        )
        assertFalse(body.has("updated_at"))
        assertEquals("user-1", body.getString("created_by"))
    }

    @Test
    fun firstInsertOmitsEvenAFuturePhoneTimestamp() {
        val future = row("Blue crate", updatedAt = "2099-01-01T00:00:00Z")
        val body = collectionCloudBody(
            future,
            ownerId = "user-1",
            includeUpdatedAt = false,
        )
        assertFalse(body.has("updated_at"))

        val server = future.copy(updatedAt = "2026-07-19T12:00:00Z")
        val hydrated = mergeCollections(listOf(future), listOf(server), emptyMap())
        assertTrue(hydrated.writes.isEmpty())
        assertEquals(server.updatedAt, hydrated.collections.single().updatedAt)
    }

    @Test
    fun parentChangesParticipateInSyncAndUseJsonNullToClear() {
        val baseline = row("Periodicals", "2026-07-19T10:00:00Z")
        val local = baseline.copy(
            parentId = "00000000-0000-0000-0000-000000000002",
            updatedAt = "2026-07-19T11:00:00Z",
        )

        val merge = mergeCollections(
            listOf(local),
            listOf(baseline),
            shadowOf(baseline),
            setOf(local.id),
        )

        assertEquals(local.parentId, merge.writes.single().row.parentId)
        assertEquals(local.parentId, collectionCloudBody(local).getString("parent_id"))
        assertTrue(collectionCloudBody(local.copy(parentId = null)).isNull("parent_id"))
    }

    @Test
    fun tagIdsParticipateInContentHashAlongsideParents() {
        val unchanged = row("Blue crate", "2026-07-19T10:00:00Z")
        val canonical = listOf(
            unchanged.id,
            unchanged.name,
            unchanged.from,
            unchanged.deleted.toString(),
            unchanged.mergedInto.orEmpty(),
            "tag:${unchanged.tagId}",
        ).joinToString("\u0000")
        val digest = MessageDigest.getInstance("SHA-256").digest(canonical.toByteArray())
        val legacyHash = digest.joinToString("") { "%02x".format(it.toInt() and 0xff) }

        assertEquals(legacyHash, collectionContentHash(unchanged))
        assertFalse(
            legacyHash == collectionContentHash(
                unchanged.copy(parentId = "00000000-0000-0000-0000-000000000002"),
            ),
        )
        assertFalse(
            legacyHash == collectionContentHash(unchanged.copy(tagId = "BLUE_CRATE_2")),
        )
    }

    @Test
    fun storeRoundTripsTombstonesAndSyncBaseline() {
        val tombstone = row(
            "Blue crate", "2026-07-19T12:00:00Z", deleted = true,
            mergedInto = "00000000-0000-0000-0000-000000000002",
        )
        val store = CollectionStore(
            listOf(tombstone), shadowOf(tombstone), dirty = setOf(tombstone.id),
        )
        assertEquals(store, collectionStoreFromJson(collectionStoreToJson(store)))
    }

    @Test
    fun cloudRowsMapFromPlaceAndKeepTheServerRevision() {
        val parsed = cloudCollectionFromJson(
            JSONObject()
                .put("id", "a")
                .put("name", "Blue crate")
                .put("from_place", "Storage")
                .put("tag_id", " blue crate 7 ")
                .put("updated_at", "2026-07-19T12:00:00Z")
                .put("deleted", true)
                .put("merged_into", "b")
                .put("parent_id", "parent"),
        )
        assertEquals(
            BookCollection(
                "a",
                "Blue crate",
                "Storage",
                "2026-07-19T12:00:00Z",
                true,
                "b",
                "parent",
                "BLUE_CRATE_7",
            ),
            parsed,
        )
    }

    @Test
    fun humanMergeTombstoneAlwaysBeatsAStaleLocalResurrection() {
        val baseline = row("Blue crate", "2026-07-19T10:00:00Z")
        val staleRename = baseline.copy(
            name = "Blue crate old id",
            updatedAt = "2099-01-01T00:00:00Z",
        )
        val merged = baseline.copy(
            deleted = true,
            updatedAt = "2026-07-19T11:00:00Z",
            mergedInto = "00000000-0000-0000-0000-000000000002",
        )

        val result = mergeCollections(
            listOf(staleRename), listOf(merged), shadowOf(baseline), setOf(baseline.id),
        )

        assertEquals(merged, result.collections.single())
        assertTrue(result.writes.isEmpty())
        assertFalse(baseline.id in result.dirty)
        assertFalse(collectionCloudBody(merged).has("merged_into"))
    }

    @Test
    fun collectionPaginationReadsPastSupabaseDefaultRowCap() {
        val rows = (0 until 1_005).map { index ->
            val id = "00000000-0000-0000-0000-${index.toString().padStart(12, '0')}"
            JSONObject()
                .put("id", id)
                .put("name", "Collection $index")
                .put("from_place", "Storage")
                .put("updated_at", "2026-07-19T12:00:00Z")
                .put("deleted", false)
        }
        var requests = 0

        val loaded = collectCollectionPages { afterId ->
            requests += 1
            val start = afterId?.let { cursor ->
                rows.indexOfFirst { it.getString("id") == cursor } + 1
            } ?: 0
            JSONArray().apply {
                rows.drop(start).take(137).forEach { put(it) }
            }
        }

        assertEquals(1_005, loaded.size)
        assertEquals(1_005, loaded.map { it.id }.distinct().size)
        assertTrue(requests > 8)
    }

    @Test
    fun opportunisticWorkCoalescesButMutationsAppend() {
        assertEquals(ExistingWorkPolicy.KEEP, collectionSyncWorkPolicy(guaranteed = false))
        assertEquals(
            ExistingWorkPolicy.APPEND_OR_REPLACE,
            collectionSyncWorkPolicy(guaranteed = true),
        )
    }

    @Test
    fun uniqueConflictRequiresHumanRetaggingInsteadOfAutomaticRelabeling() {
        val tagConflict = SupabaseClient.HttpException(
            409,
            "conflict",
            """{"code":"23505","message":"duplicate key value violates unique constraint \"collections_tag_id_key\""}""",
        )
        val otherUnique = SupabaseClient.HttpException(
            409,
            "conflict",
            """{"code":"23505","message":"duplicate key value violates unique constraint \"collections_pkey\""}""",
        )
        val permanentlyReserved = SupabaseClient.HttpException(
            409,
            "conflict",
            """{"code":"23505","message":"duplicate key value violates unique constraint \"collection_tag_reservations_pkey\""}""",
        )

        assertTrue(isCollectionTagConflict(tagConflict))
        assertTrue(isCollectionTagConflict(permanentlyReserved))
        assertFalse(isCollectionTagConflict(otherUnique))
        assertFalse(isCollectionTagConflict(SupabaseClient.HttpException(400, "bad request")))
    }

    @Test
    fun duplicateLocalAndCloudTagsDoNotSilentlyRelabelTheCloudCollection() {
        val timestamp = "2026-07-19T12:00:00Z"
        val local = collectionStoreFromJson(
            """{"version":4,"collections":[
                {"id":"a","name":"Local fungi","from":"Storage","tag_id":"FUNGI_1","updated_at":"$timestamp"},
                {"id":"b","name":"Cloud fungi","from":"Storage","tag_id":"FUNGI_1","updated_at":"$timestamp"}
            ]}""",
        )
        val cloud = row(
            name = "Cloud fungi",
            updatedAt = timestamp,
            id = "b",
        ).copy(tagId = "FUNGI_1")

        val merge = mergeCollections(
            local.collections,
            listOf(cloud),
            shadowOf(cloud),
            dirty = setOf("a"),
        )

        assertEquals(listOf("a"), merge.writes.map { it.row.id })
        assertEquals("FUNGI_1", merge.collections.single { it.id == "b" }.tagId)
    }

    @Test
    fun neverSyncedDeletedTagCollisionIsRetiredWithoutACloudWrite() {
        val timestamp = "2026-07-19T12:00:00Z"
        val deletedOffline = row(
            name = "Local fungi",
            updatedAt = timestamp,
            deleted = true,
            id = "a",
        ).copy(tagId = "FUNGI_1")
        val cloudOwner = row(
            name = "Cloud fungi",
            updatedAt = timestamp,
            id = "b",
        ).copy(tagId = "FUNGI_1")

        val merge = mergeCollections(
            local = listOf(deletedOffline),
            cloud = listOf(cloudOwner),
            shadow = emptyMap(),
            dirty = setOf(deletedOffline.id),
        )

        assertEquals(listOf(cloudOwner), merge.collections)
        assertTrue(merge.writes.isEmpty())
        assertFalse(deletedOffline.id in merge.dirty)
    }

    @Test
    fun captureSnapshotDoesNotChangeWhenCollectionIsRenamed() {
        val original = row("Blue crate", "2026-07-19T10:00:00Z")
        val captured = CaptureProvenance(original.id, original.name, original.from)
        val renamed = original.copy(name = "Blue crate A", updatedAt = "2026-07-19T11:00:00Z")

        val payload = applyProvenanceToPayload(JSONObject(), captured)

        assertEquals(original.id, payload.getString("scan_collection_id"))
        assertEquals("Blue crate", payload.getString("scan_collection"))
        assertEquals("Blue crate A", renamed.name)
    }
}
