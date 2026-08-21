package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class InspectBookLookupTest {
    private fun record(
        id: String,
        collection: String,
        title: String,
        author: String = "",
        year: String = "",
    ) = InspectBookLookupRecord(id, collection, title, author, year)

    @Test
    fun typedLookupNormalizesCasePunctuationWhitespaceAndAccents() {
        val result = InspectBookLookup.byTypedName(
            "  MATÉRIA—medica!  ",
            listOf(record("one", "box-a", "Materia Medica")),
        )

        assertEquals("one", result.bestMatch?.record?.entryId)
        assertEquals(InspectBookMatchKind.EXACT, result.bestMatch?.kind)
        assertFalse(result.ambiguous)
    }

    @Test
    fun exactThenSubstringThenTokenMatchesHaveStablePrecedence() {
        val result = InspectBookLookup.byTypedName(
            "Materia Medica",
            listOf(
                record("tokens", "box-c", "Medica Notes on Materia"),
                record("substring", "box-b", "A New Materia Medica Companion"),
                record("exact", "box-a", "Materia Medica"),
            ),
        )

        assertEquals(listOf("exact", "substring", "tokens"), result.matches.map {
            it.record.entryId
        })
        assertEquals(
            listOf(
                InspectBookMatchKind.EXACT,
                InspectBookMatchKind.SUBSTRING,
                InspectBookMatchKind.TOKENS,
            ),
            result.matches.map(InspectBookLookupMatch::kind),
        )
    }

    @Test
    fun identicalBestTitlesInDifferentCollectionsAreReportedAsAmbiguous() {
        val result = InspectBookLookup.byTypedName(
            "The Herbal",
            listOf(
                record("one", "box-a", "The Herbal"),
                record("two", "box-b", "The Herbal"),
            ),
        )

        assertTrue(result.ambiguous)
        assertEquals(setOf("box-a", "box-b"), result.topCollectionIds)
    }

    @Test
    fun duplicateCapturesInOneCollectionStillIdentifyOnePlace() {
        val result = InspectBookLookup.byTypedName(
            "The Herbal",
            listOf(
                record("one", "box-a", "The Herbal"),
                record("two", "box-a", "The Herbal"),
            ),
        )

        assertFalse(result.ambiguous)
        assertEquals(setOf("box-a"), result.topCollectionIds)
    }

    @Test
    fun blankTypedQueriesAndInvalidRecordsDoNotMatch() {
        val records = listOf(
            record("", "box-a", "Materia Medica"),
            record("one", "", "Materia Medica"),
            record("two", "box-a", ""),
        )

        assertTrue(InspectBookLookup.byTypedName("   ", records).matches.isEmpty())
        assertTrue(InspectBookLookup.byTypedName("Materia", records).matches.isEmpty())
        assertTrue(InspectBookLookup.byTypedName("Materia", records, limit = 0).matches.isEmpty())
    }

    @Test
    fun coverOcrRanksTitlePhraseThenUsesAuthorAndYearEvidence() {
        val result = InspectBookLookup.byCoverOcr(
            """
                THE COMPLETE HERBAL
                Nicholas Culpeper
                London 1653
            """.trimIndent(),
            listOf(
                record("wrong", "box-b", "The Complete Herbal", "Another Author", "1800"),
                record("right", "box-a", "The Complete Herbal", "Nicholas Culpeper", "1653"),
                record("other", "box-c", "Complete Domestic Medicine", "Culpeper", "1653"),
            ),
        )

        assertEquals("right", result.bestMatch?.record?.entryId)
        assertEquals(InspectBookMatchKind.EXACT, result.bestMatch?.kind)
        assertFalse(result.ambiguous)
    }

    @Test
    fun coverOcrCanMatchATitleSplitAcrossLinesByItsTokens() {
        val result = InspectBookLookup.byCoverOcr(
            "A COMPLEAT\nHISTORY OF\nDRUGGS\nMonsieur Pomet",
            listOf(
                record("book", "box-a", "A Compleat History of Druggs", "Monsieur Pomet"),
                record("other", "box-b", "A Complete History of Medicine"),
            ),
        )

        assertEquals("book", result.bestMatch?.record?.entryId)
        assertEquals(InspectBookMatchKind.SUBSTRING, result.bestMatch?.kind)
    }

    @Test
    fun blankAndWeakCoverOcrAreRejected() {
        val records = listOf(record("one", "box-a", "The Herbal"))

        assertTrue(InspectBookLookup.byCoverOcr("", records).matches.isEmpty())
        assertTrue(InspectBookLookup.byCoverOcr("xx", records).matches.isEmpty())
        assertTrue(InspectBookLookup.byCoverOcr("the of and", records).matches.isEmpty())
    }

    @Test
    fun aStrongSingleWordCoverTitleCanMatchAsAPhrase() {
        val result = InspectBookLookup.byCoverOcr(
            "TOBACCO\nE. R. Billings",
            listOf(record("one", "box-a", "Tobacco", "E. R. Billings")),
        )

        assertEquals("one", result.bestMatch?.record?.entryId)
        assertEquals(InspectBookMatchKind.EXACT, result.bestMatch?.kind)
    }

    @Test
    fun duplicateCoverTitlesAcrossBoxesRemainAmbiguous() {
        val result = InspectBookLookup.byCoverOcr(
            "THE HERBAL\nA practical guide",
            listOf(
                record("one", "box-a", "The Herbal"),
                record("two", "box-b", "The Herbal"),
            ),
        )

        assertTrue(result.ambiguous)
        assertEquals(setOf("box-a", "box-b"), result.topCollectionIds)
    }

    @Test
    fun freshCloudOwnsMembershipAndOnlyFillsBlankBibliographyFields() {
        val merged = mergeInspectLookupBookSources(
            entryId = "book",
            existingCollectionId = "box-old",
            existing = InspectLookupBibliography(author = "Local Author"),
            currentDesktop = InspectLookupBibliography(title = "Current Curated Title"),
            freshCloudCollectionId = "box-current",
            freshCloud = InspectLookupBibliography(
                title = "Cloud Title",
                author = "Cloud Author",
                year = "1897",
            ),
        )

        assertEquals("box-current", merged.collectionId)
        assertEquals("Current Curated Title", merged.title)
        assertEquals("Local Author", merged.author)
        assertEquals("1897", merged.year)
    }

    @Test
    fun freshCloudBibliographyMakesAnOtherwiseBlankRecordSearchable() {
        val merged = mergeInspectLookupBookSources(
            entryId = "book",
            existingCollectionId = "box-a",
            existing = InspectLookupBibliography(),
            freshCloudCollectionId = "box-b",
            freshCloud = InspectLookupBibliography(
                title = "The English Physitian",
                author = "Nicholas Culpeper",
                year = "1652",
            ),
        )

        val matches = findInspectBookMatches(listOf(merged), "English Physitian")
        assertEquals("book", matches.single().book.entryId)
        assertEquals("box-b", matches.single().book.collectionId)
    }

    @Test
    fun anUntitledDisplayPlaceholderIsNeverSynthesizedIntoTheLookupRecord() {
        val merged = mergeInspectLookupBookSources(
            entryId = "book",
            existingCollectionId = "box-a",
            existing = InspectLookupBibliography(),
        )

        assertEquals("", merged.title)
        assertTrue(findInspectBookMatches(listOf(merged), "Untitled book").isEmpty())
    }

    @Test
    fun partialCloudFailureOutranksOrdinaryAndAmbiguousMatchStatus() {
        assertEquals(
            InspectLookupStatusKind.MATCHES_CLOUD_UNAVAILABLE,
            inspectLookupStatusKind(
                matchCount = 2,
                ambiguous = true,
                cloudLoading = false,
                cloudFailed = true,
            ),
        )
        assertEquals(
            InspectLookupStatusKind.NO_MATCHES_CLOUD_UNAVAILABLE,
            inspectLookupStatusKind(
                matchCount = 0,
                ambiguous = false,
                cloudLoading = false,
                cloudFailed = true,
            ),
        )
    }

    @Test
    fun staleOwnerCompletionResetsOnlyWhenItsGenerationStillOwnsLoading() {
        assertEquals(
            InspectLookupCloudCompletion.RESET_STALE_OWNER,
            inspectLookupCloudCompletion(
                requestGeneration = 4,
                currentGeneration = 4,
                requestOwner = "owner-a",
                currentOwner = "owner-b",
                signedIn = true,
            ),
        )
        assertEquals(
            InspectLookupCloudCompletion.IGNORE_RETIRED_GENERATION,
            inspectLookupCloudCompletion(
                requestGeneration = 3,
                currentGeneration = 4,
                requestOwner = "owner-a",
                currentOwner = "owner-b",
                signedIn = true,
            ),
        )
        assertEquals(
            InspectLookupCloudCompletion.ACCEPT,
            inspectLookupCloudCompletion(
                requestGeneration = 4,
                currentGeneration = 4,
                requestOwner = "owner-a",
                currentOwner = "owner-a",
                signedIn = true,
            ),
        )
    }
}
