package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class HomeLifecyclePerformanceTest {
    private val source =
        File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()

    @Test
    fun stoppedHomeCancelsLoadingAndReleasesOnlyTrackedDynamicThumbnails() {
        val onStop = section("override fun onStop()", "private fun showAppMenu()")
        assertTrue(onStop.contains("stopHomeLoading()"))

        val stopLoading = section("private fun stopHomeLoading()", "private fun configureScanRowAccessibility")
        assertTrue(stopLoading.contains("cancelScheduledWorkerRefresh()"))
        assertTrue(stopLoading.contains("cancelScanListLoading()"))
        assertTrue(stopLoading.contains("cancelInspectListLoading()"))
        assertTrue(stopLoading.contains("resetThumbnailLoading()"))

        val resetThumbnails =
            section("private fun resetThumbnailLoading()", "private fun cancelScanListLoading()")
        assertTrue(resetThumbnails.contains("thumbJob?.cancel()"))
        assertTrue(resetThumbnails.contains("releaseDynamicThumbnails()"))

        val releaseThumbnails =
            section("private fun releaseDynamicThumbnails()", "private fun stopHomeLoading()")
        assertTrue(releaseThumbnails.contains("dynamicThumbnailViews.forEach"))
        assertTrue(releaseThumbnails.contains("setImageDrawable(null)"))
        assertTrue(releaseThumbnails.contains("dynamicThumbnailViews.clear()"))
        assertTrue(releaseThumbnails.contains("dynamicThumbnailBitmaps.values.forEach"))
        assertTrue(releaseThumbnails.contains("bitmap.recycle()"))
        assertTrue(releaseThumbnails.contains("dynamicThumbnailBitmaps.clear()"))

        assertTrue(source.contains("private fun setDynamicThumbnail(image: ImageView, bitmap: Bitmap)"))
        assertTrue(source.contains("setDynamicThumbnail(iv, bitmap)"))
    }

    @Test
    fun scanManifestAndPhotoContractReadsRunOffTheUiThread() {
        val refresh = section("private fun refreshHome()", "private fun renderHome(")
        assertTrue(refresh.contains("withContext(Dispatchers.IO)"))
        assertTrue(refresh.contains("Entries.recent(this@HomeActivity)"))
        assertTrue(refresh.contains("entry.thumbnailDescriptor()"))
        assertTrue(refresh.contains("Entries.statusLabel(this@HomeActivity, entry)"))

        val render = section("private fun renderHome(", "private fun setDynamicThumbnail(")
        assertFalse(render.contains("Entries.recent("))
        assertFalse(render.contains("thumbnailDescriptor()"))
        assertTrue(render.contains("item.titleLabel"))
        assertTrue(render.contains("item.statusLabel"))
        assertTrue(render.contains("item.thumbnail"))
    }

    @Test
    fun workerInvalidationsAreCoalescedAndResumePerformsTheAuthoritativeRefresh() {
        val observers = section(
            "// when background OCR / upload lands",
            "override fun onSaveInstanceState",
        )
        assertTrue(observers.contains("getWorkInfosLiveData(activeUniqueWorkQuery("))
        assertTrue(observers.contains("scheduleWorkerRefresh(contentChanged = true)"))
        assertTrue(observers.contains("scheduleWorkerRefresh(contentChanged = false)"))
        assertFalse(observers.contains("getWorkInfosForUniqueWorkLiveData"))
        assertFalse(observers.contains("refreshHome()"))

        val querySource =
            File("src/main/java/org/whl/bookcapture/WorkInfoQueries.kt").readText()
        assertTrue(querySource.contains("WorkInfo.State.ENQUEUED"))
        assertTrue(querySource.contains("WorkInfo.State.RUNNING"))
        assertTrue(querySource.contains("WorkInfo.State.BLOCKED"))
        assertFalse(querySource.contains("WorkInfo.State.SUCCEEDED"))
        assertFalse(querySource.contains("WorkInfo.State.FAILED"))
        assertFalse(querySource.contains("WorkInfo.State.CANCELLED"))

        val scheduler =
            section("private fun scheduleWorkerRefresh", "private fun cancelScheduledWorkerRefresh")
        assertTrue(scheduler.contains("workerRefreshJob?.isActive == true"))
        assertTrue(scheduler.contains("delay(WORK_REFRESH_COALESCE_MS)"))
        assertTrue(scheduler.contains("Lifecycle.State.STARTED"))

        val onResume = section("override fun onResume()", "override fun onStop()")
        assertTrue(onResume.contains("cancelScheduledWorkerRefresh()"))
        assertTrue(onResume.contains("showTab(activeTab)"))
    }

    private fun section(start: String, end: String): String =
        source.substringAfter(start).substringBefore(end)
}
