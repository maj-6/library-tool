package org.whl.bookcapture

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class BookPhotoHoldContractTest {

    @Test
    fun detailHeroAndCarouselSwapDirectlyToOriginalWhileHeld() {
        val source = File(
            "src/main/java/org/whl/bookcapture/EntryDetailActivity.kt",
        ).readText()

        assertTrue(source.contains(
            "installOriginalHold(binding.heroPhoto, hero, heroBitmap, heroOriginal)",
        ))
        assertTrue(source.contains("installOriginalHold(image, descriptor, bitmap, original)"))
        assertTrue(source.contains("view.onOriginalHoldChanged = original?.let"))
        assertTrue(source.contains(
            "view.setPhotoBitmap(if (showingOriginal) raw else display)",
        ))
        assertTrue(source.contains("if (showingOriginal) {"))
        assertTrue(source.contains("view.setOverlayRegions(emptyList())"))
        assertTrue(source.contains("applyOverlay(view, descriptor)"))
    }

    @Test
    fun photoSurfaceSignalsOriginalOnHoldAndProcessedOnReleaseOrCancel() {
        val source = File(
            "src/main/java/org/whl/bookcapture/ZoomablePhotoView.kt",
        ).readText()

        assertTrue(source.contains("override fun onLongPress(e: MotionEvent)"))
        assertTrue(source.contains("onOriginalHoldChanged?.invoke(true)"))
        assertTrue(source.contains("event.actionMasked == MotionEvent.ACTION_UP"))
        assertTrue(source.contains("event.actionMasked == MotionEvent.ACTION_CANCEL"))
        assertTrue(source.contains("onOriginalHoldChanged?.invoke(false)"))
    }
}
