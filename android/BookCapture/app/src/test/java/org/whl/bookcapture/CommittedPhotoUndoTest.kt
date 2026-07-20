package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CommittedPhotoUndoTest {

    @Test
    fun finalPageUndoRemovesOwnedArtifactsAndInvalidatesCaptureOutputs() = withTempDir { root ->
        val dir = File(root, "capture-1").apply { mkdirs() }
        val first = committedPhoto(dir, 1, "first page")
        val second = committedPhoto(dir, 2, "second page")
        val before = PhotoAssetStore.read(dir)
        val firstAsset = before.assets.single { it.captureFile == first.name }
        val secondAsset = before.assets.single { it.captureFile == second.name }

        // Exercise a display derivative distinct from both the CameraX path and
        // immutable original, as returned post-processing eventually will be.
        val processed = File(dir, "processed_2.jpg").apply { writeText("cleaned second page") }
        val contractJson = JSONObject(File(dir, PHOTO_ASSETS_FILE).readText())
        val assets = contractJson.getJSONArray("assets")
        for (index in 0 until assets.length()) {
            val asset = assets.getJSONObject(index)
            if (asset.getString("asset_id") == secondAsset.assetId) {
                asset.getJSONObject("display").put("reference", processed.name)
            }
        }
        Entries.atomicWrite(File(dir, PHOTO_ASSETS_FILE), contractJson.toString())
        assertTrue(PhotoAssetStore.selectPrimaryTitle(dir, secondAsset.assetId))
        assertTrue(PhotoAssetStore.selectThumbnail(dir, secondAsset.assetId))

        File(dir, first.name + ".txt").writeText("keep OCR")
        File(dir, second.name + ".txt").writeText("discard OCR")
        File(dir, second.name + Entries.MISTRAL_RESPONSE_SUFFIX).writeText("{\"page\":2}")
        File(dir, "meta.json").writeText("{\"title\":\"stale\"}")
        File(dir, Entries.MISTRAL_EXTRACTION_RESPONSE).writeText("{\"stale\":true}")
        File(dir, Entries.PROCESSING_STATE).writeText("{\"status\":\"complete\"}")
        File(dir, Entries.REPROCESS_PENDING).writeText("")
        File(dir, "reprocess.error").writeText("stale failure")

        val result = PhotoAssetStore.removeFinalCommittedPhoto(dir, 2)

        assertTrue(result is FinalCommittedPhotoRemoval.Removed)
        result as FinalCommittedPhotoRemoval.Removed
        assertEquals(2, result.pageNumber)
        assertEquals(1, result.remainingPhotoCount)
        assertTrue(result.cleanupComplete)

        assertTrue(first.isFile)
        assertTrue(File(dir, firstAsset.original.reference).isFile)
        assertTrue(File(dir, first.name + ".txt").isFile)
        assertFalse(second.exists())
        assertFalse(File(dir, secondAsset.original.reference).exists())
        assertFalse(processed.exists())
        assertFalse(File(dir, second.name + ".txt").exists())
        assertFalse(File(dir, second.name + Entries.MISTRAL_RESPONSE_SUFFIX).exists())
        for (name in listOf(
            "meta.json",
            Entries.MISTRAL_EXTRACTION_RESPONSE,
            Entries.PROCESSING_STATE,
            Entries.REPROCESS_PENDING,
            "reprocess.error",
        )) assertFalse("$name should be invalidated", File(dir, name).exists())

        val after = PhotoAssetStore.read(dir)
        assertEquals(listOf(firstAsset.assetId), after.orderedAssets().map { it.assetId })
        assertNull(after.selections.primaryTitle.assetId)
        assertNull(after.selections.thumbnail.assetId)
        assertFalse(after.selections.primaryTitle.manual)
        assertFalse(after.selections.thumbnail.manual)
        assertTrue(dir.listFiles().orEmpty().none { it.name.startsWith(".undo-photo-") })

        // The dense final slot is immediately reusable; its deterministic
        // asset identity must be rebuilt from the replacement pixels.
        val replacement = committedPhoto(dir, 2, "replacement second page")
        val replaced = PhotoAssetStore.read(dir)
        assertEquals(listOf(1, 2), replaced.orderedAssets().map { it.captureOrder })
        val replacementAsset = replaced.assets.single { it.captureFile == replacement.name }
        assertTrue(File(dir, replacementAsset.original.reference).isFile)
    }

    @Test
    fun removalRefusesAnInteriorOrNonDensePageWithoutChangingFiles() = withTempDir { root ->
        val dir = File(root, "capture-2").apply { mkdirs() }
        val first = committedPhoto(dir, 1, "first")
        val second = committedPhoto(dir, 2, "second")
        val contractBefore = File(dir, PHOTO_ASSETS_FILE).readText()

        assertEquals(
            FinalCommittedPhotoRemoval.NotFinalDensePage,
            PhotoAssetStore.removeFinalCommittedPhoto(dir, 1),
        )
        assertTrue(first.isFile)
        assertTrue(second.isFile)
        assertEquals(contractBefore, File(dir, PHOTO_ASSETS_FILE).readText())

        first.delete()
        assertEquals(
            FinalCommittedPhotoRemoval.NotFinalDensePage,
            PhotoAssetStore.removeFinalCommittedPhoto(dir, 1),
        )
        assertTrue(second.isFile)
    }

    @Test
    fun corruptAssetEvidenceFailsClosedBeforeDeletingThePhoto() = withTempDir { root ->
        val dir = File(root, "capture-3").apply { mkdirs() }
        val photo = committedPhoto(dir, 1, "page")
        File(dir, PHOTO_ASSETS_FILE).writeText("not json")

        assertEquals(
            FinalCommittedPhotoRemoval.InvalidContract,
            PhotoAssetStore.removeFinalCommittedPhoto(dir, 1),
        )
        assertTrue(photo.isFile)
    }

    @Test
    fun captureSessionUndoContractUsesThePerEntryWorkerLock() {
        val source = File("src/main/java/org/whl/bookcapture/CaptureSession.kt").readText()
        assertTrue(source.contains(
            "internal suspend fun discardLastCommittedPhoto(): LastCommittedPhotoUndoResult"
        ))
        assertTrue(source.contains("EntryOperationLocks.withLock(lockedEntryId)"))
        assertTrue(source.contains("ActiveCaptureWrites.hasAny(dir)"))
        assertTrue(source.contains("PhotoAssetStore.removeFinalCommittedPhoto(dir, committed)"))
    }

    private fun committedPhoto(dir: File, pageNumber: Int, content: String): File {
        val photo = File(dir, "photo_$pageNumber.jpg").apply { writeText(content) }
        PhotoAssetStore.registerCapturedPhoto(dir, photo, pageNumber)
        return photo
    }

    private fun withTempDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("whl-committed-photo-undo-").toFile()
        try {
            block(root)
        } finally {
            root.deleteRecursively()
        }
    }
}
