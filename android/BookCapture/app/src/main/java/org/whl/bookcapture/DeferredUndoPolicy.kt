package org.whl.bookcapture

internal enum class DeferredUndoDisposition {
    REMOVE_TARGET,
    ALREADY_UNDONE,
    INVALID,
}

/** Decide a persisted in-flight Undo without ever falling back to older data.
 * A missing target means the accepted CameraX shot failed or was reclaimed;
 * the requested undo is therefore already complete. */
internal fun deferredUndoDisposition(
    targetPage: Int?,
    committedPhotoCount: Int,
    targetExists: Boolean,
): DeferredUndoDisposition = when {
    targetPage == null || targetPage <= 0 -> DeferredUndoDisposition.INVALID
    !targetExists -> DeferredUndoDisposition.ALREADY_UNDONE
    committedPhotoCount == targetPage -> DeferredUndoDisposition.REMOVE_TARGET
    else -> DeferredUndoDisposition.INVALID
}
