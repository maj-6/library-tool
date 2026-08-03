package org.whl.bookcapture

import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** Actions shared by every long-pressable book card. */
internal fun showEntryActionDialog(
    activity: AppCompatActivity,
    entryId: String,
    onChanged: () -> Unit = {},
) {
    val entry = Entries.find(activity, entryId)
    if (entry == null) {
        Toast.makeText(activity, R.string.entry_action_missing, Toast.LENGTH_SHORT).show()
        onChanged()
        return
    }
    val actions = EntryAction.entries
    val dialog = MaterialAlertDialogBuilder(activity)
        .setTitle(Entries.titleLabel(activity, entry).take(80))
        .setItems(actions.map { RemoteUiCatalog.text(activity, it.label) }.toTypedArray()) { _, index ->
            when (actions[index]) {
                EntryAction.REMARK -> showEntryAttentionDialog(activity, entryId, onChanged)
                EntryAction.REPROCESS -> reprocessEntry(activity, entryId, onChanged)
                EntryAction.DELETE -> showEntryDeleteConfirmation(activity, entryId, onChanged)
            }
        }
        .setNegativeButton(android.R.string.cancel, null)
        .create()
    dialog.show()
    RemoteUiCatalog.apply(dialog)
}

private enum class EntryAction(val label: Int) {
    REMARK(R.string.entry_action_remark),
    REPROCESS(R.string.entry_action_reprocess),
    DELETE(R.string.entry_action_delete),
}

private enum class ReprocessRequestResult {
    QUEUED,
    ALREADY_QUEUED,
    ACTIVE_CAPTURE,
    NO_PHOTOS,
    NEED_DEEPSEEK,
    NEED_MISTRAL,
    MISSING,
    LOCAL_WRITE_FAILED,
}

private fun reprocessEntry(
    activity: AppCompatActivity,
    entryId: String,
    onChanged: () -> Unit,
) {
    activity.lifecycleScope.launch {
        val result = withContext(Dispatchers.IO) {
            EntryOperationLocks.withLock(entryId) {
                val current = Entries.find(activity, entryId)
                    ?: return@withLock ReprocessRequestResult.MISSING
                when {
                    Prefs.currentEntryId(activity) == entryId || !current.sealed ->
                        ReprocessRequestResult.ACTIVE_CAPTURE
                    current.photoCount == 0 -> ReprocessRequestResult.NO_PHOTOS
                    current.reprocessPending() -> ReprocessRequestResult.ALREADY_QUEUED
                    Prefs.deepseekKey(activity).isEmpty() -> ReprocessRequestResult.NEED_DEEPSEEK
                    needsFreshOcr(current) && Prefs.mistralKey(activity).isEmpty() ->
                        ReprocessRequestResult.NEED_MISTRAL
                    !current.requestReprocess() -> ReprocessRequestResult.LOCAL_WRITE_FAILED
                    else -> ReprocessRequestResult.QUEUED
                }
            }
        }
        when (result) {
            ReprocessRequestResult.QUEUED -> {
                Prefs.setLastProcError(activity, null)
                ProcessWorker.enqueueForcedRetry(activity, listOf(entryId))
                Toast.makeText(activity, R.string.detail_reprocess_queued, Toast.LENGTH_SHORT).show()
                onChanged()
            }
            ReprocessRequestResult.ALREADY_QUEUED -> {
                // Repair an orphaned durable marker if a previous process died
                // between writing it and asking WorkManager to run it.
                activity.lifecycleScope.launch(Dispatchers.IO) {
                    ProcessWorker.resumePendingForcedRetries(activity)
                }
                Toast.makeText(
                    activity,
                    R.string.detail_reprocess_already_queued,
                    Toast.LENGTH_SHORT,
                ).show()
            }
            ReprocessRequestResult.ACTIVE_CAPTURE -> Toast.makeText(
                activity,
                R.string.detail_reprocess_active,
                Toast.LENGTH_LONG,
            ).show()
            ReprocessRequestResult.NO_PHOTOS -> Toast.makeText(
                activity,
                R.string.detail_reprocess_no_photos,
                Toast.LENGTH_LONG,
            ).show()
            ReprocessRequestResult.NEED_DEEPSEEK -> Toast.makeText(
                activity,
                R.string.detail_need_deepseek,
                Toast.LENGTH_LONG,
            ).show()
            ReprocessRequestResult.NEED_MISTRAL -> Toast.makeText(
                activity,
                R.string.detail_need_mistral,
                Toast.LENGTH_LONG,
            ).show()
            ReprocessRequestResult.MISSING -> {
                Toast.makeText(activity, R.string.entry_action_missing, Toast.LENGTH_SHORT).show()
                onChanged()
            }
            ReprocessRequestResult.LOCAL_WRITE_FAILED -> Toast.makeText(
                activity,
                R.string.detail_reprocess_local_write_failed,
                Toast.LENGTH_LONG,
            ).show()
        }
    }
}

private fun needsFreshOcr(entry: Entries.Entry): Boolean = entry.photos().any { photo ->
    File(entry.dir, photo.name + ".txt").let { sidecar ->
        !sidecar.isFile || sidecar.length() == 0L
    }
}

private fun showEntryDeleteConfirmation(
    activity: AppCompatActivity,
    entryId: String,
    onChanged: () -> Unit,
) {
    val entry = Entries.findIncludingArchive(activity, entryId)
    if (entry == null) {
        Toast.makeText(activity, R.string.entry_action_missing, Toast.LENGTH_SHORT).show()
        onChanged()
        return
    }
    val dialog = MaterialAlertDialogBuilder(activity)
        .setTitle(RemoteUiCatalog.text(activity, R.string.detail_discard_title))
        .setMessage(
            RemoteUiCatalog.text(activity, R.string.entry_delete_message),
        )
        .setNegativeButton(android.R.string.cancel, null)
        .setPositiveButton(
            RemoteUiCatalog.text(activity, R.string.entry_action_delete_confirm),
        ) { _, _ ->
            deleteEntry(activity, entryId, onChanged)
        }
        .create()
    dialog.setOnShowListener {
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(activity.getColor(R.color.whl_red))
    }
    dialog.show()
    RemoteUiCatalog.apply(dialog)
}

private fun deleteEntry(
    activity: AppCompatActivity,
    entryId: String,
    onChanged: () -> Unit,
) {
    activity.lifecycleScope.launch {
        val result = withContext(Dispatchers.IO) {
            Entries.deleteLocalSafely(activity, entryId, allowUploaded = true)
        }
        when (result) {
            Entries.DeleteResult.DELETED -> {
                Toast.makeText(activity, R.string.entry_delete_complete, Toast.LENGTH_SHORT).show()
                onChanged()
            }
            Entries.DeleteResult.MISSING -> onChanged()
            Entries.DeleteResult.ACTIVE_CAPTURE -> Toast.makeText(
                activity,
                R.string.detail_discard_active,
                Toast.LENGTH_LONG,
            ).show()
            Entries.DeleteResult.ALREADY_UPLOADED -> {
                Toast.makeText(activity, R.string.detail_discard_uploaded, Toast.LENGTH_LONG).show()
                onChanged()
            }
            Entries.DeleteResult.DELETE_FAILED -> Toast.makeText(
                activity,
                R.string.detail_delete_failed,
                Toast.LENGTH_LONG,
            ).show()
        }
    }
}
