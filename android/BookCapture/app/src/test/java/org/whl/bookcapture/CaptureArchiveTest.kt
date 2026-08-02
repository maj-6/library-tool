package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Retention displaces captures; it must never destroy them. These pin the two
 * halves of that: the move out of sent/, and the photo budget that is the only
 * thing allowed to drop bytes — and only ever the photos.
 */
class CaptureArchiveTest {

    @Test
    fun retentionMovesTheCaptureInsteadOfDeletingIt() = withTempDir { root ->
        val sent = File(root, "sent/entry-1").apply { mkdirs() }
        File(sent, "photo_1.jpg").writeText("page")
        File(sent, "manifest.json").writeText("{}")
        val archived = File(root, "archive/entry-1")

        val moved = CaptureMetadataStore.archiveIfNoUnsyncedLocalMutation(
            sent,
            archived,
            CaptureArchive.REASON_RETENTION,
            now = 1_000L,
        )

        assertTrue(moved)
        assertFalse(sent.exists())
        assertEquals("page", File(archived, "photo_1.jpg").readText())
        assertEquals(1_000L, CaptureArchive.readStamp(archived)?.archivedAt)
        assertTrue(CaptureArchive.readStamp(archived)!!.photosRetained)
    }

    @Test
    fun anUnsentReviewEditBlocksTheArchiveMoveJustAsItBlockedDeletion() = withTempDir { root ->
        val sent = File(root, "sent/entry-1").apply { mkdirs() }
        File(sent, "manifest.json").writeText("{}")
        val dirty = editCaptureReview(
            existing = null,
            captureId = "entry-1",
            needsAttention = true,
            needsReview = true,
            reason = "Check the edition",
        )
        assertTrue(CaptureMetadataStore.mutateReview(sent) { dirty })
        val archived = File(root, "archive/entry-1")

        val moved = CaptureMetadataStore.archiveIfNoUnsyncedLocalMutation(
            sent,
            archived,
            CaptureArchive.REASON_RETENTION,
            now = 1_000L,
        )

        assertFalse(moved)
        assertTrue(File(sent, "manifest.json").isFile)
        assertFalse(archived.exists())
    }

    @Test
    fun anOlderArchivedCopyIsNeverReplaced() = withTempDir { root ->
        // The archived copy may still hold photos this one has already shed.
        val archived = File(root, "archive/entry-1").apply { mkdirs() }
        File(archived, "photo_1.jpg").writeText("original page")
        val sent = File(root, "sent/entry-1").apply { mkdirs() }
        File(sent, "manifest.json").writeText("{}")

        val moved = CaptureMetadataStore.archiveIfNoUnsyncedLocalMutation(
            sent,
            archived,
            CaptureArchive.REASON_RETENTION,
            now = 2_000L,
        )

        assertFalse(moved)
        assertEquals("original page", File(archived, "photo_1.jpg").readText())
        assertTrue(sent.isDirectory)
    }

    @Test
    fun recordOnlyKeepsEveryTextualSidecarAndDropsThePhotos() = withTempDir { root ->
        val dir = File(root, "archive/entry-1").apply { mkdirs() }
        File(dir, "photo_1.jpg").writeText("page")
        File(dir, "photo_1.jpg.txt").writeText("OCR text")
        File(dir, "photo_1.jpg.mistral.json").writeText("{\"raw\":1}")
        File(dir, "manifest.json").writeText("{}")
        File(dir, "meta.json").writeText("{\"title\":\"A book\"}")
        File(dir, Entries.PROCESSING_STATE).writeText("{\"status\":\"failed\"}")
        File(dir, "derivative.bin").writeText("binary")
        File(dir, "thumbs").apply { mkdirs() }.let { File(it, "t.jpg").writeText("thumb") }
        CaptureArchive.stamp(dir, CaptureArchive.REASON_RETENTION, 500L, photosRetained = true)

        assertTrue(CaptureArchive.demoteToRecordOnly(dir, now = 900L))

        assertFalse(File(dir, "photo_1.jpg").exists())
        assertFalse(File(dir, "derivative.bin").exists())
        assertFalse(File(dir, "thumbs").exists())
        // Load-bearing: Entries.load() treats a directory with no photos AND no
        // manifest as an empty husk and returns null. Keeping the manifest is
        // what lets a record-only capture still appear in the archive browser.
        assertTrue(File(dir, "manifest.json").isFile)
        assertEquals("OCR text", File(dir, "photo_1.jpg.txt").readText())
        assertEquals("{\"title\":\"A book\"}", File(dir, "meta.json").readText())
        assertTrue(File(dir, Entries.PROCESSING_STATE).isFile)
        assertTrue(File(dir, "photo_1.jpg.mistral.json").isFile)
    }

    @Test
    fun demotionKeepsTheOriginalArchivedAtAndRecordsThatPhotosAreGone() = withTempDir { root ->
        val dir = File(root, "archive/entry-1").apply { mkdirs() }
        File(dir, "manifest.json").writeText("{}")
        File(dir, "photo_1.jpg").writeText("page")
        CaptureArchive.stamp(dir, CaptureArchive.REASON_RETENTION, 500L, photosRetained = true)

        CaptureArchive.demoteToRecordOnly(dir, now = 900L)

        val stamp = CaptureArchive.readStamp(dir)!!
        assertEquals(500L, stamp.archivedAt)
        assertFalse(stamp.photosRetained)
        assertTrue(CaptureArchive.isRecordOnly(dir))
    }

    @Test
    fun theDefaultBudgetKeepsEveryArchivedPhoto() {
        val candidates = (1..50).map {
            CaptureArchive.ArchivePhotoCandidate("entry-$it", it.toLong(), photosRetained = true)
        }

        assertEquals(
            emptyList<String>(),
            CaptureArchive.archivePhotoOverflow(
                candidates,
                CaptureArchive.UNLIMITED_ARCHIVED_PHOTOS,
            ),
        )
    }

    @Test
    fun thePhotoBudgetShedsTheLeastRecentlyArchivedFirst() {
        val candidates = listOf(
            CaptureArchive.ArchivePhotoCandidate("newest", 300L, photosRetained = true),
            CaptureArchive.ArchivePhotoCandidate("middle", 200L, photosRetained = true),
            CaptureArchive.ArchivePhotoCandidate("oldest", 100L, photosRetained = true),
        )

        assertEquals(
            listOf("oldest"),
            CaptureArchive.archivePhotoOverflow(candidates, keepCount = 2),
        )
    }

    @Test
    fun anAlreadyDemotedEntryIsNeitherCountedNorReturnedAgain() {
        val candidates = listOf(
            CaptureArchive.ArchivePhotoCandidate("gone", 400L, photosRetained = false),
            CaptureArchive.ArchivePhotoCandidate("newest", 300L, photosRetained = true),
            CaptureArchive.ArchivePhotoCandidate("oldest", 100L, photosRetained = true),
        )

        // "gone" must not consume one of the two slots, and must not be
        // demoted a second time.
        assertEquals(
            emptyList<String>(),
            CaptureArchive.archivePhotoOverflow(candidates, keepCount = 2),
        )
    }

    @Test
    fun anUnstampedArchiveDirectoryIsTreatedAsOldestRatherThanCrashing() = withTempDir { root ->
        val dir = File(root, "archive/entry-1").apply { mkdirs() }
        File(dir, "manifest.json").writeText("{}")

        assertNull(CaptureArchive.readStamp(dir))
        assertFalse(CaptureArchive.isRecordOnly(dir))
    }

    // --- how an archived capture reads in the browser -------------------------

    @Test
    fun anArchivedRowNamesWhenItLeftTheListAndHowMuchIsStillThere() {
        assertEquals(
            "archived 2026-07-29 · 4 pages · complete · uploaded",
            archiveRowSubtitle(
                archivedOn = "2026-07-29",
                photoCount = 4,
                recordOnly = false,
                statusLabel = "complete · uploaded",
            ),
        )
    }

    @Test
    fun aRecordOnlyRowSaysSoRatherThanShowingZeroPages() {
        // "0 pages" would read as data loss; the photo budget is a deliberate,
        // user-set outcome and must say what it is.
        val text = archiveRowSubtitle(
            archivedOn = "2026-07-29",
            photoCount = 0,
            recordOnly = true,
            statusLabel = "complete · imported",
        )

        assertTrue(text, text.contains("record only"))
        assertFalse(text.contains("0 pages"))
    }

    @Test
    fun aSinglePageIsNotPluralised() {
        assertTrue(
            archiveRowSubtitle("2026-07-29", 1, recordOnly = false, statusLabel = "")
                .contains("1 page"),
        )
    }

    @Test
    fun anUnstampedRowStillReadsAsArchived() {
        assertEquals(
            "archived · 2 pages",
            archiveRowSubtitle("", 2, recordOnly = false, statusLabel = ""),
        )
    }

    private fun withTempDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("whl-capture-archive-").toFile()
        try {
            block(root)
        } finally {
            root.deleteRecursively()
        }
    }
}
