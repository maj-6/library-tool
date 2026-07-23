package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class BookPhotoHoldContractTest {

    @Test
    fun detailHeroAndCarouselLoadOriginalOnlyWhileHeld() {
        val source = File(
            "src/main/java/org/whl/bookcapture/EntryDetailActivity.kt",
        ).readText()

        assertTrue(
            Regex("""installOriginalHold\(\s*binding\.heroPhoto,\s*hero,\s*heroBitmap,""")
                .containsMatchIn(source),
        )
        assertTrue(
            Regex("""installOriginalHold\(\s*image,\s*descriptor,\s*bitmap,""")
                .containsMatchIn(source),
        )
        assertTrue(source.contains("view.onOriginalHoldChanged = { showingOriginal ->"))
        assertTrue(
            Regex("""decodeSampledOriented\(\s*descriptor\.rawFile,""")
                .containsMatchIn(source),
        )
        assertTrue(source.contains("view.setPhotoBitmap(raw)"))
        assertTrue(source.contains("view.setPhotoBitmap(display)"))
        assertTrue(source.contains("view.setOverlayRegions(emptyList())"))
        assertTrue(source.contains("applyOverlay(view, descriptor)"))
        assertTrue(source.contains("private val comparisonDecodeGate = Semaphore(permits = 1)"))
        assertTrue(source.contains("comparisonDecodeGate.withPermit"))
        assertTrue(source.contains("private var photoRenderGeneration = 0"))
        assertTrue(source.contains("val renderGeneration = ++photoRenderGeneration"))
        assertTrue(source.contains("photoRenderGeneration != renderGeneration"))
        assertTrue(source.contains("binding.heroPhoto.onOriginalHoldChanged = null"))
        assertTrue(source.contains("generation != viewerDecodeGeneration"))
        assertTrue(source.contains("displayedBitmap?.takeIf { !it.isRecycled }?.recycle()"))
        assertTrue(source.contains("ownedRaw?.takeIf { !it.isRecycled }?.recycle()"))
        assertTrue(source.contains("ownedBitmap?.takeIf { !it.isRecycled }?.recycle()"))
        assertFalse(source.contains("heroOriginal"))
        assertFalse(source.contains("val (bitmap, original)"))
    }

    @Test
    fun detailRefreshUsesActiveWorkAndStoppingDismissesTheViewer() {
        val source = File(
            "src/main/java/org/whl/bookcapture/EntryDetailActivity.kt",
        ).readText()
        val observer = source.substringAfter("val entryId = intent.getStringExtra")
            .substringBefore("override fun onSaveInstanceState")
        val onStop = source.substringAfter("override fun onStop()")
            .substringBefore("override fun onDestroy()")

        assertTrue(observer.contains("getWorkInfosLiveData(activeUniqueWorkQuery("))
        assertTrue(observer.contains("CaptureMetadataSyncWorker.WORK_NAME"))
        assertTrue(observer.contains("CaptureMetadataSyncWorker.PULL_WORK_NAME"))
        assertFalse(observer.contains("getWorkInfosForUniqueWorkLiveData"))
        assertTrue(onStop.contains("photoRenderGeneration++"))
        assertTrue(onStop.contains("photoJob?.cancel()"))
        assertTrue(onStop.contains("binding.heroPhoto.onOriginalHoldChanged = null"))
        assertTrue(onStop.contains("workerRefreshJob?.cancel()"))
        assertTrue(onStop.contains("viewerDialog?.dismiss()"))
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
