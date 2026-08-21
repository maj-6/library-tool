package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.time.Instant

/**
 * The durable decision made while a capture is still open: this physical book
 * is being set aside for digitization.
 *
 * The source is the capture's frozen collection provenance. The destination is
 * the independently selected scan collection. Current membership is staged in
 * [InspectBookMemberships], while this sidecar remains as an audit record for
 * LAN/cloud importers and survives Activity replacement before Done is tapped.
 */
internal data class CaptureScanMark(
    val sourceCollectionId: String,
    val scanCollectionId: String,
    val markedAt: String,
)

internal object CaptureScanMarkStore {
    const val FILE_NAME = "scan_mark.json"
    private const val VERSION = 1

    fun read(entryDir: File): CaptureScanMark? = try {
        val source = File(entryDir, FILE_NAME)
        if (!source.isFile) {
            null
        } else {
            val root = JSONObject(source.readText())
            val sourceCollectionId = root.optString("source_collection_id")
                .trim().lowercase()
            val scanCollectionId = root.optString("scan_collection_id")
                .trim().lowercase()
            val markedAt = root.optString("marked_at").trim()
            if (strictInteger(root.opt("version")) != VERSION.toLong() ||
                !SAFE_CAPTURE_SYNC_ID.matches(sourceCollectionId) ||
                !SAFE_CAPTURE_SYNC_ID.matches(scanCollectionId) ||
                sourceCollectionId == scanCollectionId ||
                runCatching { Instant.parse(markedAt) }.isFailure
            ) {
                null
            } else {
                CaptureScanMark(sourceCollectionId, scanCollectionId, markedAt)
            }
        }
    } catch (_: Exception) {
        null
    }

    fun write(
        entryDir: File,
        sourceCollectionId: String,
        scanCollectionId: String,
        markedAt: Instant = Instant.now(),
    ): Boolean {
        val source = sourceCollectionId.trim().lowercase()
        val destination = scanCollectionId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(source) ||
            !SAFE_CAPTURE_SYNC_ID.matches(destination) ||
            source == destination
        ) return false
        return try {
            val body = JSONObject()
                .put("version", VERSION)
                .put("source_collection_id", source)
                .put("scan_collection_id", destination)
                .put("marked_at", markedAt.toString())
                .toString()
            Entries.atomicWrite(File(entryDir, FILE_NAME), body)
            true
        } catch (_: Exception) {
            false
        }
    }

    /**
     * Remove the device-local active marker after a book leaves a scan-type
     * collection. Capture provenance and the cloud scan-state audit remain the
     * historical record; this sidecar represents only the active set-aside.
     */
    fun clear(entryDir: File): Boolean = try {
        val source = File(entryDir, FILE_NAME)
        !source.exists() || source.isFile && source.delete()
    } catch (_: Exception) {
        false
    }

    /** Make the mark visible locally and leave a crash-safe cloud outbox. */
    fun stageMembership(ctx: Context, entryId: String): Boolean {
        val entry = Entries.findIncludingArchive(ctx, entryId) ?: return false
        val mark = read(entry.dir) ?: return false
        return InspectBookMemberships.move(
            ctx,
            setOf(entryId),
            mark.scanCollectionId,
        )
    }

    /** Carry the initial mark across LAN/cloud capture ingestion. */
    fun attachToMeta(meta: JSONObject, entryDir: File): JSONObject {
        val mark = read(entryDir) ?: return meta
        return meta
            .put("scan_marked", true)
            .put("scan_destination_collection_id", mark.scanCollectionId)
            .put("scan_source_collection_id", mark.sourceCollectionId)
            .put("scan_marked_at", mark.markedAt)
    }

    private fun strictInteger(value: Any?): Long? = when (value) {
        is Byte -> value.toLong()
        is Short -> value.toLong()
        is Int -> value.toLong()
        is Long -> value
        else -> null
    }
}
