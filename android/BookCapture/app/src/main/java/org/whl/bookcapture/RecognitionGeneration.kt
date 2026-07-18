package org.whl.bookcapture

/** Pure gate kept outside the Android/Vosk types so stale-callback behavior is
 *  directly unit-testable on the JVM. */
internal fun recognitionCallbackIsCurrent(
    callbackGeneration: Long,
    currentGeneration: Long,
    paused: Boolean,
    stopped: Boolean,
): Boolean = callbackGeneration == currentGeneration && !paused && !stopped
