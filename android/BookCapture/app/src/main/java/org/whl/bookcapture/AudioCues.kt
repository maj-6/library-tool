package org.whl.bookcapture

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/**
 * Confirmation feedback: a short tone plus a very brief vibration per event —
 * each command already has a visible consequence on screen; the cue only
 * confirms the mic heard it.
 *
 * Photo capture is deliberately SPLIT: [photoHeard] plays a SOUND ONLY the
 * instant the "photo" cue is committed to (the shutter is firing), and
 * [photoCaptured] fires a VIBRATION ONLY once the frame is actually written.
 * So you hear "heard you", then feel "got it" — the two are separated in time.
 */
class AudioCues(context: Context) {

    private val tones = ToneGenerator(AudioManager.STREAM_MUSIC, 85)
    private val vibrator: Vibrator? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
        else
            @Suppress("DEPRECATION")
            (context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator)
    private var released = false      // a cue after shutdown() must no-op, not throw

    private fun tone(kind: Int, ms: Int) {
        if (!released) tones.startTone(kind, ms)
    }

    /** A very brief haptic tick. minSdk is 26, so createOneShot needs no guard. */
    private fun buzz(ms: Long = 25L) {
        if (released) return
        val v = vibrator ?: return
        if (!v.hasVibrator()) return
        v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    fun started() { tone(ToneGenerator.TONE_PROP_BEEP, 120); buzz() }
    fun saved(@Suppress("UNUSED_PARAMETER") photos: Int) { tone(ToneGenerator.TONE_PROP_ACK, 150); buzz() }
    fun cancelled() { tone(ToneGenerator.TONE_PROP_NACK, 150); buzz() }
    fun error(@Suppress("UNUSED_PARAMETER") message: String) { tone(ToneGenerator.TONE_SUP_ERROR, 200); buzz(40) }

    /** "photo" cue heard, shutter firing — SOUND ONLY. */
    fun photoHeard() = tone(ToneGenerator.TONE_PROP_BEEP2, 120)

    /** frame actually captured — VIBRATION ONLY. */
    fun photoCaptured() = buzz()

    fun shutdown() {
        released = true       // a voice command racing destroy cues nothing
        tones.release()
    }
}
