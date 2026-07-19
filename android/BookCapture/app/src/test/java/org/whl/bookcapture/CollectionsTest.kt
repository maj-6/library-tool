package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** Collection bookkeeping and the provenance a book carries away from it. */
class CollectionsTest {

    private fun collection(id: String, name: String, from: String = "") =
        BookCollection(id, name, from)

    // --- field hygiene -------------------------------------------------------

    @Test
    fun fieldsAreTrimmedAndInnerWhitespaceCollapsed() {
        assertEquals("Christopher Office", normalizeCollectionField("  Christopher   Office \n"))
        assertEquals("", normalizeCollectionField("   "))
    }

    @Test
    fun overlongFieldsAreClippedRatherThanRejected() {
        val long = "x".repeat(COLLECTION_FIELD_MAX + 40)
        assertEquals(COLLECTION_FIELD_MAX, normalizeCollectionField(long).length)
    }

    // --- persistence ---------------------------------------------------------

    @Test
    fun collectionsSurviveARoundTrip() {
        val original = listOf(
            collection("a", "Blue crate", "Storage"),
            collection("b", "Shelf 3"),
        )
        assertEquals(original, collectionsFromJson(collectionsToJson(original)))
    }

    @Test
    fun unreadableStorageReadsAsEmptyInsteadOfThrowing() {
        assertEquals(emptyList<BookCollection>(), collectionsFromJson(""))
        assertEquals(emptyList<BookCollection>(), collectionsFromJson("{\"collections\":"))
        assertEquals(emptyList<BookCollection>(), collectionsFromJson("[]"))
    }

    @Test
    fun entriesWithoutAnIdOrNameAreDropped() {
        val text = """
            {"collections":[
              {"id":"a","name":"Keep","from":"Storage"},
              {"id":"","name":"No id"},
              {"id":"c","name":"   "},
              {"id":"a","name":"Duplicate id"}
            ]}
        """.trimIndent()
        val parsed = collectionsFromJson(text)
        assertEquals(listOf(collection("a", "Keep", "Storage")), parsed)
    }

    // --- editing -------------------------------------------------------------

    @Test
    fun addingRequiresANameAndRejectsDuplicates() {
        val existing = listOf(collection("a", "Storage"))
        assertEquals(
            R.string.collections_error_name_required,
            addCollection(existing, "  ", "").error,
        )
        // duplicate detection ignores case and surrounding whitespace
        assertEquals(
            R.string.collections_error_name_taken,
            addCollection(existing, " storage ", "").error,
        )
        val added = addCollection(existing, "Christopher Office", " Christopher Office ", id = "b")
        assertNull(added.error)
        assertEquals(
            listOf(collection("a", "Storage"), collection("b", "Christopher Office", "Christopher Office")),
            added.collections,
        )
    }

    @Test
    fun renamingACollectionToItsOwnNameIsAllowed() {
        val existing = listOf(collection("a", "Storage", "Storage"), collection("b", "Shelf"))
        val same = updateCollection(existing, "a", "storage", "Attic")
        assertNull(same.error)
        assertEquals("storage", same.collections?.first()?.name)
        assertEquals("Attic", same.collections?.first()?.from)
    }

    @Test
    fun renamingOntoAnotherCollectionIsRejected() {
        val existing = listOf(collection("a", "Storage"), collection("b", "Shelf"))
        val rejected = updateCollection(existing, "b", "Storage", "")
        assertEquals(R.string.collections_error_name_taken, rejected.error)
        // A rejection must carry no list: Collections.mutate persists whatever
        // `collections` holds, so a non-null value here would write the edit to
        // disk while telling the user it failed.
        assertNull(rejected.collections)
        assertNull(addCollection(existing, "  ", "").collections)
        assertNull(updateCollection(existing, "gone", "New", "").collections)
    }

    @Test
    fun editingACollectionThatIsGoneReportsRatherThanResurrectingIt() {
        assertEquals(
            R.string.collections_error_missing,
            updateCollection(listOf(collection("a", "Storage")), "zz", "New", "").error,
        )
    }

    @Test
    fun removingIsIdempotent() {
        val existing = listOf(collection("a", "Storage"), collection("b", "Shelf"))
        assertEquals(listOf(collection("b", "Shelf")), removeCollection(existing, "a"))
        assertEquals(existing, removeCollection(existing, "missing"))
    }

    // --- which collection a new book lands in --------------------------------

    @Test
    fun nothingIsSelectedWhenThereAreNoCollections() {
        assertNull(resolveCurrentCollection(emptyList(), null))
        assertNull(resolveCurrentCollection(emptyList(), "a"))
    }

    @Test
    fun aLoneCollectionSelectsItself() {
        val only = collection("a", "Storage")
        assertEquals(only, resolveCurrentCollection(listOf(only), null))
    }

    @Test
    fun severalCollectionsRequireAnExplicitChoice() {
        val all = listOf(collection("a", "Storage"), collection("b", "Shelf"))
        assertNull(resolveCurrentCollection(all, null))
        assertEquals(all[1], resolveCurrentCollection(all, "b"))
        // a pointer at a deleted collection must not silently fall back to another
        assertNull(resolveCurrentCollection(all, "gone"))
    }

    // --- provenance on the wire ----------------------------------------------

    @Test
    fun absentProvenanceAddsNothingToAPayload() {
        val payload = JSONObject().put("id", "x")
        applyProvenance(payload, null)
        assertFalse(payload.has("collection"))
        assertFalse(payload.has("from"))
    }

    @Test
    fun anEmptyOriginIsOmittedRatherThanSentAsBlank() {
        val payload = applyProvenance(
            JSONObject(), CaptureProvenance("a", "Blue crate", ""))
        assertEquals("Blue crate", payload.getJSONObject("collection").getString("name"))
        assertFalse(payload.has("from"))
    }

    @Test
    fun theManifestKeepsTheCollectionIdForALaterMigration() {
        val payload = applyProvenance(
            JSONObject(), CaptureProvenance("a", "Blue crate", "Storage"))
        assertEquals("a", payload.getJSONObject("collection").getString("id"))
        assertEquals("Storage", payload.getString("from"))
    }

    /**
     * The desktop turns unknown `meta` keys into one rendered row each, so the
     * wire form has to stay flat strings — a nested object would show up as raw
     * JSON where a reader expects a place name.
     */
    @Test
    fun theUploadPayloadCarriesFlatStringsNotTheNestedManifestShape() {
        val meta = applyProvenanceToPayload(
            JSONObject().put("title", "Herbarium"),
            CaptureProvenance("a", "Blue crate", "Storage"),
        )
        assertEquals("Blue crate", meta.getString("scan_collection"))
        assertEquals("Storage", meta.getString("scan_from"))
        assertEquals("Herbarium", meta.getString("title"))   // extraction survives
    }

    /**
     * The desktop tells passthrough provenance from extraction output by key
     * name (`PHONE_PROVENANCE_KEYS` in tools/whl_explorer/server.py). An
     * unprefixed `collection` would both collide with a model-extracted field
     * and make a key-less phone's blank capture look extracted, skipping the
     * desktop's own OCR. Guarded by test_phone_capture.py on the other side.
     */
    @Test
    fun provenanceKeysAreNamespacedSoTheDesktopCanTellThemFromExtraction() {
        val meta = applyProvenanceToPayload(
            JSONObject(), CaptureProvenance("a", "Blue crate", "Storage"))
        assertFalse(meta.has("collection"))
        assertFalse(meta.has("from"))
        assertTrue(meta.has("scan_collection"))
        assertTrue(meta.has("scan_from"))
    }

    @Test
    fun theUploadPayloadOmitsAnEmptyOriginAndAbsentProvenance() {
        assertFalse(
            applyProvenanceToPayload(JSONObject(), CaptureProvenance("a", "Blue crate", ""))
                .has("scan_from"),
        )
        val untouched = applyProvenanceToPayload(JSONObject().put("title", "x"), null)
        assertFalse(untouched.has("scan_collection"))
        assertFalse(untouched.has("scan_from"))
    }

    // --- the provenance sidecar ----------------------------------------------

    private fun tempDir(): File = Files.createTempDirectory("collections").toFile()

    @Test
    fun theSidecarRoundTripsThroughDisk() {
        val dir = tempDir()
        val written = CaptureProvenance("a", "Blue crate", "Storage")
        assertTrue(writeProvenance(dir, written))
        assertEquals(written, readProvenance(dir))
    }

    @Test
    fun aSidecarWithoutACollectionIsNotProvenance() {
        val dir = tempDir()
        File(dir, CAPTURE_PROVENANCE_FILE).writeText("""{"from":"Storage"}""")
        assertNull(readProvenance(dir))
        File(dir, CAPTURE_PROVENANCE_FILE).writeText("not json")
        assertNull(readProvenance(dir))
        assertNull(readProvenance(tempDir()))   // no sidecar at all
    }

    @Test
    fun overridingTheOriginRewritesBothTheSidecarAndASealedManifest() {
        val dir = tempDir()
        writeProvenance(dir, CaptureProvenance("a", "Blue crate", "Storage"))
        val manifest = File(dir, "manifest.json")
        manifest.writeText(
            applyProvenance(
                JSONObject().put("id", "e1").put("note", ""),
                CaptureProvenance("a", "Blue crate", "Storage"),
            ).toString(),
        )

        assertTrue(overrideEntryFrom(dir, "  Christopher   Office "))

        assertEquals("Christopher Office", readProvenance(dir)?.from)
        val rewritten = JSONObject(manifest.readText())
        assertEquals("Christopher Office", rewritten.getString("from"))
        assertEquals("Blue crate", rewritten.getJSONObject("collection").getString("name"))
        assertEquals("e1", rewritten.getString("id"))   // unrelated keys survive
    }

    @Test
    fun clearingTheOriginRemovesItFromTheManifestInsteadOfBlankingIt() {
        val dir = tempDir()
        writeProvenance(dir, CaptureProvenance("a", "Blue crate", "Storage"))
        File(dir, "manifest.json").writeText(
            applyProvenance(JSONObject(), CaptureProvenance("a", "Blue crate", "Storage")).toString(),
        )

        assertTrue(overrideEntryFrom(dir, ""))

        assertEquals("", readProvenance(dir)?.from)
        assertFalse(JSONObject(File(dir, "manifest.json").readText()).has("from"))
    }

    @Test
    fun overridingAnUnsealedEntryUpdatesTheSidecarAlone() {
        val dir = tempDir()
        writeProvenance(dir, CaptureProvenance("a", "Blue crate", "Storage"))
        assertTrue(overrideEntryFrom(dir, "Attic"))
        assertEquals("Attic", readProvenance(dir)?.from)
        assertFalse(File(dir, "manifest.json").exists())
    }

    @Test
    fun overridingAnEntryWithNoProvenanceIsRefused() {
        assertFalse(overrideEntryFrom(tempDir(), "Storage"))
    }
}
