package org.whl.bookcapture

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.exifinterface.media.ExifInterface
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.Base64

/**
 * The phone half of the capture pipeline, run in the background right after
 * a photo lands (ProcessWorker):
 *
 *   standardize -> Mistral OCR -> bibliographic fields (DeepSeek by default,
 *                                 Mistral when only its key is configured)
 *
 * It mirrors tools/capture_pipeline.py — same 1600px/q82 standard, same OCR
 * endpoint, the SAME extraction prompt (keep them in sync) — so the desktop
 * can trust `meta` from either side. Perspective correction stays on the
 * desktop: it needs OpenCV, which would triple the APK for a correction
 * Mistral's OCR mostly shrugs at anyway.
 */
object Pipeline {

    private const val STANDARD_WIDTH = 1600
    private const val STANDARD_QUALITY = 82

    private const val MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
    private const val MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
    private const val DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
    private const val OCR_MODEL = "mistral-ocr-latest"
    private const val MISTRAL_EXTRACT_MODEL = "mistral-small-latest"
    private const val DEEPSEEK_EXTRACT_MODEL = "deepseek-chat"

    val FIELDS = listOf("title", "subtitle", "author", "volume", "edition",
                        "publisher", "year", "city", "language")

    /** A 4xx from an API: retrying won't fix it (bad key, bad request). */
    class PermanentError(message: String) : IOException(message)

    // --- 1. standardize -----------------------------------------------------------

    /** Scale the photo to the standard width and recompress, in place, honoring
     *  the EXIF rotation CameraX recorded. A file already at or under the
     *  standard width is left alone, so this is idempotent — and a ~4 MB
     *  camera JPEG becomes a ~400 KB upload. */
    fun standardizeInPlace(file: File) {
        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file.absolutePath, opts)
        if (opts.outWidth <= 0 || opts.outWidth <= STANDARD_WIDTH) return
        // rough power-of-two pre-shrink keeps peak memory at ~4x target size
        var sample = 1
        while (opts.outWidth / (sample * 2) >= STANDARD_WIDTH) sample *= 2
        val decode = BitmapFactory.Options().apply { inSampleSize = sample }
        var bmp = BitmapFactory.decodeFile(file.absolutePath, decode) ?: return
        if (bmp.width > STANDARD_WIDTH) {
            val h = (bmp.height.toLong() * STANDARD_WIDTH / bmp.width).toInt()
            val scaled = Bitmap.createScaledBitmap(bmp, STANDARD_WIDTH, h, true)
            if (scaled !== bmp) bmp.recycle()
            bmp = scaled
        }
        val rotation = when (ExifInterface(file.absolutePath)
                                 .getAttributeInt(ExifInterface.TAG_ORIENTATION,
                                                  ExifInterface.ORIENTATION_NORMAL)) {
            ExifInterface.ORIENTATION_ROTATE_90 -> 90f
            ExifInterface.ORIENTATION_ROTATE_180 -> 180f
            ExifInterface.ORIENTATION_ROTATE_270 -> 270f
            else -> 0f
        }
        if (rotation != 0f) {
            val m = Matrix().apply { postRotate(rotation) }
            val rotated = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, m, true)
            if (rotated !== bmp) bmp.recycle()
            bmp = rotated
        }
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.outputStream().use { bmp.compress(Bitmap.CompressFormat.JPEG, STANDARD_QUALITY, it) }
        bmp.recycle()
        if (!tmp.renameTo(file)) tmp.delete()      // keep the original over a torn write
    }

    // --- 2. OCR -----------------------------------------------------------------

    /** OCR one photo via Mistral; returns the page markdown, "" for a blank
     *  page. Throws PermanentError on a 4xx (bad key), IOException otherwise. */
    fun ocr(file: File, mistralKey: String): String {
        val b64 = Base64.getEncoder().encodeToString(file.readBytes())
        val payload = JSONObject()
            .put("model", OCR_MODEL)
            .put("document", JSONObject()
                .put("type", "image_url")
                .put("image_url", "data:image/jpeg;base64,$b64"))
        val data = post(MISTRAL_OCR_URL, payload, mistralKey, 90_000)
        val pages = data.optJSONArray("pages") ?: return ""
        return (0 until pages.length())
            .joinToString("\n\n") { pages.getJSONObject(it).optString("markdown") }
            .trim()
    }

    // --- 3. field extraction --------------------------------------------------------

    // Verbatim from tools/capture_pipeline.py (_EXTRACT_PROMPT) — one prompt,
    // two runners, comparable output.
    private const val EXTRACT_PROMPT = """You are cataloguing old books. Below is OCR text from photos of a book's title page and/or copyright page. Extract the bibliographic data as strict JSON.

Return a single JSON object with exactly these keys (string values; "" when absent):
  "title"      - the main title, in its original capitalization, without the subtitle
  "subtitle"   - the subtitle if present (text after the title, often following a colon)
  "author"     - primary author(s) as printed, "First Last" form, "; " between multiple
  "volume"     - volume number as a plain number string if this is one volume of a set
  "edition"    - edition statement as a short ordinal ("2nd", "3rd, revised") if stated
  "publisher"  - the publishing house
  "year"       - the publication year as a 4-digit Arabic number (convert Roman numerals)
  "city"       - the place of publication (first city if several)
  "language"   - the language of the book as a lowercase English word ("english")
  "extra"      - an object of any OTHER bibliographic facts found, using short
                 snake_case keys, e.g. printer, series, translator, illustrator,
                 copyright_year, copyright_holder, printing_number, dedication.
                 {} when none.

Do not invent data that is not in the text. Output ONLY the JSON object.

OCR TEXT:
"""

    /** OCR text -> {title, ..., extra:{}}. DeepSeek when its key is set, else
     *  Mistral (whose extraction the desktop has verified live). */
    fun extract(ocrText: String, deepseekKey: String, mistralKey: String): JSONObject {
        val (url, model, key) =
            if (deepseekKey.isNotEmpty()) Triple(DEEPSEEK_CHAT_URL, DEEPSEEK_EXTRACT_MODEL, deepseekKey)
            else Triple(MISTRAL_CHAT_URL, MISTRAL_EXTRACT_MODEL, mistralKey)
        val payload = JSONObject()
            .put("model", model)
            .put("temperature", 0)
            .put("response_format", JSONObject().put("type", "json_object"))
            .put("messages", org.json.JSONArray().put(JSONObject()
                .put("role", "user")
                .put("content", EXTRACT_PROMPT + ocrText.take(12_000))))
        val data = post(url, payload, key, 60_000)
        val raw = data.optJSONArray("choices")?.optJSONObject(0)
            ?.optJSONObject("message")?.optString("content") ?: ""
        val obj = try {
            JSONObject(raw.trim().removePrefix("```json").removePrefix("```")
                          .removeSuffix("```").trim())
        } catch (_: Exception) { JSONObject() }
        val out = JSONObject()
        for (k in FIELDS) out.put(k, obj.optString(k).trim())
        out.put("extra", obj.optJSONObject("extra") ?: JSONObject())
        return out
    }

    // --- HTTP -------------------------------------------------------------------------

    private fun post(url: String, payload: JSONObject, key: String, timeoutMs: Int): JSONObject {
        val c = URL(url).openConnection() as HttpURLConnection
        c.requestMethod = "POST"
        c.connectTimeout = 20_000
        c.readTimeout = timeoutMs
        c.setRequestProperty("Authorization", "Bearer $key")
        c.setRequestProperty("Content-Type", "application/json")
        c.doOutput = true
        c.outputStream.use { it.write(payload.toString().toByteArray()) }
        val code = c.responseCode
        val body = try {
            (if (code in 200..299) c.inputStream else c.errorStream)
                ?.use { it.readBytes().decodeToString() } ?: ""
        } catch (_: Exception) { "" }
        if (code in 400..499 && code != 408 && code != 429)
            throw PermanentError("HTTP $code: ${body.take(160)}")
        if (code !in 200..299) throw IOException("HTTP $code: ${body.take(160)}")
        return JSONObject(body)
    }
}
