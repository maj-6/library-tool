package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

internal const val CAPTURE_NOTES_FILE = "capture_notes.json"
internal const val CAPTURE_NOTES_SCHEMA = "org.whl.bookcapture.capture-notes"
internal const val CAPTURE_NOTES_VERSION = 1
internal const val CAPTURE_NOTES_MANIFEST_KEY = "capture_notes"
internal const val CAPTURE_NOTES_META_KEY = "_capture_notes"
internal const val CAPTURE_NOTE_DISCARD_MARKER_PREFIX = ".capture_note_discarded."
internal const val CAPTURE_NOTE_DISCARD_MARKER_SUFFIX = ".marker"

private val CAPTURE_NOTE_ID = Regex("[A-Za-z0-9._-]{1,128}")

/**
 * Immutable transcription snapshot stored beside a capture. [rows] are kept
 * explicitly instead of being reconstructed during upload, so a future change
 * to the live parser cannot silently reinterpret an already accepted note.
 */
internal data class StoredCaptureNote(
    val id: String,
    val transcript: String,
    val unclassifiedText: String,
    val rows: List<StructuredNoteRow>,
    val status: StructuredNoteStatus,
    val startedAtMs: Long,
    val updatedAtMs: Long,
    val completedAtMs: Long?,
    val provider: String,
    val model: String,
) {
    val isCompleted: Boolean get() = status == StructuredNoteStatus.COMPLETED

    /** Compact catalogue-facing rendering; the exact transcript remains in
     * [transcript] and in the JSON payload even when it has structured rows. */
    fun humanReadableSummary(): String = buildList {
        if (unclassifiedText.isNotBlank()) add(unclassifiedText.trim())
        rows.forEach { row ->
            add(if (row.value.isBlank()) "${row.field.label}:" else "${row.field.label}: ${row.value}")
        }
        if (isEmpty() && transcript.isNotBlank()) add(transcript.trim())
    }.joinToString("\n")

    internal fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("status", status.wireValue)
        .put("transcript", transcript)
        .put("unclassified_text", unclassifiedText)
        .put("rows", JSONArray().also { array ->
            rows.forEach { row ->
                array.put(
                    JSONObject()
                        .put("field", row.field.wireValue)
                        .put("label", row.field.label)
                        .put("value", row.value),
                )
            }
        })
        .put("started_at_ms", startedAtMs)
        .put("updated_at_ms", updatedAtMs)
        .put("completed_at_ms", completedAtMs ?: JSONObject.NULL)
        .put("provider", provider)
        .put("model", model)
}

internal data class CaptureNoteDocument(
    val captureId: String,
    val notes: List<StoredCaptureNote>,
) {
    fun humanReadableSummary(): String = notes.mapNotNull { note ->
        note.humanReadableSummary().takeIf(String::isNotBlank)
    }.joinToString("\n\n")

    /** A new object is produced on every call so callers may safely attach it
     * to a manifest or network payload without mutating the stored document. */
    fun jsonPayload(): JSONObject = JSONObject()
        .put("schema", CAPTURE_NOTES_SCHEMA)
        .put("version", CAPTURE_NOTES_VERSION)
        .put("capture_id", captureId)
        .put("notes", JSONArray().also { array -> notes.forEach { array.put(it.toJson()) } })
}

/**
 * Atomic, versioned note storage for one capture directory. Updating an
 * in-progress note replaces its same-id snapshot; once completed, the record
 * is immutable so a late speech callback cannot rewrite accepted metadata.
 */
internal object CaptureNotes {
    private val monitor = Any()

    fun read(dir: File, manifest: JSONObject? = null): CaptureNoteDocument = synchronized(monitor) {
        withoutDiscardedNotes(dir, readDocument(dir, manifest))
    }

    fun save(
        dir: File,
        noteId: String,
        note: StructuredNote,
        startedAtMs: Long,
        updatedAtMs: Long,
        provider: String,
        model: String,
    ): StoredCaptureNote = synchronized(monitor) {
        require(dir.isDirectory) { "capture directory does not exist" }
        require(noteId.matches(CAPTURE_NOTE_ID)) { "invalid capture note id" }
        require(startedAtMs >= 0 && updatedAtMs >= 0) { "invalid capture note timestamps" }
        require(provider.isNotBlank()) { "transcription provider is required" }
        require(model.isNotBlank()) { "transcription model is required" }
        check(!discardMarker(dir, noteId).isFile) { "capture note has been discarded" }

        val current = readDocument(dir, null)
        val existing = current.notes.firstOrNull { it.id == noteId }
        if (existing?.isCompleted == true) return@synchronized existing
        val effectiveStart = existing?.startedAtMs ?: startedAtMs
        require(updatedAtMs >= effectiveStart) { "capture note update predates its start" }

        val record = StoredCaptureNote(
            id = noteId,
            transcript = note.transcript,
            unclassifiedText = note.unclassifiedText,
            rows = note.rows.toList(),
            status = note.status,
            startedAtMs = effectiveStart,
            updatedAtMs = updatedAtMs,
            completedAtMs = updatedAtMs.takeIf { note.isCompleted },
            provider = existing?.provider ?: provider.trim(),
            model = existing?.model ?: model.trim(),
        )
        val notes = current.notes.toMutableList()
        val index = notes.indexOfFirst { it.id == noteId }
        if (index >= 0) notes[index] = record else notes += record
        writeDocument(dir, CaptureNoteDocument(dir.name, notes))
        record
    }

    /** Supports the voice-command Undo contract without touching any photos. */
    fun removeLast(dir: File): StoredCaptureNote? = synchronized(monitor) {
        val current = readDocument(dir, null)
        val removed = current.notes.lastOrNull { !discardMarker(dir, it.id).isFile }
            ?: return@synchronized null
        removeLocked(dir, current, removed)
    }

    /** Removes only the requested in-progress checkpoint. This is deliberately
     * id-based: a late discard must never remove a newer note that happened to
     * become the last record while its transcription was draining. */
    fun remove(dir: File, noteId: String): StoredCaptureNote? = synchronized(monitor) {
        require(noteId.matches(CAPTURE_NOTE_ID)) { "invalid capture note id" }
        val current = readDocument(dir, null)
        val removed = current.notes.firstOrNull { it.id == noteId }
        // Persist intent even if a previous attempt already removed the raw
        // record. This marker also filters an older embedded manifest snapshot.
        markDiscarded(dir, noteId)
        if (removed == null) return@synchronized null
        removeLocked(dir, current, removed, markerAlreadyWritten = true)
    }

    fun humanReadableSummary(dir: File, manifest: JSONObject? = null): String =
        read(dir, manifest).humanReadableSummary()

    fun payload(dir: File, manifest: JSONObject? = null): JSONObject =
        read(dir, manifest).jsonPayload()

    fun hasNotes(payload: JSONObject): Boolean =
        payload.optJSONArray("notes")?.length()?.let { it > 0 } == true

    private fun writeDocument(dir: File, document: CaptureNoteDocument) {
        Entries.atomicWrite(File(dir, CAPTURE_NOTES_FILE), document.jsonPayload().toString())
    }

    /** The marker is written before the larger sidecar rewrite. If that second
     * write fails (disk full, interrupted move, transient I/O), every public
     * read and payload still excludes the rejected note across process death. */
    private fun removeLocked(
        dir: File,
        current: CaptureNoteDocument,
        removed: StoredCaptureNote,
        markerAlreadyWritten: Boolean = false,
    ): StoredCaptureNote {
        if (!markerAlreadyWritten) markDiscarded(dir, removed.id)
        writeDocument(dir, current.copy(notes = current.notes.filterNot { it.id == removed.id }))
        return removed
    }

    private fun markDiscarded(dir: File, noteId: String) {
        require(dir.isDirectory) { "capture directory does not exist" }
        Entries.atomicWrite(discardMarker(dir, noteId), "discarded\n")
    }

    private fun discardMarker(dir: File, noteId: String): File = File(
        dir,
        "$CAPTURE_NOTE_DISCARD_MARKER_PREFIX$noteId$CAPTURE_NOTE_DISCARD_MARKER_SUFFIX",
    )

    private fun withoutDiscardedNotes(
        dir: File,
        document: CaptureNoteDocument,
    ): CaptureNoteDocument = document.copy(
        notes = document.notes.filterNot { discardMarker(dir, it.id).isFile },
    )

    private fun readDocument(dir: File, manifest: JSONObject?): CaptureNoteDocument {
        val sidecar = File(dir, CAPTURE_NOTES_FILE).takeIf { it.isFile }
            ?.let { runCatching { JSONObject(it.readText()) }.getOrNull() }
            ?.let { parseDocument(it, dir.name) }
        if (sidecar != null) return sidecar

        val embedded = manifest?.optJSONObject(CAPTURE_NOTES_MANIFEST_KEY)
            ?.let { parseDocument(it, dir.name) }
        return embedded ?: CaptureNoteDocument(dir.name, emptyList())
    }

    private fun parseDocument(value: JSONObject, captureId: String): CaptureNoteDocument? {
        if (value.optString("schema") != CAPTURE_NOTES_SCHEMA ||
            value.optInt("version", -1) != CAPTURE_NOTES_VERSION ||
            value.optString("capture_id") != captureId
        ) return null
        val array = value.optJSONArray("notes") ?: return null
        val notes = mutableListOf<StoredCaptureNote>()
        val ids = mutableSetOf<String>()
        for (index in 0 until array.length()) {
            val note = parseNote(array.optJSONObject(index) ?: return null) ?: return null
            if (!ids.add(note.id)) return null
            notes += note
        }
        return CaptureNoteDocument(captureId, notes)
    }

    private fun parseNote(value: JSONObject): StoredCaptureNote? {
        val id = value.optString("id")
        if (!id.matches(CAPTURE_NOTE_ID)) return null
        val status = StructuredNoteStatus.entries.firstOrNull {
            it.wireValue == value.optString("status")
        } ?: return null
        val transcript = value.opt("transcript") as? String ?: return null
        val unclassifiedText = value.opt("unclassified_text") as? String ?: return null
        val provider = (value.opt("provider") as? String)?.trim().orEmpty()
        val model = (value.opt("model") as? String)?.trim().orEmpty()
        if (provider.isEmpty() || model.isEmpty()) return null

        val startedAt = value.opt("started_at_ms").jsonLongOrNull() ?: return null
        val updatedAt = value.opt("updated_at_ms").jsonLongOrNull() ?: return null
        if (startedAt < 0 || updatedAt < startedAt) return null
        val completedAt = when (val raw = value.opt("completed_at_ms")) {
            null, JSONObject.NULL -> null
            else -> raw.jsonLongOrNull() ?: return null
        }
        if ((status == StructuredNoteStatus.COMPLETED) != (completedAt != null) ||
            completedAt != null && completedAt !in startedAt..updatedAt
        ) return null

        val rowsJson = value.optJSONArray("rows") ?: return null
        val rows = mutableListOf<StructuredNoteRow>()
        for (index in 0 until rowsJson.length()) {
            val row = rowsJson.optJSONObject(index) ?: return null
            val field = StructuredNoteField.entries.firstOrNull {
                it.wireValue == row.optString("field")
            } ?: return null
            val rowValue = row.opt("value") as? String ?: return null
            rows += StructuredNoteRow(field, rowValue)
        }
        return StoredCaptureNote(
            id = id,
            transcript = transcript,
            unclassifiedText = unclassifiedText,
            rows = rows,
            status = status,
            startedAtMs = startedAt,
            updatedAtMs = updatedAt,
            completedAtMs = completedAt,
            provider = provider,
            model = model,
        )
    }
}

private val StructuredNoteField.wireValue: String get() = name.lowercase()
private val StructuredNoteStatus.wireValue: String get() = name.lowercase()

private fun Any?.jsonLongOrNull(): Long? = when (this) {
    is Long -> this
    is Int -> toLong()
    else -> null
}
