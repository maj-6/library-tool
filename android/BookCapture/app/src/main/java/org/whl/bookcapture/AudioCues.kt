package org.whl.bookcapture

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale

/**
 * Confirmation feedback for voice commands: a short tone (instant) plus a
 * spoken word (unambiguous). While TTS is speaking — and for a grace window
 * after it ends — recognition is suppressed via [onSpeakingChanged] so cue
 * audio can't retrigger a command. Cue wording also avoids the four command
 * words entirely (e.g. "ready", not "started"): belt and braces.
 */
class AudioCues(ctx: Context, private val onSpeakingChanged: (Boolean) -> Unit) {

    private val tones = ToneGenerator(AudioManager.STREAM_MUSIC, 85)
    private var ttsReady = false
    private lateinit var tts: TextToSpeech

    init {
        tts = TextToSpeech(ctx) { status ->
            if (status == TextToSpeech.SUCCESS) {
                ttsReady = true
                tts.language = Locale.US          // only valid once the engine is up
            }
        }
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) = onSpeakingChanged(true)
            override fun onDone(id: String?) = onSpeakingChanged(false)
            @Deprecated("Deprecated in Java")
            override fun onError(id: String?) = onSpeakingChanged(false)
        })
    }

    private fun say(text: String) {
        if (!ttsReady) return
        onSpeakingChanged(true)    // suppress the mic before audio actually starts
        val rc = tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "cue-${System.nanoTime()}")
        if (rc != TextToSpeech.SUCCESS) onSpeakingChanged(false)   // never stick muted
    }

    fun started() {
        tones.startTone(ToneGenerator.TONE_PROP_BEEP, 120)
        say("ready")
    }

    fun photo(n: Int) {
        tones.startTone(ToneGenerator.TONE_PROP_BEEP2, 120)
        say("captured $n")
    }

    fun saved(photos: Int) {
        tones.startTone(ToneGenerator.TONE_PROP_ACK, 150)
        say(if (photos == 1) "saved, one page" else "saved, $photos pages")
    }

    fun cancelled() {
        tones.startTone(ToneGenerator.TONE_PROP_NACK, 150)
        say("discarded")
    }

    fun error(message: String) {
        tones.startTone(ToneGenerator.TONE_SUP_ERROR, 200)
        say(message)
    }

    fun shutdown() {
        tts.stop()
        tts.shutdown()
        tones.release()
    }
}
