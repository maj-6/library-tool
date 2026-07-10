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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Drains the sealed capture queue to Supabase whenever the network allows:
 * photos into the `captures` bucket ("<device>/<entryId>/photo_N.jpg"), then
 * one `captures` table row. The local folder is deleted only after both
 * succeed, and WorkManager retries with backoff — captures survive offline
 * sessions, app restarts, and flaky uploads (uploads are upsert-idempotent).
 */
class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        fun enqueue(ctx: Context) {
            val req = OneTimeWorkRequestBuilder<UploadWorker>()
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork("capture-upload", ExistingWorkPolicy.APPEND_OR_REPLACE, req)
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!Prefs.configured(applicationContext)) return@withContext Result.failure()
        val client = SupabaseClient(
            Prefs.supabaseUrl(applicationContext), Prefs.supabaseKey(applicationContext))
        val session = CaptureSession(applicationContext)
        var failed = false
        for (dir in session.pendingUploads()) {
            try {
                uploadEntry(client, dir)
                dir.deleteRecursively()
            } catch (e: org.json.JSONException) {
                // corrupt manifest: retrying forever would wedge the queue —
                // set the folder aside instead of deleting the photos
                val bad = File(File(applicationContext.filesDir, "failed"), dir.name)
                bad.parentFile?.mkdirs()
                if (!dir.renameTo(bad)) dir.deleteRecursively()
            } catch (e: Exception) {
                failed = true            // transient: keep the folder; retry later
            }
        }
        if (failed) Result.retry() else Result.success()
    }

    private fun uploadEntry(client: SupabaseClient, dir: File) {
        val manifest = JSONObject(File(dir, "manifest.json").readText())
        val id = manifest.getString("id")
        val device = manifest.optString("device", "phone")
        val deviceSafe = device.replace(Regex("[^A-Za-z0-9._-]"), "_").ifEmpty { "phone" }
        val names = manifest.getJSONArray("photos")
        val remote = mutableListOf<String>()
        for (i in 0 until names.length()) {
            val name = names.getString(i)
            val f = File(dir, name)
            if (!f.isFile) continue
            val objectPath = "$deviceSafe/$id/$name"
            client.uploadPhoto(objectPath, f)
            remote.add(objectPath)
        }
        if (remote.isEmpty()) return      // nothing usable; drop the folder
        client.insertCapture(id, device, remote, manifest.optString("note", ""))
    }
}
