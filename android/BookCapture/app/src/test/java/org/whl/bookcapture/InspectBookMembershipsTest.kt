package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class InspectBookMembershipsTest {

    @Test
    fun absentStoreStartsValidAndMoveCreatesAKeyedVersionedFile() {
        val target = target()

        assertTrue(InspectBookMemberships.read(target).valid)
        assertTrue(InspectBookMemberships.read(target).memberships.isEmpty())
        assertTrue(InspectBookMemberships.move(target, listOf("capture-b", "capture-a"), "box-2"))

        val root = JSONObject(target.readText())
        assertEquals(INSPECT_BOOK_MEMBERSHIPS_VERSION, root.getInt("version"))
        assertEquals(setOf("capture-a", "capture-b"), root.getJSONObject("memberships")
            .keys().asSequence().toSet())
        assertEquals(
            InspectBookMembership("box-2", removed = false),
            InspectBookMemberships.read(target).memberships.getValue("capture-a"),
        )
    }

    @Test
    fun moveAndRemoveAreIdempotentAndMoveRevivesATombstone() {
        val target = target()

        assertTrue(InspectBookMemberships.move(target, listOf("capture-a"), "box-2"))
        val movedText = target.readText()
        assertTrue(InspectBookMemberships.move(target, listOf("capture-a"), "box-2"))
        assertEquals(movedText, target.readText())

        assertTrue(InspectBookMemberships.remove(target, listOf("capture-a")))
        val removed = InspectBookMemberships.read(target).memberships.getValue("capture-a")
        assertEquals("box-2", removed.collectionId)
        assertTrue(removed.removed)
        val removedText = target.readText()
        assertTrue(InspectBookMemberships.remove(target, listOf("capture-a")))
        assertEquals(removedText, target.readText())

        assertTrue(InspectBookMemberships.move(target, listOf("capture-a"), "box-3"))
        assertEquals(
            InspectBookMembership("box-3", removed = false),
            InspectBookMemberships.read(target).memberships.getValue("capture-a"),
        )
    }

    @Test
    fun removingANewCaptureCreatesATombstoneWithoutInventingADestination() {
        val target = target()

        assertTrue(InspectBookMemberships.remove(target, listOf("capture-a")))

        assertEquals(
            InspectBookMembership("", removed = true),
            InspectBookMemberships.read(target).memberships.getValue("capture-a"),
        )
    }

    @Test
    fun setMembershipAtomicallySetsDestinationAndRemovalState() {
        val target = target()
        val owner = "11111111-1111-4111-8111-111111111111"
        assertTrue(InspectBookMemberships.move(target, listOf("capture-a"), "box-old"))
        assertTrue(InspectBookMemberships.markCloud(target, listOf("capture-a"), owner))

        assertTrue(
            InspectBookMemberships.setMembership(
                target,
                listOf("capture-a"),
                "  box-merged  ",
                removed = true,
                cleanupPending = true,
            ),
        )

        assertEquals(
            InspectBookMembership(
                "box-merged",
                removed = true,
                cloudOwnerId = owner,
                cleanupPending = true,
            ),
            InspectBookMemberships.read(target).memberships.getValue("capture-a"),
        )
        assertTrue(InspectBookMemberships.markCleanupComplete(target, listOf("capture-a")))
        assertFalse(
            InspectBookMemberships.read(target)
                .memberships
                .getValue("capture-a")
                .cleanupPending,
        )
    }

    @Test
    fun clearDropsOnlyAcknowledgedRowsAndIsIdempotent() {
        val target = target()
        assertTrue(InspectBookMemberships.move(target, listOf("a", "b", "c"), "box-2"))

        assertTrue(InspectBookMemberships.clear(target, listOf("a", "missing")))
        assertEquals(setOf("b", "c"), InspectBookMemberships.read(target).memberships.keys)
        val once = target.readText()
        assertTrue(InspectBookMemberships.clear(target, listOf("a", "missing")))
        assertEquals(once, target.readText())
    }

    @Test
    fun acknowledgementCannotDropACleanupObligation() {
        val target = target()
        assertTrue(
            InspectBookMemberships.setMembership(
                target,
                listOf("a"),
                "box-2",
                removed = true,
                cleanupPending = true,
            ),
        )

        assertTrue(InspectBookMemberships.clear(target, listOf("a")))
        assertTrue(
            InspectBookMemberships.read(target)
                .memberships
                .getValue("a")
                .cleanupPending,
        )
        assertTrue(InspectBookMemberships.markCleanupComplete(target, listOf("a")))
        assertTrue(InspectBookMemberships.clear(target, listOf("a")))
        assertTrue(InspectBookMemberships.read(target).memberships.isEmpty())
    }

    @Test
    fun compareAndSetNeverOverwritesANewerIntent() {
        val target = target()
        val original = InspectBookMembership("box-2", removed = false)
        assertTrue(InspectBookMemberships.setMembership(
            target,
            listOf("capture-a"),
            original.collectionId,
            original.removed,
        ))
        assertEquals(
            InspectMembershipCompareResult.UPDATED,
            InspectBookMemberships.compareAndSet(
                target,
                "capture-a",
                original,
                InspectBookMembership("box-3", removed = true),
            ),
        )
        assertEquals(
            InspectMembershipCompareResult.CHANGED,
            InspectBookMemberships.compareAndSet(
                target,
                "capture-a",
                original,
                replacement = null,
            ),
        )
        assertEquals(
            InspectBookMembership("box-3", removed = true),
            InspectBookMemberships.read(target).memberships.getValue("capture-a"),
        )
    }

    @Test
    fun compareAndSetCanClearTheExactSnapshotAndFailsClosedOnCorruption() {
        val target = target()
        val expected = InspectBookMembership("box-2", removed = false)
        assertTrue(InspectBookMemberships.move(target, listOf("capture-a"), "box-2"))
        assertEquals(
            InspectMembershipCompareResult.UPDATED,
            InspectBookMemberships.compareAndSet(
                target,
                "capture-a",
                expected,
                replacement = null,
            ),
        )
        assertTrue(InspectBookMemberships.read(target).memberships.isEmpty())

        target.writeText("not json")
        assertEquals(
            InspectMembershipCompareResult.FAILED,
            InspectBookMemberships.compareAndSet(
                target,
                "capture-a",
                expected,
                replacement = null,
            ),
        )
        assertEquals("not json", target.readText())
    }

    @Test
    fun cloudOwnerMarkSurvivesMoveAndRemovalUntilAcknowledged() {
        val target = target()
        val owner = "11111111-1111-4111-8111-111111111111"
        assertTrue(InspectBookMemberships.move(target, listOf("a"), "box-2"))
        assertTrue(InspectBookMemberships.markCloud(target, listOf("a"), owner.uppercase()))
        assertTrue(InspectBookMemberships.move(target, listOf("a"), "box-3"))
        assertTrue(InspectBookMemberships.remove(target, listOf("a")))

        val membership = InspectBookMemberships.read(target).memberships.getValue("a")
        assertEquals(owner, membership.cloudOwnerId)
        assertEquals("box-3", membership.collectionId)
        assertTrue(membership.removed)
    }

    @Test
    fun versionOneMembershipsMigrateWithNoCloudOwner() {
        val parsed = inspectBookMembershipStoreFromJson(
            """{"version":1,"memberships":{"a":{"collection_id":"box","removed":false}}}""",
        )
        assertTrue(parsed.valid)
        assertEquals("", parsed.memberships.getValue("a").cloudOwnerId)
        assertFalse(parsed.memberships.getValue("a").cleanupPending)
    }

    @Test
    fun versionTwoMembershipsMigrateWithNoPendingCleanup() {
        val parsed = inspectBookMembershipStoreFromJson(
            """{"version":2,"memberships":{"a":{
                "collection_id":"box","removed":true,
                "cloud_owner_id":"11111111-1111-4111-8111-111111111111"
            }}}""".trimIndent(),
        )
        assertTrue(parsed.valid)
        assertFalse(parsed.memberships.getValue("a").cleanupPending)
    }

    @Test
    fun corruptOrUnknownStoresFailClosedAndAreNeverOverwritten() {
        val invalidSources = listOf(
            "not json",
            "{}",
            """{"version":4,"memberships":{}}""",
            """{"version":1,"memberships":[]}""",
            """{"version":1,"memberships":{"a":{"collection_id":"box"}}}""",
            """{"version":1,"memberships":{"a":{"collection_id":"","removed":false}}}""",
            """{"version":3,"memberships":{"a":{
                "collection_id":"box","removed":false,"cloud_owner_id":"",
                "cleanup_pending":true
            }}}""".trimIndent(),
        )

        invalidSources.forEach { source ->
            val target = target()
            target.writeText(source)

            assertFalse(InspectBookMemberships.read(target).valid)
            assertFalse(
                InspectBookMemberships.setMembership(
                    target,
                    listOf("a"),
                    "box-2",
                    removed = true,
                ),
            )
            assertFalse(InspectBookMemberships.move(target, listOf("a"), "box-2"))
            assertFalse(InspectBookMemberships.remove(target, listOf("a")))
            assertFalse(InspectBookMemberships.clear(target, listOf("a")))
            assertFalse(InspectBookMemberships.markCleanupComplete(target, listOf("a")))
            assertFalse(
                InspectBookMemberships.markCloud(
                    target,
                    listOf("a"),
                    "11111111-1111-4111-8111-111111111111",
                ),
            )
            assertEquals(source, target.readText())
        }
    }

    @Test
    fun invalidMutationArgumentsNeverCreateOrRewriteAStore() {
        val target = target()

        assertFalse(
            InspectBookMemberships.setMembership(
                target,
                listOf("capture-a"),
                "  ",
                removed = false,
            ),
        )
        assertFalse(
            InspectBookMemberships.setMembership(
                target,
                listOf("capture-a"),
                "box-2",
                removed = false,
                cleanupPending = true,
            ),
        )
        assertFalse(
            InspectBookMemberships.setMembership(
                target,
                listOf("  "),
                "box-2",
                removed = true,
            ),
        )
        assertFalse(InspectBookMemberships.move(target, listOf("capture-a"), "  "))
        assertFalse(InspectBookMemberships.move(target, listOf(""), "box-2"))
        assertFalse(InspectBookMemberships.remove(target, listOf("  ")))
        assertFalse(InspectBookMemberships.clear(target, listOf("")))
        assertFalse(
            InspectBookMemberships.markCloud(
                target,
                listOf("capture-a"),
                "not-an-owner",
            ),
        )
        assertFalse(
            InspectBookMemberships.markCloud(
                target,
                listOf("capture-a"),
                "11111111-1111-4111-8111-111111111111",
            ),
        )
        assertFalse(target.exists())
    }

    @Test
    fun serializationIsDeterministicAndRoundTrips() {
        val store = InspectBookMembershipStore(
            linkedMapOf(
                "z" to InspectBookMembership("box-z", removed = false),
                "a" to InspectBookMembership("", removed = true),
            ),
        )

        val encoded = inspectBookMembershipStoreToJson(store)
        assertEquals(store, inspectBookMembershipStoreFromJson(encoded))
        assertTrue(encoded.indexOf("\"a\"") < encoded.indexOf("\"z\""))
    }

    private fun target(): File = File(
        Files.createTempDirectory("inspect-memberships").toFile(),
        INSPECT_BOOK_MEMBERSHIPS_FILE,
    )
}
