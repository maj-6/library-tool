package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BookDetailPresentationTest {

    @Test
    fun catalogGroupsAreOrderedAndDoNotDuplicateFields() {
        val presented = BookDetailPresenter.from(JSONObject("""
            {
              "title": "The English Physitian",
              "author": "Nicholas Culpeper",
              "year": "1652",
              "publisher": "Peter Cole",
              "language": "English",
              "edition": "First",
              "subtitle": "An astrologo-physical discourse",
              "volume": "2",
              "city": "London",
              "extra": {"shelf_mark": "RCP 42", "description": "A practical herbal."}
            }
        """.trimIndent()))

        assertEquals("The English Physitian", presented.title)
        assertEquals("Nicholas Culpeper", presented.author)
        assertEquals("1652", presented.year)
        assertEquals("Vol. 2", presented.volumeTag)
        assertEquals(
            listOf("Publisher", "Language", "Edition", "Subtitle"),
            presented.secondary.map { it.label },
        )
        assertEquals("A practical herbal.", presented.overview)
        assertEquals(listOf("City", "Shelf Mark"), presented.other.map { it.label })
        assertFalse(presented.other.any { it.label == "Volume" || it.label == "Description" })
    }

    @Test
    fun desktopVolumeNumberAliasUsesTheDesktopPrefix() {
        val presented = BookDetailPresenter.from(JSONObject().put("volume_number", "IV"))

        assertEquals("Vol. IV", presented.volumeTag)
        assertTrue(presented.other.none { it.label == "Volume Number" })
    }

    @Test
    fun topLevelOverviewWinsAndEmptyValuesDisappear() {
        val presented = BookDetailPresenter.from(JSONObject("""
            {"description":"Top level", "publisher":"", "extra":{"description":"Old", "isbn":"123"}}
        """.trimIndent()))

        assertEquals("Top level", presented.overview)
        assertEquals(emptyList<BookDetailField>(), presented.secondary)
        assertEquals(listOf(BookDetailField("ISBN", "123")), presented.other)
    }

    @Test
    fun spineTitleRemainsDistinctFromThePublishedTitle() {
        val presented = BookDetailPresenter.from(JSONObject()
            .put("title", "A Flora of California")
            .put("spine_title", "California Flora"))

        assertEquals("A Flora of California", presented.title)
        assertEquals(
            listOf(BookDetailField("Spine Title", "California Flora")),
            presented.other,
        )
    }

    @Test
    fun equivalentSpineTitleIsSuppressed() {
        val presented = BookDetailPresenter.from(JSONObject()
            .put("title", "A Flora: of California")
            .put("spine_title", "  a flora of california "))

        assertEquals("A Flora: of California", presented.title)
        assertTrue(presented.other.none { it.label == "Spine Title" })
    }

    @Test
    fun spineTitleAliasesAreUsedOnlyWhenTheyAgree() {
        val consistent = BookDetailPresenter.from(JSONObject()
            .put("spineTitle", "  California   Flora ")
            .put("extra", JSONObject().put("spine-title", "california flora")))
        val conflicting = BookDetailPresenter.from(JSONObject()
            .put("spineTitle", "California Flora")
            .put("extra", JSONObject().put("spine-title", "Flora of the West")))

        assertEquals(
            listOf(BookDetailField("Spine Title", "California   Flora")),
            consistent.other,
        )
        assertTrue(conflicting.other.none { it.label == "Spine Title" })
    }

    @Test
    fun internalAndProvenanceKeysNeverBecomeCatalogRows() {
        val presented = BookDetailPresenter.from(JSONObject()
            .put("capture_id", "capture-1")
            .put("photo_assets", "transport")
            .put("_future_internal", "hidden")
            .put("extra", JSONObject().put("binding", "cloth")))

        assertEquals(listOf(BookDetailField("Binding", "cloth")), presented.other)
    }

    @Test
    fun missingMetadataProducesAnEmptyStableProjection() {
        assertEquals(
            BookDetailPresentation("", "", "", "", emptyList(), "", emptyList()),
            BookDetailPresenter.from(null),
        )
    }
}
