package org.whl.bookcapture

import android.content.Context
import android.os.Handler
import android.os.Looper
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
 * Latency: non-destructive commands can fire from PARTIAL results.
 * Waiting for the final result means waiting for Vosk's endpointer to hear
 * ~0.5-1s of silence; a partial that shows the same command word twice in a
 * row is just as trustworthy under this grammar and arrives while the word is
 * barely off the tongue. The final result still fires (covers a word the
 * partial stream only surfaced once) — the debounce swallows the duplicate.
 *
 * Cues are plain tones now, so no suppression window is needed: a beep can't
 * spell "photo".
 *
 * Threading: start() runs on an IO thread (the model load takes ~1s);
 * setPaused()/stop() only ever run on the main thread, so they never race
 * each other and must never block behind start()'s lock. The volatile
 * `stopped`/`pausedRequested` flags are written before anything else and
 * re-read by start() after it builds the recognizer, so a pause or destroy
 * that lands mid-load wins — no recognizer left holding the microphone with
 * nobody to stop it.
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
        private val PARTIAL_COMMANDS = setOf("start", "photo")

        /** Final recognition is required for actions that seal or delete work. */
        internal fun commandFromPartial(text: String): String? =
            text.split(" ").lastOrNull { it in PARTIAL_COMMANDS }
        // long enough to swallow the final result that trails a partial-fired
        // command; page-flipping cadence never repeats a word this fast
        private const val DEBOUNCE_MS = 1500L
        private const val ERROR_RESTART_MS = 1000L
        private const val ERROR_RESTART_MAX = 3     // consecutive; a result resets it
    }

    @Volatile private var speechService: SpeechService? = null
    @Volatile private var listener: RecognitionListener? = null
    private var model: Model? = null
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var stopped = false
    @Volatile private var pausedRequested = false
    private var errorRestarts = 0
    private var pendingPartial = ""
    private var lastCommand = ""
    private var lastCommandAt = 0L

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
     *  model load takes ~1s. That load runs OUTSIDE the lock, so setPaused()/
     *  stop() on the main thread never stall behind it; only the fast decision
     *  to actually begin recording is synchronized — the same lock those two
     *  hold — so startListening can't race a concurrent stop/pause. A corrupt
     *  model wipes itself for a re-download. */
    fun start() {
        if (stopped) return
        synchronized(this) { if (speechService != null) return }
        val dir = File(File(ctx.filesDir, "model"), MODEL_DIR)
        val m = try {
            Model(dir.absolutePath)
        } catch (e: Exception) {
            dir.deleteRecursively()          // corrupt: next launch re-downloads
            onState("Voice model corrupt — restart the app to re-download")
            return
        }
        // "[unk]" absorbs everything that is not a command word
        val grammar = COMMANDS.joinToString("\", \"", "[\"", "\", \"[unk]\"]")
        val svc = SpeechService(Recognizer(m, 16000.0f, grammar), 16000.0f)
        val lst = object : RecognitionListener {
            override fun onResult(hypothesis: String?) = handleFinal(hypothesis)
            override fun onFinalResult(hypothesis: String?) = handleFinal(hypothesis)
            override fun onPartialResult(hypothesis: String?) = handlePartial(hypothesis)
            override fun onError(exception: Exception?) {
                onState("Voice error: ${exception?.message ?: "?"}")
                scheduleRestart()   // one dead recognizer must not end hands-free
            }
            override fun onTimeout() { /* keeps listening */ }
        }
        synchronized(this) {
            // a stop/pause (or a second start) landed during the load: discard
            // this service rather than leave the mic on for a dead activity
            if (stopped || speechService != null) {
                svc.shutdown()
                m.close()
                return
            }
            speechService = svc
            listener = lst
            model = m
            if (!pausedRequested) {
                svc.startListening(lst)
                onState("Listening")
            }
        }
    }

    /** Backgrounded / settings open: actually stop the recorder, not just the
     *  results — SpeechService.setPause keeps AudioRecord running and the OS
     *  mic indicator lit. stop()/startListening() cycles the record thread.
     *  Synchronized on the same lock as start()'s final block so the two can't
     *  interleave; that lock is never held during the model load, so this
     *  can't stall. Main thread only. */
    @Synchronized
    fun setPaused(paused: Boolean) {
        pausedRequested = paused
        val svc = speechService ?: return    // start() honors the flag instead
        if (paused) {
            svc.stop()
        } else {
            listener?.let { if (svc.startListening(it)) onState("Listening") }
        }
    }

    /** A recognizer that died on error gets a few delayed restarts; a clean
     *  result resets the budget, so only a hard-broken mic stays down. */
    private fun scheduleRestart() {
        if (stopped || pausedRequested || errorRestarts >= ERROR_RESTART_MAX) return
        errorRestarts += 1
        main.postDelayed({
            synchronized(this) {
                if (stopped || pausedRequested) return@postDelayed
                val svc = speechService ?: return@postDelayed
                listener?.let { if (svc.startListening(it)) onState("Listening") }
            }
        }, ERROR_RESTART_MS * errorRestarts)
    }

    /** Same safe command in two consecutive partials -> fire now. Commands
     *  that seal or delete work are accepted only from a final result. */
    private fun handlePartial(hypothesis: String?) {
        errorRestarts = 0
        if (hypothesis.isNullOrBlank()) return
        val text = try { JSONObject(hypothesis).optString("partial") } catch (_: Exception) { "" }
        val word = commandFromPartial(text) ?: return
        if (word == pendingPartial) {
            pendingPartial = ""
            fire(word)
        } else {
            pendingPartial = word
        }
    }

    private fun handleFinal(hypothesis: String?) {
        errorRestarts = 0
        pendingPartial = ""
        if (hypothesis.isNullOrBlank()) return
        val text = try { JSONObject(hypothesis).optString("text") } catch (_: Exception) { "" }
        val word = text.split(" ").lastOrNull { it in COMMANDS } ?: return
        fire(word)
    }

    private fun fire(word: String) {
        val now = System.currentTimeMillis()
        if (word == lastCommand && now - lastCommandAt < DEBOUNCE_MS) return
        lastCommand = word
        lastCommandAt = now
        onCommand(word)
    }

    fun stop() {
        stopped = true          // volatile, set BEFORE the lock: a start() whose
        main.removeCallbacksAndMessages(null)   // load is mid-flight reads this
        synchronized(this) {                     // in its final block and bails
            speechService?.stop()
            speechService?.shutdown()
            speechService = null
            listener = null
            model?.close()
            model = null
        }
    }
}
