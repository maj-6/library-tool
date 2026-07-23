package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class UploadStateTest {

    @Test
    fun zeroPhotoEntryIsRejectedAndKeptRecoverable() = withEntry { dir ->
        val problem = assertThrows(UploadEntryProblem::class.java) {
            validateUploadPhotos(dir, emptyList())
        }

        assertTrue(problem.message.orEmpty().contains("no photos"))
        assertTrue(problem.message.orEmpty().contains("kept pending"))
        assertTrue(dir.isDirectory)
    }

    @Test
    fun oneMissingOrCorruptPhotoRejectsTheWholeEntry() = withEntry { dir ->
        jpeg(File(dir, "photo_1.jpg"))
        File(dir, "photo_2.jpg").writeBytes(byteArrayOf(
            0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xd9.toByte()))

        val problem = assertThrows(UploadEntryProblem::class.java) {
            validateUploadPhotos(
                dir,
                listOf("photo_1.jpg", "photo_2.jpg", "photo_3.jpg"),
            )
        }

        assertTrue(problem.message.orEmpty().contains("photo_2.jpg (corrupt)"))
        assertTrue(problem.message.orEmpty().contains("photo_3.jpg (missing)"))
        assertTrue(File(dir, "photo_1.jpg").isFile)
        assertTrue(File(dir, "photo_2.jpg").isFile)
    }

    @Test
    fun unlistedPhotoPreventsASilentPartialDelivery() = withEntry { dir ->
        jpeg(File(dir, "photo_1.jpg"))
        jpeg(File(dir, "photo_2.jpg"))

        val problem = assertThrows(UploadEntryProblem::class.java) {
            validateUploadPhotos(dir, listOf("photo_1.jpg"))
        }

        assertTrue(problem.message.orEmpty().contains("photo_2.jpg (not listed)"))
        assertTrue(dir.isDirectory)
    }

    @Test
    fun versionedDeliveryUsesTheVerifiedCameraOriginalUnderTheLogicalPageName() =
        withEntry { dir ->
            val display = File(dir, "photo_1.jpg").also { jpeg(it, entropy = 1) }
            val original = File(dir, "original_asset-1.jpg").also { jpeg(it, entropy = 2) }
            writePhotoContract(dir, display, original, sha256(original))

            val selected = selectTransportOriginals(
                dir,
                validateUploadPhotos(dir, listOf(display.name)),
            ).single()

            assertEquals("photo_1.jpg", selected.name)
            assertEquals(original.canonicalFile, selected.file.canonicalFile)
            assertFalse(display.readBytes().contentEquals(selected.file.readBytes()))
        }

    @Test
    fun changedCameraOriginalKeepsTheCapturePending() = withEntry { dir ->
        val display = File(dir, "photo_1.jpg").also { jpeg(it, entropy = 1) }
        val original = File(dir, "original_asset-1.jpg").also { jpeg(it, entropy = 2) }
        writePhotoContract(dir, display, original, sha256(original))
        jpeg(original, entropy = 3)

        val problem = assertThrows(UploadEntryProblem::class.java) {
            selectTransportOriginals(dir, validateUploadPhotos(dir, listOf(display.name)))
        }

        assertTrue(problem.message.orEmpty().contains("changed camera original"))
        assertTrue(dir.isDirectory)
    }

    @Test
    fun outboundContractExplicitlyDeclaresOriginalTransportBytes() {
        val local = JSONObject().put("version", 1)

        val outbound = originalTransportPayload(local)

        assertFalse(local.has("transport"))
        assertEquals(
            "original",
            outbound.getJSONObject("transport").getString("representation"),
        )
        assertEquals(1, outbound.getJSONObject("transport").getInt("version"))
    }

    @Test
    fun offlineAttemptCannotCommitARecordOrProduceAReceipt() = withEntry { dir ->
        jpeg(File(dir, "photo_1.jpg"))
        val photos = validateUploadPhotos(dir, listOf("photo_1.jpg"))
        var recordInserted = false

        assertThrows(IOException::class.java) {
            deliverValidatedCapture(
                "entry-1",
                "phone",
                photos,
                uploadPhoto = { _, _ -> throw IOException("offline") },
                insertRecord = { recordInserted = true },
            )
        }

        assertFalse(recordInserted)
    }

    @Test
    fun partialAttemptDoesNotCommitAndRetryUsesTheSameObjectPaths() = withEntry { dir ->
        jpeg(File(dir, "photo_1.jpg"))
        jpeg(File(dir, "photo_2.jpg"))
        val photos = validateUploadPhotos(dir, listOf("photo_1.jpg", "photo_2.jpg"))
        val firstPaths = mutableListOf<String>()
        var firstInsertCount = 0

        assertThrows(IOException::class.java) {
            deliverValidatedCapture(
                "entry-1",
                "phone",
                photos,
                uploadPhoto = { path, _ ->
                    firstPaths += path
                    if (path.endsWith("photo_2.jpg")) throw IOException("connection lost")
                },
                insertRecord = { firstInsertCount++ },
            )
        }
        assertEquals(0, firstInsertCount)

        val retryPaths = mutableListOf<String>()
        var insertedPaths: List<String>? = null
        val receipt = deliverValidatedCapture(
            "entry-1",
            "phone",
            photos,
            uploadPhoto = { path, _ -> retryPaths += path },
            insertRecord = { insertedPaths = it },
        )

        assertEquals(firstPaths, retryPaths)
        assertEquals(retryPaths, insertedPaths)
        assertEquals(2, receipt.photoCount)
        assertEquals(retryPaths, receipt.remotePaths)
    }

    @Test
    fun recordFailureCannotProduceADeliveryReceipt() = withEntry { dir ->
        jpeg(File(dir, "photo_1.jpg"))
        val photos = validateUploadPhotos(dir, listOf("photo_1.jpg"))
        val uploaded = mutableListOf<String>()

        assertThrows(IOException::class.java) {
            deliverValidatedCapture(
                "entry-1",
                "phone",
                photos,
                uploadPhoto = { path, _ -> uploaded += path },
                insertRecord = { throw IOException("record write failed") },
            )
        }

        assertEquals(listOf("phone/entry-1/photo_1.jpg"), uploaded)
    }

    @Test
    fun deletionRaceCannotCommitARecordOrProduceAReceipt() = withEntry { dir ->
        val photo = File(dir, "photo_1.jpg")
        jpeg(photo)
        val photos = validateUploadPhotos(dir, listOf(photo.name))
        assertTrue(photo.delete())
        var recordInserted = false

        assertThrows(IOException::class.java) {
            deliverValidatedCapture(
                "entry-1",
                "phone",
                photos,
                uploadPhoto = { _, file ->
                    file.readBytes()
                    Unit
                },
                insertRecord = { recordInserted = true },
            )
        }

        assertFalse(recordInserted)
        assertTrue(dir.isDirectory)
    }

    @Test
    fun delayedImportPollingIsFiniteAndBacksOff() {
        assertEquals(6, IMPORT_POLL_DELAYS_MS.size)
        assertTrue(IMPORT_POLL_DELAYS_MS.zipWithNext().all { (a, b) -> a < b })
        assertEquals(TimeUnit.HOURS.toMillis(24), IMPORT_POLL_DELAYS_MS.last())
    }

    @Test
    fun largeBacklogAdvancesExactlyOneCapturePerCursorUnit() {
        val backlog = (5_000 downTo 1).map { index ->
            UploadQueueKey(createdAt = index.toLong(), entryId = "entry-$index")
        }
        val visited = mutableListOf<UploadQueueKey>()
        var cursor: UploadQueueKey? = null

        while (true) {
            val next = nextUploadQueueKey(backlog, cursor) ?: break
            visited += next
            cursor = next
        }

        assertEquals(5_000, visited.size)
        assertEquals(visited.distinct(), visited)
        assertEquals(backlog.sorted(), visited)
        assertEquals(null, nextUploadQueueKey(backlog, visited.last()))
    }

    @Test
    fun queueCursorIsDeterministicForCapturesSealedTogether() {
        val keys = listOf(
            UploadQueueKey(100, "capture-c"),
            UploadQueueKey(100, "capture-a"),
            UploadQueueKey(100, "capture-b"),
        )

        val first = nextUploadQueueKey(keys, null)
        val second = nextUploadQueueKey(keys, first)
        val third = nextUploadQueueKey(keys, second)

        assertEquals("capture-a", first?.entryId)
        assertEquals("capture-b", second?.entryId)
        assertEquals("capture-c", third?.entryId)
    }

    @Test
    fun processingDeferralsBackOffWithoutGrowingUnbounded() {
        assertEquals(TimeUnit.SECONDS.toMillis(30), deferredUploadRecheckDelayMs(0))
        assertEquals(TimeUnit.SECONDS.toMillis(60), deferredUploadRecheckDelayMs(1))
        assertEquals(TimeUnit.SECONDS.toMillis(120), deferredUploadRecheckDelayMs(2))
        assertEquals(TimeUnit.SECONDS.toMillis(120), deferredUploadRecheckDelayMs(20))
    }

    @Test
    fun uploadWorkerNoLongerDrainsEveryPendingDirectoryInOneRun() {
        val source = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()

        assertFalse(source.contains("for (dir in session.pendingUploads())"))
        assertTrue(source.contains("nextPendingCapture(session, cursor)"))
        assertTrue(source.contains("continueUploadChain("))
        assertTrue(source.contains("UPLOAD_PROGRESS_ENTRY_ID"))
        assertTrue(source.contains("UPLOAD_PROGRESS_STAGE"))
    }

    @Test
    fun finalRemoteImportStatesStopPolling() {
        assertTrue(isRemoteImportPending(""))
        assertTrue(isRemoteImportPending("pending"))
        assertTrue(isRemoteImportPending("processing"))
        assertTrue(isRemoteImportPending("future-in-progress-state"))

        listOf("imported", "failed", "error", "void", "cancelled", "canceled")
            .forEach { status -> assertFalse("$status must be terminal", isRemoteImportPending(status)) }
        assertFalse(isRemoteImportPending("  ERROR  "))
    }

    @Test
    fun pendingSentEntriesDoNotConsumeCompletedRetentionSlots() {
        val completed = (1..20).map { index ->
            Entries.SentRetentionCandidate(
                entryId = "completed-$index",
                createdAt = index.toLong(),
                retainLocally = sentEntryNeedsLocalRetention("imported", false),
            )
        }
        val pending = listOf(
            Entries.SentRetentionCandidate(
                entryId = "unknown-import",
                createdAt = 100,
                retainLocally = sentEntryNeedsLocalRetention("future-in-progress-state", false),
            ),
            Entries.SentRetentionCandidate(
                entryId = "pending-photo-work",
                createdAt = 99,
                retainLocally = sentEntryNeedsLocalRetention("imported", true),
            ),
        )

        val overflow = Entries.sentRetentionOverflow(completed + pending)

        assertEquals(
            (1..5).map { "completed-$it" }.toSet(),
            overflow.toSet(),
        )
        assertFalse("unknown import must remain recoverable", "unknown-import" in overflow)
        assertFalse("pending photo work must remain recoverable", "pending-photo-work" in overflow)
    }

    @Test
    fun onlyTerminalEntriesWithoutPhotoWorkArePruneEligible() {
        assertTrue(sentEntryNeedsLocalRetention("", false))
        assertTrue(sentEntryNeedsLocalRetention("pending", false))
        assertTrue(sentEntryNeedsLocalRetention("future-in-progress-state", false))
        assertTrue(sentEntryNeedsLocalRetention("imported", true))
        assertFalse(sentEntryNeedsLocalRetention("imported", false))
        assertFalse(sentEntryNeedsLocalRetention("failed", false))
    }

    @Test
    fun pruningRunsEvenWhenOtherSentEntriesStillNeedPolling() {
        val source = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val finish = source.substringAfter("private suspend fun finishUploadChain")
            .substringBefore("override suspend fun doWork")

        assertTrue(finish.contains("if (hasPendingImports(ctx)) scheduleImportPolling(ctx)"))
        assertTrue(finish.contains("Entries.pruneSent(ctx, ::retainSentEntryLocally)"))
        assertFalse(finish.contains("else Entries.pruneSent"))
    }

    @Test
    fun finalRemoteImportStatesHaveTruthfulLabels() {
        assertEquals("imported", remoteImportTerminalLabel("imported"))
        assertEquals("import failed", remoteImportTerminalLabel("failed"))
        assertEquals("import error", remoteImportTerminalLabel("error"))
        assertEquals("void", remoteImportTerminalLabel("void"))
        assertEquals("import cancelled", remoteImportTerminalLabel("cancelled"))
        assertEquals("import cancelled", remoteImportTerminalLabel("canceled"))
        assertEquals(null, remoteImportTerminalLabel("pending"))
    }

    @Test
    fun cloudRetryWritesAreServerSideIdempotent() {
        val source = File("src/main/java/org/whl/bookcapture/SupabaseClient.kt").readText()
        assertTrue(source.contains("setRequestProperty(\"x-upsert\", \"true\")"))
        assertTrue(source.contains("resolution=ignore-duplicates"))
    }

    private fun jpeg(file: File, entropy: Int = 0) {
        file.writeBytes(byteArrayOf(
            0xff.toByte(), 0xd8.toByte(),                         // SOI
            0xff.toByte(), 0xc0.toByte(), 0x00, 0x0b,             // SOF0, 1 component
            0x08, 0x00, 0x01, 0x00, 0x01, 0x01, 0x01, 0x11, 0x00,
            0xff.toByte(), 0xda.toByte(), 0x00, 0x08,             // SOS, 1 component
            0x01, 0x01, 0x00, 0x00, 0x3f, 0x00,
            entropy.toByte(),                                    // entropy payload
            0xff.toByte(), 0xd9.toByte(),                         // EOI
        ))
    }

    private fun writePhotoContract(
        dir: File,
        display: File,
        original: File,
        originalSha256: String,
    ) {
        val contract = CapturePhotoAssets(
            captureId = dir.name,
            assets = listOf(CapturePhotoAsset(
                assetId = "asset-1",
                captureOrder = 1,
                captureFile = display.name,
                original = PhotoOriginal(original.name, sha256 = originalSha256),
                display = PhotoDisplayDerivative(display.name),
            )),
        )
        File(dir, PHOTO_ASSETS_FILE).writeText(contract.toJson().toString())
    }

    private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256")
        .digest(file.readBytes())
        .joinToString("") { "%02x".format(it) }

    private fun withEntry(block: (File) -> Unit) {
        val root = Files.createTempDirectory("upload-state-test").toFile()
        val dir = File(root, "entry-1").apply { mkdirs() }
        try {
            block(dir)
        } finally {
            root.deleteRecursively()
        }
    }
}
