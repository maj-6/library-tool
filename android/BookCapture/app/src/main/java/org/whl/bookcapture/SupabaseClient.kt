package org.whl.bookcapture

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL
import java.util.concurrent.TimeUnit

internal data class PrivateObjectDownload(
    val contentType: String,
    val bytes: Long,
)

private val SAFE_CLOUD_FILTER_TOKEN = Regex("[A-Za-z0-9._-]+")
private const val DEFAULT_SUPABASE_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
private const val SUPABASE_ERROR_RESPONSE_MAX_BYTES = 16 * 1024
private const val CAPTURE_REVIEW_RESPONSE_MAX_BYTES = 128 * 1024
private const val CAPTURE_IMPORT_ROW_MAX_BYTES = 12 * 1024
internal const val SUPABASE_PHOTO_CONNECT_TIMEOUT_MS = 20_000L
internal const val SUPABASE_PHOTO_READ_TIMEOUT_MS = 120_000L
internal const val SUPABASE_PHOTO_WRITE_TIMEOUT_MS = 120_000L
internal const val SUPABASE_PHOTO_CALL_TIMEOUT_MS = 150_000L

private val JPEG_MEDIA_TYPE = "image/jpeg".toMediaType()

/** A box listing projects short jsonb fields rather than the whole `meta`, so a
 * [REMOTE_COLLECTION_BOOKS_PAGE_SIZE]-row page stays bounded independently of
 * the number of pages in the complete snapshot. */
private const val CAPTURE_COLLECTION_RESPONSE_MAX_BYTES = 2 * 1024 * 1024

/** Collection ids reach a PostgREST `in.(...)` list unquoted, so restrict them to
 * bare uuid characters rather than trusting a synced value. */
internal val SAFE_COLLECTION_FILTER_ID =
    Regex("[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

private class SupabaseResponseTooLarge : IOException("Supabase response is too large")

internal fun readBoundedSupabaseResponse(input: InputStream, maximum: Int): ByteArray {
    require(maximum > 0) { "maximum must be positive" }
    val output = java.io.ByteArrayOutputStream(minOf(maximum, 64 * 1024))
    val buffer = ByteArray(16 * 1024)
    var total = 0
    while (true) {
        val count = input.read(buffer)
        if (count < 0) break
        total += count
        if (total > maximum) throw SupabaseResponseTooLarge()
        output.write(buffer, 0, count)
    }
    return output.toByteArray()
}

/** Photo bodies previously used HttpURLConnection, whose write could wait
 * forever when a peer accepted the socket but stopped consuming the body.
 * OkHttp applies the write timeout while streaming and the call timeout as a
 * final bound over DNS, connect, request, and response work. */
internal fun newSupabasePhotoUploadClient(
    connectTimeoutMs: Long = SUPABASE_PHOTO_CONNECT_TIMEOUT_MS,
    readTimeoutMs: Long = SUPABASE_PHOTO_READ_TIMEOUT_MS,
    writeTimeoutMs: Long = SUPABASE_PHOTO_WRITE_TIMEOUT_MS,
    callTimeoutMs: Long = SUPABASE_PHOTO_CALL_TIMEOUT_MS,
): OkHttpClient {
    require(connectTimeoutMs > 0) { "connect timeout must be bounded" }
    require(readTimeoutMs > 0) { "read timeout must be bounded" }
    require(writeTimeoutMs > 0) { "write timeout must be bounded" }
    require(callTimeoutMs > 0) { "call timeout must be bounded" }
    return OkHttpClient.Builder()
        .connectTimeout(connectTimeoutMs, TimeUnit.MILLISECONDS)
        .readTimeout(readTimeoutMs, TimeUnit.MILLISECONDS)
        .writeTimeout(writeTimeoutMs, TimeUnit.MILLISECONDS)
        .callTimeout(callTimeoutMs, TimeUnit.MILLISECONDS)
        .build()
}

private val supabasePhotoUploadClient by lazy(::newSupabasePhotoUploadClient)

internal fun newSupabasePhotoUploadRequest(
    url: String,
    anonKey: String,
    accessToken: String,
    file: File,
): Request = Request.Builder()
    .url(url)
    .header("apikey", anonKey)
    .header("Authorization", "Bearer $accessToken")
    .header("Content-Type", "image/jpeg")
    .header("x-upsert", "true")
    .post(file.asRequestBody(JPEG_MEDIA_TYPE))
    .build()

/** Execute a photo upload while retaining SupabaseClient's response and
 * account-change semantics. OkHttp's call timeout cancels the exchange when
 * any individual network timeout cannot make progress. */
internal fun executeSupabasePhotoUpload(
    client: OkHttpClient,
    request: Request,
    ensureOwnerStillCurrent: () -> Unit,
) {
    client.newCall(request).execute().use { response ->
        val code = response.code
        val body = try {
            response.body?.byteStream()?.use {
                readBoundedSupabaseResponse(
                    it,
                    if (response.isSuccessful) DEFAULT_SUPABASE_RESPONSE_MAX_BYTES
                    else SUPABASE_ERROR_RESPONSE_MAX_BYTES,
                ).decodeToString()
            } ?: ""
        } catch (e: SupabaseResponseTooLarge) {
            if (response.isSuccessful) {
                throw SupabaseClient.InvalidResponse(e.message.orEmpty())
            } else {
                ""
            }
        } catch (e: Exception) {
            if (response.isSuccessful) throw e else ""
        }
        ensureOwnerStillCurrent()
        if (!response.isSuccessful) {
            throw SupabaseClient.HttpException(
                code,
                "HTTP $code: ${body.take(300)}",
                body,
            )
        }
    }
}

private fun encodedStoragePath(value: String): String = value.split('/').joinToString("/") {
    URLEncoder.encode(it, Charsets.UTF_8.name()).replace("+", "%20")
}

internal fun captureCollectionBooksPath(
    ownerId: String,
    collectionIds: Set<String>,
    afterId: String? = null,
    pageSize: Int = REMOTE_COLLECTION_BOOKS_PAGE_SIZE,
): String {
    require(SAFE_CAPTURE_SYNC_ID.matches(ownerId)) { "invalid capture owner" }
    require(pageSize in 1..REMOTE_COLLECTION_BOOKS_PAGE_SIZE) { "invalid page size" }
    val ids = collectionIds.asSequence()
        .map { it.trim().lowercase() }
        .filter(SAFE_COLLECTION_FILTER_ID::matches)
        .distinct()
        .sorted()
        .toList()
    require(ids.isNotEmpty()) { "collection ids are required" }
    val cursor = afterId?.trim()?.lowercase()?.let {
        require(SAFE_CAPTURE_SYNC_ID.matches(it)) { "invalid capture cursor" }
        "&id=gt.${URLEncoder.encode(it, Charsets.UTF_8.name())}"
    }.orEmpty()
    val owner = URLEncoder.encode(ownerId.lowercase(), Charsets.UTF_8.name())
    val filter = ids.joinToString(",")
    val select = "id,created_by,created_at,photos" +
        ",$CAPTURE_COLLECTION_ID_FIELD:meta->>scan_collection_id" +
        ",$CAPTURE_COLLECTION_NAME_FIELD:meta->>scan_collection" +
        ",title:meta->>title,author:meta->>author,year:meta->>year"
    return "/rest/v1/captures?created_by=eq.$owner" +
        "&meta->>scan_collection_id=in.($filter)" +
        "&select=$select&order=id.asc&limit=$pageSize$cursor"
}

internal fun cloudCollectionFromJson(row: JSONObject): BookCollection? {
    val id = row.optString("id").trim()
    val name = normalizeCollectionField(row.optString("name"))
    if (id.isEmpty() || name.isEmpty()) return null
    val rawTagId = when (val rawTag = row.opt("tag_id")) {
        null, JSONObject.NULL -> null
        is String -> rawTag
        else -> return null
    }
    val tagId = if (rawTagId == null) {
        defaultCollectionTagId(name)
    } else {
        normalizeCollectionTagId(rawTagId).ifEmpty { return null }
    }
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
        tagId = tagId,
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
            val row = rows.optJSONObject(index) ?: continue
            val parsed = cloudCollectionFromJson(row) ?: continue
            if (row.has("tag_id") && !row.isNull("tag_id")) {
                // Preserve an explicit duplicate so the conflict detector and
                // QR lookup fail closed. Printed box tags must never be
                // silently reassigned while reading a drifted cloud snapshot.
                out += parsed
            } else {
                // A legacy cloud snapshot can contain null tags. Resolve only
                // that synthesized fallback deterministically across pages.
                val uniqueTagId = resolveCollectionTagId(
                    parsed.name,
                    out,
                    preferredTagId = parsed.tagId,
                )
                out += if (uniqueTagId == parsed.tagId) parsed
                else parsed.copy(tagId = uniqueTagId)
            }
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
    .put("tag_id", canonicalCollectionTagId(row))
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

    class HttpException(
        val code: Int,
        message: String,
        val responseBody: String = "",
    ) : IOException(message)
    class SignedOut : IOException("signed out")
    class AccountChanged : IOException("account changed during delivery")
    class ObjectTooLarge : IOException("private object exceeds the download limit")
    class InvalidResponse(message: String) : IOException(message)

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

    private fun finish(
        conn: HttpURLConnection,
        maxResponseBytes: Int = DEFAULT_SUPABASE_RESPONSE_MAX_BYTES,
    ): String {
        val code = conn.responseCode
        val body = try {
            (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.use {
                    readBoundedSupabaseResponse(
                        it,
                        if (code in 200..299) maxResponseBytes
                        else SUPABASE_ERROR_RESPONSE_MAX_BYTES,
                    ).decodeToString()
                } ?: ""
        } catch (e: SupabaseResponseTooLarge) {
            if (code in 200..299) throw InvalidResponse(e.message.orEmpty()) else ""
        } catch (e: Exception) {
            if (code in 200..299) throw e else ""
        }
        if (Prefs.userId(ctx) != ownerId) throw AccountChanged()
        if (code !in 200..299) {
            throw HttpException(code, "HTTP $code: ${body.take(300)}", body)
        }
        return body
    }

    /** Upload one JPEG; objectPath like "PixelBooth/abcd1234/photo_1.jpg". */
    fun uploadPhoto(objectPath: String, file: File) {
        if (ownerId.isEmpty() || Prefs.userId(ctx) != ownerId) throw AccountChanged()
        val token = Auth.accessToken(ctx) ?: throw SignedOut()
        if (Prefs.userId(ctx) != ownerId) throw AccountChanged()
        val request = newSupabasePhotoUploadRequest(
            url = "$baseUrl/storage/v1/object/captures/$objectPath",
            anonKey = Prefs.anonKey(ctx),
            accessToken = token,
            file = file,
        )
        executeSupabasePhotoUpload(supabasePhotoUploadClient, request) {
            if (Prefs.userId(ctx) != ownerId) throw AccountChanged()
        }
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
    internal fun captureImportStates(ids: List<String>): Map<String, CaptureImportState> {
        val out = linkedMapOf<String, CaptureImportState>()
        for (batch in safeCaptureSyncIds(ids).chunked(CAPTURE_METADATA_BATCH_SIZE)) {
            fetchCaptureImportStatesIsolated(batch, out)
        }
        return out
    }

    /** Split a malformed or oversized response down to one row. A corrupt
     * association remains unconfirmed without suppressing unrelated imports. */
    private fun fetchCaptureImportStatesIsolated(
        batch: List<String>,
        out: MutableMap<String, CaptureImportState>,
    ) {
        if (batch.isEmpty()) return
        try {
            val filter = batch.joinToString(",") {
                URLEncoder.encode(it, Charsets.UTF_8.name())
            }
            val select = "id,status,created_by,lib_association," +
                "lib_association_revision,lib_association_updated_at"
            val conn = open(
                "GET",
                "$baseUrl/rest/v1/captures?id=in.($filter)&select=$select&order=id.asc",
                null,
            )
            val maximum = batch.size * CAPTURE_IMPORT_ROW_MAX_BYTES + 8 * 1024
            val rows = try {
                JSONArray(finish(conn, maximum).ifEmpty { "[]" })
            } catch (e: org.json.JSONException) {
                throw InvalidResponse("invalid capture import response")
            }
            for (index in 0 until rows.length()) {
                val parsed = rows.optJSONObject(index)
                    ?.let { captureImportStateFromJson(it, ownerId) }
                    ?: throw InvalidResponse("invalid capture import row")
                if (parsed.captureId !in batch || parsed.captureId in out) {
                    throw InvalidResponse("duplicate or out-of-scope capture import row")
                }
                out[parsed.captureId] = parsed
            }
        } catch (e: Exception) {
            if (e !is InvalidResponse && e !is SupabaseResponseTooLarge) throw e
            if (batch.size == 1) return
            val midpoint = batch.size / 2
            fetchCaptureImportStatesIsolated(batch.subList(0, midpoint), out)
            fetchCaptureImportStatesIsolated(batch.subList(midpoint, batch.size), out)
        }
    }

    @Deprecated("Use captureImportStates so status and association stay coupled")
    fun captureStatuses(ids: List<String>): Map<String, String> =
        captureImportStates(ids).mapValues { it.value.status }

    /**
     * Every capture this account filed into any of [collectionIds] — the one read
     * that discovers captures this handset does NOT already hold locally, which is
     * what lets a scanned box list its books after a reinstall or on a second
     * phone.
     *
     * Pass the whole merge closure (see [collectionMergeClosure]); `captures.meta`
     * still carries a merge loser's uuid forever.
     *
     * Capture RLS also admits rows from contributors assigned to this account
     * for desktop ingestion. The explicit owner predicate and response check
     * deliberately narrow Android's personal cache back to rows this account
     * created. It needs no service key and no new grant.
     */
    internal fun capturesForCollections(
        collectionIds: Collection<String>,
        pageSize: Int = REMOTE_COLLECTION_BOOKS_PAGE_SIZE,
    ): List<RemoteCollectionBook> {
        val ids = collectionIds.asSequence()
            .map { it.trim() }
            .filter { SAFE_COLLECTION_FILTER_ID.matches(it) }
            .distinct()
            .toList()
        if (ids.isEmpty()) return emptyList()
        return collectRemoteCollectionBookPages(
            ownerId,
            ids.map { it.lowercase() }.toSet(),
        ) { afterId ->
            val conn = open(
                "GET",
                "$baseUrl${captureCollectionBooksPath(
                    ownerId = ownerId,
                    collectionIds = ids.toSet(),
                    afterId = afterId,
                    pageSize = pageSize,
                )}",
                null,
            )
            try {
                JSONArray(
                    finish(conn, CAPTURE_COLLECTION_RESPONSE_MAX_BYTES).ifEmpty { "[]" },
                )
            } catch (e: org.json.JSONException) {
                throw InvalidResponse("invalid collection capture response")
            }
        }
    }

    /** Desktop-authored projections for this account's retained captures.
     * The table's RLS is owner-only; checking owner_id again fails closed if a
     * project was misconfigured or the account changed around token refresh. */
    internal fun desktopBookMetadata(ids: List<String>): Map<String, DesktopBookMetadata> {
        val out = linkedMapOf<String, DesktopBookMetadata>()
        for (batch in safeCaptureSyncIds(ids).chunked(CAPTURE_METADATA_BATCH_SIZE)) {
            fetchDesktopBookMetadataIsolated(batch, out)
        }
        return out
    }

    /** Split only malformed/oversized responses. Network and HTTP errors still
     * retry normally. A bad single row is dropped without preventing unrelated
     * captures in the explicit batch from receiving valid metadata. */
    private fun fetchDesktopBookMetadataIsolated(
        batch: List<String>,
        out: MutableMap<String, DesktopBookMetadata>,
    ) {
        if (batch.isEmpty()) return
        try {
            val filter = batch.joinToString(",") {
                URLEncoder.encode(it, Charsets.UTF_8.name())
            }
            val conn = open(
                "GET",
                "$baseUrl/rest/v1/capture_book_metadata" +
                    "?capture_id=in.($filter)&select=" +
                    "capture_id,owner_id,book_id,data,revision,updated_at" +
                    "&order=capture_id.asc",
                null,
            )
            val maximum = batch.size * (CAPTURE_METADATA_MAX_BYTES + 2 * 1024) + 8 * 1024
            val rows = try {
                JSONArray(finish(conn, maximum).ifEmpty { "[]" })
            } catch (e: org.json.JSONException) {
                throw InvalidResponse("invalid desktop book metadata response")
            }
            for (index in 0 until rows.length()) {
                val parsed = rows.optJSONObject(index)?.let(::desktopBookMetadataFromJson)
                    ?: continue
                if (parsed.ownerId != ownerId || parsed.captureId !in batch ||
                    parsed.captureId in out) continue
                out[parsed.captureId] = parsed
            }
        } catch (e: InvalidResponse) {
            if (batch.size == 1) return
            val midpoint = batch.size / 2
            fetchDesktopBookMetadataIsolated(batch.subList(0, midpoint), out)
            fetchDesktopBookMetadataIsolated(batch.subList(midpoint, batch.size), out)
        }
    }

    /** Shared attention/review state. Missing rows are meaningful: a capture
     * can be edited offline before its first explicit sync. */
    internal fun captureReviews(ids: List<String>): Map<String, CaptureReviewMetadata> {
        val out = linkedMapOf<String, CaptureReviewMetadata>()
        for (batch in safeCaptureSyncIds(ids).chunked(CAPTURE_METADATA_BATCH_SIZE)) {
            fetchCaptureReviewsIsolated(batch, out)
        }
        return out
    }

    private fun fetchCaptureReviewsIsolated(
        batch: List<String>,
        out: MutableMap<String, CaptureReviewMetadata>,
    ) {
        if (batch.isEmpty()) return
        try {
            val filter = batch.joinToString(",") {
                URLEncoder.encode(it, Charsets.UTF_8.name())
            }
            val conn = open(
                "GET",
                "$baseUrl/rest/v1/capture_reviews" +
                    "?capture_id=in.($filter)&select=" +
                    "capture_id,owner_id,needs_attention,attention_reason," +
                    "needs_review,review_id,status,revision,updated_at" +
                    "&order=capture_id.asc",
                null,
            )
            val rows = try {
                JSONArray(finish(conn, CAPTURE_REVIEW_RESPONSE_MAX_BYTES).ifEmpty { "[]" })
            } catch (e: org.json.JSONException) {
                throw InvalidResponse("invalid capture review response")
            }
            for (index in 0 until rows.length()) {
                val row = rows.optJSONObject(index) ?: continue
                if (row.opt("owner_id") !is String ||
                    row.optString("owner_id").trim() != ownerId) {
                    continue
                }
                val parsed = captureReviewFromJson(row) ?: continue
                if (parsed.captureId !in batch || parsed.captureId in out) continue
                out[parsed.captureId] = parsed
            }
        } catch (e: InvalidResponse) {
            if (batch.size == 1) return
            val midpoint = batch.size / 2
            fetchCaptureReviewsIsolated(batch.subList(0, midpoint), out)
            fetchCaptureReviewsIsolated(batch.subList(midpoint, batch.size), out)
        }
    }

    /** Insert or compare-and-set only the phone-writable review fields. The
     * database trigger owns revisions/timestamps and desktop review identity. */
    internal fun writeCaptureReview(
        write: CaptureReviewCloudWrite,
    ): CaptureReviewMetadata? {
        val state = write.state
        require(SAFE_CAPTURE_SYNC_ID.matches(state.captureId)) { "invalid capture id" }
        val expected = write.expectedCloudRevision
        val (method, url, body) = if (expected == null) {
            Triple(
                "POST",
                "$baseUrl/rest/v1/capture_reviews?on_conflict=capture_id",
                captureReviewCloudBody(state),
            )
        } else {
            val captureFilter = URLEncoder.encode(state.captureId, Charsets.UTF_8.name())
            val patch = captureReviewCloudBody(state).apply {
                remove("capture_id")
            }
            Triple(
                "PATCH",
                "$baseUrl/rest/v1/capture_reviews" +
                    "?capture_id=eq.$captureFilter&revision=eq.$expected",
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
        val rows = JSONArray(finish(conn, CAPTURE_REVIEW_RESPONSE_MAX_BYTES).ifEmpty { "[]" })
        if (rows.length() == 0) return null
        if (rows.length() != 1) {
            throw InvalidResponse("capture review write returned multiple rows")
        }
        val accepted = rows.optJSONObject(0)
            ?: throw InvalidResponse("capture review write returned an invalid row")
        if (accepted.optString("owner_id").trim() != ownerId) {
            throw InvalidResponse("capture review ownership mismatch")
        }
        val parsed = captureReviewFromJson(accepted, expectedCaptureId = state.captureId)
            ?: throw InvalidResponse("invalid accepted capture review")
        if (!reviewWritableEquals(parsed, state)) {
            throw InvalidResponse("capture review write returned different writable fields")
        }
        val requiredRevision = expected?.plus(1L) ?: 1L
        if (parsed.revision != requiredRevision) {
            throw InvalidResponse("capture review revision did not advance")
        }
        return parsed
    }

    /** Desktop-published corrected-display rows for the retained cloud
     * captures, grouped by capture. RLS is owner-or-grant; checking owner_id
     * again fails closed, and unknown or malformed rows are dropped rather
     * than allowed to suppress unrelated captures' corrections. */
    internal fun captureCorrections(ids: List<String>): Map<String, List<CaptureCorrectionRow>> {
        val out = linkedMapOf<String, MutableList<CaptureCorrectionRow>>()
        for (batch in safeCaptureSyncIds(ids).chunked(CAPTURE_METADATA_BATCH_SIZE)) {
            fetchCaptureCorrectionsIsolated(batch, out)
        }
        return out
    }

    /** Split only malformed/oversized responses down to one capture, like the
     * other metadata families. Network and HTTP errors still retry normally. */
    private fun fetchCaptureCorrectionsIsolated(
        batch: List<String>,
        out: MutableMap<String, MutableList<CaptureCorrectionRow>>,
    ) {
        if (batch.isEmpty()) return
        try {
            val filter = batch.joinToString(",") {
                URLEncoder.encode(it, Charsets.UTF_8.name())
            }
            val conn = open(
                "GET",
                "$baseUrl/rest/v1/capture_corrections" +
                    "?capture_id=in.($filter)&select=" +
                    "capture_id,asset_id,owner_id,correction_id," +
                    "source_original_sha256,result,revision,updated_at" +
                    "&order=capture_id.asc,asset_id.asc",
                null,
            )
            val rows = try {
                JSONArray(finish(conn).ifEmpty { "[]" })
            } catch (e: org.json.JSONException) {
                throw InvalidResponse("invalid capture correction response")
            }
            for (index in 0 until rows.length()) {
                val parsed = rows.optJSONObject(index)?.let(::captureCorrectionRowFromJson)
                    ?: continue
                if (parsed.ownerId != ownerId || parsed.captureId !in batch) continue
                val forCapture = out.getOrPut(parsed.captureId, ::mutableListOf)
                // One row per (capture_id, asset_id); a duplicate means a
                // drifted response, so keep only the first occurrence.
                if (forCapture.none { it.assetId == parsed.assetId }) forCapture += parsed
            }
        } catch (e: InvalidResponse) {
            if (batch.size == 1) return
            val midpoint = batch.size / 2
            fetchCaptureCorrectionsIsolated(batch.subList(0, midpoint), out)
            fetchCaptureCorrectionsIsolated(batch.subList(midpoint, batch.size), out)
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
                "?select=id,name,from_place,tag_id,updated_at,deleted,merged_into,parent_id" +
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

internal fun safeCaptureSyncIds(ids: List<String>): List<String> = ids.asSequence()
    .map { it.trim().lowercase() }
    .filter { SAFE_CAPTURE_SYNC_ID.matches(it) && it != "." && it != ".." }
    .distinct()
    .toList()
