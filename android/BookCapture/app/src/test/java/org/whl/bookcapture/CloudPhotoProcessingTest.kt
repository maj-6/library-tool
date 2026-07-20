package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest

class CloudPhotoProcessingTest {
    private val originalHash = "a".repeat(64)
    private val displayHash = "b".repeat(64)
    private val ownerId = "owner-1"

    private fun localContract(captureId: String = "capture-1"): CapturePhotoAssets {
        val profile = resolvePostProcessingProfile(
            PostProcessingPreset.AUTOMATIC_BY_DATE,
            1890,
        )
        val original = PhotoOriginal(
            reference = "original_asset-1.jpg",
            sha256 = originalHash,
            revision = 1,
            width = 100,
            height = 200,
        )
        val display = PhotoDisplayDerivative(
            reference = "photo_1.jpg",
            sha256 = displayHash,
            revision = 1,
            width = 100,
            height = 200,
        )
        val request = PhotoProcessingRequest(
            requestId = "request-1",
            requestRevision = 1,
            profile = profile,
            requestedAt = 1_000L,
            sourceAssetId = "asset-1",
            sourceRole = PhotoRole.TITLE_PAGE,
            sourceOriginalSha256 = originalHash,
            sourceOriginalRevision = 1,
            sourceDisplaySha256 = displayHash,
            sourceDisplayRevision = 1,
            operations = processingOperationsFor(profile, PhotoRole.TITLE_PAGE),
        )
        val geometry = PhotoOcrGeometry(
            assetId = "asset-1",
            sourceSha256 = originalHash,
            sourceRevision = 1,
            displayRevision = 1,
            coordinateSpace = "display_normalized",
            width = 100,
            height = 200,
            orientationDegrees = 0,
            engine = "mistral",
            model = "mistral-ocr-latest",
            regions = listOf(PhotoOcrRegion(
                id = "title",
                regionType = "title",
                polygon = listOf(
                    NormalizedPoint(.1, .1),
                    NormalizedPoint(.9, .1),
                    NormalizedPoint(.9, .2),
                    NormalizedPoint(.1, .2),
                ),
                text = "A Flora",
            )),
        )
        return CapturePhotoAssets(captureId, listOf(CapturePhotoAsset(
            assetId = "asset-1",
            captureOrder = 1,
            captureFile = "photo_1.jpg",
            original = original,
            display = display,
            lifecycle = PhotoLifecycleState(PhotoAssetLifecycle.CAPTURED),
            role = PhotoRoleAssignment(
                suggestedRole = PhotoRole.TITLE_PAGE,
                confidence = .9,
                reason = "test",
                algorithm = "test",
            ),
            geometries = listOf(geometry),
            processingRequest = request,
        )))
    }

    private fun artifact(
        hash: String = "c".repeat(64),
        bytes: Long = 123L,
        width: Int = 12,
        height: Int = 18,
    ) = CloudDisplayArtifact(
        bucket = CLOUD_DERIVATIVE_BUCKET,
        path = "$ownerId/capture-1/asset-1/r1-request-1/display-${hash.take(20)}.jpg",
        sha256 = hash,
        bytes = bytes,
        mime = "image/jpeg",
        width = width,
        height = height,
    )

    private fun completedJob(
        artifact: CloudDisplayArtifact = artifact(),
        homography: List<Double>? = listOf(
            1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
            0.0, 0.0, 1.0,
        ),
    ): CloudPhotoProcessingJob {
        val result = JSONObject()
            .put("schema", CLOUD_PHOTO_RESULT_SCHEMA)
            .put("version", CLOUD_PHOTO_RESULT_VERSION)
            .put("capture_id", "capture-1")
            .put("asset_id", "asset-1")
            .put("request_id", "request-1")
            .put("request_revision", 1)
            .put("derived_from", JSONObject()
                .put("original_sha256", originalHash)
                .put("original_revision", 1)
                .put("display_sha256", displayHash)
                .put("display_revision", 1))
            .put("processor", JSONObject()
                .put("name", "whl-image-processor")
                .put("version", "0.1.0"))
            .put("artifacts", JSONObject().put("display", JSONObject()
                .put("bucket", artifact.bucket)
                .put("path", artifact.path)
                .put("sha256", artifact.sha256)
                .put("bytes", artifact.bytes)
                .put("mime", artifact.mime)
                .put("width", artifact.width)
                .put("height", artifact.height)))
            .put("display", JSONObject()
                .put("target_revision", 2)
                .put("recipe", "whl-cloud-book-cleanup")
                .put("recipe_version", "0.1.0")
                .put("merge_base", JSONObject()
                    .put("sha256", displayHash)
                    .put("revision", 1))
                .put(
                    "base_to_output_homography",
                    homography?.let { values ->
                        JSONArray().apply { values.forEach(::put) }
                    } ?: JSONObject.NULL,
                )
                .put(
                    "geometry_strategy",
                    if (homography == null) "replace_and_reocr" else "homography",
                )
                .put("reocr_required", homography == null))
        return CloudPhotoProcessingJob(
            id = "job-1",
            captureId = "capture-1",
            ownerId = ownerId,
            assetId = "asset-1",
            requestId = "request-1",
            requestRevision = 1,
            sourceSha256 = originalHash,
            state = "completed",
            result = result,
            lastError = "",
        )
    }

    @Test
    fun completedResultRequiresExactIdentityLineageMergeBaseAndArtifactPath() {
        val local = localContract()
        assertTrue(validateCloudPhotoResult(
            local,
            completedJob(),
            ownerId,
        ) is CloudResultDecision.Ready)

        val wrongRequest = completedJob().copy(requestRevision = 2)
        assertEquals(
            CloudResultDecision.NotApplicable,
            validateCloudPhotoResult(local, wrongRequest, ownerId),
        )

        fun mutated(block: (JSONObject) -> Unit): CloudResultDecision {
            val job = completedJob()
            val result = JSONObject(checkNotNull(job.result).toString())
            block(result)
            return validateCloudPhotoResult(local, job.copy(result = result), ownerId)
        }
        assertTrue(mutated {
            it.getJSONObject("derived_from").put("original_sha256", "d".repeat(64))
        } is CloudResultDecision.Rejected)
        assertTrue(mutated {
            it.getJSONObject("display").getJSONObject("merge_base").put("revision", 2)
        } is CloudResultDecision.Rejected)
        assertTrue(mutated {
            it.getJSONObject("artifacts").getJSONObject("display")
                .put("path", "$ownerId/capture-1/asset-1/../another.jpg")
        } is CloudResultDecision.Rejected)
        assertTrue(mutated {
            it.getJSONObject("display").put(
                "base_to_output_homography",
                JSONArray(List(9) { 0.0 }),
            )
        } is CloudResultDecision.Rejected)
    }

    @Test
    fun authenticatedDownloadMustMatchMimeSizeJpegChecksumAndDimensions() {
        val dir = Files.createTempDirectory("cloud-download-").toFile()
        try {
            val file = File(dir, "display.jpg").apply { writeBytes(jpeg(12, 18)) }
            val good = artifact(
                hash = sha256(file.readBytes()),
                bytes = file.length(),
                width = 12,
                height = 18,
            )
            assertNull(verifyCloudDisplayDownload(file, good, "image/jpeg", file.length()))
            assertEquals(
                "response MIME",
                verifyCloudDisplayDownload(file, good, "application/octet-stream", file.length()),
            )
            assertEquals(
                "artifact size",
                verifyCloudDisplayDownload(file, good, "image/jpeg", file.length() - 1),
            )
            assertEquals(
                "artifact checksum",
                verifyCloudDisplayDownload(
                    file,
                    good.copy(sha256 = "d".repeat(64)),
                    "image/jpeg",
                    file.length(),
                ),
            )
            assertEquals(
                "JPEG dimensions",
                verifyCloudDisplayDownload(
                    file,
                    good.copy(width = 13),
                    "image/jpeg",
                    file.length(),
                ),
            )
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun verifiedDerivativeInstallsSeparatelyAndHomographyCarriesGeometry() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val job = completedJob(artifact)
            val plan = (validateCloudPhotoResult(
                PhotoAssetStore.read(dir),
                job,
                ownerId,
            ) as CloudResultDecision.Ready).plan
            val downloaded = File(dir, ".verified.part").apply { writeBytes(bytes) }

            assertTrue(PhotoAssetStore.installCloudDisplayDerivative(
                dir,
                plan,
                downloaded,
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))

            val installed = PhotoAssetStore.read(dir).assets.single()
            assertEquals("original_asset-1.jpg", installed.original.reference)
            assertEquals("immutable-camera", File(dir, installed.original.reference).readText())
            assertEquals("local-display", File(dir, installed.captureFile).readText())
            assertTrue(installed.display.reference.startsWith("cloud_asset-1_r2_"))
            assertEquals(bytes.toList(), File(dir, installed.display.reference).readBytes().toList())
            assertEquals(2, installed.display.revision)
            assertEquals(PhotoAssetLifecycle.COMPLETED, installed.lifecycle.state)
            assertEquals("job-1", installed.lifecycle.jobId)
            assertTrue(installed.original.reference != installed.display.reference)
            assertTrue(PhotoAssetStore.hasVerifiedCloudDisplay(dir, plan))
            assertTrue(PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).isEmpty())
            val migrated = installed.geometries.single { it.displayRevision == 2 }
            assertEquals(12, migrated.width)
            assertEquals(18, migrated.height)
            assertEquals(
                localContract().assets.single().geometries.single().regions.single().polygon,
                migrated.regions.single().polygon,
            )
        }
    }

    @Test
    fun nonlinearDerivativeQueuesRevisionPinnedReocrAndAppliesOnlyCorrectedGeometry() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val job = completedJob(artifact, homography = null)
            val plan = (validateCloudPhotoResult(
                PhotoAssetStore.read(dir),
                job,
                ownerId,
            ) as CloudResultDecision.Ready).plan
            val downloaded = File(dir, ".verified.part").apply { writeBytes(bytes) }
            val canonicalOcr = File(dir, "photo_1.jpg.txt").apply {
                writeText("OCR from immutable capture transport")
            }

            assertTrue(PhotoAssetStore.installCloudDisplayDerivative(
                dir,
                plan,
                downloaded,
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))

            val installed = PhotoAssetStore.read(dir).assets.single()
            assertNull(installed.display.sourceToDisplayHomography)
            assertFalse(installed.geometries.any { it.displayRevision == 2 })
            assertTrue(PhotoAssetStore.descriptors(dir).single().geometry.isEmpty())
            val target = PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).single()
            assertEquals("capture-1", target.captureId)
            assertEquals("asset-1", target.assetId)
            assertEquals("job-1", target.jobId)
            assertEquals(2, target.displayRevision)
            assertEquals(artifact.sha256, target.displaySha256)
            assertEquals(
                File(dir, installed.display.reference).canonicalFile,
                PhotoAssetStore.cloudDisplayReocrFile(dir, target)?.canonicalFile,
            )

            val correctedGeometry = OcrGeometryDraft(
                width = 12,
                height = 18,
                engine = "mistral",
                model = "mistral-ocr-latest",
                regions = listOf(PhotoOcrRegion(
                    id = "corrected-title",
                    regionType = "text",
                    polygon = listOf(
                        NormalizedPoint(.2, .2),
                        NormalizedPoint(.8, .2),
                        NormalizedPoint(.8, .3),
                        NormalizedPoint(.2, .3),
                    ),
                    text = "Corrected title",
                )),
            )
            assertTrue(PhotoAssetStore.mergeCloudDisplayReocrGeometry(
                dir,
                target,
                correctedGeometry,
            ))

            val aligned = PhotoAssetStore.read(dir).assets.single()
            assertEquals(listOf(1, 2), aligned.geometries.map { it.displayRevision })
            assertEquals(
                "Corrected title",
                aligned.geometries.single { it.displayRevision == 2 }.regions.single().text,
            )
            assertTrue(PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).isEmpty())
            assertEquals("immutable-camera", File(dir, aligned.original.reference).readText())
            assertEquals("local-display", File(dir, aligned.captureFile).readText())
            assertEquals("OCR from immutable capture transport", canonicalOcr.readText())
        }
    }

    @Test
    fun nonlinearReocrRejectsStaleRevisionAndUsesABoundedRetryBudget() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val job = completedJob(artifact, homography = null)
            val plan = (validateCloudPhotoResult(
                PhotoAssetStore.read(dir),
                job,
                ownerId,
            ) as CloudResultDecision.Ready).plan
            assertTrue(PhotoAssetStore.installCloudDisplayDerivative(
                dir,
                plan,
                File(dir, ".verified.part").apply { writeBytes(bytes) },
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))
            val target = PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).single()
            val draft = OcrGeometryDraft(
                width = 12,
                height = 18,
                engine = "mistral",
                model = "mistral-ocr-latest",
                regions = localContract().assets.single().geometries.single().regions,
            )

            assertFalse(PhotoAssetStore.mergeCloudDisplayReocrGeometry(
                dir,
                target.copy(displayRevision = target.displayRevision + 1),
                draft,
            ))
            assertTrue(PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).isNotEmpty())
            assertTrue(shouldRetryCloudDisplayReocr(0))
            assertTrue(shouldRetryCloudDisplayReocr(1))
            assertFalse(shouldRetryCloudDisplayReocr(2))
        }
    }

    @Test
    fun terminalLifecyclePersistsAndCannotBeResurrectedByAStaleLiveRow() {
        withEntryDir { dir ->
            val base = completedJob().copy(result = null)
            assertTrue(PhotoAssetStore.recordCloudJobState(dir, base.copy(state = "queued")))
            assertEquals(
                PhotoAssetLifecycle.QUEUED,
                PhotoAssetStore.read(dir).assets.single().lifecycle.state,
            )
            assertTrue(PhotoAssetStore.recordCloudJobState(dir, base.copy(
                state = "retrying",
                lastError = " temporary\n backend   error ",
            )))
            assertEquals(
                "temporary backend error",
                PhotoAssetStore.read(dir).assets.single().lifecycle.error,
            )
            assertTrue(PhotoAssetStore.recordCloudJobState(dir, base.copy(
                state = "failed",
                lastError = "input rejected",
            )))
            assertFalse(cloudPhotoWorkPending(PhotoAssetStore.read(dir)))
            assertFalse(PhotoAssetStore.descriptors(dir).single().postProcessingPending)

            assertTrue(PhotoAssetStore.recordCloudJobState(dir, base.copy(state = "queued")))
            assertEquals(
                PhotoAssetLifecycle.FAILED,
                PhotoAssetStore.read(dir).assets.single().lifecycle.state,
            )
        }
    }

    @Test
    fun jobRowsAndAuthenticatedStorageRouteAreWiredIntoImportPolling() {
        val job = completedJob()
        val row = JSONObject()
            .put("id", job.id)
            .put("capture_id", job.captureId)
            .put("owner_id", job.ownerId)
            .put("asset_id", job.assetId)
            .put("request_id", job.requestId)
            .put("request_revision", job.requestRevision)
            .put("source_sha256", job.sourceSha256)
            .put("state", job.state)
            .put("result", job.result)
            .put("last_error", "")
        assertEquals(job.captureId, cloudPhotoProcessingJobFromJson(row)?.captureId)
        assertNull(cloudPhotoProcessingJobFromJson(JSONObject(row.toString())
            .put("request_revision", "1")))
        assertNull(cloudPhotoProcessingJobFromJson(JSONObject(row.toString())
            .put("state", "mystery")))

        val clientSource = File("src/main/java/org/whl/bookcapture/SupabaseClient.kt").readText()
        val workerSource = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()
        val reocrSource = File(
            "src/main/java/org/whl/bookcapture/CloudDisplayReocrWorker.kt",
        ).readText()
        val settingsSource = File("src/main/java/org/whl/bookcapture/SettingsActivity.kt").readText()
        assertTrue(clientSource.contains("/storage/v1/object/authenticated/"))
        assertTrue(clientSource.contains("/rest/v1/photo_processing_jobs"))
        assertTrue(workerSource.contains("client.photoProcessingJobs(sent.map { it.id })"))
        assertTrue(workerSource.indexOf("client.photoProcessingJobs(sent.map { it.id })") <
            workerSource.indexOf("client.captureStatuses(waitingForImport.map { it.id })"))
        assertTrue(workerSource.contains("CloudDisplayReocrWorker.enqueuePending(ctx, entry.id)"))
        assertTrue(settingsSource.contains("CloudDisplayReocrWorker.enqueueAllPending(this)"))
        assertTrue(reocrSource.contains("Pipeline.ocrResult(display, mistralKey)"))
        assertFalse(reocrSource.contains("photo_1.jpg.txt"))
    }

    private fun withEntryDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("cloud-entry-root-").toFile()
        val dir = File(root, "capture-1").apply { mkdirs() }
        try {
            File(dir, "photo_1.jpg").writeText("local-display")
            File(dir, "original_asset-1.jpg").writeText("immutable-camera")
            File(dir, PHOTO_ASSETS_FILE).writeText(localContract().toJson().toString())
            block(dir)
        } finally {
            root.deleteRecursively()
        }
    }

    private fun jpeg(width: Int, height: Int): ByteArray = ByteArrayOutputStream().apply {
        write(byteArrayOf(0xff.toByte(), 0xd8.toByte()))
        write(byteArrayOf(
            0xff.toByte(), 0xc0.toByte(), 0x00, 0x11, 0x08,
            (height ushr 8).toByte(), height.toByte(),
            (width ushr 8).toByte(), width.toByte(),
            0x03,
            0x01, 0x11, 0x00,
            0x02, 0x11, 0x00,
            0x03, 0x11, 0x00,
        ))
        write(byteArrayOf(
            0xff.toByte(), 0xda.toByte(), 0x00, 0x0c, 0x03,
            0x01, 0x00,
            0x02, 0x11,
            0x03, 0x11,
            0x00, 0x3f, 0x00,
            0x00,
            0xff.toByte(), 0xd9.toByte(),
        ))
    }.toByteArray()

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(it) }
}
