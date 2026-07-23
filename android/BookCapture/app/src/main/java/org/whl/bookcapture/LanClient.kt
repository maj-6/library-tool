package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL
import java.util.UUID

/**
 * The offline transport: sends captures straight to a paired desktop over the
 * LAN, bypassing the cloud. One multipart POST per entry to /lan/capture,
 * authorised by the pairing token. The desktop imports synchronously, so a 200
 * IS "imported" — there is nothing to poll afterwards. Mirrors the error shape
 * of SupabaseClient so UploadWorker's transient/permanent split still applies.
 */
class LanClient(ctx: Context, frozenHost: String? = null) {

    class HttpException(val code: Int, message: String) : IOException(message)
    class NotConfigured : IOException("no paired desktop")
    class UnsafeEndpoint : IOException("cleartext LAN address is not private")

    private val base: String = run {
        val h = frozenHost?.trim().orEmpty().ifEmpty { Prefs.lanHost(ctx) }
        if (h.isEmpty()) throw NotConfigured()
        if (h.startsWith("http", ignoreCase = true)) h.trimEnd('/') else "http://${h.trimEnd('/')}"
    }
    private val token = Prefs.lanToken(ctx)

    private fun open(path: String): HttpURLConnection {
        val url = URL("$base$path")
        if (url.protocol != "http" && url.protocol != "https") throw UnsafeEndpoint()
        // Cleartext hostnames would be resolved a second time by URLConnection
        // and permit DNS rebinding after this check. Require a private literal
        // address (the pairing UI advertises one) or loopback.
        if (url.protocol == "http" && !isPrivateLanHost(url.host)) throw UnsafeEndpoint()
        return url.openConnection() as HttpURLConnection
    }

    /** Confirm both branded liveness and token authorization. The paired
     * desktop must echo a fresh nonce from its side-effect-free pair route. */
    fun ping(): Boolean = try {
        if (token.isBlank()) false else {
            val c = open("/lan/ping")
            c.connectTimeout = 3_000; c.readTimeout = 3_000
            val branded = c.responseCode == 200 && try {
                JSONObject(c.inputStream.use {
                    readBounded(it, MAX_LAN_CONTROL_RESPONSE_BYTES).decodeToString()
                })
                    .optString("app") == "whl-capture"
            } catch (_: Exception) { false }
            c.disconnect()
            if (!branded) false else {
                val nonce = UUID.randomUUID().toString()
                val probe = open("/lan/pair")
                probe.requestMethod = "POST"
                probe.connectTimeout = 3_000; probe.readTimeout = 3_000
                probe.doOutput = true
                probe.setRequestProperty("X-WHL-Token", token)
                probe.setRequestProperty("Content-Type", "application/json")
                probe.outputStream.use {
                    it.write(JSONObject().put("nonce", nonce).toString().toByteArray())
                }
                val code = probe.responseCode
                val response = if (code in 200..299) try {
                    JSONObject(probe.inputStream.use {
                        readBounded(it, MAX_LAN_CONTROL_RESPONSE_BYTES).decodeToString()
                    })
                } catch (_: Exception) { JSONObject() } else JSONObject()
                val authorized = isValidPairingResponse(
                    nonce,
                    code,
                    response.optString("app"),
                    response.optString("nonce"),
                )
                probe.disconnect()
                authorized
            }
        }
    } catch (_: Exception) { false }

    /** One capture -> the desktop: a "meta" JSON field + N "photo" file parts. */
    fun uploadCapture(id: String, device: String, note: String, createdAt: String,
                      ocr: JSONObject, meta: JSONObject, photoAssets: JSONObject,
                      captureReview: JSONObject?,
                      photos: List<Pair<String, File>>) {
        val metaJson = JSONObject()
            .put("id", id).put("device", device).put("note", note)
            .put("created_at", createdAt).put("ocr", ocr).put("meta", meta)
            .put(PHOTO_ASSETS_MANIFEST_KEY, photoAssets)
        captureReview?.let { metaJson.put("capture_review", it) }
        val boundary = "whl" + UUID.randomUUID().toString().replace("-", "")
        val crlf = "\r\n"; val dash = "--"
        val c = open("/lan/capture")
        c.requestMethod = "POST"
        c.connectTimeout = 20_000
        c.readTimeout = 120_000
        c.doOutput = true
        c.setRequestProperty("X-WHL-Token", token)
        c.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        c.outputStream.use { out ->
            fun w(s: String) = out.write(s.toByteArray())
            w("$dash$boundary$crlf")
            w("Content-Disposition: form-data; name=\"meta\"$crlf$crlf")
            w(metaJson.toString()); w(crlf)
            for ((name, f) in photos) {
                w("$dash$boundary$crlf")
                w("Content-Disposition: form-data; name=\"photo\"; filename=\"$name\"$crlf")
                w("Content-Type: image/jpeg$crlf$crlf")
                f.inputStream().use { it.copyTo(out) }
                w(crlf)
            }
            w("$dash$boundary$dash$crlf")
        }
        val code = c.responseCode
        if (code !in 200..299) {
            val body = try {
                c.errorStream?.use {
                    readBounded(it, MAX_LAN_ERROR_RESPONSE_BYTES).decodeToString()
                } ?: ""
            } catch (_: Exception) { "" }
            throw HttpException(code, "HTTP $code: ${body.take(200)}")
        }
        val response = try {
            JSONObject(c.inputStream.use {
                readBounded(it, MAX_LAN_CAPTURE_RECEIPT_BYTES).decodeToString()
            })
        } catch (e: Exception) {
            c.disconnect()
            throw IOException("desktop returned an unreadable capture receipt", e)
        }
        c.disconnect()
        if (!isValidCaptureReceipt(
                expectedId = id,
                responseCode = code,
                app = response.optString("app"),
                status = response.optString("status"),
                returnedId = response.optString("id"),
            )
        ) throw IOException("desktop did not confirm capture $id")
    }

    /** Pull desktop projections and, only for an explicit Sync, send the dirty
     * review snapshots supplied by the caller. The pairing token authenticates
     * the same desktop that accepted the capture; no Supabase row is required. */
    internal fun syncMetadata(
        captureIds: List<String>,
        reviews: List<JSONObject>,
    ): LanMetadataExchange {
        val ids = captureIds.distinct().also { values ->
            require(values.size <= CAPTURE_METADATA_BATCH_SIZE * 2 &&
                values.all { SAFE_CAPTURE_SYNC_ID.matches(it) })
        }
        val body = JSONObject()
            .put("capture_ids", JSONArray(ids))
            .put("reviews", JSONArray(reviews))
        val c = open("/lan/metadata")
        c.requestMethod = "POST"
        c.connectTimeout = 10_000
        c.readTimeout = 60_000
        c.doOutput = true
        c.setRequestProperty("X-WHL-Token", token)
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        val code = c.responseCode
        val response = try {
            val stream = if (code in 200..299) c.inputStream else c.errorStream
            val limit = if (code in 200..299) MAX_LAN_METADATA_RESPONSE_BYTES else 8 * 1024
            JSONObject(stream?.use { readBounded(it, limit) }?.decodeToString().orEmpty())
        } catch (e: Exception) {
            c.disconnect()
            throw IOException("desktop returned unreadable metadata", e)
        }
        c.disconnect()
        if (code !in 200..299) {
            throw HttpException(code, "HTTP $code: ${response.optString("error").take(200)}")
        }
        if (response.optString("app") != "whl-capture") {
            throw IOException("desktop returned an unbranded metadata response")
        }
        val allowed = ids.toSet()
        val books = linkedMapOf<String, DesktopBookMetadata>()
        val bookRows = response.optJSONArray("books") ?: JSONArray()
        for (index in 0 until bookRows.length()) {
            val parsed = bookRows.optJSONObject(index)?.let(::desktopBookMetadataFromJson)
                ?: throw IOException("desktop returned invalid book metadata")
            if (parsed.captureId !in allowed || books.put(parsed.captureId, parsed) != null) {
                throw IOException("desktop returned duplicate or out-of-scope book metadata")
            }
        }
        val reviewRows = linkedMapOf<String, CaptureReviewMetadata>()
        val reviewJson = response.optJSONArray("reviews") ?: JSONArray()
        for (index in 0 until reviewJson.length()) {
            val parsed = reviewJson.optJSONObject(index)?.let(::captureReviewFromJson)
                ?: throw IOException("desktop returned invalid review metadata")
            if (parsed.captureId !in allowed || reviewRows.put(parsed.captureId, parsed) != null) {
                throw IOException("desktop returned duplicate or out-of-scope review metadata")
            }
        }
        val rejected = mutableSetOf<String>()
        val errors = response.optJSONArray("errors") ?: JSONArray()
        for (index in 0 until errors.length()) {
            errors.optJSONObject(index)?.optString("capture_id")
                ?.takeIf { it in allowed }
                ?.let(rejected::add)
        }
        return LanMetadataExchange(books, reviewRows, rejected)
    }
}

private const val MAX_LAN_CONTROL_RESPONSE_BYTES = 8 * 1024
private const val MAX_LAN_CAPTURE_RECEIPT_BYTES = 16 * 1024
private const val MAX_LAN_ERROR_RESPONSE_BYTES = 16 * 1024
private const val MAX_LAN_METADATA_RESPONSE_BYTES = 24 * 1024 * 1024

internal fun readBounded(input: InputStream, maximum: Int): ByteArray {
    val output = java.io.ByteArrayOutputStream(minOf(maximum, 64 * 1024))
    val buffer = ByteArray(16 * 1024)
    var total = 0
    while (true) {
        val count = input.read(buffer)
        if (count < 0) break
        total += count
        if (total > maximum) throw IOException("desktop response is too large")
        output.write(buffer, 0, count)
    }
    return output.toByteArray()
}

internal data class LanMetadataExchange(
    val books: Map<String, DesktopBookMetadata>,
    val reviews: Map<String, CaptureReviewMetadata>,
    val rejectedReviewIds: Set<String>,
)

internal fun isPrivateLanAddress(address: InetAddress): Boolean =
    address.isSiteLocalAddress || address.isLinkLocalAddress || address.isLoopbackAddress ||
        (address.address.size == 16 && (address.address[0].toInt() and 0xfe) == 0xfc)

internal fun isPrivateLanHost(host: String): Boolean {
    val value = host.trim().removePrefix("[").removeSuffix("]")
    if (value.equals("localhost", ignoreCase = true)) return true
    val looksLiteral = value.contains(':') || value.matches(Regex("[0-9.]+"))
    if (!looksLiteral) return false
    return try { isPrivateLanAddress(InetAddress.getByName(value)) }
        catch (_: Exception) { false }
}

internal fun isValidPairingResponse(
    expectedNonce: String,
    responseCode: Int,
    app: String,
    returnedNonce: String,
): Boolean = responseCode in 200..299 && app == "whl-capture" &&
    expectedNonce.isNotEmpty() && returnedNonce == expectedNonce

internal fun isValidCaptureReceipt(
    expectedId: String,
    responseCode: Int,
    app: String,
    status: String,
    returnedId: String,
): Boolean = responseCode in 200..299 && app == "whl-capture" &&
    status in setOf("imported", "duplicate") && expectedId.isNotEmpty() &&
    returnedId == expectedId
