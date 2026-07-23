package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

class CollectionTagIdTest {

    private fun row(
        id: String,
        name: String,
        tagId: String = defaultCollectionTagId(name),
        deleted: Boolean = false,
        mergedInto: String? = null,
    ) = BookCollection(
        id = id,
        name = name,
        from = "Storage",
        deleted = deleted,
        mergedInto = mergedInto,
        tagId = tagId,
    )

    @Test
    fun defaultIsCanonicalCompactAndNumbered() {
        assertEquals("FUNGI_1", defaultCollectionTagId("Fungi"))
        assertEquals("MEDICINAL_PLANTS_1", defaultCollectionTagId("  Medicinal   plants "))
        assertEquals("CAFE_ARCHIVE_1", defaultCollectionTagId("Caf\u00e9 archive"))
        assertEquals("MEDICINE_1", defaultCollectionTagId("M\u00e9dicine"))
        assertEquals("ANGSTROM_1", defaultCollectionTagId("\u00c5ngstr\u00f6m"))
        assertEquals("A_B_1", defaultCollectionTagId("A\u2014B"))
        assertEquals("COLLECTION_1", defaultCollectionTagId("\u836f\u8349"))
        assertTrue(defaultCollectionTagId("A very long collection name ".repeat(4)).length <= 32)
    }

    @Test
    fun canonicalizationIsSharedByEditingAndQrInput() {
        assertEquals("FUNGI_BOX_7", normalizeCollectionTagId("  fungi / box #7  "))
        val added = addCollection(
            emptyList(),
            name = "Fungi",
            from = "Storage",
            id = "uuid-a",
            tagId = " fungi / box #7 ",
        )

        assertNull(added.error)
        assertEquals("FUNGI_BOX_7", requireNotNull(added.collections).single().tagId)
    }

    @Test
    fun suggestionsResolveCollisionsWithoutReusingRetiredLabels() {
        val existing = listOf(
            row("a", "Fungi", "FUNGI_1"),
            row("b", "Old fungi", "FUNGI_2", deleted = true),
            row("c", "Merged fungi", "FUNGI_3", mergedInto = "a"),
        )

        assertEquals("FUNGI_4", suggestCollectionTagId("Fungi", existing))
        assertTrue(collectionTagIdTaken(existing, " fungi-2 "))
        assertEquals(
            "CUSTOM_2",
            resolveCollectionTagId(
                name = "Ignored for an explicit tag",
                collections = listOf(row("x", "Other", "CUSTOM_1")),
                preferredTagId = "custom_1",
            ),
        )
        assertTrue(collectionTagIdsAreUnique(existing))
        assertFalse(
            collectionTagIdsAreUnique(existing + row("new", "New box", "FUNGI_2")),
        )
    }

    @Test
    fun explicitDuplicateIsRejectedButAnOmittedAddTagIsResolved() {
        val existing = listOf(row("a", "Fungi", "FUNGI_1"))
        val duplicate = addCollection(
            existing,
            name = "Mushrooms",
            from = "",
            id = "b",
            tagId = "fungi-1",
        )
        assertEquals(R.string.collections_error_tag_id_taken, duplicate.error)
        assertNull(duplicate.collections)

        val resolved = addCollection(existing, "Fungi field notes", "", id = "b")
        assertNull(resolved.error)
        assertEquals("FUNGI_FIELD_NOTES_1", requireNotNull(resolved.collections).last().tagId)
    }

    @Test
    fun renamePreservesTagUnlessTheCallerEditsIt() {
        val existing = listOf(row("a", "Fungi", "FUNGI_ARCHIVE_7"))

        val renamed = updateCollection(existing, "a", "Mushrooms", "Attic")
        assertEquals("FUNGI_ARCHIVE_7", requireNotNull(renamed.collections).single().tagId)

        val retagged = updateCollection(
            existing,
            "a",
            "Mushrooms",
            "Attic",
            tagId = " mushroom box 2 ",
        )
        assertEquals("MUSHROOM_BOX_2", requireNotNull(retagged.collections).single().tagId)
    }

    @Test
    fun staleEditorPreservesTheTagThatSyncedWhileItWasOpen() {
        val synced = listOf(row("a", "Fungi", "FUNGI_CLOUD_2"))

        val saved = updateCollection(
            synced,
            id = "a",
            name = "Fungi reference",
            from = "Attic",
            tagId = null,
        )

        assertEquals("FUNGI_CLOUD_2", requireNotNull(saved.collections).single().tagId)
    }

    @Test
    fun versionThreeMigrationAssignsStableUniqueTagsAndVersionFourRoundTrips() {
        val legacy = collectionStoreFromJson(
            """{"version":3,"collections":[
                {"id":"a","name":"Fungi","from":"Storage"},
                {"id":"b","name":"Fungi!","from":"Storage"},
                {"id":"c","name":"Mushrooms","from":"Storage"}
            ]}""",
        )

        assertTrue(legacy.valid)
        assertEquals(listOf("FUNGI_1", "FUNGI_2", "MUSHROOMS_1"), legacy.collections.map { it.tagId })
        val encoded = collectionStoreToJson(legacy)
        assertEquals(4, JSONObject(encoded).getInt("version"))
        assertEquals(legacy, collectionStoreFromJson(encoded))
    }

    @Test
    fun legacyTagCollisionsUseUuidOrderNotLocalFileOrder() {
        val legacy = collectionStoreFromJson(
            """{"version":3,"collections":[
                {"id":"00000000-0000-0000-0000-000000000002","name":"Fungi!"},
                {"id":"00000000-0000-0000-0000-000000000001","name":"Fungi"}
            ]}""",
        )

        assertEquals(listOf("FUNGI_2", "FUNGI_1"), legacy.collections.map { it.tagId })
    }

    @Test
    fun versionThreeMigrationRebasesOnlyAnUnchangedLegacySyncShadow() {
        val canonical = listOf("a", "Fungi", "Storage", "false", "")
            .joinToString("\u0000")
        val oldHash = MessageDigest.getInstance("SHA-256")
            .digest(canonical.toByteArray())
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }
        val timestamp = "2026-07-19T12:00:00Z"
        val migrated = collectionStoreFromJson(
            """{"version":3,"collections":[
                {"id":"a","name":"Fungi","from":"Storage","updated_at":"$timestamp"}
            ],"sync_shadow":{"a":{"hash":"$oldHash","updated_at":"$timestamp"}}}""",
        )

        assertTrue(migrated.valid)
        assertEquals("FUNGI_1", migrated.collections.single().tagId)
        assertEquals(
            collectionContentHash(migrated.collections.single()),
            migrated.shadow.getValue("a").hash,
        )

        val genuinelyChanged = collectionStoreFromJson(
            """{"version":3,"collections":[
                {"id":"a","name":"Fungi edited","from":"Storage","updated_at":"$timestamp"}
            ],"sync_shadow":{"a":{"hash":"$oldHash","updated_at":"$timestamp"}},
            "sync_dirty":["a"]}""",
        )
        assertEquals(oldHash, genuinelyChanged.shadow.getValue("a").hash)
        assertEquals(setOf("a"), genuinelyChanged.dirty)
    }

    @Test
    fun versionFourCanonicalizesButPreservesDuplicateTagConflicts() {
        val parsed = collectionStoreFromJson(
            """{"version":4,"collections":[
                {"id":"a","name":"Fungi","tag_id":" fungi-1 "},
                {"id":"b","name":"Mushrooms","tag_id":"FUNGI_1"},
                {"id":"c","name":"Herbs","tag_id":42}
            ]}""",
        )

        assertTrue(parsed.valid)
        assertEquals(listOf("FUNGI_1", "FUNGI_1"), parsed.collections.map { it.tagId })
        assertEquals(listOf("a", "b"), parsed.collections.map { it.id })
        assertNull(findCollectionByTagId(parsed.collections, "FUNGI_1"))
    }

    @Test
    fun qrLookupCanonicalizesAndReturnsOnlyOneLiveMatch() {
        val fungi = row("a", "Fungi", "FUNGI_1")
        assertEquals(fungi, findCollectionByTagId(listOf(fungi), "  fungi-1\n"))
        assertNull(findCollectionByTagId(listOf(fungi), "!!!"))
        val maximumTag = "A".repeat(COLLECTION_TAG_ID_MAX)
        val maximum = row("maximum", "Maximum", maximumTag)
        assertEquals(maximum, findCollectionByTagId(listOf(maximum), maximumTag))
        assertNull(findCollectionByTagId(listOf(maximum), maximumTag + "B"))
        assertNull(findCollectionByTagId(listOf(fungi.copy(deleted = true)), "FUNGI_1"))
        assertNull(
            findCollectionByTagId(
                listOf(fungi, row("b", "Duplicate", "FUNGI_1")),
                "FUNGI_1",
            ),
        )
    }

    @Test
    fun qrLookupFollowsAuthoritativeMergeAliasesButRejectsBrokenChains() {
        val survivor = row("survivor", "Fungi archive", "FUNGI_NEW_1")
        val oldLabel = row(
            "loser",
            "Fungi",
            "FUNGI_1",
            deleted = true,
            mergedInto = survivor.id,
        )
        assertEquals(
            survivor,
            findCollectionByTagId(listOf(oldLabel, survivor), "fungi-1"),
        )

        val chainedSurvivor = survivor.copy(mergedInto = "final", deleted = true)
        val final = row("final", "Final fungi", "FINAL_1")
        assertEquals(
            final,
            findCollectionByTagId(listOf(oldLabel, chainedSurvivor, final), "FUNGI_1"),
        )
        assertNull(findCollectionByTagId(listOf(oldLabel), "FUNGI_1"))

        val cycleA = oldLabel.copy(id = "cycle-a", mergedInto = "cycle-b", tagId = "OLD_1")
        val cycleB = oldLabel.copy(id = "cycle-b", mergedInto = "cycle-a", tagId = "OTHER_1")
        assertNull(findCollectionByTagId(listOf(cycleA, cycleB), "OLD_1"))
    }

    @Test
    fun cloudMappingCarriesTagInBothDirections() {
        val row = row("a", "Fungi", "FUNGI_7")
        val body = collectionCloudBody(row)
        assertEquals("FUNGI_7", body.getString("tag_id"))
        assertEquals(
            "FUNGI_7",
            collectionCloudBody(row.copy(tagId = " fungi-7 ")).getString("tag_id"),
        )

        val parsed = cloudCollectionFromJson(
            JSONObject()
                .put("id", "a")
                .put("name", "Fungi")
                .put("from_place", "Storage")
                .put("tag_id", " fungi 7 "),
        )
        assertEquals("FUNGI_7", parsed?.tagId)

        val legacyCloud = cloudCollectionFromJson(
            JSONObject().put("id", "b").put("name", "Fungi").put("from_place", ""),
        )
        assertEquals("FUNGI_1", legacyCloud?.tagId)
        assertFalse(collectionCloudBody(row).isNull("tag_id"))
    }
}
