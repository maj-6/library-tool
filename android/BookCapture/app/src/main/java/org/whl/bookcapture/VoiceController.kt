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
 *
 * Suppression: Vosk finalizes an utterance a moment AFTER the audio ends, so
 * a cue spoken through the speaker could come back as a command after the
 * `speaking` flag drops. suppress(false) therefore opens a grace window, and
 * results inside it are discarded.
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
        private const val DEBOUNCE_MS = 700L
        private const val ECHO_GRACE_MS = 1500L
        private const val SUPPRESS_MAX_MS = 6000L   // a stuck flag can't mute forever
    }

    private var speechService: SpeechService? = null
    private var model: Model? = null
    @Volatile private var suppressedFlag = false
    @Volatile private var suppressedAt = 0L
    @Volatile private var graceUntil = 0L
    private var lastCommand = ""
    private var lastCommandAt = 0L

    /** TTS feedback started/stopped: mute recognition + a post-cue grace window. */
    fun suppress(active: Boolean) {
        val now = System.currentTimeMillis()
        if (active) {
            suppressedFlag = true
            suppressedAt = now
        } else {
            suppressedFlag = false
            graceUntil = now + ECHO_GRACE_MS
        }
    }

    private fun muted(): Boolean {
        val now = System.currentTimeMillis()
        if (suppressedFlag && now - suppressedAt < SUPPRESS_MAX_MS) return true
        return now < graceUntil
    }

    val modelReady: Boolean
        get() = File(File(ctx.filesDir, "model"), MODEL_DIR).isDirectory

    /** Download + unzip the model with coarse progress callbacks. Blocking.
     *  Unzips into a temp dir and renames at the end, so an interrupted run
     *  can't leave a half-model that bricks every later startup. */
    fun downloadModel(onProgress: (String) -> Unit) {
        val root = File(ctx.filesDir, "model").apply { mkdirs() }
        val target = File(root, MODEL_DIR)
        if (target.isDirectory) return
        val zip = File(root, "model.zip")
        val stage = File(root, ".unzip-tmp")
        stage.deleteRecursively()
        onProgress("Downloading voice model…")
        val conn = URL(MODEL_URL).openConnection() as HttpURLConnection
        conn.connectTimeout = 20_000
        conn.readTimeout = 60_000
        conn.instanceFollowRedirects = true
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
        val stageCanonical = stage.canonicalPath + File.separator
        stage.mkdirs()
        ZipInputStream(zip.inputStream().buffered()).use { z ->
            while (true) {
                val e = z.nextEntry ?: break
                val f = File(stage, e.name)
                if (!f.canonicalPath.startsWith(stageCanonical))   // zip-slip guard
                    throw SecurityException("bad zip entry: ${e.name}")
                if (e.isDirectory) f.mkdirs()
                else {
                    f.parentFile?.mkdirs()
                    FileOutputStream(f).use { out -> z.copyTo(out) }
                }
            }
        }
        zip.delete()
        val unpacked = File(stage, MODEL_DIR)
        if (!unpacked.isDirectory || !unpacked.renameTo(target))
            throw IllegalStateException("model unzip failed")
        stage.deleteRecursively()
    }

    /** Start continuous recognition. Call OFF the main thread: the native
     *  model load takes ~1s. A corrupt model wipes itself for a re-download. */
    fun start() {
        if (speechService != null) return
        val dir = File(File(ctx.filesDir, "model"), MODEL_DIR)
        val m = try {
            Model(dir.absolutePath)
        } catch (e: Exception) {
            dir.deleteRecursively()          // corrupt: next launch re-downloads
            onState("Voice model corrupt — restart the app to re-download")
            return
        }
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

    /** Mute the mic while the app is backgrounded / settings are open. */
    fun setPaused(paused: Boolean) {
        speechService?.setPause(paused)
    }

    private fun handle(hypothesis: String?) {
        if (hypothesis.isNullOrBlank() || muted()) return
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
