package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class InspectCollectionSearchTest {
    private fun collection(
        id: String,
        name: String,
        from: String = "",
        tagId: String = id.uppercase(),
        deleted: Boolean = false,
        mergedInto: String? = null,
    ) = BookCollection(
        id = id,
        name = name,
        from = from,
        tagId = tagId,
        deleted = deleted,
        mergedInto = mergedInto,
    )

    @Test
    fun blankQueryReturnsABoundedAlphabeticListOfLiveChoices() {
        val records = listOf(
            collection("z", "Zulu"),
            collection("a", "Alpha"),
            collection("retired", "Aardvark", deleted = true),
            collection("merged", "Abacus", mergedInto = "a"),
            collection("b", "Beta"),
        )

        val choices = matchingInspectCollectionChoices(
            collections = records,
            collectionPaths = mapOf("z" to "Warehouse > Zulu"),
            query = "   ",
            limit = 2,
        )

        assertEquals(listOf("a", "b"), choices.map { it.collectionId })
        assertEquals(listOf("Alpha", "Beta"), choices.map { it.toString() })
        assertTrue(matchingInspectCollectionChoices(records, emptyMap(), "", limit = 0).isEmpty())
    }

    @Test
    fun pathNameTagAndOriginAreSearchableAndEveryTermIsRequired() {
        val records = listOf(
            collection(
                id = "fungi",
                name = "Fungi Cabinet",
                from = "Cold Storage",
                tagId = "FUNGI_BOX_7",
            ),
            collection(
                id = "herbs",
                name = "Herbal Folios",
                from = "Reading Room",
                tagId = "HERB_BOX_2",
            ),
        )
        val paths = mapOf(
            "fungi" to "Salinas Annex > Fungi Cabinet",
            "herbs" to "Main Library > Herbal Folios",
        )

        fun ids(query: String) = matchingInspectCollectionChoices(records, paths, query)
            .map { it.collectionId }

        assertEquals(listOf("fungi"), ids("  SALINAS   cabinet "))
        assertEquals(listOf("fungi"), ids("fungi cabinet"))
        assertEquals(listOf("fungi"), ids("fungi box 7"))
        assertEquals(listOf("fungi"), ids("fungi_box_7"))
        assertEquals(listOf("fungi"), ids("cold storage"))
        assertEquals(listOf("fungi"), ids("storage fungi"))
        assertTrue(ids("fungi missing").isEmpty())
    }

    @Test
    fun unicodeEquivalentTextMatchesButSymbolOnlyQueriesDoNotOpenAnUnrelatedBox() {
        val records = listOf(
            collection("cafe", "Caf\u00e9 Archive", tagId = "BOX_1"),
            collection("istanbul", "\u0130stanbul Shelf", tagId = "BOX_2"),
        )

        fun ids(query: String) = matchingInspectCollectionChoices(records, emptyMap(), query)
            .map { it.collectionId }

        assertEquals(listOf("cafe"), ids("Cafe\u0301"))
        assertEquals(listOf("istanbul"), ids("istanbul"))
        assertTrue(ids("---").isEmpty())
        assertTrue(ids("\ud83d\udce6").isEmpty())
    }

    @Test
    fun exactAndPrefixMatchesRankAheadOfSubstringMatchesDeterministically() {
        val records = listOf(
            collection("substring-z", "Herb Box", tagId = "HERB_CONTAINER"),
            collection("prefix-z", "Box Zulu", tagId = "ZULU_CONTAINER"),
            collection("exact", "Box", tagId = "EXACT_CONTAINER"),
            collection("prefix-a", "Box Alpha", tagId = "ALPHA_CONTAINER"),
            collection("substring-a", "Archive Box", tagId = "ARCHIVE_CONTAINER"),
        )

        val choices = matchingInspectCollectionChoices(records, emptyMap(), "box")

        assertEquals(
            listOf("exact", "prefix-a", "prefix-z", "substring-a", "substring-z"),
            choices.map { it.collectionId },
        )
    }

    @Test
    fun wholeWordRanksAheadOfWordPrefixAndInnerSubstring() {
        val records = listOf(
            collection("inner", "Archive Matchbox"),
            collection("prefix", "Archive Boxcar"),
            collection("word", "Archive Box"),
        )

        assertEquals(
            listOf("word", "prefix", "inner"),
            matchingInspectCollectionChoices(records, emptyMap(), "box")
                .map { it.collectionId },
        )
    }

    @Test
    fun returnedPresentationKeepsStableIdentityAndOriginalFieldValues() {
        val record = collection(
            id = "d9e8b8c5-0fa5-4eca-a6be-463a286c9a69",
            name = "  Drawer Seven  ",
            from = "  West Annex  ",
            tagId = "  DRAWER_7  ",
        )

        val choice = matchingInspectCollectionChoices(
            collections = listOf(record),
            collectionPaths = mapOf(record.id to " Archive > Drawer Seven "),
            query = "drawer 7",
        ).single()

        assertEquals(record.id, choice.collectionId)
        assertEquals("Archive > Drawer Seven", choice.displayPath)
        assertEquals("Drawer Seven", choice.name)
        assertEquals("DRAWER_7", choice.tagId)
        assertEquals("West Annex", choice.origin)
    }
}
