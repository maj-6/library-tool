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
import java.time.Instant
import java.util.concurrent.TimeUnit

/**
 * Drains the sealed capture queue to Supabase whenever the network allows:
 * photos into the `captures` bucket ("<device>/<entryId>/photo_N.jpg"), then
 * one `captures` table row carrying the contributor and whatever OCR/meta the
 * background pipeline has produced. Uploads run as the signed-in user; the
 * folder moves to sent/ (the recent list's history) only after both steps.
 *
 * A freshly sealed entry gets a grace period for the pipeline to finish, so
 * the row usually ships WITH its extraction; an entry that cannot process
 * (no keys, hard API error) ships anyway once it ages past the window —
 * photos are the cargo, metadata is the bonus.
 *
 * Errors split two ways: transient (network, 5xx) retries with backoff;
 * permanent (4xx: revoked session, missing policies) stops the chain and is
 * recorded for the main screen.
 */
class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        private const val PROCESS_GRACE_MS = 10 * 60 * 1000L

        /** Fresh run now, fresh backoff clock. REPLACE cancels a chain that
         *  may be hours into exponential backoff — right after "done" or a
         *  settings fix, waiting that out is wrong. Safe because every run
         *  drains the whole queue and uploads are upsert-idempotent. */
        fun enqueue(ctx: Context) {
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork("capture-upload", ExistingWorkPolicy.REPLACE, request())
        }

        /** Opportunistic nudge (onResume, post-processing): starts a run only
         *  if none is queued, never resets a live chain's backoff. */
        fun kick(ctx: Context) {
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork("capture-upload", ExistingWorkPolicy.KEEP, request())
        }

        private fun request() = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()

        // 4xx is permanent — except the two that are really the network's or
        // the server's mood: timeout and rate limit.
        private fun permanent(code: Int) = code in 400..499 && code != 408 && code != 429
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val session = CaptureSession(ctx)
        session.recoverOrphans()      // crash leftovers become uploads, not leaks

        // transport: a paired desktop over the LAN (offline) when selected, or
        // in "auto" when it answers; otherwise the cloud path below.
        val mode = Prefs.transport(ctx)
        val lan = if (mode != "cloud" && Prefs.lanHost(ctx).isNotEmpty())
            try { LanClient(ctx) } catch (_: Exception) { null } else null
        if (lan != null && (mode == "lan" || (mode == "auto" && lan.ping())))
            return@withContext drainViaLan(ctx, session, lan)

        if (!Prefs.configured(ctx) || !Auth.signedIn(ctx)) {
            Prefs.setLastUploadError(ctx, if (Auth.signedIn(ctx)) null else "signed out")
            return@withContext Result.failure()
        }
        val client = SupabaseClient(ctx)
        var transient = false
        var permanentError: String? = null
        val now = System.currentTimeMillis()

        for (dir in session.pendingUploads()) {
            val entry = Entries.find(ctx, dir.name) ?: continue
            if (File(dir, Entries.REPROCESS_PENDING).isFile) continue
            // give the pipeline its window, unless nothing will ever process
            val canProcess = Prefs.mistralKey(ctx).isNotEmpty()
            if (canProcess && entry.meta == null && now - entry.createdAt < PROCESS_GRACE_MS)
                continue                              // ProcessWorker kicks us after
            try {
                uploadEntry(client, dir)
                markUploaded(ctx, dir)
            } catch (e: org.json.JSONException) {
                // torn manifest: delete it and let the next run's
                // recoverOrphans reseal the folder from the photos on disk
                File(dir, "manifest.json").delete()
                transient = true
            } catch (e: SupabaseClient.SignedOut) {
                permanentError = "signed out"
            } catch (e: SupabaseClient.HttpException) {
                if (permanent(e.code)) permanentError = e.message?.take(120) ?: "HTTP ${e.code}"
                else transient = true
            } catch (e: Exception) {
                transient = true      // network et al: keep the folder; retry later
            }
        }

        pollImports(ctx, client)
        Entries.pruneSent(ctx)
        // set or clear: any full drain wipes a stale error off the main screen
        Prefs.setLastUploadError(ctx, permanentError)
        when {
            transient -> Result.retry()
            permanentError != null -> Result.failure()   // enqueue() after the fix restarts it
            else -> Result.success()
        }
    }

    private fun uploadEntry(client: SupabaseClient, dir: File) {
        val manifest = JSONObject(File(dir, "manifest.json").readText())
        val id = manifest.getString("id")
        val device = manifest.optString("device", "phone")
        val deviceSafe = device.replace(Regex("[^A-Za-z0-9._-]"), "_")
            .trim('.').ifEmpty { "phone" }        // "." / ".." would bend the URL path
        val names = manifest.getJSONArray("photos")
        val remote = mutableListOf<String>()
        val ocr = JSONObject()
        for (i in 0 until names.length()) {
            val name = names.getString(i)
            val f = File(dir, name)
            if (!f.isFile) continue
            val objectPath = "$deviceSafe/$id/$name"
            client.uploadPhoto(objectPath, f)
            remote.add(objectPath)
            File(dir, "$name.txt").takeIf { it.isFile }
                ?.let { ocr.put(name, it.readText().take(20_000)) }
        }
        if (remote.isEmpty()) return      // nothing usable; drop the folder
        val createdMs = manifest.optLong("created_at", 0L)
        val createdAt = if (createdMs > 0) Instant.ofEpochMilli(createdMs).toString() else ""
        val meta = File(dir, "meta.json").takeIf { it.isFile }
            ?.let { try { JSONObject(it.readText()) } catch (_: Exception) { null } }
            ?: JSONObject()
        client.insertCapture(id, device, remote, manifest.optString("note", ""),
                             createdAt, ocr, meta)
    }

    /** queue/<id> -> sent/<id>, stamped; the recent list's "uploaded". */
    private fun markUploaded(ctx: Context, dir: File) {
        val manifest = JSONObject(File(dir, "manifest.json").readText())
            .put("uploaded_at", System.currentTimeMillis())
            .put("cloud_status", "pending")
        File(dir, "manifest.json").writeText(manifest.toString())
        val target = File(Entries.sentRoot(ctx), dir.name)
        if (!dir.renameTo(target)) dir.deleteRecursively()   // same volume; can't loop
    }

    // --- LAN transport ----------------------------------------------------------

    /** POST each queued entry to the paired desktop, which imports synchronously
     *  — a 200 IS "imported", so there is nothing to poll afterwards. No signed-in
     *  account or grace wait is needed: the desktop does its own OCR on ingest. */
    private fun drainViaLan(ctx: Context, session: CaptureSession, client: LanClient): Result {
        var transient = false
        var permanentError: String? = null
        for (dir in session.pendingUploads()) {
            Entries.find(ctx, dir.name) ?: continue
            try {
                uploadEntryLan(client, dir)
                markSentImported(ctx, dir)
            } catch (e: org.json.JSONException) {
                File(dir, "manifest.json").delete()           // torn: reseal next run
                transient = true
            } catch (e: LanClient.HttpException) {
                if (permanent(e.code)) permanentError = e.message?.take(120) ?: "HTTP ${e.code}"
                else transient = true
            } catch (e: Exception) {
                transient = true                              // desktop unreachable: retry
            }
        }
        Entries.pruneSent(ctx)
        Prefs.setLastUploadError(ctx, permanentError)
        return when {
            transient -> Result.retry()
            permanentError != null -> Result.failure()
            else -> Result.success()
        }
    }

    private fun uploadEntryLan(client: LanClient, dir: File) {
        val manifest = JSONObject(File(dir, "manifest.json").readText())
        val id = manifest.getString("id")
        val device = manifest.optString("device", "phone")
        val names = manifest.getJSONArray("photos")
        val photos = mutableListOf<Pair<String, File>>()
        val ocr = JSONObject()
        for (i in 0 until names.length()) {
            val name = names.getString(i)
            val f = File(dir, name)
            if (!f.isFile) continue
            photos.add(name to f)
            File(dir, "$name.txt").takeIf { it.isFile }
                ?.let { ocr.put(name, it.readText().take(20_000)) }
        }
        if (photos.isEmpty()) return
        val createdMs = manifest.optLong("created_at", 0L)
        val createdAt = if (createdMs > 0) Instant.ofEpochMilli(createdMs).toString() else ""
        val meta = File(dir, "meta.json").takeIf { it.isFile }
            ?.let { try { JSONObject(it.readText()) } catch (_: Exception) { null } }
            ?: JSONObject()
        client.uploadCapture(id, device, manifest.optString("note", ""),
                             createdAt, ocr, meta, photos)
    }

    /** queue/<id> -> sent/<id>, marked imported (LAN import is synchronous). */
    private fun markSentImported(ctx: Context, dir: File) {
        val mf = File(dir, "manifest.json")
        mf.writeText(JSONObject(mf.readText())
            .put("uploaded_at", System.currentTimeMillis())
            .put("cloud_status", "imported").toString())
        val target = File(Entries.sentRoot(ctx), dir.name)
        if (!dir.renameTo(target)) dir.deleteRecursively()
    }

    /** Ask the cloud whether the desktop has imported what we sent. */
    private fun pollImports(ctx: Context, client: SupabaseClient) {
        val waiting = Entries.recent(ctx)
            .filter { it.uploaded && it.cloudStatus != "imported" }
        if (waiting.isEmpty()) return
        val statuses = try {
            client.captureStatuses(waiting.map { it.id })
        } catch (_: Exception) { return }            // cosmetic; never fails the run
        for (e in waiting) {
            val s = statuses[e.id] ?: continue
            if (s == e.cloudStatus) continue
            try {
                val mf = File(e.dir, "manifest.json")
                mf.writeText(JSONObject(mf.readText()).put("cloud_status", s).toString())
            } catch (_: Exception) { }
        }
    }
}
