package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class CaptureMetadataSyncTest {

    @get:Rule
    val temporary = TemporaryFolder()

    private val captureId = "00000000-0000-0000-0000-000000000001"
    private val secondCaptureId = "00000000-0000-0000-0000-000000000002"
    private val ownerId = "00000000-0000-0000-0000-000000000010"

    private fun desktopRow(
        revision: Long = 1,
        bookId: String = "manual:book-1",
        data: JSONObject = JSONObject(),
        owner: String = ownerId,
        updatedAt: String = "2026-07-22T12:00:00Z",
    ) = JSONObject()
        .put("capture_id", captureId)
        .put("owner_id", owner)
        .put("book_id", bookId)
        .put("revision", revision)
        .put("updated_at", updatedAt)
        .put("data", data)

    private fun review(
        revision: Long = 1,
        attention: Boolean = false,
        reason: String = "",
        needsReview: Boolean = false,
        reviewId: String = "",
        status: String = "",
    ) = CaptureReviewMetadata(
        captureId = captureId,
        revision = revision,
        updatedAt = "2026-07-22T12:00:00Z",
        needsAttention = attention || needsReview,
        attentionReason = reason,
        needsReview = needsReview,
        reviewId = reviewId,
        status = status,
    )

    @Test
    fun desktopProjectionExposesEveryListAndPopupIndicator() {
        val data = JSONObject()
            .put(
                "copyright",
                JSONObject()
                    .put("status", "In copyright")
                    .put(
                        "registration_records",
                        JSONArray().put(JSONObject().put("number", "A123")),
                    )
                    .put(
                        "renewal_records",
                        JSONArray().put(JSONObject().put("number", "R456")),
                    ),
            )
            .put(
                "availability",
                JSONObject()
                    .put(
                        "whl",
                        JSONObject().put("state", "available")
                            .put("url", "https://worldherblibrary.org/book"),
                    )
                    .put(
                        "internet_archive",
                        JSONObject().put("available", false)
                            .put("identifier", "old-herbal"),
                    ),
            )
            .put("scan_status", "needs scan")
            .put("remarks", JSONArray().put("First remark").put(
                JSONObject().put("text", "Second remark"),
            ))

        val parsed = desktopBookMetadataFromJson(desktopRow(data = data))!!

        assertTrue(parsed.registered)
        assertEquals("In copyright", parsed.copyright.status)
        assertEquals("A123", parsed.copyright.registrationRecords.single().getString("number"))
        assertEquals("R456", parsed.copyright.renewalRecords.single().getString("number"))
        assertEquals(DesktopAvailabilityState.AVAILABLE, parsed.whl.state)
        assertEquals(true, parsed.whl.available)
        assertEquals(DesktopAvailabilityState.UNAVAILABLE, parsed.internetArchive.state)
        assertEquals(false, parsed.internetArchive.available)
        assertEquals("needs scan", parsed.scanStatus)
        assertEquals(listOf("First remark", "Second remark"), parsed.remarks)
    }

    @Test
    fun desktopProjectionRetainsUnknownDataButDoesNotExposeMutableStoreJson() {
        val parsed = desktopBookMetadataFromJson(
            desktopRow(data = JSONObject().put("future", JSONObject().put("shelf", 3))),
        )!!
        val copy = parsed.dataCopy()
        copy.getJSONObject("future").put("shelf", 9)

        assertEquals(3, parsed.dataCopy().getJSONObject("future").getInt("shelf"))
        assertNull(
            desktopBookMetadataFromJson(
                desktopRow(data = JSONObject().put("large", "x".repeat(300_000))),
            ),
        )
    }

    @Test
    fun desktopSidecarOnlyAdvancesOnHigherServerRevision() {
        val dir = temporary.newFolder(captureId)
        val first = desktopBookMetadataFromJson(
            desktopRow(revision = 2, data = JSONObject().put("scan_status", "queued")),
        )!!
        val stale = desktopBookMetadataFromJson(
            desktopRow(revision = 1, data = JSONObject().put("scan_status", "old")),
        )!!
        val conflicting = desktopBookMetadataFromJson(
            desktopRow(revision = 2, data = JSONObject().put("scan_status", "different")),
        )!!
        val newer = desktopBookMetadataFromJson(
            desktopRow(revision = 3, data = JSONObject().put("scan_status", "ready")),
        )!!

        assertEquals(DesktopMetadataApplyResult.APPLIED,
            CaptureMetadataStore.applyDesktopBook(dir, first))
        assertEquals(DesktopMetadataApplyResult.STALE,
            CaptureMetadataStore.applyDesktopBook(dir, stale))
        assertEquals(DesktopMetadataApplyResult.CONFLICT,
            CaptureMetadataStore.applyDesktopBook(dir, conflicting))
        assertEquals(DesktopMetadataApplyResult.APPLIED,
            CaptureMetadataStore.applyDesktopBook(dir, newer))
        assertEquals("ready", CaptureMetadataStore.readDesktopBook(dir)!!.scanStatus)
    }

    @Test
    fun higherRevisionCanExplicitlyUnregisterWithoutStaleResurrection() {
        val dir = temporary.newFolder(captureId)
        val registered = desktopBookMetadataFromJson(desktopRow(revision = 4))!!
        val removed = desktopBookMetadataFromJson(
            desktopRow(revision = 5, bookId = "", data = JSONObject()),
        )!!

        CaptureMetadataStore.applyDesktopBook(dir, registered)
        CaptureMetadataStore.applyDesktopBook(dir, removed)

        val local = CaptureMetadataStore.readDesktopBook(dir)!!
        assertFalse(local.registered)
        assertEquals(5, local.revision)
        assertEquals(DesktopMetadataApplyResult.STALE,
            CaptureMetadataStore.applyDesktopBook(dir, registered))
    }

    @Test
    fun lanProjectionRecoversAfterOwnerRotationOrNewerLedgerReset() {
        val dir = temporary.newFolder(captureId)
        val original = desktopBookMetadataFromJson(desktopRow(
            revision = 9,
            data = JSONObject().put("scan_status", "old"),
            updatedAt = "2026-07-22T12:00:00Z",
        ))!!
        val reset = desktopBookMetadataFromJson(desktopRow(
            revision = 1,
            data = JSONObject().put("scan_status", "reset"),
            updatedAt = "2026-07-22T12:01:00Z",
        ))!!
        val olderReset = desktopBookMetadataFromJson(desktopRow(
            revision = 1,
            data = JSONObject().put("scan_status", "stale"),
            updatedAt = "2026-07-22T11:59:00Z",
        ))!!
        val rotated = desktopBookMetadataFromJson(desktopRow(
            revision = 1,
            owner = "00000000-0000-0000-0000-000000000011",
            data = JSONObject().put("scan_status", "rotated"),
            updatedAt = "2026-07-22T11:00:00Z",
        ))!!

        CaptureMetadataStore.applyDesktopBook(dir, original)
        assertEquals(DesktopMetadataApplyResult.APPLIED,
            CaptureMetadataStore.applyDesktopBook(dir, reset))
        assertEquals(DesktopMetadataApplyResult.STALE,
            CaptureMetadataStore.applyDesktopBook(dir, olderReset))
        assertEquals(DesktopMetadataApplyResult.APPLIED,
            CaptureMetadataStore.applyDesktopBook(dir, rotated))
        assertEquals("rotated", CaptureMetadataStore.readDesktopBook(dir)!!.scanStatus)
    }

    @Test
    fun localReviewEditIsDurableAndDirtyWithoutEnqueuingNetworkWork() {
        val dir = temporary.newFolder(captureId)
        val edited = editCaptureReview(
            existing = null,
            captureId = captureId,
            needsAttention = false,
            needsReview = true,
            reason = "  Verify the title page  ",
        )

        assertTrue(edited.dirty)
        assertTrue(edited.current.needsAttention)
        assertTrue(edited.current.needsReview)
        assertEquals("Verify the title page", edited.current.attentionReason)
        assertTrue(CaptureMetadataStore.mutateReview(dir) { edited })
        assertEquals(edited, CaptureMetadataStore.readReview(dir))

        val entriesSource = java.io.File(
            "src/main/java/org/whl/bookcapture/Entries.kt",
        ).readText().substringAfter("fun setCaptureReview(").substringBefore("private fun load(")
        assertFalse(entriesSource.contains("enqueue"))
        assertFalse(entriesSource.contains("WorkManager"))
    }

    @Test
    fun sentRetentionCannotDeleteAnUnacknowledgedPhoneReview() {
        val dir = temporary.newFolder(captureId)
        val dirty = editCaptureReview(
            existing = null,
            captureId = captureId,
            needsAttention = true,
            needsReview = true,
            reason = "Check the edition",
        )
        assertTrue(CaptureMetadataStore.mutateReview(dir) { dirty })

        assertFalse(CaptureMetadataStore.deleteIfNoUnsyncedLocalMutation(dir))
        assertTrue(dir.isDirectory)
        assertTrue(CaptureMetadataStore.readReview(dir)!!.dirty)

        val acknowledged = review(
            revision = 2,
            attention = true,
            reason = "Check the edition",
            needsReview = true,
            reviewId = "review-1",
            status = "open",
        )
        assertTrue(CaptureMetadataStore.mutateReview(dir) {
            CaptureReviewStore(acknowledged, acknowledged, dirty = false)
        })
        assertTrue(CaptureMetadataStore.deleteIfNoUnsyncedLocalMutation(dir))
        assertFalse(dir.exists())

        val entriesSource = java.io.File(
            "src/main/java/org/whl/bookcapture/Entries.kt",
        ).readText()
        assertTrue(entriesSource.contains("deleteIfNoUnsyncedLocalMutation(dir)"))
    }

    @Test
    fun oneSidedOfflineReviewEditPlansARevisionCas() {
        val baseline = review(revision = 7, attention = false)
        val local = editCaptureReview(
            CaptureReviewStore(baseline, baseline, false),
            captureId,
            needsAttention = true,
            needsReview = false,
            reason = "Check author",
        )

        val merge = mergeCaptureReview(local, baseline)!!

        assertEquals(7L, merge.write!!.expectedCloudRevision)
        assertEquals("Check author", merge.write.state.attentionReason)
        assertTrue(merge.store.dirty)
    }

    @Test
    fun concurrentDesktopReviewCannotBeSilentlyClearedByPhone() {
        val baseline = review(revision = 2, attention = true, reason = "Old reason")
        val localClear = editCaptureReview(
            CaptureReviewStore(baseline, baseline, false),
            captureId,
            needsAttention = false,
            needsReview = false,
            reason = "",
        )
        val cloud = review(
            revision = 3,
            attention = true,
            reason = "Desktop found a rights conflict",
            needsReview = true,
            reviewId = "review-9",
            status = "open",
        )

        val merge = mergeCaptureReview(localClear, cloud)!!

        assertTrue(merge.store.current.needsAttention)
        assertTrue(merge.store.current.needsReview)
        assertEquals("Desktop found a rights conflict", merge.store.current.attentionReason)
        assertEquals("review-9", merge.store.current.reviewId)
        assertEquals(3L, merge.write!!.expectedCloudRevision)
    }

    @Test
    fun acknowledgementDoesNotOverwriteASecondEditMadeDuringHttp() {
        val baseline = review(revision = 1)
        val sent = baseline.copy(needsAttention = true, attentionReason = "First")
        val editedDuringHttp = sent.copy(attentionReason = "Second")
        val accepted = sent.copy(
            revision = 2,
            updatedAt = "2026-07-22T12:01:00Z",
            reviewId = "review-1",
            status = "open",
        )

        val acknowledged = acknowledgeCaptureReviewWrite(
            CaptureReviewStore(editedDuringHttp, baseline, dirty = true),
            sent,
            accepted,
        )

        assertTrue(acknowledged.dirty)
        assertEquals("Second", acknowledged.current.attentionReason)
        assertEquals(2, acknowledged.current.revision)
        assertEquals("review-1", acknowledged.current.reviewId)
        assertEquals(accepted, acknowledged.shadow)
    }

    @Test
    fun atomicReviewMutationSerializesInterleavedUiAndWorkerEdits() {
        val dir = temporary.newFolder(captureId)
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val failure = AtomicReference<Throwable?>()
        val first = Thread {
            try {
                assertTrue(CaptureMetadataStore.mutateReview(dir) { current ->
                    firstEntered.countDown()
                    assertTrue(releaseFirst.await(2, TimeUnit.SECONDS))
                    editCaptureReview(
                        current,
                        captureId,
                        needsAttention = true,
                        needsReview = false,
                        reason = "First reason",
                    )
                })
            } catch (error: Throwable) {
                failure.compareAndSet(null, error)
            }
        }
        first.start()
        assertTrue(firstEntered.await(2, TimeUnit.SECONDS))

        val second = Thread {
            try {
                assertTrue(CaptureMetadataStore.mutateReview(dir) { current ->
                    val value = current ?: error("first mutation was not observed")
                    value.copy(
                        current = value.current.copy(
                            needsAttention = true,
                            needsReview = true,
                        ),
                        dirty = true,
                    )
                })
            } catch (error: Throwable) {
                failure.compareAndSet(null, error)
            }
        }
        second.start()
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
        while (second.state != Thread.State.BLOCKED && System.nanoTime() < deadline) {
            Thread.yield()
        }
        val blockedState = second.state
        releaseFirst.countDown()
        first.join(2_000)
        second.join(2_000)
        assertEquals(Thread.State.BLOCKED, blockedState)
        assertFalse(first.isAlive)
        assertFalse(second.isAlive)
        failure.get()?.let { throw AssertionError("concurrent mutation failed", it) }

        val saved = CaptureMetadataStore.readReview(dir)!!
        assertTrue(saved.current.needsAttention)
        assertTrue(saved.current.needsReview)
        assertEquals("First reason", saved.current.attentionReason)
        assertTrue(saved.dirty)
    }

    @Test
    fun corruptReviewSidecarIsNeverMistakenForMissingOrOverwritten() {
        val dir = temporary.newFolder(captureId)
        val sidecar = File(dir, "capture_review.json")
        val corrupt = """{"schema":"org.whl.bookcapture.capture-review","version":1,"capture_id":"$captureId","current":{},"shadow":null,"dirty":"true"}"""
        sidecar.writeText(corrupt)
        var transformCalled = false

        assertNull(CaptureMetadataStore.readReview(dir))
        assertTrue(CaptureMetadataStore.reviewState(dir) is CaptureReviewFileState.Corrupt)
        assertTrue(CaptureMetadataStore.hasPendingReviewSync(dir))
        assertFalse(CaptureMetadataStore.mutateReview(dir) {
            transformCalled = true
            editCaptureReview(it, captureId, true, false, "must not replace")
        })
        assertFalse(transformCalled)
        assertEquals(corrupt, sidecar.readText())
        assertFalse(CaptureMetadataStore.deleteIfNoUnsyncedLocalMutation(dir))
    }

    @Test
    fun dirtyReviewReconcilesAgainstARecreatedLowerRevisionRow() {
        val old = review(revision = 9, attention = false)
        val local = CaptureReviewStore(
            old.copy(needsAttention = true, attentionReason = "Phone reason"),
            shadow = old,
            dirty = true,
        )
        val recreated = review(
            revision = 1,
            attention = true,
            reason = "Desktop reason",
        ).copy(updatedAt = "2026-07-22T12:01:00Z")

        val merge = mergeCaptureReview(local, recreated)!!

        assertEquals(1L, merge.write!!.expectedCloudRevision)
        assertTrue(merge.store.dirty)
        assertEquals(
            "Desktop: Desktop reason\nPhone: Phone reason",
            merge.write.state.attentionReason,
        )
        assertNull(merge.conflict)
    }

    @Test
    fun dirtyReviewIgnoresAnOlderLowerRevisionResponse() {
        val old = review(revision = 9, attention = false)
            .copy(updatedAt = "2026-07-22T12:00:00Z")
        val local = CaptureReviewStore(
            old.copy(needsAttention = true, attentionReason = "Phone reason"),
            shadow = old,
            dirty = true,
        )
        val delayed = review(
            revision = 1,
            attention = true,
            reason = "Delayed desktop reason",
        ).copy(updatedAt = "2026-07-22T11:59:00Z")

        val merge = mergeCaptureReview(local, delayed)!!

        assertEquals(local, merge.store)
        assertNull(merge.write)
        assertNull(merge.conflict)
    }

    @Test
    fun cleanReviewAdoptsOnlyANewerLowerRevisionLedgerReset() {
        val localValue = review(
            revision = 9,
            attention = true,
            reason = "Old desktop reason",
        ).copy(updatedAt = "2026-07-22T12:00:00Z")
        val local = CaptureReviewStore(localValue, localValue, dirty = false)
        val newerReset = review(
            revision = 1,
            attention = true,
            reason = "Reset desktop reason",
        ).copy(updatedAt = "2026-07-22T12:01:00Z")
        val olderReset = newerReset.copy(
            attentionReason = "Delayed response",
            updatedAt = "2026-07-22T11:59:00Z",
        )

        val adopted = mergeCaptureReview(local, newerReset)!!
        val retained = mergeCaptureReview(local, olderReset)!!

        assertEquals(newerReset, adopted.store.current)
        assertFalse(adopted.store.dirty)
        assertEquals(local, retained.store)
    }

    @Test
    fun simultaneousReasonsAreCombinedOrReportedAsAConflict() {
        val baseline = review(revision = 4, attention = true, reason = "Original")
        val local = CaptureReviewStore(
            baseline.copy(attentionReason = "Phone found marginalia"),
            shadow = baseline,
            dirty = true,
        )
        val cloud = baseline.copy(
            revision = 5,
            attentionReason = "Desktop found a rights issue",
        )
        val merged = mergeCaptureReview(local, cloud)!!

        assertEquals(
            "Desktop: Desktop found a rights issue\nPhone: Phone found marginalia",
            merged.store.current.attentionReason,
        )
        assertNull(merged.conflict)

        val oversized = mergeCaptureReview(
            local.copy(current = local.current.copy(attentionReason = "p".repeat(700))),
            cloud.copy(attentionReason = "d".repeat(700)),
        )!!
        assertTrue(oversized.conflict!!.contains("1000-character"))
        assertNull(oversized.write)
    }

    @Test
    fun cloudBodyCannotRewriteDesktopManagedReviewIdentityOrRevision() {
        val body = captureReviewCloudBody(
            review(
                revision = 8,
                attention = true,
                reason = "Check this",
                needsReview = true,
                reviewId = "review-secret",
                status = "open",
            ),
        )

        assertFalse(body.has("owner_id"))
        assertFalse(body.has("revision"))
        assertFalse(body.has("updated_at"))
        assertFalse(body.has("review_id"))
        assertFalse(body.has("status"))
    }

    @Test
    fun lanReviewCarriesTheValidatedSidecarEnvelope() {
        val body = captureReviewLanBody(
            review(revision = 3, attention = true, reason = "Check", needsReview = true),
        )

        assertEquals("org.whl.bookcapture.capture-review", body.getString("schema"))
        assertEquals(1, body.getInt("version"))
        assertEquals(captureId, body.getString("capture_id"))
        assertEquals(3L, body.getLong("revision"))
        assertTrue(body.getBoolean("needs_review"))
    }

    @Test
    fun cloudAndLanResponseReadersRejectOversizeBodies() {
        val exact = ByteArray(32) { 7 }
        assertEquals(32, readBounded(ByteArrayInputStream(exact), 32).size)
        assertEquals(32, readBoundedSupabaseResponse(ByteArrayInputStream(exact), 32).size)

        for (reader in listOf<(ByteArrayInputStream) -> Unit>(
            { readBounded(it, 31) },
            { readBoundedSupabaseResponse(it, 31) },
        )) {
            try {
                reader(ByteArrayInputStream(exact))
                throw AssertionError("oversized response was accepted")
            } catch (_: IOException) {
                // expected
            }
        }
    }

    @Test
    fun everyLanResponseAndMetadataCloudResponseIsBounded() {
        val lan = File("src/main/java/org/whl/bookcapture/LanClient.kt").readText()
        val cloud = File("src/main/java/org/whl/bookcapture/SupabaseClient.kt").readText()

        assertFalse(lan.contains("bufferedReader().use { it.readText() }"))
        assertFalse(lan.contains("errorStream?.readBytes()"))
        assertTrue(lan.contains("MAX_LAN_CONTROL_RESPONSE_BYTES"))
        assertTrue(lan.contains("MAX_LAN_CAPTURE_RECEIPT_BYTES"))
        assertTrue(cloud.contains("readBoundedSupabaseResponse"))
        assertTrue(cloud.contains("fetchDesktopBookMetadataIsolated"))
        assertTrue(cloud.contains("fetchCaptureReviewsIsolated"))
    }

    @Test
    fun captureQueriesDiscardUnsafeAndDuplicateIdentifiers() {
        assertEquals(
            listOf(captureId, secondCaptureId),
            safeCaptureSyncIds(
                listOf(captureId, "../bad", secondCaptureId, secondCaptureId, ""),
            ),
        )
    }

    @Test
    fun workerHasSeparatePullOnlyAndExplicitPushEntryPoints() {
        val source = java.io.File(
            "src/main/java/org/whl/bookcapture/CaptureMetadataSyncWorker.kt",
        ).readText()

        assertTrue(source.contains("fun enqueuePull(ctx: Context)"))
        assertTrue(source.contains("fun enqueueExplicitSync(ctx: Context)"))
        assertTrue(source.contains("if (!pushReviews || plan == null) continue"))
    }
}
