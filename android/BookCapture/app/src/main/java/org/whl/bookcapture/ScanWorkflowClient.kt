package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL

internal const val SCAN_SEARCH_PAGE_SIZE = 25
internal const val SCAN_SEARCH_LIVE_STATUS_FILTER = "pending,proposed,failed"
private const val SCAN_SEARCH_RESPONSE_MAX_BYTES = 3 * 1024 * 1024

/** Includes one sentinel row so an exactly-full queue cannot hide overflow. */
internal fun scanSearchQueuePageLimit(offset: Int): Int? {
    if (offset !in 0..ScanSearchQueue.MAX_ITEMS) return null
    return minOf(SCAN_SEARCH_PAGE_SIZE, ScanSearchQueue.MAX_ITEMS + 1 - offset)
}

internal fun scanSearchEnqueueBody(item: ScanSearchQueueItem): JSONObject {
    val normalized = requireNotNull(normalizedScanSearchQueueItem(item)) {
        "invalid scan search queue item"
    }
    require(normalized.status != ScanSearchStatus.FAILED) {
        "failed scan searches are server-authored"
    }
    require(normalized.status == ScanSearchStatus.PENDING &&
        normalized.scanCollectionId.isNotEmpty()) {
        "only routed pending scan searches may be enqueued"
    }
    return JSONObject()
        .put("p_id", normalized.id)
        .put("p_session_id", normalized.sessionId)
        .put("p_scan_collection_id", normalized.scanCollectionId)
        .put("p_photo_role", normalized.photoRole.wireValue)
        .put("p_ocr_text", normalized.ocrText)
        .put(
            "p_visual_signature",
            normalized.visualSignature.takeIf(String::isNotEmpty)
                ?.let(::JSONObject) ?: JSONObject.NULL,
        )
}

internal fun scanSearchProposalDecisionBody(
    queueId: String,
    captureId: String,
): JSONObject {
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
    fun nullableId(field: String): String? = when (val raw = row.opt(field)) {
        null, JSONObject.NULL -> ""
        is String -> raw
        else -> null
    }
    val matched = nullableId("matched_capture_id") ?: return null
    val candidate = nullableId("candidate_capture_id") ?: return null
    val session = nullableId("session_id")?.ifEmpty {
        row.opt("id") as? String ?: return null
    } ?: return null
    fun nullableJson(field: String): String? = when (val raw = row.opt(field)) {
        null, JSONObject.NULL -> ""
        is JSONObject -> raw.toString()
        is String -> raw
        else -> null
    }
    val visualSignature = nullableJson("visual_signature") ?: return null
    val matchEvidence = nullableJson("match_evidence") ?: return null
    val confidence = when (val raw = row.opt("match_confidence")) {
        null, JSONObject.NULL -> null
        is Number -> raw.toDouble()
        else -> return null
    }
    val normalized = normalizedScanSearchQueueItem(
        ScanSearchQueueItem(
            id = row.opt("id") as? String ?: return null,
            ownerId = row.opt("owner_id") as? String ?: return null,
            sessionId = session,
            scanCollectionId = row.opt("scan_collection_id") as? String ?: return null,
            photoRole = ScanSearchPhotoRole.fromWire(
                row.opt("photo_role") as? String ?: return null,
            ) ?: return null,
            ocrText = row.opt("ocr_text") as? String ?: return null,
            visualSignature = visualSignature,
            status = ScanSearchStatus.fromWire(
                row.opt("status") as? String ?: return null,
            ) ?: return null,
            candidateCaptureId = candidate,
            matchConfidence = confidence,
            matchEvidence = matchEvidence,
            matchedCaptureId = matched,
            revision = revision,
            createdAt = row.opt("created_at") as? String ?: return null,
            updatedAt = row.opt("updated_at") as? String ?: return null,
            dirty = false,
        ),
    ) ?: return null
    return normalized.takeIf {
        SAFE_CAPTURE_SYNC_ID.matches(it.ownerId) &&
            SAFE_CAPTURE_SYNC_ID.matches(it.scanCollectionId)
    }
}

/**
 * Narrow, user-JWT client for the scan-search workflow. No service key and no
 * camera image reaches this API: only bounded Mistral OCR text and the
 * non-reversible whl-cover-v1 descriptor are persisted remotely.
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

    fun approve(queueId: String, captureId: String): ScanSearchQueueItem = rpc(
        "approve_scan_search",
        scanSearchProposalDecisionBody(queueId, captureId),
        queueId,
    )

    fun reject(queueId: String, captureId: String): ScanSearchQueueItem = rpc(
        "reject_scan_search",
        scanSearchProposalDecisionBody(queueId, captureId),
        queueId,
    )

    fun queue(): List<ScanSearchQueueItem> {
        requireCurrentOwner()
        val owner = URLEncoder.encode(ownerId, Charsets.UTF_8.name())
        val select = "id,owner_id,session_id,scan_collection_id,photo_role,ocr_text," +
            "visual_signature,status,candidate_capture_id,match_confidence," +
            "match_evidence,matched_capture_id,revision,created_at,updated_at"
        val seen = linkedSetOf<String>()
        return buildList {
            var offset = 0
            while (true) {
                val limit = scanSearchQueuePageLimit(offset)
                    ?: throw SupabaseClient.InvalidResponse("too many unresolved scan searches")
                val conn = open(
                    "GET",
                    "$baseUrl/rest/v1/scan_search_queue?owner_id=eq.$owner" +
                        "&status=in.($SCAN_SEARCH_LIVE_STATUS_FILTER)" +
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
                    if (!item.status.isLiveCloudQueueStatus()) {
                        throw SupabaseClient.InvalidResponse("terminal scan search in live queue")
                    }
                    add(item)
                    if (size > ScanSearchQueue.MAX_ITEMS) {
                        throw SupabaseClient.InvalidResponse(
                            "too many unresolved scan searches",
                        )
                    }
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
