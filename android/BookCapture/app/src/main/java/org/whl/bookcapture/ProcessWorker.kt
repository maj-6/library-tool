package org.whl.bookcapture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit

/**
 * Background processing, kicked after every photo and every seal: standardize
 * new photos, OCR them (Mistral), and once an entry is sealed with text,
 * extract the bibliography (DeepSeek default, Mistral fallback). Results land
 * next to the photos (photo_N.jpg.txt, meta.json) so the recent list can show
 * a book record instead of "Processing…", and the upload carries them.
 *
 * Photos are processed while the entry is still OPEN — OCR runs during the
 * seconds the user is flipping to the next page, so by "done" most of the
 * work is already behind us.
 */
class ProcessWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        private const val KEY_ENTRY_ID = "entry_id"

        fun enqueue(ctx: Context, entryId: String? = null) {
            val builder = OneTimeWorkRequestBuilder<ProcessWorker>()
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            if (!entryId.isNullOrEmpty()) builder.setInputData(workDataOf(KEY_ENTRY_ID to entryId))
            val req = builder.build()
            // APPEND_OR_REPLACE: a photo taken mid-run chains one more pass
            // (each pass rescans everything, so one queued run is enough)
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork("capture-process", ExistingWorkPolicy.APPEND_OR_REPLACE, req)
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val mistral = Prefs.mistralKey(ctx)
        val deepseek = Prefs.deepseekKey(ctx)
        val requestedId = inputData.getString(KEY_ENTRY_ID)?.takeIf { it.isNotBlank() }
        var transient = false
        var permanent: String? = null

        val dirs = requestedId?.let { id -> Entries.find(ctx, id)?.let { listOf(it.dir) } ?: emptyList() }
            ?: (Entries.queueRoot(ctx).listFiles { f: File -> f.isDirectory }?.toList() ?: emptyList())
        for (dir in dirs) {
            val forced = requestedId == dir.name
            val photos = dir.listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
                ?.sortedBy { photoNumber(it.name) } ?: continue
            for (photo in photos) {
                // a photo CameraX is still stream-copying is a truncated JPEG;
                // standardizing it would re-encode garbage over the page. Leave
                // it for the next run (its onImageSaved re-enqueues us).
                if (!isCompleteJpeg(photo)) { transient = true; continue }
                try {
                    Pipeline.standardizeInPlace(photo)
                } catch (_: Exception) { /* the original still uploads */ }
                if (mistral.isEmpty()) continue
                val sidecar = File(dir, photo.name + ".txt")
                if (sidecar.isFile) continue
                try {
                    val text = Pipeline.ocr(photo, mistral)
                    val tmp = File(dir, sidecar.name + ".tmp")
                    tmp.writeText(text)
                    tmp.renameTo(sidecar)
                } catch (e: Pipeline.PermanentError) {
                    permanent = "OCR: ${e.message?.take(120)}"
                } catch (e: Exception) {
                    transient = true
                }
            }
            // extraction: sealed, has text, not yet extracted
            if (!File(dir, "manifest.json").isFile) continue
            if (File(dir, "meta.json").isFile && !forced) continue
            if (mistral.isEmpty() && deepseek.isEmpty()) continue
            val entry = Entries.find(ctx, dir.name) ?: continue
            if (forced && deepseek.isEmpty()) {
                val message = "DeepSeek API key is missing"
                entry.finishReprocess(message)
                permanent = message
                continue
            }
            val text = entry.ocrText()
            // wait for every photo's OCR before extracting, else the fields
            // come from half a title page and never get revised
            if (text.isEmpty() || photos.any { !File(dir, it.name + ".txt").isFile }) {
                if (forced) {
                    val message = permanent ?: if (mistral.isEmpty())
                        "OCR is incomplete; add a Mistral key and try again"
                    else {
                        transient = true
                        null
                    }
                    if (message != null) {
                        entry.finishReprocess(message)
                        permanent = message
                    }
                }
                continue
            }
            try {
                val meta = Pipeline.extract(text, deepseek, mistral, entry.customInstructions())
                Entries.atomicWrite(File(dir, "meta.json"), meta.toString())
                entry.finishReprocess()
            } catch (e: Pipeline.PermanentError) {
                val message = "extract: ${e.message?.take(120)}"
                if (forced) entry.finishReprocess(message)
                permanent = message
            } catch (e: Exception) {
                transient = true
            }
        }

        Prefs.setLastProcError(ctx, permanent)
        // freshly processed entries may be ready to ship
        UploadWorker.kick(ctx)
        when {
            transient -> Result.retry()
            permanent != null -> Result.failure()   // fixed keys re-enqueue this
            else -> Result.success()
        }
    }

    /** A complete JPEG ends in the EOI marker FF D9; a file mid-write does not.
     *  Cheap two-byte tail read, so a half-copied capture is never processed. */
    private fun isCompleteJpeg(f: File): Boolean = try {
        val len = f.length()
        len >= 4 && RandomAccessFile(f, "r").use { raf ->
            raf.seek(len - 2)
            raf.read() == 0xFF && raf.read() == 0xD9
        }
    } catch (_: Exception) { false }
}
