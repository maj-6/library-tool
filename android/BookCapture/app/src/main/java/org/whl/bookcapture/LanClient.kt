package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
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

    private val base: String = run {
        val h = Prefs.lanHost(ctx)
        if (h.isEmpty()) throw NotConfigured()
        if (h.startsWith("http", ignoreCase = true)) h.trimEnd('/') else "http://${h.trimEnd('/')}"
    }
    private val token = Prefs.lanToken(ctx)

    /** True if the paired desktop answers /lan/ping. Cheap reachability probe. */
    fun ping(): Boolean = try {
        val c = URL("$base/lan/ping").openConnection() as HttpURLConnection
        c.connectTimeout = 3_000; c.readTimeout = 3_000
        val ok = c.responseCode == 200
        c.disconnect(); ok
    } catch (_: Exception) { false }

    /** One capture -> the desktop: a "meta" JSON field + N "photo" file parts. */
    fun uploadCapture(id: String, device: String, note: String, createdAt: String,
                      ocr: JSONObject, meta: JSONObject, photos: List<Pair<String, File>>) {
        val metaJson = JSONObject()
            .put("id", id).put("device", device).put("note", note)
            .put("created_at", createdAt).put("ocr", ocr).put("meta", meta)
        val boundary = "whl" + UUID.randomUUID().toString().replace("-", "")
        val crlf = "\r\n"; val dash = "--"
        val c = URL("$base/lan/capture").openConnection() as HttpURLConnection
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
        try { c.inputStream.use { it.readBytes() } } catch (_: Exception) { }   // drain + close
        c.disconnect()
    }
}
