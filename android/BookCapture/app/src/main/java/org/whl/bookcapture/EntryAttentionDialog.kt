package org.whl.bookcapture

import android.view.View
import android.widget.CheckBox
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Mirror the desktop Q interaction. Opening the dialog immediately establishes
 * a durable attention mark; dismissing it keeps the plain mark, while Save adds
 * the optional reason and/or shared-review request. Nothing is sent until the
 * user presses Sync captures.
 */
internal fun showEntryAttentionDialog(
    activity: AppCompatActivity,
    entryId: String,
    onChanged: () -> Unit = {},
) {
    val entry = Entries.find(activity, entryId) ?: return
    val local = entry.captureReview
    val reason = local?.attentionReason.orEmpty()
    val needsReview = local?.needsReview == true

    activity.lifecycleScope.launch {
        if (local?.needsAttention != true) {
            val marked = withContext(Dispatchers.IO) {
                Entries.setCaptureReview(
                    activity,
                    entryId,
                    needsAttention = true,
                    needsReview = needsReview,
                    reason = reason,
                )
            }
            if (!marked) {
                Toast.makeText(activity, R.string.attention_save_failed, Toast.LENGTH_LONG).show()
                return@launch
            }
            onChanged()
        }

        val view = activity.layoutInflater.inflate(R.layout.dialog_entry_attention, null)
        val reasonInput = view.findViewById<EditText>(R.id.attentionReason).apply {
            setText(reason)
            setSelection(text.length)
        }
        val reviewInput = view.findViewById<CheckBox>(R.id.attentionNeedsReview).apply {
            isChecked = needsReview
        }
        val title = Entries.titleLabel(activity, entry).take(80)
        val dialog = AlertDialog.Builder(activity)
            .setTitle(title.ifBlank { activity.getString(R.string.attention_title) })
            .setView(view)
            .setNeutralButton(R.string.attention_clear, null)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.attention_save, null)
            .create()
        dialog.setOnShowListener {
            val save = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            val clear = dialog.getButton(AlertDialog.BUTTON_NEUTRAL)
            save.setOnClickListener {
                save.isEnabled = false
                clear.isEnabled = false
                activity.lifecycleScope.launch {
                    val saved = withContext(Dispatchers.IO) {
                        Entries.setCaptureReview(
                            activity,
                            entryId,
                            needsAttention = true,
                            needsReview = reviewInput.isChecked,
                            reason = reasonInput.text?.toString().orEmpty().trim(),
                        )
                    }
                    if (saved) {
                        onChanged()
                        dialog.dismiss()
                    } else {
                        save.isEnabled = true
                        clear.isEnabled = true
                        Toast.makeText(
                            activity,
                            R.string.attention_save_failed,
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                }
            }
            clear.setOnClickListener {
                save.isEnabled = false
                clear.isEnabled = false
                activity.lifecycleScope.launch {
                    val cleared = withContext(Dispatchers.IO) {
                        Entries.setCaptureReview(
                            activity,
                            entryId,
                            needsAttention = false,
                            needsReview = false,
                            reason = "",
                        )
                    }
                    if (cleared) {
                        onChanged()
                        dialog.dismiss()
                    } else {
                        save.isEnabled = true
                        clear.isEnabled = true
                        Toast.makeText(
                            activity,
                            R.string.attention_save_failed,
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                }
            }
        }
        dialog.show()
        dialog.window?.decorView?.importantForAccessibility =
            View.IMPORTANT_FOR_ACCESSIBILITY_YES
        RemoteUiCatalog.apply(view)
        RemoteUiCatalog.apply(dialog)
    }
}
