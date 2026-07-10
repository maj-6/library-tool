package org.whl.bookcapture

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale

/**
 * Confirmation feedback for voice commands: a short tone (instant) plus a
 * spoken word (unambiguous). While TTS is speaking, the recognizer is
 * suppressed via [speaking] so a cue like "photo two" can't retrigger the
 * "photo" command through the microphone.
 */
class AudioCues(ctx: Context, private val onSpeakingChanged: (Boolean) -> Unit) {

    @Volatile var speaking = false
        private set

    private val tones = ToneGenerator(AudioManager.STREAM_MUSIC, 85)
    private var ttsReady = false
    private val tts = TextToSpeech(ctx) { status -> ttsReady = status == TextToSpeech.SUCCESS }

    init {
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String?) = setSpeaking(true)
            override fun onDone(id: String?) = setSpeaking(false)
            @Deprecated("Deprecated in Java")
            override fun onError(id: String?) = setSpeaking(false)
        })
        tts.language = Locale.US
    }

    private fun setSpeaking(v: Boolean) {
        speaking = v
        onSpeakingChanged(v)
    }

    private fun say(text: String) {
        if (!ttsReady) return
        setSpeaking(true)          // suppress the mic before audio actually starts
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "cue-${System.nanoTime()}")
    }

    fun started() {
        tones.startTone(ToneGenerator.TONE_PROP_BEEP, 120)
        say("started")
    }

    fun photo(n: Int) {
        tones.startTone(ToneGenerator.TONE_PROP_BEEP2, 120)
        say("photo $n")
    }

    fun saved(photos: Int) {
        tones.startTone(ToneGenerator.TONE_PROP_ACK, 150)
        say(if (photos == 1) "saved, one photo" else "saved, $photos photos")
    }

    fun cancelled() {
        tones.startTone(ToneGenerator.TONE_PROP_NACK, 150)
        say("cancelled")
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
