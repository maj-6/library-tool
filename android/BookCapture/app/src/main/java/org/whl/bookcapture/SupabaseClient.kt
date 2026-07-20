package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL

internal data class PrivateObjectDownload(
    val contentType: String,
    val bytes: Long,
)

private val SAFE_CLOUD_FILTER_TOKEN = Regex("[A-Za-z0-9._-]+")

private fun encodedStoragePath(value: String): String = value.split('/').joinToString("/") {
    URLEncoder.encode(it, Charsets.UTF_8.name()).replace("+", "%20")
}

internal fun cloudCollectionFromJson(row: JSONObject): BookCollection? {
    val id = row.optString("id").trim()
    val name = normalizeCollectionField(row.optString("name"))
    if (id.isEmpty() || name.isEmpty()) return null
    val mergedInto = if (row.isNull("merged_into")) null
    else row.optString("merged_into").trim().ifEmpty { null }
    val parentId = if (row.isNull("parent_id")) null
    else row.optString("parent_id").trim().ifEmpty { null }
    return BookCollection(
        id = id,
        name = name,
        from = normalizeCollectionField(row.optString("from_place")),
        updatedAt = row.optString("updated_at").trim(),
        deleted = row.optBoolean("deleted", false),
        mergedInto = mergedInto,
        parentId = parentId,
    )
}

internal const val COLLECTION_PAGE_SIZE = 500

/** Consume stable id-keyset pages. Looping until an empty page also works when
 * a project's PostgREST max_rows is configured below [COLLECTION_PAGE_SIZE]. */
internal fun collectCollectionPages(
    fetchPage: (afterId: String?) -> JSONArray,
): List<BookCollection> {
    val out = mutableListOf<BookCollection>()
    var afterId: String? = null
    while (true) {
        val rows = fetchPage(afterId)
        if (rows.length() == 0) return out
        for (index in 0 until rows.length()) {
            rows.optJSONObject(index)?.let(::cloudCollectionFromJson)?.let(out::add)
        }
        val next = rows.optJSONObject(rows.length() - 1)
            ?.optString("id")
            ?.trim()
            .orEmpty()
        if (next.isEmpty() || next == afterId) {
            throw IOException("collection pagination did not advance")
        }
        afterId = next
    }
}

/** Keep the LWW stamp isolated here: a future reviewed server-time protocol can
 * replace this field without changing the local store or merge algorithm. */
internal fun collectionCloudBody(
    row: BookCollection,
    ownerId: String? = null,
    includeUpdatedAt: Boolean = true,
): JSONObject = JSONObject()
    .put("id", row.id)
    .put("name", row.name)
    .put("from_place", row.from)
    // JSON null deliberately clears a parent during PATCH.
    .put("parent_id", row.parentId ?: JSONObject.NULL)
    .put("deleted", row.deleted)
    .apply {
        ownerId?.takeIf { it.isNotEmpty() }?.let { put("created_by", it) }
        if (includeUpdatedAt && row.updatedAt.isNotEmpty()) put("updated_at", row.updatedAt)
    }

/**
 * Supabase REST for the capture flow, authorized as the signed-in USER: the
 * apikey header carries the public anon key and the bearer token is the
 * account's — row-level security does the rest (captures_insert_own,
 * captures bucket policies). The service_role key never touches the phone.
 */
class SupabaseClient(
    private val ctx: Context,
    expectedUserId: String? = null,
) {

    class HttpException(val code: Int, message: String) : IOException(message)
    class SignedOut : IOException("signed out")
    class AccountChanged : IOException("account changed during delivery")
    class ObjectTooLarge : IOException("private object exceeds the download limit")

    private val baseUrl = Prefs.supabaseUrl(ctx)
    private val ownerId = expectedUserId?.trim().orEmpty().ifEmpty { Prefs.userId(ctx) }
    private val contributor = Prefs.displayName(ctx).ifEmpty { Prefs.email(ctx) }

    private fun open(method: String, url: String, contentType: String?): HttpURLConnection {
        if (ownerId.isEmpty() || Prefs.userId(ctx) != ownerId) throw AccountChanged()
        val token = Auth.accessToken(ctx) ?: throw SignedOut()
        if (Prefs.userId(ctx) != ownerId) throw AccountChanged()
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 20_000
        conn.readTimeout = 120_000
        conn.setRequestProperty("apikey", Prefs.anonKey(ctx))
        conn.setRequestProperty("Authorization", "Bearer $token")
        if (contentType != null) conn.setRequestProperty("Content-Type", contentType)
        return conn
    }

    private fun finish(conn: HttpURLConnection): String {
        val code = conn.responseCode
        val body = try {
            (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.use { it.readBytes().decodeToString() } ?: ""
        } catch (_: Exception) { "" }
        if (code !in 200..299) throw HttpException(code, "HTTP $code: ${body.take(300)}")
        return body
    }

    /** Upload one JPEG; objectPath like "PixelBooth/abcd1234/photo_1.jpg". */
    fun uploadPhoto(objectPath: String, file: File) {
        val conn = open("POST", "$baseUrl/storage/v1/object/captures/$objectPath", "image/jpeg")
        conn.setRequestProperty("x-upsert", "true")   // retried uploads overwrite
        conn.doOutput = true
        conn.setFixedLengthStreamingMode(file.length())
        conn.outputStream.use { out -> file.inputStream().use { it.copyTo(out) } }
        finish(conn)
    }

    /** Insert the capture row the desktop sync will pick up, carrying the
     *  contributor and whatever the phone already extracted. */
    fun insertCapture(id: String, device: String, photoPaths: List<String>, note: String,
                      createdAt: String, ocr: JSONObject, meta: JSONObject) {
        val body = JSONObject()
            .put("id", id)
            .put("device", device)
            .put("status", "pending")
            .put("photos", JSONArray(photoPaths))
            .put("note", note)
            .put("created_by", ownerId)
            .put("contributor", contributor)
            .put("ocr", ocr)
            .put("meta", meta)
        if (createdAt.isNotEmpty()) body.put("created_at", createdAt)
        val conn = open("POST", "$baseUrl/rest/v1/captures", "application/json")
        // ignore-duplicates: a retried upload after the desktop already imported
        // the row must NOT reset its status back to pending
        conn.setRequestProperty("Prefer", "return=minimal,resolution=ignore-duplicates")
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toString().toByteArray()) }
        finish(conn)
    }

    /** status per capture id for OUR rows (RLS scopes the select) — how the
     *  recent list learns "uploaded" became "imported". */
    fun captureStatuses(ids: List<String>): Map<String, String> {
        if (ids.isEmpty()) return emptyMap()
        val list = ids.joinToString(",")
        val conn = open("GET", "$baseUrl/rest/v1/captures?id=in.($list)&select=id,status", null)
        val rows = JSONArray(finish(conn).ifEmpty { "[]" })
        return (0 until rows.length()).associate {
            rows.getJSONObject(it).let { r -> r.getString("id") to r.optString("status") }
        }
    }

    /** Owner-readable processing rows for the sent captures retained locally. */
    internal fun photoProcessingJobs(ids: List<String>): List<CloudPhotoProcessingJob> {
        val safeIds = ids.distinct().filter {
            it.isNotEmpty() && it.length <= 160 && it.matches(SAFE_CLOUD_FILTER_TOKEN) &&
                it != "." && it != ".."
        }
        if (safeIds.isEmpty()) return emptyList()
        val filter = safeIds.joinToString(",") {
            URLEncoder.encode(it, Charsets.UTF_8.name())
        }
        val select = "id,capture_id,owner_id,asset_id,request_id,request_revision," +
            "source_sha256,state,result,last_error"
        val conn = open(
            "GET",
            "$baseUrl/rest/v1/photo_processing_jobs" +
                "?capture_id=in.($filter)&select=$select" +
                "&order=capture_id.asc,asset_id.asc,request_revision.asc&limit=1000",
            null,
        )
        val rows = JSONArray(finish(conn).ifEmpty { "[]" })
        return (0 until rows.length()).map { index ->
            rows.optJSONObject(index)?.let(::cloudPhotoProcessingJobFromJson)
                ?: throw IOException("invalid photo processing job row")
        }
    }

    /**
     * Stream one private object with the signed-in user's JWT. The caller still
     * verifies the result-declared MIME, exact byte count, JPEG structure,
     * dimensions, and checksum before installation.
     */
    internal fun downloadPrivateObject(
        bucket: String,
        objectPath: String,
        destination: File,
        maxBytes: Long,
    ): PrivateObjectDownload {
        require(maxBytes > 0L) { "maxBytes must be positive" }
        val url = "$baseUrl/storage/v1/object/authenticated/" +
            "${encodedStoragePath(bucket)}/${encodedStoragePath(objectPath)}"
        val conn = open("GET", url, null)
        destination.delete()
        try {
            val code = conn.responseCode
            if (code !in 200..299) {
                finish(conn)
                throw HttpException(code, "HTTP $code")
            }
            val declared = conn.contentLengthLong
            if (declared > maxBytes) throw ObjectTooLarge()
            var total = 0L
            conn.inputStream.use { input ->
                destination.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        val read = input.read(buffer)
                        if (read < 0) break
                        if (read == 0) continue
                        total += read
                        if (total > maxBytes) throw ObjectTooLarge()
                        output.write(buffer, 0, read)
                    }
                    output.flush()
                }
            }
            return PrivateObjectDownload(conn.contentType.orEmpty(), total)
        } catch (e: Exception) {
            destination.delete()
            throw e
        } finally {
            conn.disconnect()
        }
    }

    /** All shared collection rows, including soft-deleted tombstones. */
    fun collections(): List<BookCollection> = collectCollectionPages { afterId ->
        val cursor = afterId?.let {
            "&id=gt.${URLEncoder.encode(it, Charsets.UTF_8.name())}"
        }.orEmpty()
        val conn = open(
            "GET",
            "$baseUrl/rest/v1/collections" +
                "?select=id,name,from_place,updated_at,deleted,merged_into,parent_id" +
                "&order=id.asc&limit=$COLLECTION_PAGE_SIZE$cursor",
            null,
        )
        JSONArray(finish(conn).ifEmpty { "[]" })
    }

    /**
     * Apply one merge decision without overwriting a row that changed after
     * the preceding GET. Inserts ignore an id that appeared concurrently;
     * updates compare-and-set its exact `updated_at`. A null result is a benign
     * race and causes CollectionSyncWorker to fetch and merge again.
     */
    internal fun writeCollection(write: CollectionCloudWrite): BookCollection? {
        val expected = write.expectedCloudUpdatedAt
        val row = if (expected == null) write.row else write.row.copy(
            updatedAt = collectionPatchTimestamp(expected),
        )
        val (method, url, body) = if (expected == null) {
            Triple(
                "POST",
                "$baseUrl/rest/v1/collections?on_conflict=id",
                // There is no remote revision to order against yet. Let
                // Postgres default now() establish a trustworthy baseline so
                // a phone clock set years ahead cannot poison shared LWW.
                collectionCloudBody(row, ownerId, includeUpdatedAt = false),
            )
        } else {
            val idFilter = URLEncoder.encode(row.id, Charsets.UTF_8.name())
            val revisionFilter = URLEncoder.encode(expected, Charsets.UTF_8.name())
            // id/created_by are immutable after insert, matching the column
            // grants in migration 009.
            val patch = collectionCloudBody(row)
            patch.remove("id")
            Triple(
                "PATCH",
                "$baseUrl/rest/v1/collections" +
                    "?id=eq.$idFilter&updated_at=eq.$revisionFilter",
                patch,
            )
        }
        val conn = open(method, url, "application/json")
        conn.setRequestProperty(
            "Prefer",
            if (expected == null) "resolution=ignore-duplicates,return=representation"
            else "return=representation",
        )
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toString().toByteArray()) }
        val rows = JSONArray(finish(conn).ifEmpty { "[]" })
        return rows.optJSONObject(0)?.let(::cloudCollectionFromJson)
    }

    /** Settings probe: is the session alive and can this account file captures? */
    fun testConnection(): String? = try {
        val conn = open("GET", "$baseUrl/rest/v1/captures?select=id&limit=1", null)
        finish(conn)
        null
    } catch (e: SignedOut) {
        "signed out — sign in again"
    } catch (e: Exception) {
        e.message ?: e.javaClass.simpleName
    }
}
