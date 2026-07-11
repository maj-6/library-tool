package org.whl.bookcapture

import android.media.AudioManager
import android.media.ToneGenerator

/**
 * Confirmation feedback: one short, distinct tone per event, nothing spoken.
 * Each command already has a visible consequence on screen; the tone only
 * confirms the mic heard it. (Tones can't retrigger recognition, so the old
 * TTS suppression machinery is gone with the voice.)
 */
class AudioCues {

    private val tones = ToneGenerator(AudioManager.STREAM_MUSIC, 85)
    private var released = false      // a cue after shutdown() must no-op, not throw

    private fun tone(kind: Int, ms: Int) {
        if (!released) tones.startTone(kind, ms)
    }

    fun started() = tone(ToneGenerator.TONE_PROP_BEEP, 120)
    fun photo(@Suppress("UNUSED_PARAMETER") n: Int) = tone(ToneGenerator.TONE_PROP_BEEP2, 120)
    fun saved(@Suppress("UNUSED_PARAMETER") photos: Int) = tone(ToneGenerator.TONE_PROP_ACK, 150)
    fun cancelled() = tone(ToneGenerator.TONE_PROP_NACK, 150)
    fun error(@Suppress("UNUSED_PARAMETER") message: String) =
        tone(ToneGenerator.TONE_SUP_ERROR, 200)

    fun shutdown() {
        released = true       // a voice command racing destroy cues nothing
        tones.release()
    }
}
