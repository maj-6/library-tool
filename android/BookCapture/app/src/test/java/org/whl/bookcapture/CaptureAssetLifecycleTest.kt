package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CaptureAssetLifecycleTest {
    private val originalHash = "a".repeat(64)
    private val correctionId = "c".repeat(64)

    private fun asset(
        lifecycle: DesktopPhotoAssetLifecycle = DesktopPhotoAssetLifecycle(),
    ) = CapturePhotoAsset(
        assetId = "asset-2",
        captureOrder = 2,
        captureFile = "photo_2.jpg",
        original = PhotoOriginal(
            reference = "original_asset-2.jpg",
            sha256 = originalHash,
            revision = 1,
            width = 100,
            height = 200,
        ),
        display = PhotoDisplayDerivative(
            reference = "desktop_asset-2_r3.jpg",
            sha256 = "b".repeat(64),
            revision = 3,
            width = 90,
            height = 180,
            recipe = DESKTOP_CORRECTION_RECIPE,
        ),
        geometries = listOf(PhotoOcrGeometry(
            assetId = "asset-2",
            sourceSha256 = originalHash,
            sourceRevision = 1,
            displayRevision = 3,
            coordinateSpace = "display_normalized",
            width = 90,
            height = 180,
            orientationDegrees = 0,
            engine = "mistral",
            model = "mistral-ocr-latest",
            regions = emptyList(),
        )),
        appliedDesktopCorrectionId = correctionId,
        appliedDesktopCorrectionRevision = 7L,
        desktopLifecycle = lifecycle,
    )

    private fun rowJson(
        captureId: String = "capture-1",
        assetId: String = "asset-2",
        ownerId: String = "owner-1",
        state: String = "deleted",
        captureOrder: Int = 2,
        sourceSha256: String = originalHash,
        lifecycleRevision: Long = 4L,
        changedAt: Long = 1_722_000_000_123L,
        rowRevision: Long = 9L,
    ) = JSONObject()
        .put("capture_id", captureId)
        .put("asset_id", assetId)
        .put("owner_id", ownerId)
        .put("source_original_sha256", sourceSha256)
        .put("result", JSONObject()
            .put("schema", CAPTURE_ASSET_LIFECYCLE_SCHEMA)
            .put("version", CAPTURE_ASSET_LIFECYCLE_VERSION)
            .put("capture_id", captureId)
            .put("asset_id", assetId)
            .put("state", state)
            .put("capture_order", captureOrder)
            .put("source_original_sha256", sourceSha256)
            .put("lifecycle_revision", lifecycleRevision)
            .put("changed_at", changedAt))
        .put("revision", rowRevision)
        .put("updated_at", "2026-08-09T12:00:00Z")

    @Test
    fun lifecyclePagesContinueAfterServerCappedShortResponses() {
        val captureId = "2ec86526-1133-4e74-a2c7-497886201d76"
        val ownerId = "7a1d2f30-59c2-4a41-9a3b-8f6f6f0c2ad1"
        val calls = mutableListOf<Pair<CaptureAssetLifecycleCursor?, Int>>()

        val loaded = collectCaptureAssetLifecyclePages(
            expectedOwnerId = ownerId,
            expectedCaptureIds = listOf(captureId),
        ) { after, limit ->
            calls += after to limit
            when (after?.assetId) {
                null -> JSONArray().put(rowJson(
                    captureId = captureId,
                    assetId = "asset-1",
                    ownerId = ownerId,
                ))
                "asset-1" -> JSONArray().put(rowJson(
                    captureId = captureId,
                    assetId = "asset-2",
                    ownerId = ownerId,
                ))
                else -> JSONArray()
            }
        }

        assertEquals(listOf("asset-1", "asset-2"), loaded.map { it.assetId })
        assertEquals(
            listOf(null, "asset-1", "asset-2"),
            calls.map { it.first?.assetId },
        )
        assertEquals(listOf(500, 500, 500), calls.map { it.second })
    }

    @Test
    fun lifecyclePagesFailClosedInsteadOfReturningSafetyTruncation() {
        val captureId = "2ec86526-1133-4e74-a2c7-497886201d76"
        val ownerId = "7a1d2f30-59c2-4a41-9a3b-8f6f6f0c2ad1"
        val limits = mutableListOf<Int>()

        assertThrows(SupabaseClient.InvalidResponse::class.java) {
            collectCaptureAssetLifecyclePages(
                expectedOwnerId = ownerId,
                expectedCaptureIds = listOf(captureId),
                maximumRows = 1,
            ) { after, limit ->
                limits += limit
                val assetId = if (after == null) "asset-1" else "asset-2"
                JSONArray().put(rowJson(
                    captureId = captureId,
                    assetId = assetId,
                    ownerId = ownerId,
                ))
            }
        }
        assertEquals(listOf(2, 1), limits)
    }

    @Test
    fun lifecycleQueryPinsScopeStableOrderLimitAndCompoundCursor() {
        val captureA = "2ec86526-1133-4e74-a2c7-497886201d76"
        val captureB = "ed3cb24e-490a-49b1-a066-4e9768bf3f00"

        assertEquals(
            "/rest/v1/capture_asset_lifecycle" +
                "?capture_id=in.($captureA,$captureB)&select=" +
                "capture_id,asset_id,owner_id,source_original_sha256," +
                "result,revision,updated_at" +
                "&order=capture_id.asc,asset_id.asc&limit=500",
            captureAssetLifecyclePath(listOf(captureB, captureA)),
        )
        assertEquals(
            "/rest/v1/capture_asset_lifecycle" +
                "?capture_id=in.($captureA)&select=" +
                "capture_id,asset_id,owner_id,source_original_sha256," +
                "result,revision,updated_at" +
                "&or=(capture_id.gt.$captureA," +
                "and(capture_id.eq.$captureA,asset_id.gt.asset-1))" +
                "&order=capture_id.asc,asset_id.asc&limit=17",
            captureAssetLifecyclePath(
                listOf(captureA),
                CaptureAssetLifecycleCursor(captureA, "asset-1"),
                17,
            ),
        )
    }

    @Test
    fun lifecycleQueryQuotesDottedAssetCursorForPostgrestGrammar() {
        val capture = "2ec86526-1133-4e74-a2c7-497886201d76"

        val path = captureAssetLifecyclePath(
            listOf(capture),
            CaptureAssetLifecycleCursor(capture, "asset.page-1"),
            17,
        )

        assertTrue(path.contains("asset_id.gt.%22asset.page-1%22"))
    }

    @Test
    fun cloudRowParserPinsTheVersionedLifecycleEnvelope() {
        val parsed = captureAssetLifecycleRowFromJson(rowJson())!!

        assertEquals("capture-1", parsed.captureId)
        assertEquals("asset-2", parsed.assetId)
        assertEquals(DesktopPhotoAssetState.DELETED, parsed.state)
        assertEquals(2, parsed.captureOrder)
        assertEquals(4L, parsed.lifecycleRevision)
        assertEquals(1_722_000_000_123L, parsed.changedAt)
        assertEquals(9L, parsed.revision)

        assertNull(captureAssetLifecycleRowFromJson(
            rowJson(state = "archived"),
        ))
        assertNull(captureAssetLifecycleRowFromJson(
            rowJson(lifecycleRevision = 0L),
        ))
        assertNull(captureAssetLifecycleRowFromJson(
            rowJson(changedAt = 0L),
        ))
        assertNull(captureAssetLifecycleRowFromJson(
            rowJson(captureOrder = 0),
        ))
        assertNull(captureAssetLifecycleRowFromJson(
            rowJson(rowRevision = 0L),
        ))
        val mismatchedAnchor = rowJson()
        mismatchedAnchor.getJSONObject("result")
            .put("source_original_sha256", "d".repeat(64))
        assertNull(captureAssetLifecycleRowFromJson(mismatchedAnchor))
        val mismatchedAsset = rowJson()
        mismatchedAsset.getJSONObject("result").put("asset_id", "asset-3")
        assertNull(captureAssetLifecycleRowFromJson(mismatchedAsset))
        val extendedV1 = rowJson()
        extendedV1.getJSONObject("result").put("unexpected", true)
        assertNull(captureAssetLifecycleRowFromJson(extendedV1))
    }

    @Test
    fun lifecycleValidationIsAnchorOrderAndRevisionBound() {
        val deleted = DesktopPhotoAssetLifecycle(
            DesktopPhotoAssetState.DELETED,
            revision = 4L,
            updatedAt = 1_722_000_000_123L,
        )
        val local = CapturePhotoAssets("capture-1", listOf(asset(deleted)))
        val exact = captureAssetLifecycleRowFromJson(rowJson())!!
        assertEquals(
            CaptureAssetLifecycleDecision.AlreadyApplied,
            validateCaptureAssetLifecycle(local, exact, "owner-1"),
        )

        val staleRestore = captureAssetLifecycleRowFromJson(rowJson(
            state = "active",
            lifecycleRevision = 3L,
        ))!!
        assertEquals(
            CaptureAssetLifecycleDecision.NotApplicable,
            validateCaptureAssetLifecycle(local, staleRestore, "owner-1"),
        )

        val collision = captureAssetLifecycleRowFromJson(rowJson(
            state = "active",
            changedAt = deleted.updatedAt + 1L,
        ))!!
        assertTrue(
            validateCaptureAssetLifecycle(local, collision, "owner-1") is
                CaptureAssetLifecycleDecision.Rejected,
        )
        assertTrue(validateCaptureAssetLifecycle(
            local,
            captureAssetLifecycleRowFromJson(rowJson(captureOrder = 3))!!,
            "owner-1",
        ) is CaptureAssetLifecycleDecision.Rejected)
        assertTrue(validateCaptureAssetLifecycle(
            local,
            captureAssetLifecycleRowFromJson(rowJson(sourceSha256 = "d".repeat(64)))!!,
            "owner-1",
        ) is CaptureAssetLifecycleDecision.Rejected)

        val restored = captureAssetLifecycleRowFromJson(rowJson(
            state = "active",
            lifecycleRevision = 5L,
            changedAt = deleted.updatedAt + 2L,
        ))!!
        val ready = validateCaptureAssetLifecycle(local, restored, "owner-1") as
            CaptureAssetLifecycleDecision.Ready
        assertEquals("asset-2", ready.assetId)
        assertEquals(DesktopPhotoAssetState.ACTIVE, ready.lifecycle.state)
        assertEquals(5L, ready.lifecycle.revision)
    }

    @Test
    fun storeDeleteAndRestoreOnlyMutateVisibility() {
        val dir = Files.createTempDirectory("capture-lifecycle-").toFile()
        try {
            val initial = asset()
            File(dir, initial.captureFile).writeText("immutable-capture")
            File(dir, initial.original.reference).writeText("immutable-original")
            File(dir, initial.display.reference).writeText("corrected-display")
            File(dir, PHOTO_ASSETS_FILE).writeText(
                CapturePhotoAssets(dir.name, listOf(initial)).toJson().toString(),
            )
            fun parsedRow(
                state: String,
                lifecycleRevision: Long,
                changedAt: Long,
            ): CaptureAssetLifecycleRow {
                val json = rowJson(
                    state = state,
                    lifecycleRevision = lifecycleRevision,
                    changedAt = changedAt,
                ).put("capture_id", dir.name)
                json.getJSONObject("result").put("capture_id", dir.name)
                return captureAssetLifecycleRowFromJson(json)!!
            }

            assertTrue(PhotoAssetStore.applyDesktopLifecycle(
                dir,
                parsedRow("deleted", 4L, 4_000L),
                "owner-1",
            ))
            val hidden = PhotoAssetStore.read(dir).assets.single()
            assertTrue(hidden.desktopLifecycle.deleted)
            assertEquals(initial.copy(desktopLifecycle = hidden.desktopLifecycle), hidden)
            assertTrue(PhotoAssetStore.descriptors(dir).isEmpty())
            assertEquals("immutable-capture", File(dir, initial.captureFile).readText())
            assertEquals("immutable-original", File(dir, initial.original.reference).readText())
            assertEquals("corrected-display", File(dir, initial.display.reference).readText())
            assertEquals(1, PhotoAssetStore.payload(dir).getJSONArray("assets").length())

            assertFalse(PhotoAssetStore.applyDesktopLifecycle(
                dir,
                parsedRow("active", 3L, 5_000L),
                "owner-1",
            ))
            assertTrue(PhotoAssetStore.read(dir).assets.single().desktopLifecycle.deleted)

            assertTrue(PhotoAssetStore.applyDesktopLifecycle(
                dir,
                parsedRow("active", 5L, 6_000L),
                "owner-1",
            ))
            val restored = PhotoAssetStore.read(dir).assets.single()
            assertFalse(restored.desktopLifecycle.deleted)
            assertEquals(2, PhotoAssetStore.descriptors(dir).single().captureOrder)
            assertEquals(initial.display, restored.display)
            assertEquals(initial.geometries, restored.geometries)
            assertEquals(initial.appliedDesktopCorrectionId, restored.appliedDesktopCorrectionId)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun queuedDisplayReOcrStopsWhenItsAssetIsTombstoned() {
        val dir = Files.createTempDirectory("capture-lifecycle-reocr-").toFile()
        try {
            val initial = asset().let { value ->
                val profile = resolvePostProcessingProfile(
                    PostProcessingPreset.AUTOMATIC_BY_DATE,
                    1890,
                )
                val display = value.display.copy(
                    sha256 = "2cc3d5e7a7bf90d23958e9628fb046bac9d6471da00779d904291fed0e28152e",
                )
                val role = PhotoRoleAssignment(
                    suggestedRole = PhotoRole.TITLE_PAGE,
                    confidence = 0.9,
                    reason = "test role",
                    algorithm = "test",
                    algorithmVersion = "1",
                )
                value.copy(display = display, role = role,
                    processingRequest = PhotoProcessingRequest(
                        requestId = "request-before-delete",
                        requestRevision = 1,
                        profile = profile,
                        requestedAt = 1_000L,
                        sourceAssetId = value.assetId,
                        sourceRole = role.effectiveRole,
                        sourceOriginalSha256 = value.original.sha256,
                        sourceOriginalRevision = value.original.revision,
                        sourceDisplaySha256 = display.sha256,
                        sourceDisplayRevision = display.revision,
                        operations = processingOperationsFor(profile, role.effectiveRole),
                    ), lifecycle = PhotoLifecycleState(
                    state = PhotoAssetLifecycle.QUEUED,
                    jobId = "job-before-delete",
                    updatedAt = 2_000L,
                ))
            }
            File(dir, initial.captureFile).writeText("immutable-capture")
            File(dir, initial.original.reference).writeText("immutable-original")
            File(dir, initial.display.reference).writeText("corrected-display")
            File(dir, PHOTO_ASSETS_FILE).writeText(
                CapturePhotoAssets(dir.name, listOf(initial)).toJson().toString(),
            )
            val target = CloudDisplayReocrTarget(
                captureId = dir.name,
                assetId = initial.assetId,
                jobId = initial.appliedDesktopCorrectionId,
                displayReference = initial.display.reference,
                displaySha256 = initial.display.sha256,
                displayRevision = initial.display.revision,
            )
            val marker = File(
                dir,
                ".cloud-reocr-${initial.assetId}-r${initial.display.revision}-" +
                    "${initial.display.sha256.take(20)}.pending",
            ).apply { writeText("pending\n") }
            assertEquals(
                File(dir, initial.display.reference),
                PhotoAssetStore.cloudDisplayReocrFile(dir, target),
            )

            val deletedJson = rowJson(
                captureId = dir.name,
                lifecycleRevision = 4L,
                changedAt = 4_000L,
            )
            val deleted = captureAssetLifecycleRowFromJson(deletedJson)!!
            assertTrue(PhotoAssetStore.applyDesktopLifecycle(dir, deleted, "owner-1"))

            val draft = OcrGeometryDraft(
                width = initial.display.width,
                height = initial.display.height,
                engine = "mistral",
                model = "mistral-ocr-latest",
                regions = listOf(PhotoOcrRegion(
                    id = "region-1",
                    regionType = "text",
                    polygon = listOf(
                        NormalizedPoint(0.1, 0.1),
                        NormalizedPoint(0.9, 0.1),
                        NormalizedPoint(0.9, 0.2),
                        NormalizedPoint(0.1, 0.2),
                    ),
                    text = "must stay unapplied",
                )),
            )
            val beforeMerge = File(dir, PHOTO_ASSETS_FILE).readText()
            val queuedJob = CloudPhotoProcessingJob(
                id = "job-before-delete",
                captureId = dir.name,
                ownerId = "owner-1",
                assetId = initial.assetId,
                requestId = checkNotNull(initial.processingRequest).requestId,
                requestRevision = initial.processingRequest.requestRevision,
                sourceSha256 = initial.original.sha256,
                state = "running",
                result = null,
                lastError = "",
            )

            assertNull(PhotoAssetStore.cloudDisplayReocrFile(dir, target))
            assertFalse(PhotoAssetStore.recordCloudJobState(dir, queuedJob))
            assertFalse(PhotoAssetStore.mergeCloudDisplayReocrGeometry(
                dir,
                target,
                draft,
            ))
            assertEquals(beforeMerge, File(dir, PHOTO_ASSETS_FILE).readText())
            assertTrue(marker.isFile)
            assertTrue(PhotoAssetStore.read(dir).assets.single().desktopLifecycle.deleted)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun lifecyclePullRunsBeforeCorrectionApplicationAndMissingRowsStayAbsent() {
        val client = File("src/main/java/org/whl/bookcapture/SupabaseClient.kt").readText()
        val worker = File(
            "src/main/java/org/whl/bookcapture/CaptureMetadataSyncWorker.kt",
        ).readText()

        assertTrue(client.contains("/rest/v1/capture_asset_lifecycle"))
        assertTrue(client.contains("capture_id,asset_id,owner_id,source_original_sha256,"))
        assertTrue(client.contains("result,revision,updated_at"))
        assertFalse(client.contains("CaptureAssetLifecycleRow(" + "\n" + "            state = ACTIVE"))
        assertTrue(worker.contains("client.captureAssetLifecycles(ids)"))
        assertTrue(
            worker.indexOf("for ((captureId, rows) in assetLifecycleRows)") <
                worker.indexOf("for ((captureId, corrections) in correctionRows)"),
        )
    }
}
