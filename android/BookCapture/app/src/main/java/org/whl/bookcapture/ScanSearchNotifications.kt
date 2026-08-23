package org.whl.bookcapture

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import java.util.UUID

/** System notification counterpart to the durable error shown in the queue tab. */
internal object ScanSearchNotifications {
    const val CHANNEL_ID = "physical-scan-errors"

    @SuppressLint("MissingPermission")
    fun failure(ctx: Context, itemId: String, errorMessage: String): Boolean {
        val id = itemId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(id)) return false
        return show(
            ctx,
            id,
            boundedScanSearchErrorMessage(errorMessage),
            scanSearchNotificationId(id),
        )
    }

    @SuppressLint("MissingPermission")
    fun syncFailure(ctx: Context, itemId: String): Boolean {
        val id = itemId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(id)) return false
        return show(
            ctx,
            id,
            ctx.getString(R.string.scan_queue_notification_sync_failed),
            scanSearchSyncNotificationId(id),
        )
    }

    fun clearSyncFailure(ctx: Context, itemId: String) {
        val id = itemId.trim().lowercase()
        if (!SAFE_CAPTURE_SYNC_ID.matches(id)) return
        NotificationManagerCompat.from(ctx).cancel(scanSearchSyncNotificationId(id))
    }

    @SuppressLint("MissingPermission")
    private fun show(
        ctx: Context,
        id: String,
        detail: String,
        notificationId: Int,
    ): Boolean {
        ensureChannel(ctx)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return false
        val manager = NotificationManagerCompat.from(ctx)
        if (!manager.areNotificationsEnabled()) return false

        val intent = Intent(ctx, HomeActivity::class.java)
            .putExtra(HOME_EXTRA_OPEN_SCAN_QUEUE, true)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pendingIntent = PendingIntent.getActivity(
            ctx,
            notificationId,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_scan_status)
            .setContentTitle(ctx.getString(R.string.scan_queue_notification_title))
            .setContentText(detail)
            .setStyle(NotificationCompat.BigTextStyle().bigText(detail))
            .setCategory(NotificationCompat.CATEGORY_ERROR)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .build()
        manager.notify(notificationId, notification)
        return true
    }

    private fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = ctx.getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                ctx.getString(R.string.scan_queue_notification_channel),
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = ctx.getString(R.string.scan_queue_notification_channel_description)
            },
        )
    }
}

internal fun scanSearchNotificationId(itemId: String): Int = try {
    (UUID.fromString(itemId).hashCode() xor 0x5343414e) and Int.MAX_VALUE
} catch (_: IllegalArgumentException) {
    0x5343414e
}

internal fun scanSearchSyncNotificationId(itemId: String): Int = try {
    (UUID.fromString(itemId).hashCode() xor 0x53594e43) and Int.MAX_VALUE
} catch (_: IllegalArgumentException) {
    0x53594e43
}
