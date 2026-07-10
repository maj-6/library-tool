package org.whl.bookcapture

import android.content.Context
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.RecognitionListener
import org.vosk.android.SpeechService
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.zip.ZipInputStream

/**
 * Always-on offline keyword spotting via Vosk, restricted to a four-word
 * grammar so recognition is fast and hard to false-trigger. The small English
 * model (~40 MB) is downloaded once on first run into filesDir/model.
 */
class VoiceController(
    private val ctx: Context,
    private val onCommand: (String) -> Unit,
    private val onState: (String) -> Unit,
) {
    companion object {
        const val MODEL_URL =
            "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
        const val MODEL_DIR = "vosk-model-small-en-us-0.15"
        val COMMANDS = listOf("start", "photo", "done", "cancel")
        private const val DEBOUNCE_MS = 1200L
    }

    private var speechService: SpeechService? = null
    private var model: Model? = null
    @Volatile var suppressed = false          // true while TTS cues are speaking
    private var lastCommand = ""
    private var lastCommandAt = 0L

    val modelReady: Boolean
        get() = File(File(ctx.filesDir, "model"), MODEL_DIR).isDirectory

    /** Download + unzip the model with coarse progress callbacks. Blocking. */
    fun downloadModel(onProgress: (String) -> Unit) {
        val root = File(ctx.filesDir, "model").apply { mkdirs() }
        val target = File(root, MODEL_DIR)
        if (target.isDirectory) return
        val zip = File(root, "model.zip")
        onProgress("Downloading voice model…")
        val conn = URL(MODEL_URL).openConnection() as HttpURLConnection
        conn.connectTimeout = 20_000
        conn.readTimeout = 60_000
        val total = conn.contentLengthLong
        conn.inputStream.use { input ->
            FileOutputStream(zip).use { out ->
                val buf = ByteArray(256 * 1024)
                var got = 0L
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    got += n
                    if (total > 0) onProgress(
                        "Downloading voice model… ${(got * 100 / total)}%")
                }
            }
        }
        onProgress("Unpacking voice model…")
        val rootCanonical = root.canonicalPath + File.separator
        ZipInputStream(zip.inputStream().buffered()).use { z ->
            while (true) {
                val e = z.nextEntry ?: break
                val f = File(root, e.name)
                if (!f.canonicalPath.startsWith(rootCanonical))   // zip-slip guard
                    throw SecurityException("bad zip entry: ${e.name}")
                if (e.isDirectory) f.mkdirs()
                else {
                    f.parentFile?.mkdirs()
                    FileOutputStream(f).use { out -> z.copyTo(out) }
                }
            }
        }
        zip.delete()
        if (!target.isDirectory) throw IllegalStateException("model unzip failed")
    }

    /** Start continuous recognition (model must be ready, mic permission granted). */
    fun start() {
        if (speechService != null) return
        val dir = File(File(ctx.filesDir, "model"), MODEL_DIR)
        val m = Model(dir.absolutePath)
        model = m
        // "[unk]" absorbs everything that is not a command word
        val grammar = COMMANDS.joinToString("\", \"", "[\"", "\", \"[unk]\"]")
        val rec = Recognizer(m, 16000.0f, grammar)
        val svc = SpeechService(rec, 16000.0f)
        speechService = svc
        svc.startListening(object : RecognitionListener {
            override fun onResult(hypothesis: String?) = handle(hypothesis)
            override fun onFinalResult(hypothesis: String?) = handle(hypothesis)
            override fun onPartialResult(hypothesis: String?) { /* waiting for the utterance end */ }
            override fun onError(exception: Exception?) {
                onState("Voice error: ${exception?.message ?: "?"}")
            }
            override fun onTimeout() { /* keeps listening */ }
        })
        onState("Listening")
    }

    private fun handle(hypothesis: String?) {
        if (hypothesis.isNullOrBlank() || suppressed) return
        val text = try { JSONObject(hypothesis).optString("text") } catch (_: Exception) { "" }
        if (text.isBlank()) return
        // an utterance may carry several tokens; act on the last command word
        val word = text.split(" ").lastOrNull { it in COMMANDS } ?: return
        val now = System.currentTimeMillis()
        if (word == lastCommand && now - lastCommandAt < DEBOUNCE_MS) return
        lastCommand = word
        lastCommandAt = now
        onCommand(word)
    }

    fun stop() {
        speechService?.stop()
        speechService?.shutdown()
        speechService = null
        model?.close()
        model = null
    }
}
