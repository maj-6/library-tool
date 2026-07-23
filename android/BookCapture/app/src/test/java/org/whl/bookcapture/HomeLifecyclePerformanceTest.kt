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
        assertTrue(stopLoading.contains("cancelCollectionBarLoading()"))
        assertTrue(stopLoading.contains("cancelCollectionListLoading()"))
        assertTrue(stopLoading.contains("cancelInspectLoading()"))
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
        assertTrue(source.contains("setDynamicThumbnail(request.image, readyBitmap)"))
    }

    @Test
    fun blockingTabSnapshotsAreSerializedAndCancellationChecked() {
        val loader = section(
            "private suspend fun <T> loadHomeSnapshot(",
            "private fun refreshCollectionBar()",
        )
        assertTrue(loader.contains("withContext(Dispatchers.IO)"))
        assertTrue(loader.contains("SNAPSHOT_LOAD_MUTEX.withLock"))
        assertTrue(loader.contains("loadContext.ensureActive()"))
        assertTrue(source.contains("val SNAPSHOT_LOAD_MUTEX = Mutex()"))
        assertTrue(source.contains("val THUMBNAIL_LOAD_MUTEX = Mutex()"))
    }

    @Test
    fun scanManifestReadsAreSerializedAndOnlyVisiblePhotosAreResolved() {
        val refresh = section("private fun refreshHome()", "private fun resetScanPagination()")
        assertTrue(refresh.contains("loadHomeSnapshot"))
        assertTrue(refresh.contains("Entries.recent(this@HomeActivity)"))
        assertFalse(refresh.contains("thumbnailDescriptor()"))
        assertTrue(refresh.contains("Entries.statusLabel(this@HomeActivity, entry)"))
        assertTrue(refresh.contains("loadContext.ensureActive()"))
        assertTrue(refresh.contains("resetScanPagination()"))
        assertTrue(refresh.contains("renderHome(snapshot)"))
        assertTrue(
            refresh.indexOf("resetScanPagination()") <
                refresh.indexOf("renderHome(snapshot)"),
        )

        val render = section("private fun renderHome(", "private fun startThumbnailLoading(")
        assertFalse(render.contains("Entries.recent("))
        assertFalse(render.contains("thumbnailDescriptor()"))
        assertTrue(render.contains("item.titleLabel"))
        assertTrue(render.contains("item.statusLabel"))
        assertTrue(render.contains("ThumbnailRequest("))
        assertTrue(render.contains("startThumbnailLoading(thumbs, HomeTab.SCANS)"))
        assertTrue(render.contains("scanGroupPage("))
        assertTrue(render.contains("HOME_SCAN_PAGE_SIZE"))
        assertTrue(render.contains("for (e in page.items)"))
        assertFalse(render.contains("for (e in group.items)"))
        assertTrue(render.contains("R.plurals.home_group_show_newer"))
        assertTrue(render.contains("R.plurals.home_group_show_older"))
        assertTrue(render.contains("R.string.home_group_page_status"))
        assertTrue(render.contains("AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS"))
        assertTrue(render.contains("updateCollectionBar = false"))
        assertTrue(render.contains("resetThumbnailLoading()"))

        val thumbnails =
            section("private fun startThumbnailLoading(", "private fun setDynamicThumbnail(")
        assertTrue(thumbnails.contains("Dispatchers.IO"))
        assertTrue(thumbnails.contains("val loadContext = currentCoroutineContext()"))
        assertTrue(thumbnails.contains("loadContext.ensureActive()"))
        assertTrue(thumbnails.contains("THUMBNAIL_LOAD_MUTEX.withLock"))
        assertTrue(thumbnails.contains("request.entry.thumbnailDescriptor()"))
        assertTrue(thumbnails.contains("!request.image.isAttachedToWindow"))
        assertTrue(thumbnails.contains("decodedBitmap?.takeIf { !it.isRecycled }?.recycle()"))
    }

    @Test
    fun inspectInventoryIsSerializedAndOnlyVisiblePhotosAreResolved() {
        val refresh = section("private fun refreshInspect()", "private fun renderInspect(")
        assertTrue(refresh.contains("loadHomeSnapshot"))
        assertTrue(refresh.contains("CollectionInventory.items(this@HomeActivity)"))
        assertTrue(refresh.contains("Entries.statusLabel(this@HomeActivity, it)"))
        assertTrue(refresh.contains("loadContext.ensureActive()"))
        assertFalse(refresh.contains("thumbnailDescriptor()"))

        val render = section("private fun renderInspect(", "override fun onDestroy()")
        assertFalse(render.contains("CollectionInventory.items("))
        assertFalse(render.contains("Entries.statusLabel("))
        assertFalse(render.contains("thumbnailDescriptor()"))
        assertTrue(render.contains("snapshot.statusLabel"))
        assertTrue(render.contains("item.current?.let"))
        assertTrue(render.contains("ThumbnailRequest("))
        assertTrue(render.contains("startThumbnailLoading(thumbnails, HomeTab.INSPECT)"))
        assertTrue(render.contains("selectedItems.take(inspectVisibleBookLimit)"))
        assertTrue(render.contains("INSPECT_BOOK_PAGE_SIZE"))
        assertTrue(render.contains("R.string.inspect_show_more"))
    }

    @Test
    fun collectionBookCountsRunOffTheUiThread() {
        val refresh = section("private fun refreshCollections()", "private fun renderCollections(")
        assertTrue(refresh.contains("loadHomeSnapshot"))
        assertTrue(refresh.contains("CollectionInventory.items(this@HomeActivity)"))
        assertTrue(refresh.contains("loadContext.ensureActive()"))

        val render = section("private fun renderCollections(", "private fun emptyNotice")
        assertFalse(render.contains("CollectionInventory.items("))
        assertTrue(render.contains("snapshot.bookCounts"))
    }

    @Test
    fun collectionBarRefreshRunsOffTheUiThreadWithoutRebuildingTheScanList() {
        val refresh = section("private fun refreshCollectionBar()", "private fun renderCollectionBar(")
        assertTrue(refresh.contains("loadHomeSnapshot"))
        assertTrue(refresh.contains("Collections.allRecords(this@HomeActivity)"))
        assertTrue(refresh.contains("loadContext.ensureActive()"))
        assertTrue(refresh.contains("activeTab != HomeTab.SCANS"))

        val scheduler =
            section("private fun scheduleWorkerRefresh", "private fun cancelScheduledWorkerRefresh")
        assertTrue(scheduler.contains("HomeTab.SCANS -> refreshCollectionBar()"))
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
