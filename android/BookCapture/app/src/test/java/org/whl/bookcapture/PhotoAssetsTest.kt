package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

class PhotoAssetsTest {

    private val sourceHash = "a".repeat(64)
    private val displayHash = "b".repeat(64)

    @Test
    fun unrelatedEntriesDoNotShareOneProcessWidePhotoContractLock() {
        val source = File("src/main/java/org/whl/bookcapture/PhotoAssets.kt").readText()

        assertTrue(source.contains("private val entryMonitors = Array(32) { Any() }"))
        assertTrue(source.contains("private fun monitorFor(dir: File): Any"))
        assertTrue(source.contains("synchronized(monitorFor(dir))"))
        assertFalse(source.contains("private val monitor = Any()"))
        assertFalse(source.contains("synchronized(monitor)"))
    }

    private fun asset(
        id: String,
        order: Int,
        role: PhotoRole = PhotoRole.OTHER,
        revision: Int = 1,
        derivativeHash: String = displayHash,
        geometries: List<PhotoOcrGeometry> = emptyList(),
        assignment: PhotoRoleAssignment = PhotoRoleAssignment(
            suggestedRole = role,
            confidence = if (role == PhotoRole.OTHER) 0.0 else 0.75,
            reason = "test evidence",
            algorithm = "test",
            algorithmVersion = "1",
        ),
    ) = CapturePhotoAsset(
        assetId = id,
        captureOrder = order,
        captureFile = "photo_$order.jpg",
        original = PhotoOriginal(
            reference = "original_$id.jpg",
            sha256 = sourceHash,
            revision = 1,
            width = 3000,
            height = 4000,
            orientationDegrees = 90,
        ),
        display = PhotoDisplayDerivative(
            reference = "photo_$order.jpg",
            sha256 = derivativeHash,
            revision = revision,
            width = 1500,
            height = 2000,
            orientationDegrees = 0,
            recipe = "android-standardize",
            recipeVersion = "1",
            sourceToDisplayHomography = listOf(
                0.5, 0.0, 0.0,
                0.0, 0.5, 0.0,
                0.0, 0.0, 1.0,
            ),
        ),
        lifecycle = PhotoLifecycleState(
            state = PhotoAssetLifecycle.COMPLETED,
            jobId = "job-$id",
            updatedAt = 100L + revision,
        ),
        role = assignment,
        geometries = geometries,
    )

    private fun geometry(
        assetId: String,
        revision: Int,
        hash: String = sourceHash,
    ) = PhotoOcrGeometry(
        assetId = assetId,
        sourceSha256 = hash,
        sourceRevision = 1,
        displayRevision = revision,
        coordinateSpace = "display_normalized",
        width = 1500,
        height = 2000,
        orientationDegrees = 0,
        engine = "mistral",
        model = "mistral-ocr-latest",
        engineVersion = "ocr-4-blocks",
        regions = listOf(
            PhotoOcrRegion(
                id = "heading-1",
                regionType = "text",
                polygon = listOf(
                    NormalizedPoint(0.1, 0.2),
                    NormalizedPoint(0.9, 0.2),
                    NormalizedPoint(0.9, 0.3),
                    NormalizedPoint(0.1, 0.3),
                ),
                text = "A Flora",
                confidence = 0.97,
            ),
        ),
    )

    private fun processingRequest(
        source: CapturePhotoAsset,
        revision: Int = 1,
        profile: PostProcessingProfile = resolvePostProcessingProfile(
            PostProcessingPreset.AUTOMATIC_BY_DATE,
            1890,
        ),
        operations: List<PhotoProcessingOperation> =
            processingOperationsFor(profile, source.role.effectiveRole),
    ) = PhotoProcessingRequest(
        requestId = "request-$revision",
        requestRevision = revision,
        profile = profile,
        requestedAt = 1_000L + revision,
        sourceAssetId = source.assetId,
        sourceRole = source.role.effectiveRole,
        sourceOriginalSha256 = source.original.sha256,
        sourceOriginalRevision = source.original.revision,
        sourceDisplaySha256 = source.display.sha256,
        sourceDisplayRevision = source.display.revision,
        operations = operations,
    )

    @Test
    fun versionedContractRoundTripsAllAssetFacts() {
        val source = asset(
            id = "asset-1",
            order = 1,
            role = PhotoRole.TITLE_PAGE,
            revision = 2,
            geometries = listOf(geometry("asset-1", 2)),
            assignment = PhotoRoleAssignment(
                suggestedRole = PhotoRole.TITLE_PAGE,
                confidence = 0.89,
                reason = "Matched title, author, and year",
                algorithm = "android-bibliographic-title-page",
                algorithmVersion = "1",
                manualOverride = PhotoRole.COVER,
                manualRevision = 3,
                manualUpdatedAt = 55L,
            ),
        )
        val first = source.copy(processingRequest = processingRequest(source))
        val contract = CapturePhotoAssets(
            captureId = "capture-1",
            assets = listOf(first),
            selections = CapturePhotoSelections(
                primaryTitle = PhotoSelectionChoice("asset-1", manual = true, revision = 2, updatedAt = 70L),
                thumbnail = PhotoSelectionChoice("asset-1", manual = false, revision = 1, updatedAt = 60L),
            ),
        )

        assertEquals(contract, capturePhotoAssetsFromJson(contract.toJson(), "capture-1"))
        assertEquals(PHOTO_ASSETS_SCHEMA, contract.toJson().getString("schema"))
        assertEquals(PHOTO_ASSETS_VERSION, contract.toJson().getInt("version"))
    }

    @Test
    fun futureVersionsAndWrongCaptureIdsAreRejected() {
        val json = CapturePhotoAssets("capture-1", listOf(asset("asset-1", 1))).toJson()

        assertNull(capturePhotoAssetsFromJson(JSONObject(json.toString()).put("version", 2)))
        assertNull(capturePhotoAssetsFromJson(json, "capture-2"))
    }

    @Test
    fun malformedAssetsRejectTheWholeContract() {
        val json = CapturePhotoAssets("capture-1", listOf(asset("asset-1", 1))).toJson()
        json.getJSONArray("assets").put(JSONObject()
            .put("asset_id", "unsafe/asset")
            .put("capture_order", 2)
            .put("capture_file", "photo_2.jpg"))

        assertNull(capturePhotoAssetsFromJson(json, "capture-1"))
    }

    @Test
    fun legacyFallbackIsStableExplicitAndOrdered() {
        val dir = Files.createTempDirectory("photo-assets-legacy-").toFile()
        try {
            File(dir, "photo_2.jpg").writeText("second")
            File(dir, "photo_1.jpg").writeText("first")

            val firstRead = PhotoAssetStore.read(dir)
            val secondRead = PhotoAssetStore.read(dir)

            assertTrue(firstRead.legacyFallback)
            assertEquals(listOf(1, 2), firstRead.orderedAssets().map { it.captureOrder })
            assertEquals(firstRead.assets.map { it.assetId }, secondRead.assets.map { it.assetId })
            assertTrue(firstRead.assets.all { it.role.suggestedRole == PhotoRole.OTHER })
            assertTrue(firstRead.assets.all { it.role.confidence == 0.0 })
            assertTrue(firstRead.assets.all { it.role.algorithm == "legacy-fallback" })
            assertEquals("photo_1.jpg", PhotoAssetStore.detailHero(dir)?.displayFile?.name)
            assertEquals("photo_1.jpg", PhotoAssetStore.thumbnail(dir)?.displayFile?.name)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun missingDeclaredDisplaySuppressesRevisionBoundGeometryOnCaptureFallback() {
        val dir = Files.createTempDirectory("photo-assets-display-fallback-").toFile()
        try {
            File(dir, "photo_1.jpg").writeText("capture-pixels")
            val corrected = asset(
                id = "asset-1",
                order = 1,
                revision = 2,
                geometries = listOf(geometry("asset-1", 2)),
            ).copy(display = asset("asset-1", 1, revision = 2).display.copy(
                reference = "corrected_1.jpg",
            ))
            File(dir, PHOTO_ASSETS_FILE).writeText(
                CapturePhotoAssets(dir.name, listOf(corrected)).toJson().toString(),
            )

            val descriptor = PhotoAssetStore.descriptors(dir).single()

            assertEquals("photo_1.jpg", descriptor.displayFile.name)
            assertTrue(descriptor.geometry.isEmpty())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun captureRegistrationPreservesAnImmutableRawReference() {
        val dir = Files.createTempDirectory("photo-assets-raw-").toFile()
        try {
            val photo = File(dir, "photo_1.jpg").apply { writeText("raw-camera-bytes") }
            val registered = PhotoAssetStore.registerCapturedPhoto(dir, photo, 1)
            val original = File(dir, registered.assets.single().original.reference)

            assertTrue(original.isFile)
            assertNotEquals(photo.name, original.name)
            assertEquals(PhotoAssetLifecycle.CAPTURED, registered.assets.single().lifecycle.state)
            assertEquals("android-capture", registered.assets.single().role.algorithm)
            val replacement = File(dir, "photo_1.jpg.replacement").apply { writeText("display-bytes") }
            Files.move(replacement.toPath(), photo.toPath(), StandardCopyOption.REPLACE_EXISTING)

            assertEquals("raw-camera-bytes", original.readText())
            assertEquals("display-bytes", photo.readText())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun processingRequestSnapshotsEveryOutcomeWithoutFakingCompletion() {
        val dir = Files.createTempDirectory("photo-assets-request-").toFile()
        try {
            val photo = File(dir, "photo_1.jpg").apply { writeText("camera-source") }
            val registered = PhotoAssetStore.registerCapturedPhoto(dir, photo, 1)
            val assetId = registered.assets.single().assetId
            assertTrue(PhotoAssetStore.setManualRole(dir, assetId, PhotoRole.TITLE_PAGE))
            val profile = resolvePostProcessingProfile(
                PostProcessingPreset.AUTOMATIC_BY_DATE,
                1890,
            )
            val operations = processingOperationsFor(profile, PhotoRole.TITLE_PAGE)

            PhotoAssetStore.completeForUpload(dir, listOf(photo))
            assertEquals(
                PhotoAssetLifecycle.CAPTURED,
                PhotoAssetStore.read(dir).assets.single().lifecycle.state,
            )
            assertTrue(PhotoAssetStore.requestProcessing(
                dir,
                assetId,
                profile,
            ))
            val firstRequest = PhotoAssetStore.read(dir).assets.single().processingRequest
            assertTrue(PhotoAssetStore.requestProcessing(dir, assetId, profile))
            assertEquals(
                firstRequest,
                PhotoAssetStore.read(dir).assets.single().processingRequest,
            )

            val contract = PhotoAssetStore.read(dir)
            val planned = contract.assets.single()
            assertNotNull(planned.processingRequest)
            val request = planned.processingRequest!!
            assertEquals(PhotoAssetLifecycle.CAPTURED, planned.lifecycle.state)
            assertEquals(operations, request.operations)
            assertEquals(planned.original.sha256, request.sourceOriginalSha256)
            assertEquals(planned.display.sha256, request.sourceDisplaySha256)
            val requestJson = contract.toJson().getJSONArray("assets")
                .getJSONObject(0).getJSONObject("processing_request")
            assertEquals("requested", requestJson.getString("status"))
            assertTrue(requestJson.isNull("result"))
            val profileJson = requestJson.getJSONObject("profile")
            assertEquals("automatic_by_date", profileJson.getString("selected_preset"))
            assertEquals("older", profileJson.getString("resolved_treatment"))
            assertEquals(1890, profileJson.getInt("publication_year"))
            assertEquals(70, profileJson.getInt("page_dewarp_strength_percent"))
            assertEquals(4, profileJson.getInt("detected_margin_padding_percent"))
            assertEquals(50, profileJson.getInt("contrast_strength_percent"))
            assertEquals(75, profileJson.getInt("paper_tone_retention_percent"))
            assertEquals(
                setOf(
                    PhotoProcessingOutcome.PAGE_DEWARP,
                    PhotoProcessingOutcome.DETECTED_MARGIN_CROP,
                    PhotoProcessingOutcome.CONTRAST_NORMALIZATION,
                    PhotoProcessingOutcome.SPINE_CROP,
                ),
                (operations + processingOperationsFor(profile, PhotoRole.SPINE))
                    .map { it.outcome }.toSet(),
            )
            assertEquals(contract, capturePhotoAssetsFromJson(contract.toJson(), dir.name))

            val disabledProfile = resolvePostProcessingProfile(
                PostProcessingPreset.AUTOMATIC_BY_DATE,
                1890,
                PostProcessingFeatures(
                    dewarpPerspectiveAndPageCurvature = false,
                    cropToDetectedPageMargins = false,
                    normalizePageAndTextContrast = false,
                    detectAndCropSpine = false,
                ),
            )
            assertTrue(PhotoAssetStore.requestProcessing(dir, assetId, disabledProfile))
            val disabled = PhotoAssetStore.read(dir).assets.single().processingRequest!!
            assertEquals(request.requestRevision + 1, disabled.requestRevision)
            assertTrue(disabled.operations.isEmpty())
            assertEquals(
                "disabled",
                PhotoAssetStore.read(dir).toJson().getJSONArray("assets")
                    .getJSONObject(0).getJSONObject("processing_request").getString("status"),
            )
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun malformedProcessingRecipesRejectTheWholeContract() {
        val source = asset("asset-1", 1, role = PhotoRole.TITLE_PAGE)
        val contract = CapturePhotoAssets(
            "capture-1",
            listOf(source.copy(processingRequest = processingRequest(source))),
        )

        val wrongLineage = JSONObject(contract.toJson().toString())
        requestJsonFrom(wrongLineage).getJSONObject("source")
            .put("original_sha256", "c".repeat(64))
        assertNull(capturePhotoAssetsFromJson(wrongLineage, "capture-1"))

        val duplicateOutcome = JSONObject(contract.toJson().toString())
        val operations = requestJsonFrom(duplicateOutcome).getJSONArray("operations")
        operations.put(JSONObject(operations.getJSONObject(0).toString()))
        assertNull(capturePhotoAssetsFromJson(duplicateOutcome, "capture-1"))

        val unlinkedSpine = JSONObject(contract.toJson().toString())
        requestJsonFrom(unlinkedSpine).getJSONArray("operations")
            .getJSONObject(0)
            .put("outcome", PhotoProcessingOutcome.SPINE_CROP.wireValue)
            .put("result_role", JSONObject.NULL)
        assertNull(capturePhotoAssetsFromJson(unlinkedSpine, "capture-1"))

        val inventedResult = JSONObject(contract.toJson().toString())
        requestJsonFrom(inventedResult).put("result", JSONObject().put("state", "completed"))
        assertNull(capturePhotoAssetsFromJson(inventedResult, "capture-1"))

        val inconsistentProfile = JSONObject(contract.toJson().toString())
        requestJsonFrom(inconsistentProfile).getJSONObject("profile")
            .put("contrast_strength_percent", 99)
        assertNull(capturePhotoAssetsFromJson(inconsistentProfile, "capture-1"))
    }

    @Test
    fun processingRequestMergeIsMonotonicAndLineageBound() {
        val source = asset("asset-1", 1, role = PhotoRole.TITLE_PAGE)
        val localRequest = processingRequest(
            source,
            revision = 2,
            profile = resolvePostProcessingProfile(PostProcessingPreset.OLDER_1850_TO_1949, 1890),
        )
        val local = CapturePhotoAssets(
            "capture-1",
            listOf(source.copy(processingRequest = localRequest)),
        )
        fun incoming(request: PhotoProcessingRequest) = CapturePhotoAssets(
            "capture-1",
            listOf(source.copy(processingRequest = request)),
        )

        val stale = processingRequest(source, revision = 1)
        assertEquals(
            localRequest,
            mergePhotoAssetContracts(local, incoming(stale)).assets.single().processingRequest,
        )

        val sameRevisionCollision = localRequest.copy(
            requestId = "conflicting-request",
            profile = resolvePostProcessingProfile(PostProcessingPreset.MODERN_1950_AND_LATER, 1890),
            operations = processingOperationsFor(
                resolvePostProcessingProfile(PostProcessingPreset.MODERN_1950_AND_LATER, 1890),
                PhotoRole.TITLE_PAGE,
            ),
        )
        assertEquals(
            localRequest,
            mergePhotoAssetContracts(local, incoming(sameRevisionCollision))
                .assets.single().processingRequest,
        )

        val newer = processingRequest(
            source,
            revision = 3,
            profile = resolvePostProcessingProfile(PostProcessingPreset.EARLY_BEFORE_1850, 1890),
        )
        assertEquals(
            newer,
            mergePhotoAssetContracts(local, incoming(newer)).assets.single().processingRequest,
        )

        val wrongLineage = newer.copy(sourceOriginalSha256 = "c".repeat(64))
        assertEquals(
            localRequest,
            mergePhotoAssetContracts(local, incoming(wrongLineage)).assets.single().processingRequest,
        )
    }

    @Test
    fun primaryTitleAndThumbnailResolveIndependently() {
        val cover = asset("cover", 1, PhotoRole.COVER)
        val title = asset("title", 2, PhotoRole.TITLE_PAGE)
        val automatic = CapturePhotoAssets("capture-1", listOf(title, cover))

        assertEquals("title", automatic.resolvedPrimaryTitleAsset()?.assetId)
        assertEquals("cover", automatic.resolvedThumbnailAsset()?.assetId)

        val manuallyInverted = automatic.copy(
            selections = CapturePhotoSelections(
                primaryTitle = PhotoSelectionChoice("cover", manual = true, revision = 1),
                thumbnail = PhotoSelectionChoice("title", manual = true, revision = 1),
            ),
        )
        assertEquals("cover", manuallyInverted.resolvedPrimaryTitleAsset()?.assetId)
        assertEquals("title", manuallyInverted.resolvedThumbnailAsset()?.assetId)
    }

    @Test
    fun bibliographicEvidenceUsesDeterministicScoreOrderAndIdRanking() {
        val metadata = JSONObject()
            .put("title", "A Flora of California")
            .put("author", "Jane Botanist")
            .put("year", "1897")
        assertEquals(
            listOf("title", "author", "year"),
            matchedBibliographicFields(
                "A FLORA OF CALIFORNIA — Jane Botanist, MDCCCLXXXXVII 1897",
                metadata,
            ),
        )

        val ranked = rankTitlePageEvidence(listOf(
            BibliographicPhotoEvidence("later", 2, listOf("title", "author")),
            BibliographicPhotoEvidence("b", 1, listOf("title", "author")),
            BibliographicPhotoEvidence("a", 1, listOf("title", "author")),
            BibliographicPhotoEvidence("most-fields", 9, listOf("title", "author", "year")),
        ))
        assertEquals(listOf("most-fields", "a", "b", "later"), ranked.map { it.assetId })
    }

    @Test
    fun bibliographicAndAspectEvidenceSuggestTitleCoverAndSpine() {
        val dir = Files.createTempDirectory("photo-assets-roles-").toFile()
        try {
            val title = asset("title", 2)
            val cover = asset("cover", 1)
            val spine = asset("spine", 3).copy(display = asset("spine", 3).display.copy(
                width = 300,
                height = 1800,
            ))
            listOf(title, cover, spine).forEach { File(dir, it.captureFile).writeText("photo") }
            File(dir, "${title.captureFile}.txt")
                .writeText("A Flora of California Jane Botanist 1897")
            File(dir, "${cover.captureFile}.txt")
                .writeText("A Flora of California Jane Botanist")
            File(dir, "${spine.captureFile}.txt").writeText("A Flora of California")
            File(dir, PHOTO_ASSETS_FILE).writeText(
                CapturePhotoAssets(dir.name, listOf(cover, title, spine)).toJson().toString(),
            )

            assertTrue(PhotoAssetStore.applyBibliographicSuggestions(
                dir,
                JSONObject()
                    .put("title", "A Flora of California")
                    .put("author", "Jane Botanist")
                    .put("year", "1897"),
            ))

            val updated = PhotoAssetStore.read(dir)
            assertEquals(PhotoRole.TITLE_PAGE, updated.assets.single { it.assetId == "title" }.role.suggestedRole)
            assertEquals(PhotoRole.COVER, updated.assets.single { it.assetId == "cover" }.role.suggestedRole)
            assertEquals(PhotoRole.SPINE, updated.assets.single { it.assetId == "spine" }.role.suggestedRole)
            assertEquals("title", updated.resolvedPrimaryTitleAsset()?.assetId)
            assertEquals("cover", updated.resolvedThumbnailAsset()?.assetId)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun staleOrMismatchedDerivativesCannotReplaceLocalState() {
        val localGeometry = geometry("asset-1", 2)
        val localRole = PhotoRoleAssignment(
            suggestedRole = PhotoRole.OTHER,
            manualOverride = PhotoRole.COVER,
            manualRevision = 5,
            manualUpdatedAt = 500L,
        )
        val localAsset = asset(
            "asset-1", 1, revision = 2, derivativeHash = displayHash,
            geometries = listOf(localGeometry), assignment = localRole,
        )
        val local = CapturePhotoAssets("capture-1", listOf(localAsset))
        val stale = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 1, derivativeHash = "c".repeat(64),
            geometries = listOf(geometry("asset-1", 1)),
        )))
        val collision = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 2, derivativeHash = "d".repeat(64),
            geometries = listOf(geometry("asset-1", 2)),
        )))

        assertEquals(local, mergePhotoAssetContracts(local, stale))
        assertEquals(local, mergePhotoAssetContracts(local, collision))

        val wrongSourceAsset = asset(
            "asset-1", 1, revision = 3, derivativeHash = "e".repeat(64),
        ).copy(original = localAsset.original.copy(sha256 = "f".repeat(64)))
        assertEquals(local, mergePhotoAssetContracts(
            local,
            CapturePhotoAssets("capture-1", listOf(wrongSourceAsset)),
        ))
    }

    @Test
    fun newerDerivativeMergeIsIdempotentAndPreservesManualRole() {
        val localRole = PhotoRoleAssignment(
            suggestedRole = PhotoRole.OTHER,
            manualOverride = PhotoRole.COVER,
            manualRevision = 5,
            manualUpdatedAt = 500L,
        )
        val local = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 2, assignment = localRole,
        )))
        val incomingRole = PhotoRoleAssignment(
            suggestedRole = PhotoRole.TITLE_PAGE,
            confidence = 0.89,
            reason = "three field matches",
            algorithm = "server-title-page",
            algorithmVersion = "2",
            manualRevision = 1,
        )
        val incoming = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 3, derivativeHash = "c".repeat(64),
            geometries = listOf(geometry("asset-1", 3)), assignment = incomingRole,
        )))

        val once = mergePhotoAssetContracts(local, incoming)
        val twice = mergePhotoAssetContracts(once, incoming)

        assertEquals(3, once.assets.single().display.revision)
        assertEquals(PhotoRole.TITLE_PAGE, once.assets.single().role.suggestedRole)
        assertEquals(PhotoRole.COVER, once.assets.single().role.effectiveRole)
        assertEquals(listOf(3), once.assets.single().geometries.map { it.displayRevision })
        assertEquals(once, twice)
    }

    @Test
    fun correctedDerivativeHomographyCarriesOcrGeometryForward() {
        val localAsset = asset(
            "asset-1",
            1,
            revision = 2,
            geometries = listOf(geometry("asset-1", 2)),
        )
        val local = CapturePhotoAssets("capture-1", listOf(localAsset))
        val correctedDisplay = asset(
            "asset-1",
            1,
            revision = 3,
            derivativeHash = "c".repeat(64),
        ).display.copy(
            width = 1200,
            height = 1800,
            sourceToDisplayHomography = listOf(
                0.8, 0.0, 0.1,
                0.0, 0.8, 0.05,
                0.0, 0.0, 1.0,
            ),
        )
        val incomingAsset = asset(
            "asset-1",
            1,
            revision = 3,
            derivativeHash = "c".repeat(64),
        ).copy(display = correctedDisplay, geometries = emptyList())
        val incoming = CapturePhotoAssets("capture-1", listOf(incomingAsset))

        val once = mergePhotoAssetContracts(local, incoming)
        val twice = mergePhotoAssetContracts(once, incoming)
        val migrated = once.assets.single().geometries.single { it.displayRevision == 3 }

        assertEquals(1200, migrated.width)
        assertEquals(1800, migrated.height)
        val expected = listOf(
            NormalizedPoint(0.18, 0.21),
            NormalizedPoint(0.82, 0.21),
            NormalizedPoint(0.82, 0.29),
            NormalizedPoint(0.18, 0.29),
        )
        expected.zip(migrated.regions.single().polygon).forEach { (wanted, actual) ->
            assertEquals(wanted.x, actual.x, 1e-9)
            assertEquals(wanted.y, actual.y, 1e-9)
        }
        assertEquals(once, twice)
    }

    @Test
    fun transformedGeometryIsClippedToCorrectedPhotoBounds() {
        val source = asset(
            "asset-1",
            1,
            revision = 2,
            geometries = listOf(geometry("asset-1", 2)),
        )
        val target = source.display.copy(
            revision = 3,
            sha256 = "c".repeat(64),
            sourceToDisplayHomography = listOf(
                1.4, 0.0, -0.2,
                0.0, 1.0, 0.0,
                0.0, 0.0, 1.0,
            ),
        )

        val transformed = transformGeometryForDisplay(
            source.geometries.single(),
            source.original,
            source.display,
            target,
        )

        assertNotNull(transformed)
        assertTrue(transformed!!.regions.single().polygon.all { point ->
            point.x in 0.0..1.0 && point.y in 0.0..1.0
        })
        assertTrue(transformed.regions.single().polygon.any { it.x == 0.0 })
        assertTrue(transformed.regions.single().polygon.any { it.x == 1.0 })
    }

    @Test
    fun cleanupRequestIsPendingOnlyForItsExactVisiblePixels() {
        val source = asset("title", 1, role = PhotoRole.TITLE_PAGE)
        val pending = source.copy(processingRequest = processingRequest(source))
        val disabled = source.copy(processingRequest = processingRequest(source, operations = emptyList()))
        val corrected = pending.copy(display = pending.display.copy(
            revision = pending.display.revision + 1,
            sha256 = "c".repeat(64),
        ))

        assertTrue(photoPostProcessingPending(pending))
        assertFalse(photoPostProcessingPending(disabled))
        assertFalse(photoPostProcessingPending(corrected))
    }

    @Test
    fun sameRevisionReOcrReplacesGeometryFromTheSameProvider() {
        val oldGeometry = geometry("asset-1", 2)
        val newGeometry = oldGeometry.copy(regions = oldGeometry.regions.map {
            it.copy(text = "Updated OCR")
        })
        val local = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 2, geometries = listOf(oldGeometry),
        )))
        val incoming = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 2, geometries = listOf(newGeometry),
        )))

        val merged = mergePhotoAssetContracts(local, incoming)

        assertEquals(1, merged.assets.single().geometries.size)
        assertEquals("Updated OCR", merged.assets.single().geometries.single().regions.single().text)
    }

    @Test
    fun emptyOrMisboundsIncomingGeometryCannotEraseVisibleGeometry() {
        val visible = geometry("asset-1", 2)
        val local = CapturePhotoAssets("capture-1", listOf(asset(
            "asset-1", 1, revision = 2, geometries = listOf(visible),
        )))
        val empty = visible.copy(regions = emptyList())
        val wrongDimensions = visible.copy(width = 999)

        for (invalid in listOf(empty, wrongDimensions)) {
            val incoming = CapturePhotoAssets("capture-1", listOf(asset(
                "asset-1", 1, revision = 2, geometries = listOf(invalid),
            )))
            assertEquals(
                listOf(visible),
                mergePhotoAssetContracts(local, incoming).assets.single().geometries,
            )
        }
    }

    @Test
    fun futureEmbeddedManifestIsNeverReplacedByAV1Sidecar() {
        val dir = Files.createTempDirectory("photo-assets-future-").toFile()
        try {
            val photo = File(dir, "photo_1.jpg").apply { writeText("camera") }
            val future = CapturePhotoAssets(dir.name, listOf(asset("asset-1", 1)))
                .toJson().put("version", 2)
            val manifest = File(dir, "manifest.json").apply {
                writeText(JSONObject().put(PHOTO_ASSETS_MANIFEST_KEY, future).toString())
            }

            PhotoAssetStore.registerCapturedPhoto(dir, photo, 1)

            assertFalse(File(dir, PHOTO_ASSETS_FILE).exists())
            assertEquals(
                2,
                JSONObject(manifest.readText())
                    .getJSONObject(PHOTO_ASSETS_MANIFEST_KEY)
                    .getInt("version"),
            )
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun futureEmbeddedManifestIsProtectedEvenWhenAV1SidecarExists() {
        val dir = Files.createTempDirectory("photo-assets-mixed-future-").toFile()
        try {
            val current = CapturePhotoAssets(dir.name, listOf(asset("asset-1", 1)))
            val sidecar = File(dir, PHOTO_ASSETS_FILE).apply {
                writeText(current.toJson().toString())
            }
            val manifest = File(dir, "manifest.json").apply {
                writeText(JSONObject().put(
                    PHOTO_ASSETS_MANIFEST_KEY,
                    current.toJson().put("version", 2),
                ).toString())
            }

            assertFalse(PhotoAssetStore.setManualRole(dir, "asset-1", PhotoRole.COVER))
            assertEquals(current, capturePhotoAssetsFromJson(JSONObject(sidecar.readText()), dir.name))
            assertEquals(
                2,
                JSONObject(manifest.readText())
                    .getJSONObject(PHOTO_ASSETS_MANIFEST_KEY)
                    .getInt("version"),
            )
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun ocrBlocksNormalizeToDisplayCoordinatesAndLegacyResponsesRemainValid() {
        val block = JSONObject()
            .put("id", "block-1")
            .put("type", "text")
            .put("top_left_x", 100)
            .put("top_left_y", 400)
            .put("bottom_right_x", 900)
            .put("bottom_right_y", 800)
            .put("content", "A Flora")
            .put("confidence", 1.2)
        val current = JSONObject()
            .put("model", "mistral-ocr-latest")
            .put("pages", JSONArray().put(JSONObject()
                .put("markdown", "# A Flora")
                .put("dimensions", JSONObject().put("width", 1000).put("height", 2000))
                .put("blocks", JSONArray().put(block))))

        val parsed = Pipeline.parseOcrResponse(current)
        assertEquals("# A Flora", parsed.markdown)
        assertEquals(current.toString(), parsed.providerResponse)
        assertNotNull(parsed.geometry)
        val draft = parsed.geometry!!
        assertEquals("display_normalized", draft.coordinateSpace)
        assertEquals(1000, draft.width)
        assertEquals(2000, draft.height)
        assertEquals(
            listOf(
                NormalizedPoint(0.1, 0.2),
                NormalizedPoint(0.9, 0.2),
                NormalizedPoint(0.9, 0.4),
                NormalizedPoint(0.1, 0.4),
            ),
            draft.regions.single().polygon,
        )
        assertEquals(1.0, draft.regions.single().confidence!!, 0.0)

        val legacy = Pipeline.parseOcrResponse(JSONObject().put(
            "pages",
            JSONArray().put(JSONObject().put("markdown", "legacy markdown")),
        ))
        assertEquals("legacy markdown", legacy.markdown)
        assertNull(legacy.geometry)
        assertEquals(
            "legacy markdown",
            JSONObject(legacy.providerResponse).getJSONArray("pages")
                .getJSONObject(0).getString("markdown"),
        )
    }

    @Test
    fun geometryLessOcrNeverErasesExistingSidecarGeometry() {
        val dir = Files.createTempDirectory("photo-assets-geometry-").toFile()
        try {
            val photo = File(dir, "photo_1.jpg").apply { writeText("display") }
            val contract = CapturePhotoAssets("${dir.name}", listOf(asset(
                "asset-1", 1, revision = 2, geometries = listOf(geometry("asset-1", 2)),
            )))
            val sidecar = File(dir, PHOTO_ASSETS_FILE).apply { writeText(contract.toJson().toString()) }
            val before = sidecar.readText()

            assertFalse(PhotoAssetStore.mergeGeometry(dir, photo, null))
            assertEquals(before, sidecar.readText())
            assertEquals(1, PhotoAssetStore.read(dir).assets.single().geometries.size)
        } finally {
            dir.deleteRecursively()
        }
    }

    private fun requestJsonFrom(contract: JSONObject): JSONObject = contract
        .getJSONArray("assets").getJSONObject(0).getJSONObject("processing_request")
}
