package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class CaptureLibAssociationTest {
    private val captureId = "11111111-2222-4333-8444-555555555555"
    private val streamId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    private fun association(state: String = "current"): JSONObject = JSONObject()
        .put("schema", "org.whl.capture-lib-association")
        .put("version", 1)
        .put("capture_id", captureId)
        .put("book_id", "b-" + "a".repeat(32))
        .put("archive_sha256", "b".repeat(64))
        .put("archive_bytes", 12_345)
        .put("format_version", "3.0")
        .put("state", state)
        .put("generated_at", "2026-07-23T12:34:56+00:00")
        .put("source_revision", "sha256:" + "c".repeat(64))
        .put("source_fingerprint", "d".repeat(64))

    private fun confirmation(
        revision: Long = 1,
        state: String = "current",
        updatedAt: String = "2026-07-23T12:35:00+00:00",
        publisher: String = streamId,
    ): CaptureLibConfirmation = captureLibConfirmationFromJson(
        JSONObject()
            .put("schema", "org.whl.capture-lib-confirmation")
            .put("version", 1)
            .put("capture_id", captureId)
            .put("stream_id", publisher)
            .put("revision", revision)
            .put("updated_at", updatedAt)
            .put("association", association(state)),
        captureId,
    )!!

    @Test
    fun exactCurrentAssociationIsConfirmedWhileStaleIsNot() {
        val current = captureLibAssociationFromJson(association(), captureId)!!
        val stale = captureLibAssociationFromJson(association("stale"), captureId)!!
        assertTrue(current.confirmed)
        assertEquals(CaptureLibAssociationState.CURRENT, current.state)
        assertFalse(stale.confirmed)
        assertEquals(CaptureLibAssociationState.STALE, stale.state)
        assertEquals(association().toString(), current.toJson().toString())
    }

    @Test
    fun availableAndUnknownOrNonportableFieldsAreRejected() {
        assertNull(captureLibAssociationFromJson(association("available"), captureId))
        assertNull(captureLibAssociationFromJson(
            association().put("desktop_path", "C:\\private\\book.lib"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("capture_id", streamId),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("archive_bytes", CAPTURE_LIB_MAX_ARCHIVE_BYTES + 1),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("generated_at", "2026-07-23T12:34:56"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("generated_at", "2026-07-23T12:34:56+0000"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("generated_at", "2026-W30-4T12:34:56+00:00"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("generated_at", "2026-07-23T12:34:60+00:00"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("generated_at", "2026-07-23T12:34:56+14:01"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("source_revision", "C:\\private\\capture"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("source_revision", "/private/capture"),
            captureId,
        ))
        assertNull(captureLibAssociationFromJson(
            association().put("capture_id", streamId.uppercase()),
            streamId,
        ))
    }

    @Test
    fun cloudRowsAcceptLegacyNullButRequireCompleteRevisionEnvelope() {
        val legacy = JSONObject()
            .put("id", captureId)
            .put("status", "imported")
            .put("created_by", streamId)
            .put("lib_association", JSONObject.NULL)
            .put("lib_association_revision", 0)
            .put("lib_association_updated_at", JSONObject.NULL)
        val parsedLegacy = captureImportStateFromJson(legacy, streamId)!!
        assertEquals("imported", parsedLegacy.status)
        assertNull(parsedLegacy.confirmation)

        val confirmed = JSONObject(legacy.toString())
            .put("lib_association", association())
            .put("lib_association_revision", 3)
            .put("lib_association_updated_at", "2026-07-23T13:00:00+00:00")
        val parsed = captureImportStateFromJson(confirmed, streamId)!!
        val confirmation = parsed.confirmation!!
        assertEquals(3, confirmation.revision)
        assertTrue(confirmation.confirmed)

        assertNull(captureImportStateFromJson(
            JSONObject(confirmed.toString()).put("created_by", captureId),
            streamId,
        ))
        assertNull(captureImportStateFromJson(
            JSONObject(confirmed.toString()).put("lib_association_revision", 0),
            streamId,
        ))
    }

    @Test
    fun cloudRowAndLanEnvelopeProduceTheSameConfirmation() {
        val envelope = confirmation(revision = 3).toJson()
        val lan = captureLibConfirmationFromJson(envelope, captureId)!!
        val cloud = captureImportStateFromJson(
            JSONObject()
                .put("id", captureId)
                .put("status", "imported")
                .put("created_by", streamId)
                .put("lib_association", association())
                .put("lib_association_revision", 3)
                .put("lib_association_updated_at", "2026-07-23T12:35:00+00:00"),
            streamId,
        )!!.confirmation
        assertEquals(lan, cloud)
        assertTrue(lan.confirmed)
    }

    @Test
    fun mergeIgnoresOldRowsAndConflictsOnEqualDifferentPayload() {
        val local = confirmation(revision = 4)
        assertEquals(
            CaptureLibApplyResult.STALE,
            captureLibMergeResult(
                local,
                confirmation(
                    revision = 3,
                    updatedAt = "2026-07-23T12:34:00+00:00",
                ),
            ),
        )
        assertEquals(
            CaptureLibApplyResult.UNCHANGED,
            captureLibMergeResult(local, confirmation(revision = 4)),
        )
        assertEquals(
            CaptureLibApplyResult.CONFLICT,
            captureLibMergeResult(local, confirmation(revision = 4, state = "stale")),
        )
        assertEquals(
            CaptureLibApplyResult.APPLIED,
            captureLibMergeResult(local, confirmation(revision = 5, state = "stale")),
        )
    }

    @Test
    fun newerTimestampAllowsLegitimateStreamRotationAfterLedgerLoss() {
        val local = confirmation(
            revision = 9,
            updatedAt = "2026-07-23T12:35:00+00:00",
        )
        assertEquals(
            CaptureLibApplyResult.APPLIED,
            captureLibMergeResult(
                local,
                confirmation(
                    revision = 1,
                    updatedAt = "2026-07-23T12:35:01+00:00",
                    publisher = captureId,
                ),
            ),
        )
        assertEquals(
            CaptureLibApplyResult.APPLIED,
            captureLibMergeResult(
                local,
                confirmation(
                    revision = 1,
                    updatedAt = "2026-07-24T12:35:00+00:00",
                ),
            ),
        )
        assertEquals(
            CaptureLibApplyResult.STALE,
            captureLibMergeResult(
                local,
                confirmation(
                    revision = 2,
                    updatedAt = "2026-07-24T12:35:00+00:00",
                ),
            ),
        )
    }

    @Test
    fun delayedPreviousStreamCannotOverwriteRotatedConfirmation() {
        val rotated = confirmation(
            revision = 1,
            updatedAt = "2026-07-23T12:35:02+00:00",
            publisher = captureId,
        )
        val delayedPreviousStream = confirmation(
            revision = 10,
            updatedAt = "2026-07-23T12:35:01+00:00",
        )
        assertEquals(
            CaptureLibApplyResult.STALE,
            captureLibMergeResult(rotated, delayedPreviousStream),
        )
        assertEquals(
            CaptureLibApplyResult.CONFLICT,
            captureLibMergeResult(
                rotated,
                delayedPreviousStream.copy(updatedAt = rotated.updatedAt),
            ),
        )
    }

    @Test
    fun sidecarSurvivesRestartAndCorruptionRemovesConfirmation() {
        val root = Files.createTempDirectory("capture-lib-association").toFile()
        val dir = root.resolve(captureId).apply { mkdirs() }
        try {
            val incoming = confirmation()
            assertEquals(
                CaptureLibApplyResult.APPLIED,
                CaptureLibAssociationStore.apply(dir, incoming),
            )
            assertEquals(incoming, CaptureLibAssociationStore.read(dir))
            assertTrue(dir.resolve(CAPTURE_LIB_ASSOCIATION_FILE).isFile)

            dir.resolve(CAPTURE_LIB_ASSOCIATION_FILE).writeText("{broken")
            assertNull(CaptureLibAssociationStore.read(dir))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun storeRevalidatesTypedConfirmationsBeforePersisting() {
        val root = Files.createTempDirectory("capture-lib-association-invalid").toFile()
        val dir = root.resolve(captureId).apply { mkdirs() }
        try {
            val invalid = confirmation().copy(
                association = confirmation().association.copy(
                    sourceRevision = "/private/capture",
                ),
            )
            assertEquals(
                CaptureLibApplyResult.CONFLICT,
                CaptureLibAssociationStore.apply(dir, invalid),
            )
            assertFalse(dir.resolve(CAPTURE_LIB_ASSOCIATION_FILE).exists())
        } finally {
            root.deleteRecursively()
        }
    }
}
