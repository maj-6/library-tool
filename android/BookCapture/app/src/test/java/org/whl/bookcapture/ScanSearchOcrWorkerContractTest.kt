package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ScanSearchOcrWorkerContractTest {
    private val firstId = "11111111-1111-4111-8111-111111111111"
    private val secondId = "22222222-2222-4222-8222-222222222222"

    @Test
    fun workerUsesDurablePrivateStagingPinnedMistralAndBoundedRetries() {
        val worker = source("ScanSearchOcrWorker")
        val home = source("HomeActivity")

        assertTrue(worker.contains("ctx.noBackupFilesDir"))
        assertTrue(worker.contains("\"${'$'}id.jpg\""))
        assertTrue(worker.contains("Pipeline.coverOcr(staged, mistralKey)"))
        assertEquals("mistral-ocr-4-1", Pipeline.COVER_OCR_MODEL)
        assertTrue(worker.contains("ScanSearchPhotoRole.COVER"))
        assertTrue(worker.contains("NetworkType.CONNECTED"))
        assertTrue(worker.contains(".addTag(WORK_TAG)"))
        assertTrue(worker.contains(").result.get()"))
        assertTrue(worker.contains("runAttemptCount < MAX_TRANSIENT_RETRIES"))
        assertTrue(worker.contains("error !is Pipeline.PermanentError"))
        assertTrue(worker.contains("catch (_: Pipeline.PermanentError)"))
        assertTrue(worker.contains("ScanSearchQueue.completeProcessing("))
        assertTrue(worker.contains("ScanSearchQueue.failProcessing("))
        assertTrue(worker.contains("ScanSearchQueueSyncWorker.enqueue(ctx)"))
        assertTrue(worker.contains("fun resumePending(ctx: Context)"))
        assertTrue(worker.contains("fun abandonOwner(ctx: Context, ownerId: String)"))
        assertTrue(worker.contains("ScanSearchQueue.failProcessingForWorker("))
        assertTrue(worker.contains("scan_queue_notification_key_missing"))
        assertTrue(worker.contains("cleanupStagedImage(ctx, id)"))
        assertTrue(worker.contains("if (!source.delete())"))
        assertTrue(worker.contains("destination.delete()"))
        assertFalse(home.contains("scan_queue_requires_mistral_key"))
        assertFalse(worker.contains("putString(\"path\""))
        assertFalse(worker.contains("putString(\"mistral"))
    }

    @Test
    fun failureNotificationIsStablePermissionAwareAndOpensQueueTab() {
        val notifications = source("ScanSearchNotifications")
        val manifest = File("src/main/AndroidManifest.xml").readText()
        val strings = File("src/main/res/values/strings.xml").readText()

        assertEquals(scanSearchNotificationId(firstId), scanSearchNotificationId(firstId))
        assertNotEquals(scanSearchNotificationId(firstId), scanSearchNotificationId(secondId))
        assertNotEquals(scanSearchNotificationId(firstId), scanSearchSyncNotificationId(firstId))
        assertEquals(
            scanSearchSyncNotificationId(firstId),
            scanSearchSyncNotificationId(firstId),
        )
        assertTrue(manifest.contains("android.permission.POST_NOTIFICATIONS"))
        assertTrue(notifications.contains("Manifest.permission.POST_NOTIFICATIONS"))
        assertTrue(notifications.contains("HOME_EXTRA_OPEN_SCAN_QUEUE"))
        assertTrue(notifications.contains("NotificationChannel("))
        assertTrue(strings.contains("scan_queue_notification_channel"))
        assertTrue(strings.contains("scan_queue_notification_title"))
        assertTrue(strings.contains("scan_queue_notification_sync_failed"))

        val syncWorker = source("ScanSearchQueueSyncWorker")
        assertTrue(syncWorker.contains("ScanSearchNotifications.syncFailure(ctx, it)"))
        assertTrue(syncWorker.contains(
            "notificationItemIds.forEach { ScanSearchNotifications.clearSyncFailure(ctx, it) }",
        ))
    }

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()
}
