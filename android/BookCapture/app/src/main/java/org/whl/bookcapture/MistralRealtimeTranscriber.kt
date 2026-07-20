package org.whl.bookcapture

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.util.Base64
import androidx.core.content.ContextCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

internal const val MISTRAL_REALTIME_TRANSCRIPTION_MODEL =
    "voxtral-mini-transcribe-realtime-2602"
private const val MISTRAL_REALTIME_URL =
    "wss://api.mistral.ai/v1/audio/transcriptions/realtime"
private const val NOTE_SAMPLE_RATE = 16_000
private const val NOTE_CHUNK_MS = 480
private const val NOTE_TARGET_DELAY_MS = 480
private const val NORMAL_CLOSE = 1000
private const val DRAIN_TIMEOUT_MS = 2_000L

internal sealed interface MistralRealtimeEvent {
    data object SessionCreated : MistralRealtimeEvent
    data class TextDelta(val text: String) : MistralRealtimeEvent
    data class Done(val text: String) : MistralRealtimeEvent
    data class Failure(val message: String) : MistralRealtimeEvent
    data object Ignored : MistralRealtimeEvent
}

/** Parse the documented Voxtral event names while tolerating the shorter
 * aliases used by early realtime deployments. Unknown events remain harmless. */
internal fun parseMistralRealtimeEvent(payload: String): MistralRealtimeEvent {
    val value = runCatching { JSONObject(payload) }.getOrNull()
        ?: return MistralRealtimeEvent.Failure("Mistral returned invalid realtime data")
    return when (value.optString("type")) {
        "session.created", "transcription.session.created" ->
            MistralRealtimeEvent.SessionCreated
        "transcription.text.delta", "transcription.delta" ->
            MistralRealtimeEvent.TextDelta(
                value.optString("text").ifEmpty { value.optString("delta") },
            )
        "transcription.done", "transcription.text.done" ->
            MistralRealtimeEvent.Done(value.optString("text"))
        "error", "transcription.error" -> {
            val error = value.opt("error")
            val message = when (error) {
                is JSONObject -> error.optString("message")
                    .ifEmpty { error.optString("detail") }
                is String -> error
                else -> value.optString("message")
            }.ifEmpty { "Mistral realtime transcription failed" }
            MistralRealtimeEvent.Failure(message.take(300))
        }
        else -> MistralRealtimeEvent.Ignored
    }
}

/** Foreground-only microphone stream for Voxtral Realtime.
 *
 * The app already uses the operator's configured Mistral key for OCR. Native
 * Android WebSockets can carry the same Bearer header; no key is compiled into
 * the APK or written to logs. Vosk must be paused by the owner before [start]
 * because both engines otherwise compete for Android's single microphone.
 */
internal class MistralRealtimeTranscriber(
    private val context: Context,
    private val apiKey: String,
    private val listener: Listener,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build(),
) {
    interface Listener {
        fun onConnected()
        fun onTranscript(text: String, final: Boolean)
        fun onError(message: String)
    }

    private val stopped = AtomicBoolean(false)
    private val terminal = AtomicBoolean(false)
    private val audioStarted = AtomicBoolean(false)
    private val draining = AtomicBoolean(false)
    private val transcript = StringBuilder()
    private val sendMonitor = Any()
    private val drainMonitor = Any()
    private val mainHandler = Handler(Looper.getMainLooper())
    @Volatile private var socket: WebSocket? = null
    @Volatile private var recorder: AudioRecord? = null
    @Volatile private var audioThread: Thread? = null
    @Volatile private var drainCallback: ((String) -> Unit)? = null
    private val drainTimeout = Runnable { completeDrain(currentTranscript()) }

    fun start(): Boolean {
        if (apiKey.isBlank() || ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.RECORD_AUDIO,
            ) != PackageManager.PERMISSION_GRANTED
        ) return false

        val request = Request.Builder()
            .url("$MISTRAL_REALTIME_URL?model=$MISTRAL_REALTIME_TRANSCRIPTION_MODEL")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .header("User-Agent", "library-tool-android/${BuildConfig.VERSION_NAME}")
            .build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                if (terminal.get()) return
                when (val event = parseMistralRealtimeEvent(text)) {
                    MistralRealtimeEvent.SessionCreated -> configureAndRecord(webSocket)
                    is MistralRealtimeEvent.TextDelta -> {
                        if (event.text.isNotEmpty()) {
                            synchronized(transcript) { transcript.append(event.text) }
                            listener.onTranscript(currentTranscript(), false)
                        }
                    }
                    is MistralRealtimeEvent.Done -> {
                        val finalText = event.text.trim().ifEmpty { currentTranscript().trim() }
                        if (finalText.isNotEmpty()) listener.onTranscript(finalText, true)
                        if (draining.get()) completeDrain(finalText)
                    }
                    is MistralRealtimeEvent.Failure -> fail(event.message)
                    MistralRealtimeEvent.Ignored -> Unit
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (!terminal.get()) {
                    val detail = response?.message?.takeIf { it.isNotBlank() }
                        ?: t.message?.takeIf { it.isNotBlank() }
                        ?: t.javaClass.simpleName
                    if (draining.get()) completeDrain(currentTranscript())
                    else fail("Mistral transcription unavailable: $detail")
                }
            }
        })
        return true
    }

    private fun configureAndRecord(webSocket: WebSocket) {
        if (stopped.get() || terminal.get()) return
        val session = JSONObject()
            .put(
                "audio_format",
                JSONObject()
                    .put("encoding", "pcm_s16le")
                    .put("sample_rate", NOTE_SAMPLE_RATE),
            )
            .put("target_streaming_delay_ms", NOTE_TARGET_DELAY_MS)
        try {
            val configured = synchronized(sendMonitor) {
                // Configuration, recorder startup, audio appends, and the
                // terminal flush/end pair share one ordering boundary. A very
                // fast stop can therefore never enqueue end before update or
                // start a recorder after the session has been ended.
                if (stopped.get() || terminal.get() || audioStarted.get()) {
                    false
                } else if (!webSocket.send(
                        JSONObject().put("type", "session.update")
                            .put("session", session).toString(),
                    )
                ) {
                    false
                } else {
                    audioStarted.set(true)
                    startAudio(webSocket)
                    true
                }
            }
            if (!configured) {
                if (!stopped.get() && !terminal.get()) {
                    fail("Could not configure Mistral transcription")
                }
                return
            }
            if (!stopped.get() && !draining.get()) listener.onConnected()
        } catch (e: Exception) {
            fail("Microphone unavailable: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    @SuppressLint("MissingPermission")
    private fun startAudio(webSocket: WebSocket) {
        val channel = AudioFormat.CHANNEL_IN_MONO
        val encoding = AudioFormat.ENCODING_PCM_16BIT
        val chunkBytes = NOTE_SAMPLE_RATE * NOTE_CHUNK_MS / 1000 * 2
        val minBuffer = AudioRecord.getMinBufferSize(NOTE_SAMPLE_RATE, channel, encoding)
        check(minBuffer > 0) { "unsupported 16 kHz microphone format" }
        val record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            NOTE_SAMPLE_RATE,
            channel,
            encoding,
            maxOf(minBuffer * 2, chunkBytes * 2),
        )
        try {
            check(record.state == AudioRecord.STATE_INITIALIZED) { "microphone initialization failed" }
            recorder = record
            record.startRecording()
            check(record.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                "microphone did not start"
            }
        } catch (error: Exception) {
            releaseAudioRecord(record)
            throw error
        }
        val thread = Thread({
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO)
            val buffer = ByteArray(chunkBytes)
            try {
                while (!stopped.get()) {
                    val count = record.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                    if (count <= 0 || stopped.get()) continue
                    var sent = true
                    synchronized(sendMonitor) {
                        // finish() sets stopped before taking this same lock.
                        // Rechecking here guarantees no append can overtake the
                        // terminal flush/end pair.
                        if (stopped.get()) return@synchronized
                        if (webSocket.queueSize() > 1_048_576L) {
                            sent = false
                            return@synchronized
                        }
                        val audio = Base64.encodeToString(buffer, 0, count, Base64.NO_WRAP)
                        sent = webSocket.send(
                            JSONObject().put("type", "input_audio.append")
                                .put("audio", audio).toString(),
                        )
                    }
                    if (!sent) {
                        fail("Mistral transcription connection closed")
                        break
                    }
                }
            } catch (e: Exception) {
                if (!stopped.get()) fail(
                    "Microphone stopped: ${e.message ?: e.javaClass.simpleName}",
                )
            } finally {
                releaseAudioRecord(record)
            }
        }, "mistral-note-audio")
        audioThread = thread
        try {
            thread.start()
        } catch (error: Exception) {
            if (audioThread === thread) audioThread = null
            releaseAudioRecord(record)
            throw error
        }
    }

    private fun releaseAudioRecord(record: AudioRecord) {
        runCatching {
            if (record.recordingState == AudioRecord.RECORDSTATE_RECORDING) record.stop()
        }
        runCatching { record.release() }
        if (recorder === record) recorder = null
    }

    fun currentTranscript(): String = synchronized(transcript) { transcript.toString() }

    /** Stop recording and let Mistral drain its target-delay buffer. The final
     * callback fires on `transcription.done`, or after a bounded timeout with
     * every delta already received. The socket remains open while draining. */
    fun finish(onComplete: (String) -> Unit) {
        var alreadyTerminal = false
        synchronized(drainMonitor) {
            if (terminal.get()) {
                alreadyTerminal = true
            } else if (draining.get()) {
                return
            } else {
                // Publish the callback before exposing draining=true to a
                // WebSocket event that may complete synchronously.
                drainCallback = onComplete
                draining.set(true)
            }
        }
        if (alreadyTerminal) {
            onComplete(currentTranscript())
            return
        }
        val flushing = synchronized(sendMonitor) {
            stopped.set(true)
            val webSocket = socket
            audioStarted.get() && webSocket != null &&
                webSocket.send(JSONObject().put("type", "input_audio.flush").toString()) &&
                webSocket.send(JSONObject().put("type", "input_audio.end").toString())
        }
        stopRecorder()
        if (!flushing) {
            completeDrain(currentTranscript())
            return
        }
        mainHandler.postDelayed(drainTimeout, DRAIN_TIMEOUT_MS)
    }

    /** Abort immediately for discarded notes or Activity teardown. */
    fun stop() {
        if (!terminal.compareAndSet(false, true)) return
        stopped.set(true)
        mainHandler.removeCallbacks(drainTimeout)
        drainCallback = null
        stopRecorder()
        socket?.cancel()
        socket = null
        shutdownClient()
    }

    private fun completeDrain(finalTranscript: String) {
        if (!terminal.compareAndSet(false, true)) return
        mainHandler.removeCallbacks(drainTimeout)
        stopRecorder()
        socket?.close(NORMAL_CLOSE, "note complete")
        socket = null
        shutdownClient()
        val callback = synchronized(drainMonitor) {
            drainCallback.also { drainCallback = null }
        }
        callback?.invoke(finalTranscript.trim().ifEmpty { currentTranscript().trim() })
    }

    private fun fail(message: String) {
        if (draining.get()) {
            completeDrain(currentTranscript())
            return
        }
        if (!terminal.compareAndSet(false, true)) return
        stopped.set(true)
        stopRecorder()
        socket?.cancel()
        socket = null
        shutdownClient()
        listener.onError(message)
    }

    private fun stopRecorder() {
        recorder?.let { record ->
            runCatching {
                if (record.recordingState == AudioRecord.RECORDSTATE_RECORDING) record.stop()
            }
        }
        audioThread?.interrupt()
        audioThread = null
    }

    private fun shutdownClient() {
        client.dispatcher.cancelAll()
        client.connectionPool.evictAll()
    }
}
