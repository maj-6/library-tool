package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.io.IOException
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
class LanClient(ctx: Context) {

    class HttpException(val code: Int, message: String) : IOException(message)
    class NotConfigured : IOException("no paired desktop")
    class UnsafeEndpoint : IOException("cleartext LAN address is not private")

    private val base: String = run {
        val h = Prefs.lanHost(ctx)
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
                JSONObject(c.inputStream.bufferedReader().use { it.readText() })
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
                    JSONObject(probe.inputStream.bufferedReader().use { it.readText() })
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
                      photos: List<Pair<String, File>>) {
        val metaJson = JSONObject()
            .put("id", id).put("device", device).put("note", note)
            .put("created_at", createdAt).put("ocr", ocr).put("meta", meta)
            .put(PHOTO_ASSETS_MANIFEST_KEY, photoAssets)
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
            val body = try { c.errorStream?.readBytes()?.decodeToString() ?: "" } catch (_: Exception) { "" }
            throw HttpException(code, "HTTP $code: ${body.take(200)}")
        }
        val response = try {
            JSONObject(c.inputStream.bufferedReader().use { it.readText() })
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
}

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
