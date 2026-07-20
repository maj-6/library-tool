package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CaptureNotesTest {

    @Test
    fun atomicSidecarPreservesTranscriptRowsTimingAndProvider() = withCaptureDir { dir ->
        val draft = StructuredNote.inProgress("Shelf copy. Price: twelve dollars. Pages: 240")
        CaptureNotes.save(
            dir = dir,
            noteId = "note-1",
            note = draft,
            startedAtMs = 1_000,
            updatedAtMs = 1_200,
            provider = "mistral",
            model = "voxtral-mini-transcribe-realtime-2602",
        )
        val completed = draft.complete(
            "Shelf copy. Price: $12.00. Pages: 240. Condition: sound",
        )
        CaptureNotes.save(
            dir = dir,
            noteId = "note-1",
            note = completed,
            startedAtMs = 9_999, // an update cannot move the original start time
            updatedAtMs = 1_500,
            provider = "mistral",
            model = "voxtral-mini-transcribe-realtime-2602",
        )

        val stored = CaptureNotes.read(dir).notes.single()
        assertEquals(completed.transcript, stored.transcript)
        assertEquals("Shelf copy.", stored.unclassifiedText)
        assertEquals(completed.rows, stored.rows)
        assertEquals(1_000L, stored.startedAtMs)
        assertEquals(1_500L, stored.updatedAtMs)
        assertEquals(1_500L, stored.completedAtMs)
        assertEquals("mistral", stored.provider)
        assertEquals("voxtral-mini-transcribe-realtime-2602", stored.model)
        assertTrue(stored.isCompleted)

        val json = JSONObject(File(dir, CAPTURE_NOTES_FILE).readText())
        assertEquals(CAPTURE_NOTES_SCHEMA, json.getString("schema"))
        assertEquals(dir.name, json.getString("capture_id"))
        assertEquals("condition", json.getJSONArray("notes").getJSONObject(0)
            .getJSONArray("rows").getJSONObject(2).getString("field"))
        assertTrue(dir.listFiles().orEmpty().none { it.name.endsWith(".tmp") })
    }

    @Test
    fun completedRecordRejectsLateCallbacksAndUndoRemovesOnlyTheLastNote() = withCaptureDir { dir ->
        CaptureNotes.save(
            dir,
            "first",
            StructuredNote.completed("Remark: keep jacket"),
            10,
            20,
            "mistral",
            "voxtral-realtime",
        )
        val accepted = CaptureNotes.save(
            dir,
            "first",
            StructuredNote.inProgress("Remark: overwritten"),
            10,
            30,
            "mistral",
            "voxtral-realtime",
        )
        CaptureNotes.save(
            dir,
            "second",
            StructuredNote.completed("Condition: fragile"),
            40,
            50,
            "mistral",
            "voxtral-realtime",
        )

        assertEquals("keep jacket", accepted.rows.single().value)
        assertEquals("second", CaptureNotes.removeLast(dir)?.id)
        assertEquals(listOf("first"), CaptureNotes.read(dir).notes.map { it.id })
        assertEquals("first", CaptureNotes.removeLast(dir)?.id)
        assertNull(CaptureNotes.removeLast(dir))
        assertTrue(CaptureNotes.read(dir).notes.isEmpty())
    }

    @Test
    fun checkpointDiscardRemovesOnlyTheRequestedNoteId() = withCaptureDir { dir ->
        for ((id, text) in listOf(
            "first" to "Remark: first note",
            "middle" to "Remark: middle note",
            "last" to "Remark: last note",
        )) {
            CaptureNotes.save(
                dir,
                id,
                StructuredNote.inProgress(text),
                10,
                20,
                "mistral",
                "voxtral-realtime",
            )
        }

        assertEquals("middle", CaptureNotes.remove(dir, "middle")?.id)
        assertEquals(listOf("first", "last"), CaptureNotes.read(dir).notes.map { it.id })
        assertNull(CaptureNotes.remove(dir, "middle"))
    }

    @Test
    fun durableDiscardMarkerFiltersStaleSidecarAndManifestAcrossRecreation() =
        withCaptureDir { dir ->
            CaptureNotes.save(
                dir,
                "private-note",
                StructuredNote.inProgress("Remark: do not retain"),
                10,
                20,
                "mistral",
                "voxtral-realtime",
            )
            val stalePayload = CaptureNotes.payload(dir)
            val staleSidecar = File(dir, CAPTURE_NOTES_FILE).readText()

            assertEquals("private-note", CaptureNotes.remove(dir, "private-note")?.id)
            assertTrue(dir.listFiles().orEmpty().any {
                it.name == "$CAPTURE_NOTE_DISCARD_MARKER_PREFIX" +
                    "private-note$CAPTURE_NOTE_DISCARD_MARKER_SUFFIX"
            })

            // Model a failed/rolled-back physical sidecar cleanup and a stale
            // sealed manifest. The durable marker remains authoritative.
            File(dir, CAPTURE_NOTES_FILE).writeText(staleSidecar)
            val staleManifest = JSONObject().put(CAPTURE_NOTES_MANIFEST_KEY, stalePayload)
            assertTrue(CaptureNotes.read(dir).notes.isEmpty())
            assertFalse(CaptureNotes.hasNotes(CaptureNotes.payload(dir)))
            File(dir, CAPTURE_NOTES_FILE).delete()
            assertTrue(CaptureNotes.read(dir, staleManifest).notes.isEmpty())

            val resurrected = runCatching {
                CaptureNotes.save(
                    dir,
                    "private-note",
                    StructuredNote.completed("Remark: resurrected"),
                    10,
                    30,
                    "mistral",
                    "voxtral-realtime",
                )
            }
            assertTrue(resurrected.isFailure)
        }

    @Test
    fun summaryAndPayloadCanFallBackToSealedManifestSnapshot() = withCaptureDir { dir ->
        CaptureNotes.save(
            dir,
            "summary",
            StructuredNote.completed("Auction copy. Price: $8. Pages: 96"),
            100,
            200,
            "mistral",
            "voxtral-realtime",
        )
        val payload = CaptureNotes.payload(dir)
        val manifest = JSONObject().put(CAPTURE_NOTES_MANIFEST_KEY, payload)
        File(dir, CAPTURE_NOTES_FILE).delete()

        assertEquals(
            "Auction copy.\nPrice: $8\nPages: 96",
            CaptureNotes.humanReadableSummary(dir, manifest),
        )
        val restored = CaptureNotes.payload(dir, manifest)
        assertTrue(CaptureNotes.hasNotes(restored))
        assertEquals("Auction copy. Price: $8. Pages: 96",
            restored.getJSONArray("notes").getJSONObject(0).getString("transcript"))

        val empty = CaptureNotes.payload(dir)
        assertFalse(CaptureNotes.hasNotes(empty))
        assertEquals(dir.name, empty.getString("capture_id"))
    }

    @Test
    fun uploadMetadataCarriesNotesWithoutMetaJsonAndRejectsAReservedCollision() =
        withCaptureDir { dir ->
            CaptureNotes.save(
                dir,
                "transport",
                StructuredNote.completed("Illustrations: twelve plates"),
                1,
                2,
                "mistral",
                "voxtral-realtime",
            )
            val authoritative = CaptureNotes.payload(dir)
            val outgoing = attachCaptureNotes(
                JSONObject().put(CAPTURE_NOTES_META_KEY, "extractor collision"),
                authoritative,
            )

            val transported = outgoing.getJSONObject(CAPTURE_NOTES_META_KEY)
            assertEquals(CAPTURE_NOTES_SCHEMA, transported.getString("schema"))
            assertEquals(
                "twelve plates",
                transported.getJSONArray("notes").getJSONObject(0)
                    .getJSONArray("rows").getJSONObject(0).getString("value"),
            )

            val withoutNotes = attachCaptureNotes(
                JSONObject().put(CAPTURE_NOTES_META_KEY, "stale"),
                null,
            )
            assertFalse(withoutNotes.has(CAPTURE_NOTES_META_KEY))
        }

    private fun withCaptureDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("capture-notes-").toFile()
        val dir = File(root, "capture-id").apply { mkdirs() }
        try {
            block(dir)
        } finally {
            root.deleteRecursively()
        }
    }
}
