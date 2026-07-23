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

class CaptureBookPreviewTest {

    @Test
    fun openCaptureDoesNotDisplaceLastSubmittedBook() {
        val previous = entry("previous", sealed = true, createdAt = 100)
        val open = entry("open", sealed = false, createdAt = 200)

        assertEquals(previous, selectLastSubmittedEntry(listOf(open, previous)))
        assertNull(selectLastSubmittedEntry(listOf(open)))
    }

    @Test
    fun subsequentSubmittedCaptureReplacesPreviousBook() {
        val previous = entry("previous", sealed = true, createdAt = 100)
        val subsequent = entry("subsequent", sealed = true, createdAt = 200)

        assertEquals(subsequent, selectLastSubmittedEntry(listOf(previous, subsequent)))
    }

    @Test
    fun editNeverFallsBackToAnOlderBookWhenTheLatestWasSent() {
        val olderLocal = entry("older-local", sealed = true, createdAt = 100)
        val latestSent = entry(
            "latest-sent",
            sealed = true,
            createdAt = 300,
            uploaded = true,
        )
        val open = entry("open", sealed = false, createdAt = 400)
        assertNull(selectLastEditableEntry(listOf(olderLocal, latestSent, open)))

        val newestLocal = entry("newest-local", sealed = true, createdAt = 500)
        assertEquals(
            newestLocal,
            selectLastEditableEntry(listOf(olderLocal, latestSent, open, newestLocal)),
        )
    }

    @Test
    fun reopeningKeepsPageEvidenceAndMetadataButInvalidatesSealedExtraction() = withTempDir { dir ->
        File(dir, "photo_1.jpg").writeText("page")
        File(dir, "photo_1.jpg.txt").writeText("ocr")
        File(dir, "manifest.json").writeText("{}")
        File(dir, "meta.json").writeText("{\"title\":\"Kept title\"}")
        File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE).writeText("old extraction")
        File(dir, Entries.PROCESSING_STATE).writeText("old state")

        val result = prepareCaptureForEditing(dir)

        assertTrue(result is CaptureReopenResult.Reopened)
        assertFalse(File(dir, "manifest.json").exists())
        assertEquals("page", File(dir, "photo_1.jpg").readText())
        assertEquals("ocr", File(dir, "photo_1.jpg.txt").readText())
        assertTrue(File(dir, "meta.json").readText().contains("Kept title"))
        assertFalse(File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE).exists())
        assertTrue(File(dir, Entries.PROCESSING_STATE).readText().contains("\"status\":\"waiting\""))
    }

    @Test
    fun deletingAnInteriorThumbnailCompactsPagesAndKeepsStableAssets() = withTempDir { dir ->
        for (page in 1..3) {
            File(dir, "photo_$page.jpg").writeText("photo-$page")
            File(dir, "photo_$page.jpg.txt").writeText("ocr-$page")
            File(dir, "photo_$page.jpg${Entries.MISTRAL_RESPONSE_SUFFIX}")
                .writeText("mistral-$page")
            File(dir, "original-a$page.jpg").writeText("original-$page")
        }
        File(dir, "corrected-a2.jpg").writeText("corrected-2")
        File(dir, "meta.json").writeText("old metadata")
        File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE).writeText("old extraction")
        File(dir, Entries.PROCESSING_STATE).writeText("old processing")
        val contract = CapturePhotoAssets(
            captureId = dir.name,
            assets = listOf(
                asset("a1", 1, "photo_1.jpg", "original-a1.jpg", "photo_1.jpg"),
                asset("a2", 2, "photo_2.jpg", "original-a2.jpg", "corrected-a2.jpg"),
                asset("a3", 3, "photo_3.jpg", "original-a3.jpg", "photo_3.jpg"),
            ),
            selections = CapturePhotoSelections(
                primaryTitle = PhotoSelectionChoice("a3", manual = true, revision = 2),
                thumbnail = PhotoSelectionChoice("a2", manual = true, revision = 4),
            ),
        )
        Entries.atomicWrite(File(dir, PHOTO_ASSETS_FILE), contract.toJson().toString())

        val result = deleteCaptureThumbnail(dir, 2)

        assertTrue(result is CaptureThumbnailDeleteResult.Deleted)
        assertEquals("photo-1", File(dir, "photo_1.jpg").readText())
        assertEquals("photo-3", File(dir, "photo_2.jpg").readText())
        assertFalse(File(dir, "photo_3.jpg").exists())
        assertEquals("ocr-3", File(dir, "photo_2.jpg.txt").readText())
        assertEquals(
            "mistral-3",
            File(dir, "photo_2.jpg${Entries.MISTRAL_RESPONSE_SUFFIX}").readText(),
        )
        assertFalse(File(dir, "original-a2.jpg").exists())
        assertFalse(File(dir, "corrected-a2.jpg").exists())
        assertEquals("original-3", File(dir, "original-a3.jpg").readText())
        assertFalse(File(dir, "meta.json").exists())
        assertFalse(File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE).exists())
        assertFalse(File(dir, Entries.PROCESSING_STATE).exists())

        val updated = capturePhotoAssetsFromJson(
            JSONObject(File(dir, PHOTO_ASSETS_FILE).readText()),
            dir.name,
        )!!
        assertEquals(listOf("a1", "a3"), updated.orderedAssets().map { it.assetId })
        assertEquals(listOf(1, 2), updated.orderedAssets().map { it.captureOrder })
        assertEquals("photo_2.jpg", updated.assets.single { it.assetId == "a3" }.captureFile)
        assertEquals("photo_2.jpg", updated.assets.single { it.assetId == "a3" }.display.reference)
        assertEquals("a3", updated.selections.primaryTitle.assetId)
        assertTrue(updated.selections.primaryTitle.manual)
        assertNull(updated.selections.thumbnail.assetId)
        assertFalse(updated.selections.thumbnail.manual)
    }

    @Test
    fun thumbnailLongPressIsWiredWithoutAConfirmationDialog() {
        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val start = source.indexOf("private fun addThumbnail(")
        val end = source.indexOf("private fun updateThumbnailStripVisibility", start)
        val thumbnailFunctions = source.substring(start, end)

        assertTrue(thumbnailFunctions.contains("iv.setOnLongClickListener"))
        assertTrue(thumbnailFunctions.contains("removeCaptureThumbnail(pageNumber)"))
        assertFalse(thumbnailFunctions.contains("AlertDialog"))
        assertFalse(thumbnailFunctions.contains("setNegativeButton"))
    }

    @Test
    fun committedThumbnailTombstonesAreRetriedWithoutDeletingUnknownRecoveryData() =
        withTempDir { dir ->
            val committed = File(dir, ".delete-photo-1-committed").apply { mkdirs() }
            File(committed, ".committed-delete").writeText("1")
            File(committed, "deleted-original.jpg").writeText("old")
            val unknown = File(dir, ".delete-photo-2-interrupted").apply { mkdirs() }
            File(unknown, "only-copy.jpg").writeText("keep")

            assertTrue(cleanupCommittedThumbnailDeletes(dir))
            assertFalse(committed.exists())
            assertTrue(File(unknown, "only-copy.jpg").isFile)
        }

    @Test
    fun interruptedThumbnailDeleteMidStagingRestoresEveryOriginalPage() = withTempDir { dir ->
        seedThreePages(dir)
        val before = File(dir, PHOTO_ASSETS_FILE).readText()
        val stage = thumbnailDeleteStage(
            dir,
            before,
            moves = listOf("photo_2.jpg" to "0-photo_2.jpg", "photo_3.jpg" to "1-photo_3.jpg"),
            relocations = listOf("1-photo_3.jpg" to "photo_2.jpg"),
        )
        assertTrue(File(dir, "photo_2.jpg").renameTo(File(stage, "0-photo_2.jpg")))

        assertTrue(cleanupCommittedThumbnailDeletes(dir))
        assertEquals("page-2", File(dir, "photo_2.jpg").readText())
        assertEquals("page-3", File(dir, "photo_3.jpg").readText())
        assertEquals(before, File(dir, PHOTO_ASSETS_FILE).readText())
        assertFalse(stage.exists())
    }

    @Test
    fun interruptedThumbnailDeleteMidRelocationRestoresDensePageOrder() = withTempDir { dir ->
        seedThreePages(dir)
        val before = File(dir, PHOTO_ASSETS_FILE).readText()
        val stage = thumbnailDeleteStage(
            dir,
            before,
            moves = listOf("photo_2.jpg" to "0-photo_2.jpg", "photo_3.jpg" to "1-photo_3.jpg"),
            relocations = listOf("1-photo_3.jpg" to "photo_2.jpg"),
        )
        assertTrue(File(dir, "photo_2.jpg").renameTo(File(stage, "0-photo_2.jpg")))
        assertTrue(File(dir, "photo_3.jpg").renameTo(File(stage, "1-photo_3.jpg")))
        assertTrue(File(stage, "1-photo_3.jpg").renameTo(File(dir, "photo_2.jpg")))

        assertTrue(cleanupCommittedThumbnailDeletes(dir))
        assertEquals(listOf("page-1", "page-2", "page-3"), (1..3).map { page ->
            File(dir, "photo_$page.jpg").readText()
        })
        assertFalse(stage.exists())
    }

    @Test
    fun interruptedThumbnailDeleteAfterSidecarPublishRollsBackInsteadOfGuessingCommit() =
        withTempDir { dir ->
            seedThreePages(dir)
            val sidecar = File(dir, PHOTO_ASSETS_FILE)
            val before = sidecar.readText()
            val stage = thumbnailDeleteStage(
                dir,
                before,
                moves = listOf(
                    "photo_2.jpg" to "0-photo_2.jpg",
                    "photo_3.jpg" to "1-photo_3.jpg",
                ),
                relocations = listOf("1-photo_3.jpg" to "photo_2.jpg"),
            )
            assertTrue(File(dir, "photo_2.jpg").renameTo(File(stage, "0-photo_2.jpg")))
            assertTrue(File(dir, "photo_3.jpg").renameTo(File(stage, "1-photo_3.jpg")))
            assertTrue(File(stage, "1-photo_3.jpg").renameTo(File(dir, "photo_2.jpg")))
            Entries.atomicWrite(sidecar, "published-delete-contract")

            assertTrue(cleanupCommittedThumbnailDeletes(dir))
            assertEquals(before, sidecar.readText())
            assertEquals("page-2", File(dir, "photo_2.jpg").readText())
            assertEquals("page-3", File(dir, "photo_3.jpg").readText())
            assertFalse(stage.exists())
        }

    @Test
    fun ambiguousRollbackKeepsBothCopiesAndTheRecoveryJournal() = withTempDir { dir ->
        File(dir, "photo_1.jpg").writeText("live")
        val stage = thumbnailDeleteStage(
            dir,
            sidecarBefore = null,
            moves = listOf("photo_1.jpg" to "0-photo_1.jpg"),
            relocations = emptyList(),
        )
        File(stage, "0-photo_1.jpg").writeText("staged-only-copy")

        assertFalse(cleanupCommittedThumbnailDeletes(dir))
        assertEquals("live", File(dir, "photo_1.jpg").readText())
        assertEquals("staged-only-copy", File(stage, "0-photo_1.jpg").readText())
        assertTrue(File(stage, ".delete-journal.json").isFile)
    }

    @Test
    fun lastBookCardLongPressUsesTheSharedAttentionDialog() {
        val source = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val start = source.indexOf("private fun renderLastCapturedBook(")
        val end = source.indexOf("private fun showCaptureExtras", start)
        val render = source.substring(start, end)

        assertTrue(render.contains("binding.lastBookPreview.setOnLongClickListener(attentionListener)"))
        assertTrue(render.contains("binding.lastBookPrimary.setOnLongClickListener(attentionListener)"))
        assertTrue(render.contains("binding.lastBookExtras.setOnLongClickListener(attentionListener)"))
        assertTrue(render.contains("binding.lastBookAttention.apply"))
        assertTrue(render.contains("binding.lastBookPreview.setOnClickListener(openBook)"))
        assertTrue(render.contains("binding.lastBookPreview.isFocusable = false"))
        assertTrue(render.contains("showEntryAttentionDialog(this, entry.id)"))
        assertTrue(render.contains("refreshLastCapturedBook()"))
    }

    @Test
    fun extrasContainEveryNonPrimaryCatalogFieldAndNoTransportMetadata() {
        val metadata = JSONObject()
            .put("title", "Primary title")
            .put("author", "Primary author")
            .put("publisher", "Root publisher")
            .put("volume", "2")
            .put("scan_collection", "Blue crate")
            .put("_capture_photo_assets", JSONObject().put("version", 1))
            .put("extra", JSONObject()
                .put("binding", "cloth")
                .put("shelf_mark", "  QA 12  ")
                .put("blank", "  ")
                .put("null_value", JSONObject.NULL))

        assertEquals(
            listOf(
                CaptureExtraField("publisher", "Publisher", "Root publisher"),
                CaptureExtraField("volume", "Volume", "2"),
                CaptureExtraField("binding", "Binding", "cloth"),
                CaptureExtraField("shelf_mark", "Shelf Mark", "QA 12"),
            ),
            captureExtraFields(metadata),
        )
    }

    @Test
    fun missingExtrasProduceNoPopupRows() {
        assertEquals(emptyList<CaptureExtraField>(), captureExtraFields(null))
        assertEquals(emptyList<CaptureExtraField>(), captureExtraFields(JSONObject().put("title", "Only")))
    }

    private fun asset(
        id: String,
        order: Int,
        capture: String,
        original: String,
        display: String,
    ) = CapturePhotoAsset(
        assetId = id,
        captureOrder = order,
        captureFile = capture,
        original = PhotoOriginal(original),
        display = PhotoDisplayDerivative(display),
    )

    private fun entry(
        id: String,
        sealed: Boolean,
        createdAt: Long,
        uploaded: Boolean = false,
    ) = Entries.Entry(
        id = id,
        dir = File(id),
        sealed = sealed,
        uploaded = uploaded,
        createdAt = createdAt,
        photoCount = 1,
        meta = null,
        cloudStatus = "",
        processing = Entries.ProcessingState(
            status = Entries.ProcessingStatus.WAITING,
            stage = Entries.ProcessingStage.WAITING,
            retryable = true,
            lastError = "",
            updatedAt = 0,
        ),
        processingRecorded = false,
    )

    private fun seedThreePages(dir: File) {
        for (page in 1..3) File(dir, "photo_$page.jpg").writeText("page-$page")
        File(dir, PHOTO_ASSETS_FILE).writeText("old-photo-assets")
    }

    private fun thumbnailDeleteStage(
        dir: File,
        sidecarBefore: String?,
        moves: List<Pair<String, String>>,
        relocations: List<Pair<String, String>>,
    ): File {
        val stage = File(dir, ".delete-photo-2-crash-test").apply { mkdirs() }
        val journal = JSONObject()
            .put("schema", "org.whl.bookcapture.thumbnail-delete-transaction")
            .put("version", 1)
            .put("capture_id", dir.name)
            .put("page_number", 2)
            .put("sidecar_before", sidecarBefore ?: JSONObject.NULL)
            .put("moves", JSONArray().apply {
                moves.forEach { (source, staged) ->
                    put(JSONObject().put("source", source).put("staged", staged))
                }
            })
            .put("relocations", JSONArray().apply {
                relocations.forEach { (staged, destination) ->
                    put(JSONObject().put("staged", staged).put("destination", destination))
                }
            })
        Entries.atomicWrite(File(stage, ".delete-journal.json"), journal.toString())
        return stage
    }

    private fun withTempDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("capture-preview-").toFile()
        val dir = File(root, "capture-id").apply { mkdirs() }
        try {
            block(dir)
        } finally {
            root.deleteRecursively()
        }
    }
}
