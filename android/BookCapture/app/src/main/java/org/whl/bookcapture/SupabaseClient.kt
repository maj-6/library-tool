package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal Supabase REST access: photo upload into the `captures` storage
 * bucket and one row per book entry into the `captures` table. Mirrors what
 * tools/supabase_sync.py reads on the desktop side.
 */
class SupabaseClient(private val baseUrl: String, private val key: String) {

    class HttpException(val code: Int, message: String) : IOException(message)

    private fun open(method: String, url: String, contentType: String?): HttpURLConnection {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 20_000
        conn.readTimeout = 120_000
        conn.setRequestProperty("apikey", key)
        conn.setRequestProperty("Authorization", "Bearer $key")
        if (contentType != null) conn.setRequestProperty("Content-Type", contentType)
        return conn
    }

    private fun finish(conn: HttpURLConnection) {
        val code = conn.responseCode
        if (code !in 200..299) {
            val detail = try {
                (conn.errorStream ?: conn.inputStream)?.readBytes()
                    ?.decodeToString()?.take(300) ?: ""
            } catch (_: Exception) { "" }
            throw HttpException(code, "HTTP $code: $detail")
        }
        conn.inputStream.use { it.readBytes() }   // drain so the connection is reusable
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

    /** Insert the capture row the desktop sync will pick up. */
    fun insertCapture(id: String, device: String, photoPaths: List<String>, note: String) {
        val body = JSONObject()
            .put("id", id)
            .put("device", device)
            .put("status", "pending")
            .put("photos", JSONArray(photoPaths))
            .put("note", note)
        val conn = open("POST", "$baseUrl/rest/v1/captures", "application/json")
        // ignore-duplicates: a retried upload after the desktop already imported
        // the row must NOT reset its status back to pending
        conn.setRequestProperty("Prefer", "return=minimal,resolution=ignore-duplicates")
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toString().toByteArray()) }
        finish(conn)
    }

    /** Cheap reachability probe for the settings screen. */
    fun testConnection(): String? = try {
        val conn = open("GET", "$baseUrl/rest/v1/captures?select=id&limit=1", null)
        finish(conn)
        null
    } catch (e: Exception) {
        e.message ?: e.javaClass.simpleName
    }
}
