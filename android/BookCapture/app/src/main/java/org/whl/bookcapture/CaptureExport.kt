package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File

/**
 * Copying captures somewhere the app does not own.
 *
 * The app's own storage is `android:allowBackup="false"` app-private storage:
 * uninstalling, clearing data, or losing the handset takes every capture with
 * it, archive included. Export is the only path off the device that does not
 * depend on the cloud round-trip working — which is exactly the thing being
 * troubleshot when it is needed.
 *
 * Destination is a Storage Access Framework tree the user picks once
 * (`ACTION_OPEN_DOCUMENT_TREE`) and the app holds a persistable grant on: an
 * SD card, a USB-OTG stick, or a cloud provider's folder. Nothing else offers
 * a user-chosen location that survives reinstall on API 26..34 without the
 * broad storage permissions this app deliberately does not request.
 *
 * Layout at the destination, one directory per capture, files verbatim:
 *
 *   <tree>/library-tool-captures/<entryId>/photo_1.jpg
 *                                          manifest.json
 *                                          meta.json
 *                                          processing.json
 *                                          photo_1.jpg.txt
 *                                          export.json
 *
 * Verbatim, not a container format: a directory of JPEGs and JSON is readable
 * by the desktop importer, by a file manager, and by a human with no tooling
 * at all a year from now. `export.json` records where the capture came from
 * (queue/sent/archive) and whether it was record-only at the time, so a
 * photo-free directory is never mistaken for a failed copy.
 *
 * Progress is bookkept locally rather than by interrogating the destination:
 * SAF metadata queries are slow enough that re-listing the tree would dominate
 * an incremental export. [ExportLedger] therefore maps entry id to a content
 * signature per destination tree, and only changed captures are rewritten. The
 * cost is that files deleted at the destination behind the app's back are not
 * noticed — hence the explicit "export everything again" path, which clears
 * the ledger for that tree instead of trying to diff it.
 */
internal object CaptureExport {

    const val EXPORT_DIR_NAME = "library-tool-captures"
    const val EXPORT_STAMP = "export.json"
    private const val LEDGER_FILE = "export_ledger.json"

    const val ORIGIN_QUEUE = "queue"
    const val ORIGIN_SENT = "sent"
    const val ORIGIN_ARCHIVE = "archive"

    /** One capture's worth of work. [signature] changes whenever any file in
     * the directory is added, removed, resized or rewritten. */
    internal data class ExportItem(
        val entryId: String,
        val sourceDir: File,
        val origin: String,
        val recordOnly: Boolean,
        val signature: String,
        val files: List<File>,
    )

    internal data class ExportPlan(
        val items: List<ExportItem>,
        val unchanged: Int,
    ) {
        val pending: Int get() = items.size
    }

    /** Content signature over the files that will actually be copied. Size and
     * mtime rather than a digest: a capture directory is mostly multi-megabyte
     * JPEGs, and hashing every one on every export would cost more than the
     * copy it is trying to avoid. Names are included so a deletion registers. */
    internal fun signatureOf(files: List<File>): String {
        val parts = files.asSequence()
            .sortedBy { it.name }
            .joinToString("|") { "${it.name}:${it.length()}:${it.lastModified()}" }
        return "v1:${parts.hashCode()}:${files.size}"
    }

    /** Everything in a capture directory except the app's own scratch. Hidden
     * temp files from [Entries.atomicWrite] are excluded; a half-written temp
     * copied to the destination would look like a real sidecar. */
    internal fun exportableFiles(dir: File): List<File> =
        (dir.listFiles() ?: emptyArray())
            .filter { it.isFile && !it.name.startsWith(".") }
            .sortedBy { it.name }

    internal fun itemFor(dir: File, origin: String): ExportItem? {
        if (!dir.isDirectory) return null
        val files = exportableFiles(dir)
        if (files.isEmpty()) return null
        return ExportItem(
            entryId = dir.name,
            sourceDir = dir,
            origin = origin,
            recordOnly = origin == ORIGIN_ARCHIVE && CaptureArchive.isRecordOnly(dir),
            signature = signatureOf(files),
            files = files,
        )
    }

    /** Every capture the device holds, archive included, newest origin first.
     * Ordering is queue -> sent -> archive so an interrupted first export gets
     * the not-yet-delivered captures out first: those are the only ones with
     * no copy anywhere else. */
    internal fun collect(ctx: Context): List<ExportItem> {
        val roots = listOf(
            Entries.queueRoot(ctx) to ORIGIN_QUEUE,
            Entries.sentRoot(ctx) to ORIGIN_SENT,
            CaptureArchive.archiveRoot(ctx) to ORIGIN_ARCHIVE,
        )
        return roots.flatMap { (root, origin) ->
            (root.listFiles { f: File -> f.isDirectory } ?: emptyArray())
                .sortedBy { it.name }
                .mapNotNull { itemFor(it, origin) }
        }
    }

    /** Drop the captures whose signature the ledger already records for this
     * destination. */
    internal fun plan(items: List<ExportItem>, exported: Map<String, String>): ExportPlan {
        val pending = items.filter { exported[it.entryId] != it.signature }
        return ExportPlan(items = pending, unchanged = items.size - pending.size)
    }

    internal fun stampJson(item: ExportItem, now: Long): String =
        JSONObject()
            .put("entry_id", item.entryId)
            .put("origin", item.origin)
            .put("record_only", item.recordOnly)
            .put("exported_at", now)
            .put("signature", item.signature)
            .put("file_count", item.files.size)
            .toString()

    // --- ledger ---------------------------------------------------------------

    /** Per-destination record of what has already been written. Keyed by the
     * tree Uri string so pointing the export at a second card does not make
     * the app believe that card already holds the captures. */
    internal object ExportLedger {

        private val lock = Any()

        private fun file(ctx: Context): File = File(ctx.filesDir, LEDGER_FILE)

        private fun readAll(ctx: Context): JSONObject = try {
            file(ctx).takeIf { it.isFile }?.readText()?.let(::JSONObject) ?: JSONObject()
        } catch (_: Exception) {
            JSONObject()
        }

        fun exported(ctx: Context, treeUri: String): Map<String, String> =
            synchronized(lock) {
                val tree = readAll(ctx).optJSONObject(treeUri) ?: return emptyMap()
                tree.keys().asSequence().associateWith { tree.optString(it) }
            }

        /** Recorded one capture at a time: an export interrupted by process
         * death must not re-copy everything it had already finished. */
        fun record(ctx: Context, treeUri: String, entryId: String, signature: String): Boolean =
            synchronized(lock) {
                try {
                    val all = readAll(ctx)
                    val tree = all.optJSONObject(treeUri) ?: JSONObject()
                    tree.put(entryId, signature)
                    all.put(treeUri, tree)
                    Entries.atomicWrite(file(ctx), all.toString())
                    true
                } catch (_: Exception) {
                    false
                }
            }

        fun forget(ctx: Context, treeUri: String) = synchronized(lock) {
            try {
                val all = readAll(ctx)
                all.remove(treeUri)
                Entries.atomicWrite(file(ctx), all.toString())
            } catch (_: Exception) {
                // A ledger that cannot be cleared only costs a skipped re-export.
            }
        }
    }
}
