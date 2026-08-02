package org.whl.bookcapture

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import androidx.work.BackoffPolicy
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.TimeUnit

internal const val EXPORT_PROGRESS_DONE = "export-done"
internal const val EXPORT_PROGRESS_TOTAL = "export-total"
internal const val EXPORT_PROGRESS_SKIPPED = "export-skipped"
internal const val EXPORT_PROGRESS_FAILED = "export-failed"
internal const val EXPORT_RESULT_ERROR = "export-error"

/** MIME by extension. The destination provider stores what it is told, and a
 * wrong type here is what makes a gallery refuse to open an exported page. */
internal fun exportMimeType(name: String): String = when {
    name.endsWith(".jpg", true) || name.endsWith(".jpeg", true) -> "image/jpeg"
    name.endsWith(".png", true) -> "image/png"
    name.endsWith(".json", true) -> "application/json"
    name.endsWith(".txt", true) -> "text/plain"
    else -> "application/octet-stream"
}

/**
 * Copies every capture on the device into a user-chosen Storage Access
 * Framework tree. See [CaptureExport] for the layout and why it is verbatim
 * files rather than an archive container.
 *
 * One capture is committed at a time: its files are written, then its stamp,
 * then the ledger entry. A kill between any two captures costs at most the
 * one in flight, because the ledger is only advanced after the stamp lands.
 * The stamp is written last within a capture for the same reason — a
 * directory without `export.json` is an interrupted copy, and the next run
 * rewrites it.
 */
class CaptureExportWorker(ctx: Context, params: WorkerParameters) :
    CoroutineWorker(ctx, params) {

    companion object {
        const val UNIQUE_WORK_NAME = "capture-export"
        private const val KEY_TREE_URI = "export_tree_uri"
        private const val KEY_FULL = "export_full"

        fun enqueue(ctx: Context, treeUri: String, fullReexport: Boolean) {
            if (treeUri.isBlank()) return
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                UNIQUE_WORK_NAME,
                ExistingWorkPolicy.REPLACE,
                OneTimeWorkRequestBuilder<CaptureExportWorker>()
                    .setInputData(
                        workDataOf(
                            KEY_TREE_URI to treeUri,
                            KEY_FULL to fullReexport,
                        ),
                    )
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                    .build(),
            )
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val treeUriText = inputData.getString(KEY_TREE_URI)?.takeIf { it.isNotBlank() }
            ?: return@withContext Result.failure(
                workDataOf(EXPORT_RESULT_ERROR to "No export folder is set"),
            )
        val treeUri = try {
            Uri.parse(treeUriText)
        } catch (_: Exception) {
            return@withContext Result.failure(
                workDataOf(EXPORT_RESULT_ERROR to "Export folder is unreadable"),
            )
        }
        if (inputData.getBoolean(KEY_FULL, false)) {
            CaptureExport.ExportLedger.forget(ctx, treeUriText)
        }

        val resolver = ctx.contentResolver
        if (!Prefs.exportGrantHeld(ctx, treeUriText)) {
            return@withContext Result.failure(
                workDataOf(EXPORT_RESULT_ERROR to "Export folder permission was revoked"),
            )
        }

        val all = CaptureExport.collect(ctx)
        val plan = CaptureExport.plan(all, CaptureExport.ExportLedger.exported(ctx, treeUriText))
        setProgress(
            workDataOf(
                EXPORT_PROGRESS_DONE to 0,
                EXPORT_PROGRESS_TOTAL to plan.pending,
                EXPORT_PROGRESS_SKIPPED to plan.unchanged,
                EXPORT_PROGRESS_FAILED to 0,
            ),
        )
        if (plan.items.isEmpty()) {
            return@withContext Result.success(
                workDataOf(
                    EXPORT_PROGRESS_DONE to 0,
                    EXPORT_PROGRESS_TOTAL to 0,
                    EXPORT_PROGRESS_SKIPPED to plan.unchanged,
                    EXPORT_PROGRESS_FAILED to 0,
                ),
            )
        }

        val root = try {
            openExportRoot(resolver, treeUri)
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            null
        } ?: return@withContext Result.failure(
            workDataOf(EXPORT_RESULT_ERROR to "Could not open the export folder"),
        )

        var done = 0
        var failed = 0
        for (item in plan.items) {
            currentCoroutineContext().ensureActive()
            // The capture must not move between the file listing and the copy.
            val copied = EntryOperationLocks.withLock(item.entryId) {
                runCatching { exportOne(resolver, treeUri, root, item) }.getOrDefault(false)
            }
            if (copied) {
                CaptureExport.ExportLedger.record(ctx, treeUriText, item.entryId, item.signature)
                done++
            } else {
                failed++
            }
            setProgress(
                workDataOf(
                    EXPORT_PROGRESS_DONE to done,
                    EXPORT_PROGRESS_TOTAL to plan.pending,
                    EXPORT_PROGRESS_SKIPPED to plan.unchanged,
                    EXPORT_PROGRESS_FAILED to failed,
                ),
            )
        }
        Prefs.setLastExportSummary(ctx, done, failed, plan.unchanged, System.currentTimeMillis())
        Result.success(
            workDataOf(
                EXPORT_PROGRESS_DONE to done,
                EXPORT_PROGRESS_TOTAL to plan.pending,
                EXPORT_PROGRESS_SKIPPED to plan.unchanged,
                EXPORT_PROGRESS_FAILED to failed,
            ),
        )
    }

    /** The one app-owned directory inside the user's tree. Everything else in
     * that folder belongs to the user and is never touched. */
    private fun openExportRoot(resolver: ContentResolver, treeUri: Uri): Uri? {
        val rootDoc = DocumentsContract.buildDocumentUriUsingTree(
            treeUri,
            DocumentsContract.getTreeDocumentId(treeUri),
        )
        return findChild(resolver, treeUri, rootDoc, CaptureExport.EXPORT_DIR_NAME)
            ?: DocumentsContract.createDocument(
                resolver,
                rootDoc,
                DocumentsContract.Document.MIME_TYPE_DIR,
                CaptureExport.EXPORT_DIR_NAME,
            )
    }

    private fun exportOne(
        resolver: ContentResolver,
        treeUri: Uri,
        root: Uri,
        item: CaptureExport.ExportItem,
    ): Boolean {
        val dir = findChild(resolver, treeUri, root, item.entryId)
            ?: DocumentsContract.createDocument(
                resolver,
                root,
                DocumentsContract.Document.MIME_TYPE_DIR,
                item.entryId,
            )
            ?: return false
        for (file in item.files) {
            if (file.name == CaptureExport.EXPORT_STAMP) continue
            if (!writeFile(resolver, treeUri, dir, file.name, file.readBytes())) return false
        }
        // Last, so an interrupted copy has no stamp and is redone next run.
        return writeFile(
            resolver,
            treeUri,
            dir,
            CaptureExport.EXPORT_STAMP,
            CaptureExport.stampJson(item, System.currentTimeMillis()).toByteArray(),
        )
    }

    /** Reuse an existing document rather than creating a second one:
     * [DocumentsContract.createDocument] resolves a name collision by renaming
     * ("photo_1 (1).jpg"), which would accumulate a copy per export. */
    private fun writeFile(
        resolver: ContentResolver,
        treeUri: Uri,
        parent: Uri,
        name: String,
        bytes: ByteArray,
    ): Boolean = try {
        val target = findChild(resolver, treeUri, parent, name)
            ?: DocumentsContract.createDocument(resolver, parent, exportMimeType(name), name)
        target != null && resolver.openOutputStream(target, "wt")?.use { out ->
            out.write(bytes)
            out.flush()
            true
        } == true
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        false
    }

    private fun findChild(
        resolver: ContentResolver,
        treeUri: Uri,
        parent: Uri,
        name: String,
    ): Uri? = try {
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(
            treeUri,
            DocumentsContract.getDocumentId(parent),
        )
        resolver.query(
            children,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            ),
            null,
            null,
            null,
        )?.use { cursor ->
            var found: Uri? = null
            while (cursor.moveToNext()) {
                if (cursor.getString(1) == name) {
                    found = DocumentsContract.buildDocumentUriUsingTree(treeUri, cursor.getString(0))
                    break
                }
            }
            found
        }
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        null
    }
}
