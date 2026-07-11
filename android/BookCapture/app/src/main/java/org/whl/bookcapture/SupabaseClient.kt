package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Supabase REST for the capture flow, authorized as the signed-in USER: the
 * apikey header carries the public anon key and the bearer token is the
 * account's — row-level security does the rest (captures_insert_own,
 * captures bucket policies). The service_role key never touches the phone.
 */
class SupabaseClient(private val ctx: Context) {

    class HttpException(val code: Int, message: String) : IOException(message)
    class SignedOut : IOException("signed out")

    private val baseUrl = Prefs.supabaseUrl(ctx)

    private fun open(method: String, url: String, contentType: String?): HttpURLConnection {
        val token = Auth.accessToken(ctx) ?: throw SignedOut()
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
            .put("created_by", Prefs.userId(ctx))
            .put("contributor", Prefs.displayName(ctx).ifEmpty { Prefs.email(ctx) })
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
