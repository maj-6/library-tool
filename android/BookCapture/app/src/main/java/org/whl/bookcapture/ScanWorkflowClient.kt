package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL

private const val SCAN_SEARCH_PAGE_SIZE = 25
private const val SCAN_SEARCH_RESPONSE_MAX_BYTES = 3 * 1024 * 1024

internal fun scanSearchEnqueueBody(item: ScanSearchQueueItem): JSONObject {
    val normalized = requireNotNull(normalizedScanSearchQueueItem(item)) {
        "invalid scan search queue item"
    }
    require(normalized.status != ScanSearchStatus.FAILED) {
        "failed scan searches are server-authored"
    }
    return JSONObject()
        .put("p_id", normalized.id)
        .put("p_scan_collection_id", normalized.scanCollectionId)
        .put("p_photo_role", normalized.photoRole.wireValue)
        .put("p_ocr_text", normalized.ocrText)
}

internal fun scanSearchCompleteBody(queueId: String, captureId: String): JSONObject {
    val queue = queueId.trim().lowercase()
    val capture = captureId.trim().lowercase()
    require(SAFE_CAPTURE_SYNC_ID.matches(queue)) { "invalid scan search id" }
    require(SAFE_CAPTURE_SYNC_ID.matches(capture)) { "invalid capture id" }
    return JSONObject().put("p_id", queue).put("p_capture_id", capture)
}

internal fun scanSearchQueueItemFromCloudJson(row: JSONObject): ScanSearchQueueItem? {
    val revision = when (val raw = row.opt("revision")) {
        is Byte -> raw.toLong()
        is Short -> raw.toLong()
        is Int -> raw.toLong()
        is Long -> raw
        else -> return null
    }
    if (revision <= 0L) return null
    val matched = when (val raw = row.opt("matched_capture_id")) {
        null, JSONObject.NULL -> ""
        is String -> raw
        else -> return null
    }
    return normalizedScanSearchQueueItem(
        ScanSearchQueueItem(
            id = row.opt("id") as? String ?: return null,
            ownerId = row.opt("owner_id") as? String ?: return null,
            scanCollectionId = row.opt("scan_collection_id") as? String ?: return null,
            photoRole = ScanSearchPhotoRole.fromWire(
                row.opt("photo_role") as? String ?: return null,
            ) ?: return null,
            ocrText = row.opt("ocr_text") as? String ?: return null,
            status = ScanSearchStatus.fromWire(
                row.opt("status") as? String ?: return null,
            ) ?: return null,
            matchedCaptureId = matched,
            revision = revision,
            createdAt = row.opt("created_at") as? String ?: return null,
            updatedAt = row.opt("updated_at") as? String ?: return null,
            dirty = false,
        ),
    )
}

/**
 * Narrow, user-JWT client for the scan-search workflow. No service key and no
 * camera image reaches this API: only Mistral OCR text is persisted remotely.
 */
internal class ScanWorkflowClient(
    private val ctx: Context,
    expectedOwnerId: String? = null,
) {
    private val baseUrl = Prefs.supabaseUrl(ctx).trimEnd('/')
    private val ownerId = expectedOwnerId?.trim()?.lowercase().orEmpty()
        .ifEmpty { Prefs.userId(ctx).trim().lowercase() }

    fun enqueue(item: ScanSearchQueueItem): ScanSearchQueueItem {
        requireCurrentOwner()
        val normalized = requireNotNull(normalizedScanSearchQueueItem(item)) {
            "invalid scan search queue item"
        }
        require(normalized.ownerId == ownerId) { "scan search belongs to another account" }
        return rpc(
            "enqueue_scan_search",
            scanSearchEnqueueBody(normalized),
            normalized.id,
        )
    }

    fun complete(queueId: String, captureId: String): ScanSearchQueueItem = rpc(
        "complete_scan_search",
        scanSearchCompleteBody(queueId, captureId),
        queueId,
    )

    fun queue(): List<ScanSearchQueueItem> {
        requireCurrentOwner()
        val owner = URLEncoder.encode(ownerId, Charsets.UTF_8.name())
        val select = "id,owner_id,scan_collection_id,photo_role,ocr_text,status," +
            "matched_capture_id,revision,created_at,updated_at"
        val seen = linkedSetOf<String>()
        return buildList {
            var offset = 0
            while (offset < ScanSearchQueue.MAX_ITEMS) {
                val limit = minOf(SCAN_SEARCH_PAGE_SIZE, ScanSearchQueue.MAX_ITEMS - offset)
                val conn = open(
                    "GET",
                    "$baseUrl/rest/v1/scan_search_queue?owner_id=eq.$owner" +
                        "&select=$select&order=created_at.asc,id.asc" +
                        "&limit=$limit&offset=$offset",
                    null,
                )
                val rows = parseRows(finish(conn))
                if (rows.length() > limit) {
                    throw SupabaseClient.InvalidResponse("oversized scan search queue page")
                }
                if (rows.length() == 0) break
                for (index in 0 until rows.length()) {
                    val item = rows.optJSONObject(index)?.let(::scanSearchQueueItemFromCloudJson)
                        ?: throw SupabaseClient.InvalidResponse("invalid scan search queue row")
                    if (item.ownerId != ownerId || !seen.add(item.id)) {
                        throw SupabaseClient.InvalidResponse("out-of-scope scan search queue row")
                    }
                    add(item)
                }
                offset += rows.length()
                if (rows.length() < limit) break
            }
        }
    }

    private fun rpc(
        name: String,
        body: JSONObject,
        expectedId: String,
    ): ScanSearchQueueItem {
        val normalizedId = expectedId.trim().lowercase()
        require(SAFE_CAPTURE_SYNC_ID.matches(normalizedId)) { "invalid scan search id" }
        val conn = open("POST", "$baseUrl/rest/v1/rpc/$name", "application/json")
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toString().toByteArray()) }
        val rows = parseRows(finish(conn))
        if (rows.length() != 1) {
            throw SupabaseClient.InvalidResponse("incomplete scan search response")
        }
        val item = rows.optJSONObject(0)?.let(::scanSearchQueueItemFromCloudJson)
            ?: throw SupabaseClient.InvalidResponse("invalid scan search response")
        if (item.id != normalizedId || item.ownerId != ownerId) {
            throw SupabaseClient.InvalidResponse("out-of-scope scan search response")
        }
        return item
    }

    private fun requireCurrentOwner() {
        if (!SAFE_CAPTURE_SYNC_ID.matches(ownerId) ||
            Prefs.userId(ctx).trim().lowercase() != ownerId
        ) {
            throw SupabaseClient.AccountChanged()
        }
    }

    private fun open(method: String, url: String, contentType: String?): HttpURLConnection {
        requireCurrentOwner()
        val token = Auth.accessToken(ctx) ?: throw SupabaseClient.SignedOut()
        requireCurrentOwner()
        return (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 20_000
            readTimeout = 120_000
            setRequestProperty("apikey", Prefs.anonKey(ctx))
            setRequestProperty("Authorization", "Bearer $token")
            if (contentType != null) setRequestProperty("Content-Type", contentType)
        }
    }

    private fun finish(conn: HttpURLConnection): String {
        val code = conn.responseCode
        val body = try {
            (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.use {
                    readBoundedSupabaseResponse(
                        it,
                        if (code in 200..299) SCAN_SEARCH_RESPONSE_MAX_BYTES else 16 * 1024,
                    ).decodeToString()
                }.orEmpty()
        } catch (e: Exception) {
            if (code in 200..299) throw e else ""
        } finally {
            conn.disconnect()
        }
        requireCurrentOwner()
        if (code !in 200..299) {
            throw SupabaseClient.HttpException(code, "HTTP $code: ${body.take(300)}", body)
        }
        return body
    }

    private fun parseRows(body: String): JSONArray = try {
        JSONArray(body.ifEmpty { "[]" })
    } catch (_: Exception) {
        throw SupabaseClient.InvalidResponse("invalid scan search JSON response")
    }
}
