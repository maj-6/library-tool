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
 * Fast capture mirrors tools/capture_pipeline.py at 1600px/q82; the explicit
 * More Detail profile preserves up to 2048px. The OCR endpoint and extraction
 * prompt stay shared so the desktop can trust `meta` from either side.
 * Perspective correction stays on the
 * desktop: it needs OpenCV, which would triple the APK for a correction
 * Mistral's OCR mostly shrugs at anyway.
 */
object Pipeline {

    private const val FAST_STANDARD_WIDTH = 1600
    private const val DETAIL_STANDARD_WIDTH = 2048
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

    /** A successful HTTP response that cannot be trusted as an extraction.
     *  Retrying is safe because callers do not replace the last good metadata. */
    class InvalidExtractionError(message: String) : IOException(message)

    data class ExtractionResult(
        val metadata: JSONObject,
        val complete: Boolean,
        val warning: String? = null,
    )

    // --- 1. standardize -----------------------------------------------------------

    /** Scale the photo to its capture profile's standard width and recompress,
     *  in place, honoring the EXIF rotation CameraX recorded. A file already at
     *  or under that width is left alone, so this is idempotent. Legacy files
     *  without frozen profile metadata conservatively keep up to 2048px. */
    fun standardizeInPlace(file: File) {
        val standardWidth = standardWidthForCaptureProfile(readCaptureProfile(file.parentFile))
        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file.absolutePath, opts)
        if (opts.outWidth <= 0 || opts.outWidth <= standardWidth) return
        // rough power-of-two pre-shrink keeps peak memory at ~4x target size
        var sample = 1
        while (opts.outWidth / (sample * 2) >= standardWidth) sample *= 2
        val decode = BitmapFactory.Options().apply { inSampleSize = sample }
        var bmp = BitmapFactory.decodeFile(file.absolutePath, decode) ?: return
        if (bmp.width > standardWidth) {
            val h = (bmp.height.toLong() * standardWidth / bmp.width).toInt()
            val scaled = Bitmap.createScaledBitmap(bmp, standardWidth, h, true)
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

    private fun readCaptureProfile(dir: File?): String? = try {
        dir?.let { JSONObject(File(it, CAPTURE_METADATA_FILE).readText()) }
            ?.optString("camera_profile")
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
    } catch (_: Exception) {
        null
    }

    /** Missing/unknown metadata comes from an older capture and must not be
     * destructively reduced when its original camera profile is unknowable. */
    internal fun standardWidthForCaptureProfile(storedProfile: String?): Int =
        if (storedProfile?.trim() == Prefs.CAMERA_PROFILE_FAST) FAST_STANDARD_WIDTH
        else DETAIL_STANDARD_WIDTH

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
  "title"      - the main title without the subtitle; render it in regular title case,
                 normalizing all-caps or erratic OCR capitalization
  "subtitle"   - the subtitle if present; render it in regular title case
  "author"     - primary author name(s) only, in "First Last" form, with "; " between
                 multiple; omit honorifics and titles such as Dr., Prof., Rev., or Sir
  "volume"     - volume number as an Arabic numeral string if this is one volume of a set;
                 convert Roman numerals and spelled-out numbers
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
    fun extract(ocrText: String, deepseekKey: String, mistralKey: String,
                customInstructions: String = ""): ExtractionResult {
        val (url, model, key) =
            if (deepseekKey.isNotEmpty()) Triple(DEEPSEEK_CHAT_URL, DEEPSEEK_EXTRACT_MODEL, deepseekKey)
            else Triple(MISTRAL_CHAT_URL, MISTRAL_EXTRACT_MODEL, mistralKey)
        val custom = customInstructions.trim().take(4_000)
        val prompt = buildString {
            append(EXTRACT_PROMPT.removeSuffix("OCR TEXT:\n"))
            if (custom.isNotEmpty()) {
                append("BOOK-SPECIFIC INSTRUCTIONS:\n")
                append(custom)
                append("\n\n")
            }
            append("OCR TEXT:\n")
            append(ocrText.take(12_000))
        }
        val payload = JSONObject()
            .put("model", model)
            .put("temperature", 0)
            .put("response_format", JSONObject().put("type", "json_object"))
            .put("messages", org.json.JSONArray().put(JSONObject()
                .put("role", "user")
                .put("content", prompt)))
        val data = post(url, payload, key, 60_000)
        val raw = data.optJSONArray("choices")?.optJSONObject(0)
            ?.optJSONObject("message")?.optString("content") ?: ""
        return parseExtraction(raw)
    }

    /** Validate and normalize a model response without silently turning bad JSON
     *  into a completed empty record. A response with usable fields but an
     *  incomplete schema is retained as partial so those fields stay visible. */
    internal fun parseExtraction(raw: String): ExtractionResult {
        val cleaned = raw.trim().removePrefix("```json").removePrefix("```")
            .removeSuffix("```").trim()
        if (cleaned.isEmpty()) throw InvalidExtractionError("Extraction returned an empty response")
        val obj = try {
            JSONObject(cleaned)
        } catch (e: Exception) {
            throw InvalidExtractionError("Extraction returned invalid JSON")
        }
        val out = JSONObject()
        val problems = mutableListOf<String>()
        var populated = false
        for (k in FIELDS) {
            val value = when {
                !obj.has(k) -> {
                    problems += "$k is missing"
                    ""
                }
                obj.opt(k) == JSONObject.NULL -> {
                    problems += "$k is not a string"
                    ""
                }
                obj.opt(k) is String -> obj.optString(k).trim()
                else -> {
                    problems += "$k is not a string"
                    obj.opt(k)?.toString()?.trim().orEmpty()
                }
            }
            if (value.isNotEmpty()) populated = true
            out.put(k, value)
        }

        val extraOut = JSONObject()
        val extra = obj.opt("extra")
        if (extra is JSONObject) {
            for (key in extra.keys()) {
                val rawValue = extra.opt(key)
                val value = when {
                    rawValue == null || rawValue == JSONObject.NULL -> ""
                    rawValue is String -> rawValue.trim()
                    else -> {
                        problems += "extra.$key is not a string"
                        rawValue.toString().trim()
                    }
                }
                if (value.isNotEmpty()) {
                    extraOut.put(key, value)
                    populated = true
                }
            }
        } else {
            problems += "extra is missing or is not an object"
        }
        out.put("extra", extraOut)

        if (!populated)
            throw InvalidExtractionError("Extraction returned no bibliographic fields")
        val warning = problems.distinct().takeIf { it.isNotEmpty() }?.let {
            val shown = it.take(3).joinToString(", ")
            if (it.size > 3) "Partial extraction response: $shown (+${it.size - 3} more)"
            else "Partial extraction response: $shown"
        }
        return ExtractionResult(out, complete = warning == null, warning = warning)
    }

    /** Merge an accepted response over the prior record without ever erasing a
     *  populated field. Automatic retries only fill gaps; an explicit user
     *  reprocess may replace values, but still cannot replace one with blank. */
    internal fun mergeExtraction(
        existing: JSONObject?,
        incoming: JSONObject,
        replaceExisting: Boolean = false,
    ): JSONObject {
        val out = JSONObject()
        for (key in FIELDS) {
            val old = existing?.optString(key)?.trim().orEmpty()
            val fresh = incoming.optString(key).trim()
            out.put(key, when {
                old.isEmpty() -> fresh
                fresh.isEmpty() -> old
                replaceExisting -> fresh
                else -> old
            })
        }
        val extraOut = JSONObject()
        fun addExtra(source: JSONObject?, replace: Boolean) {
            if (source == null) return
            for (key in source.keys()) {
                val value = source.optString(key).trim()
                if (value.isNotEmpty() && (replace || !extraOut.has(key))) extraOut.put(key, value)
            }
        }
        addExtra(existing?.optJSONObject("extra"), replace = false)
        addExtra(incoming.optJSONObject("extra"), replace = replaceExisting)
        out.put("extra", extraOut)
        return out
    }

    internal fun hasPopulatedMetadata(metadata: JSONObject): Boolean {
        if (FIELDS.any { metadata.optString(it).trim().isNotEmpty() }) return true
        val extra = metadata.optJSONObject("extra") ?: return false
        return extra.keys().asSequence().any { extra.optString(it).trim().isNotEmpty() }
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
