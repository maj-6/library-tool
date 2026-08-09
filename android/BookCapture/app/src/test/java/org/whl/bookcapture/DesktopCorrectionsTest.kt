package org.whl.bookcapture

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

class DesktopCorrectionsTest {
    private val originalHash = "a".repeat(64)
    private val displayHash = "b".repeat(64)
    private val ownerId = "owner-1"
    private val correctionA = "a1".repeat(32)
    private val correctionB = "b2".repeat(32)

    private fun localContract(
        appliedCorrectionId: String = "",
    ): CapturePhotoAssets {
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
        return CapturePhotoAssets("capture-1", listOf(CapturePhotoAsset(
            assetId = "asset-1",
            captureOrder = 1,
            captureFile = "photo_1.jpg",
            original = PhotoOriginal(
                reference = "original_asset-1.jpg",
                sha256 = originalHash,
                revision = 1,
                width = 100,
                height = 200,
            ),
            display = PhotoDisplayDerivative(
                reference = "photo_1.jpg",
                sha256 = displayHash,
                revision = 1,
                width = 100,
                height = 200,
            ),
            geometries = listOf(geometry),
            appliedDesktopCorrectionId = appliedCorrectionId,
        )))
    }

    private fun artifact(
        hash: String = "c".repeat(64),
        bytes: Long = 123L,
        width: Int = 12,
        height: Int = 18,
        correctionId: String = correctionA,
    ) = CloudDisplayArtifact(
        bucket = CLOUD_DERIVATIVE_BUCKET,
        path = "$ownerId/capture-1/asset-1/" +
            "desktop-${correctionId.take(20)}/display-${hash.take(20)}.jpg",
        sha256 = hash,
        bytes = bytes,
        mime = "image/jpeg",
        width = width,
        height = height,
    )

    private fun correctionResult(
        correctionId: String = correctionA,
        artifact: CloudDisplayArtifact = artifact(),
    ): JSONObject = JSONObject()
        .put("schema", DESKTOP_CORRECTION_RESULT_SCHEMA)
        .put("version", DESKTOP_CORRECTION_RESULT_VERSION)
        .put("processor", DESKTOP_CORRECTION_PROCESSOR)
        .put("recipe", DESKTOP_CORRECTION_RECIPE)
        .put("correction_id", correctionId)
        .put("source", JSONObject()
            .put("original_sha256", originalHash)
            .put("desktop_display_sha256", "e".repeat(64)))
        .put("geometry_strategy", "replace_and_reocr")
        .put("artifacts", JSONObject().put("display", JSONObject()
            .put("bucket", artifact.bucket)
            .put("path", artifact.path)
            .put("sha256", artifact.sha256)
            .put("bytes", artifact.bytes)
            .put("width", artifact.width)
            .put("height", artifact.height)
            .put("content_type", "image/jpeg")))
        .put("generated_at", "2026-08-01T00:00:00Z")

    private fun correctionRow(
        correctionId: String = correctionA,
        artifact: CloudDisplayArtifact = artifact(),
        result: JSONObject? = correctionResult(correctionId, artifact),
        sourceOriginalSha256: String = originalHash,
    ) = CaptureCorrectionRow(
        captureId = "capture-1",
        assetId = "asset-1",
        ownerId = ownerId,
        correctionId = correctionId,
        sourceOriginalSha256 = sourceOriginalSha256,
        result = result,
        revision = 1,
        updatedAt = "2026-08-01T00:00:00Z",
    )

    @Test
    fun rowParsingRequiresHexFingerprintsAndAPositiveRevision() {
        val row = JSONObject()
            .put("capture_id", "capture-1")
            .put("asset_id", "asset-1")
            .put("owner_id", ownerId)
            .put("correction_id", correctionA)
            .put("source_original_sha256", originalHash)
            .put("result", correctionResult())
            .put("revision", 3L)
            .put("updated_at", "2026-08-01T00:00:00Z")
        val parsed = captureCorrectionRowFromJson(row)!!
        assertEquals("capture-1", parsed.captureId)
        assertEquals(correctionA, parsed.correctionId)
        assertEquals(3L, parsed.revision)

        assertNull(captureCorrectionRowFromJson(JSONObject(row.toString())
            .put("correction_id", "not-hex")))
        assertNull(captureCorrectionRowFromJson(JSONObject(row.toString())
            .put("source_original_sha256", originalHash.take(60))))
        assertNull(captureCorrectionRowFromJson(JSONObject(row.toString())
            .put("revision", 0)))
        assertNull(captureCorrectionRowFromJson(JSONObject(row.toString())
            .put("result", "not-an-object")))
        assertNull(captureCorrectionRowFromJson(JSONObject(row.toString())
            .put("asset_id", "..")))
        // A NULL result still parses; the validator later rejects it as missing.
        assertNull(captureCorrectionRowFromJson(
            JSONObject(row.toString()).put("result", JSONObject.NULL),
        )!!.result)
    }

    @Test
    fun validatorAcceptsAWellFormedRowAndPinsItsInstallBase() {
        val decision = validateDesktopCorrection(localContract(), correctionRow(), ownerId)
        val plan = (decision as DesktopCorrectionDecision.Ready).plan
        assertEquals(displayHash, plan.baseDisplaySha256)
        assertEquals(1, plan.baseDisplayRevision)
        assertEquals("", plan.baseAppliedCorrectionId)
        assertEquals(2, plan.targetRevision)
        assertEquals(artifact(), plan.artifact)
        assertEquals(
            "desktop_asset-1_r2_${"c".repeat(20)}.jpg",
            desktopCorrectionDisplayFileName(plan),
        )
    }

    @Test
    fun validatorRejectsEveryTamperedAxisWithoutTrustingTheRowsPath() {
        val local = localContract()
        fun mutated(block: (JSONObject) -> Unit): DesktopCorrectionDecision {
            val result = correctionResult()
            block(result)
            return validateDesktopCorrection(local, correctionRow(result = result), ownerId)
        }
        fun reason(decision: DesktopCorrectionDecision): String =
            (decision as DesktopCorrectionDecision.Rejected).reason

        assertEquals("missing result", reason(
            validateDesktopCorrection(local, correctionRow(result = null), ownerId),
        ))
        assertEquals("result schema", reason(mutated { it.put("schema", "org.whl.other") }))
        assertEquals("result schema", reason(mutated { it.put("version", 2) }))
        assertEquals("processor identity", reason(mutated {
            it.put("processor", "whl-image-processor")
        }))
        assertEquals("correction recipe", reason(mutated {
            it.put("recipe", "whl-cloud-book-cleanup")
        }))
        assertEquals("correction identity", reason(mutated {
            it.put("correction_id", correctionB)
        }))
        assertEquals("source anchor", reason(mutated {
            it.getJSONObject("source").put("original_sha256", "d".repeat(64))
        }))
        assertEquals("geometry strategy", reason(mutated {
            it.put("geometry_strategy", "homography")
        }))
        assertEquals("display artifact path", reason(mutated {
            it.getJSONObject("artifacts").getJSONObject("display")
                .put("path", "$ownerId/capture-1/asset-1/other/display-x.jpg")
        }))
        assertEquals("display artifact metadata", reason(mutated {
            it.getJSONObject("artifacts").getJSONObject("display")
                .put("path", "$ownerId/capture-1/asset-1/../display.jpg")
        }))
        assertEquals("display artifact metadata", reason(mutated {
            it.getJSONObject("artifacts").getJSONObject("display")
                .put("bytes", MAX_CLOUD_DERIVATIVE_BYTES + 1)
        }))
        assertEquals("display artifact metadata", reason(mutated {
            it.getJSONObject("artifacts").getJSONObject("display").put("width", 0)
        }))
        assertEquals("display artifact metadata", reason(mutated {
            val display = it.getJSONObject("artifacts").getJSONObject("display")
            display.remove("content_type")
            display.put("mime", "image/jpeg")
        }))

        // Anchor and applicability axes live on the row itself.
        assertEquals("original anchor", reason(validateDesktopCorrection(
            local,
            correctionRow(sourceOriginalSha256 = "d".repeat(64)),
            ownerId,
        )))
        assertEquals(
            DesktopCorrectionDecision.NotApplicable,
            validateDesktopCorrection(local, correctionRow(), "someone-else"),
        )
        assertEquals(
            DesktopCorrectionDecision.NotApplicable,
            validateDesktopCorrection(
                local,
                correctionRow().copy(assetId = "asset-2"),
                ownerId,
            ),
        )
        // The id alone is not a no-op: this contract still names the original
        // phone display, so the same-id row must repair it.
        val sameIdMismatch = validateDesktopCorrection(
            localContract(appliedCorrectionId = correctionA),
            correctionRow(),
            ownerId,
        ) as DesktopCorrectionDecision.Ready
        assertEquals(2, sameIdMismatch.plan.targetRevision)
        assertEquals(artifact().sha256, sameIdMismatch.plan.artifact.sha256)
        assertTrue(validateDesktopCorrection(
            localContract(appliedCorrectionId = correctionA),
            correctionRow(result = null),
            ownerId,
        ) is DesktopCorrectionDecision.Rejected)
    }

    @Test
    fun appliedCorrectionIdIsOptionalInTheContractAndRoundTrips() {
        val without = localContract().toJson()
        assertFalse(without.toString().contains("applied_desktop_correction_id"))
        assertEquals(
            "",
            capturePhotoAssetsFromJson(without, "capture-1")!!
                .assets.single().appliedDesktopCorrectionId,
        )

        val with = localContract(appliedCorrectionId = correctionA).toJson()
        assertEquals(
            correctionA,
            capturePhotoAssetsFromJson(with, "capture-1")!!
                .assets.single().appliedDesktopCorrectionId,
        )

        val corrupt = localContract(appliedCorrectionId = correctionA).toJson()
        corrupt.getJSONArray("assets").getJSONObject(0)
            .put("applied_desktop_correction_id", "not-hex")
        assertNull(capturePhotoAssetsFromJson(corrupt, "capture-1"))
    }

    @Test
    fun verifiedCorrectionInstallsANewRevisionMarkerAndBookkeeping() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val row = correctionRow(artifact = artifact)
            val plan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                row,
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan
            val downloaded = File(dir, ".verified.part").apply { writeBytes(bytes) }

            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                plan,
                downloaded,
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))

            val installed = PhotoAssetStore.read(dir).assets.single()
            assertEquals("immutable-camera", File(dir, installed.original.reference).readText())
            assertEquals("local-display", File(dir, "photo_1.jpg").readText())
            assertEquals(
                "desktop_asset-1_r2_${artifact.sha256.take(20)}.jpg",
                installed.display.reference,
            )
            assertEquals(bytes.toList(), File(dir, installed.display.reference).readBytes().toList())
            assertEquals(2, installed.display.revision)
            assertEquals(DESKTOP_CORRECTION_RECIPE, installed.display.recipe)
            assertNull(installed.display.sourceToDisplayHomography)
            assertEquals(correctionA, installed.appliedDesktopCorrectionId)
            assertFalse(installed.geometries.any { it.displayRevision == 2 })
            assertTrue(PhotoAssetStore.descriptors(dir).single().geometry.isEmpty())

            // The durable marker the CloudDisplayReocrWorker machinery scans.
            assertTrue(File(
                dir,
                ".cloud-reocr-asset-1-r2-${artifact.sha256.take(20)}.pending",
            ).isFile)
            val target = PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).single()
            assertEquals("capture-1", target.captureId)
            assertEquals("asset-1", target.assetId)
            assertEquals(correctionA, target.jobId)
            assertEquals(2, target.displayRevision)
            assertEquals(artifact.sha256, target.displaySha256)
            assertEquals(
                File(dir, installed.display.reference).canonicalFile,
                PhotoAssetStore.cloudDisplayReocrFile(dir, target)?.canonicalFile,
            )

            // The same row is now a no-op regardless of its server revision.
            val alreadyApplied = validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                row.copy(revision = 9),
                ownerId,
            ) as DesktopCorrectionDecision.AlreadyApplied
            assertEquals(2, alreadyApplied.plan.targetRevision)
            assertEquals(artifact, alreadyApplied.plan.artifact)

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
            assertEquals(
                "Corrected title",
                PhotoAssetStore.read(dir).assets.single()
                    .geometries.single { it.displayRevision == 2 }.regions.single().text,
            )
            assertTrue(PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).isEmpty())
        }
    }

    @Test
    fun sameIdDamagedDisplayCanRepairWithoutAdvancingTheRevision() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val row = correctionRow(artifact = artifact)
            val firstPlan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                row,
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan
            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                firstPlan,
                File(dir, ".first.part").apply { writeBytes(bytes) },
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))

            val installed = PhotoAssetStore.read(dir).assets.single()
            val installedFile = File(dir, installed.display.reference)
            installedFile.writeBytes(jpeg(12, 19))
            val repairPlan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                row.copy(revision = 8),
                ownerId,
            ) as DesktopCorrectionDecision.AlreadyApplied).plan

            assertEquals(2, repairPlan.targetRevision)
            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                repairPlan,
                File(dir, ".repair.part").apply { writeBytes(bytes) },
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))
            val repaired = PhotoAssetStore.read(dir).assets.single()
            assertEquals(2, repaired.display.revision)
            assertEquals(correctionA, repaired.appliedDesktopCorrectionId)
            assertEquals(bytes.toList(), installedFile.readBytes().toList())
        }
    }

    @Test
    fun downloadedCorrectionRestagesAfterConcurrentNonDesktopDisplayInstall() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val row = correctionRow(artifact = artifact)
            val staged = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                row,
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan

            val concurrent = PhotoAssetStore.read(dir)
            val concurrentAsset = concurrent.assets.single()
            val concurrentReference = "cloud_display_r2.jpg"
            File(dir, concurrentReference).writeText("concurrent-cloud-display")
            File(dir, PHOTO_ASSETS_FILE).writeText(concurrent.copy(assets = listOf(
                concurrentAsset.copy(display = concurrentAsset.display.copy(
                    reference = concurrentReference,
                    sha256 = "d".repeat(64),
                    revision = 2,
                )),
            )).toJson().toString())

            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                staged,
                File(dir, ".downloaded-before-race.part").apply { writeBytes(bytes) },
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))

            val installed = PhotoAssetStore.read(dir).assets.single()
            assertEquals(3, installed.display.revision)
            assertEquals(correctionA, installed.appliedDesktopCorrectionId)
            assertEquals(
                "desktop_asset-1_r3_${artifact.sha256.take(20)}.jpg",
                installed.display.reference,
            )
            assertEquals("concurrent-cloud-display", File(dir, concurrentReference).readText())
        }
    }

    @Test
    fun aDifferentCorrectionIdSupersedesAndBumpsTheRevisionAgain() {
        withEntryDir { dir ->
            val firstBytes = jpeg(12, 18)
            val first = artifact(sha256(firstBytes), firstBytes.size.toLong(), 12, 18)
            val firstPlan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                correctionRow(artifact = first),
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan
            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                firstPlan,
                File(dir, ".first.part").apply { writeBytes(firstBytes) },
                PrivateObjectDownload("image/jpeg", firstBytes.size.toLong()),
            ))

            val secondBytes = jpeg(10, 14)
            val second = artifact(
                sha256(secondBytes),
                secondBytes.size.toLong(),
                10,
                14,
                correctionId = correctionB,
            )
            val secondRow = correctionRow(correctionId = correctionB, artifact = second)
            val secondPlan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                secondRow,
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan
            assertEquals(2, secondPlan.baseDisplayRevision)
            assertEquals(3, secondPlan.targetRevision)
            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                secondPlan,
                File(dir, ".second.part").apply { writeBytes(secondBytes) },
                PrivateObjectDownload("image/jpeg", secondBytes.size.toLong()),
            ))

            val installed = PhotoAssetStore.read(dir).assets.single()
            assertEquals(3, installed.display.revision)
            assertEquals(correctionB, installed.appliedDesktopCorrectionId)
            assertEquals(
                "desktop_asset-1_r3_${second.sha256.take(20)}.jpg",
                installed.display.reference,
            )
            assertEquals("local-display", File(dir, "photo_1.jpg").readText())
            assertEquals(3, PhotoAssetStore.pendingCloudDisplayReocrTargets(dir)
                .single().displayRevision)
            // A stale plan against the superseded base can no longer install.
            assertFalse(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                firstPlan,
                File(dir, ".stale.part").apply { writeBytes(firstBytes) },
                PrivateObjectDownload("image/jpeg", firstBytes.size.toLong()),
            ))
        }
    }

    @Test
    fun installRefusesADownloadThatFailsByteVerification() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val plan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                correctionRow(artifact = artifact),
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan
            val tampered = File(dir, ".tampered.part").apply { writeBytes(jpeg(12, 19)) }

            assertFalse(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                plan,
                tampered,
                PrivateObjectDownload("image/jpeg", tampered.length()),
            ))
            val untouched = PhotoAssetStore.read(dir).assets.single()
            assertEquals(1, untouched.display.revision)
            assertEquals("", untouched.appliedDesktopCorrectionId)
            assertTrue(PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).isEmpty())
        }
    }

    @Test
    fun alreadyAppliedRowsStillExposeThePendingReocrMarkerForReArming() {
        withEntryDir { dir ->
            val bytes = jpeg(12, 18)
            val artifact = artifact(sha256(bytes), bytes.size.toLong(), 12, 18)
            val row = correctionRow(artifact = artifact)
            val plan = (validateDesktopCorrection(
                PhotoAssetStore.read(dir),
                row,
                ownerId,
            ) as DesktopCorrectionDecision.Ready).plan
            assertTrue(PhotoAssetStore.installDesktopCorrectionDisplay(
                dir,
                plan,
                File(dir, ".installed.part").apply { writeBytes(bytes) },
                PrivateObjectDownload("image/jpeg", bytes.size.toLong()),
            ))

            // Simulate CloudDisplayReocrWorker spending its whole retry budget
            // (a Mistral 401 fails immediately): the durable marker
            // deliberately stays on disk for a later retry. On the next pull
            // the same row validates AlreadyApplied while the pending target
            // is still discoverable — exactly the pair the sync worker keys
            // its enqueuePending re-arm off, so Ready is not the only trigger.
            assertFalse(shouldRetryCloudDisplayReocr(MAX_CLOUD_DISPLAY_REOCR_ATTEMPTS - 1))
            assertTrue(
                validateDesktopCorrection(PhotoAssetStore.read(dir), row, ownerId) is
                    DesktopCorrectionDecision.AlreadyApplied,
            )
            val target = PhotoAssetStore.pendingCloudDisplayReocrTargets(dir).single()
            assertEquals(2, target.displayRevision)
            assertEquals(artifact.sha256, target.displaySha256)
        }
    }

    @Test
    fun correctionRowsAndInstallAreWiredIntoTheMetadataPull() {
        val clientSource = File("src/main/java/org/whl/bookcapture/SupabaseClient.kt").readText()
        val workerSource = File(
            "src/main/java/org/whl/bookcapture/CaptureMetadataSyncWorker.kt",
        ).readText()
        assertTrue(clientSource.contains("/rest/v1/capture_corrections"))
        assertTrue(clientSource.contains("capture_id,asset_id,owner_id,correction_id,"))
        assertTrue(clientSource.contains("source_original_sha256,result,revision,updated_at"))
        // Call-shape assertions only: local variable spellings may change
        // freely without breaking this wiring check.
        assertTrue(workerSource.contains(".captureCorrections("))
        assertTrue(workerSource.contains("PhotoAssetStore.installDesktopCorrectionDisplay"))
        assertTrue(workerSource.contains("CloudDisplayReocrWorker.enqueuePending("))
        // Re-arm keying: the worker must consume AlreadyApplied — not only a
        // fresh Ready install — so a terminally failed re-OCR is re-armed by
        // every later pull (the marker-side behavior is covered above).
        assertTrue(workerSource.contains("DesktopCorrectionDecision.AlreadyApplied"))
        val alreadyAppliedBranch = workerSource
            .substringAfter("is DesktopCorrectionDecision.AlreadyApplied")
            .substringBefore("is DesktopCorrectionDecision.Ready")
        assertTrue(alreadyAppliedBranch.contains("verifyCloudDisplayDownload("))
        assertTrue(alreadyAppliedBranch.contains("download(decision.plan)"))
        // Not push-gated: corrections must flow on plain pulls (Home resume),
        // not only explicit syncs. The first `if (!pushReviews` in the worker
        // opens the push-only tail of doWork, so the corrections fetch and the
        // re-OCR re-arm must both appear before it in the source.
        val firstPushGate = workerSource.indexOf("if (!pushReviews")
        assertTrue(firstPushGate > 0)
        assertTrue(workerSource.indexOf(".captureCorrections(") < firstPushGate)
        assertTrue(
            workerSource.indexOf("CloudDisplayReocrWorker.enqueuePending(") < firstPushGate,
        )
        // Per-entry isolation: correction application must not fail the pull.
        assertTrue(workerSource.contains("desktop correction skipped"))
    }

    private fun withEntryDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("desktop-correction-root-").toFile()
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
