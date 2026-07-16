package org.whl.bookcapture

import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.View
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityEntryDetailBinding
import org.whl.bookcapture.databinding.DialogDeepseekInstructionsBinding

/** One recent scan, in full: extracted record, pages, OCR text, status. */
class EntryDetailActivity : AppCompatActivity() {

    companion object { const val EXTRA_ID = "entry_id" }

    private lateinit var binding: ActivityEntryDetailBinding
    private var photoJob: Job? = null
    private var instructionsDialog: AlertDialog? = null
    private var instructionsDialogBinding: DialogDeepseekInstructionsBinding? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityEntryDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.deepseekInstructions.setOnClickListener { showDeepseekInstructions() }
        WorkManager.getInstance(this)
            .getWorkInfosForUniqueWorkLiveData("capture-process")
            .observe(this) { render() }
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onDestroy() {
        photoJob?.cancel()
        instructionsDialog?.dismiss()
        instructionsDialog = null
        instructionsDialogBinding = null
        super.onDestroy()
    }

    private fun render() {
        val entry = Entries.find(this, intent.getStringExtra(EXTRA_ID) ?: "") ?: return finish()

        binding.title.text = Entries.titleLabel(this, entry)
        binding.subline.text = listOf(entry.author, entry.year)
            .filter { it.isNotEmpty() }.joinToString(" · ")
        binding.stateLine.text = listOf(
            Entries.statusLabel(this, entry),
            resources.getQuantityString(
                R.plurals.capture_count, entry.photoCount, entry.photoCount),
            android.text.format.DateFormat.format("yyyy-MM-dd HH:mm", entry.createdAt),
        ).joinToString("  ·  ")

        // extracted fields, mono rows, only what exists
        binding.fields.removeAllViews()
        entry.meta?.let { meta ->
            for (k in Pipeline.FIELDS) {
                val v = meta.optString(k).trim()
                if (v.isEmpty()) continue
                binding.fields.addView(fieldRow(k, v))
            }
            meta.optJSONObject("extra")?.let { extra ->
                for (k in extra.keys()) {
                    val v = extra.optString(k).trim()
                    if (v.isNotEmpty()) binding.fields.addView(fieldRow(k, v))
                }
            }
        }

        val ocr = entry.ocrText()
        binding.ocrText.text = ocr.ifEmpty { getString(R.string.detail_no_ocr) }

        renderDeepseekDialog(entry)

        // pages, decoded off the UI thread at thumbnail scale. Cancel any decode
        // still running from a previous render (a second onResume) so its views
        // can't append onto the freshly-cleared strip and duplicate pages.
        photoJob?.cancel()
        binding.photos.removeAllViews()
        val photos = entry.photos()
        photoJob = lifecycleScope.launch {
            for (p in photos) {
                val bmp = withContext(Dispatchers.IO) {
                    BitmapFactory.decodeFile(p.absolutePath,
                        BitmapFactory.Options().apply { inSampleSize = 4 })
                } ?: continue
                val iv = ImageView(this@EntryDetailActivity)
                val h = 320
                val w = h * bmp.width / bmp.height.coerceAtLeast(1)
                iv.layoutParams = LinearLayout.LayoutParams(w, h).apply { marginEnd = 10 }
                iv.scaleType = ImageView.ScaleType.CENTER_CROP
                iv.setImageBitmap(bmp)
                binding.photos.addView(iv)
            }
        }

        // discard is for what has not shipped; the cloud copy is the desktop's
        binding.discard.visibility = if (entry.uploaded) View.GONE else View.VISIBLE
        binding.discard.setOnClickListener {
            Entries.deleteLocal(this, entry)
            finish()
        }
    }

    private fun showDeepseekInstructions() {
        if (instructionsDialog?.isShowing == true) return
        val entry = Entries.find(this, intent.getStringExtra(EXTRA_ID) ?: "") ?: return
        val dialogBinding = DialogDeepseekInstructionsBinding.inflate(layoutInflater)
        dialogBinding.customInstructions.setText(entry.customInstructions())
        dialogBinding.customInstructions.setSelection(dialogBinding.customInstructions.length())

        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.detail_deepseek_instructions)
            .setView(dialogBinding.root)
            .setNegativeButton(R.string.close, null)
            .create()
        instructionsDialog = dialog
        instructionsDialogBinding = dialogBinding
        dialogBinding.resubmit.setOnClickListener {
            resubmit(dialogBinding.customInstructions.text?.toString().orEmpty())
        }
        dialog.setOnDismissListener {
            instructionsDialog = null
            instructionsDialogBinding = null
        }
        renderDeepseekDialog(entry)
        dialog.show()
    }

    private fun renderDeepseekDialog(entry: Entries.Entry) {
        val dialogBinding = instructionsDialogBinding ?: return
        val pending = entry.reprocessPending()
        val error = entry.reprocessError()
        dialogBinding.resubmit.isEnabled = !pending
        dialogBinding.reprocessState.text = when {
            pending -> getString(R.string.detail_reprocessing)
            error.isNotEmpty() -> getString(R.string.detail_reprocess_error, error)
            else -> ""
        }
    }

    private fun resubmit(customInstructions: String) {
        val entry = Entries.find(this, intent.getStringExtra(EXTRA_ID) ?: "") ?: return
        if (Prefs.deepseekKey(this).isEmpty()) {
            Toast.makeText(this, R.string.detail_need_deepseek, Toast.LENGTH_LONG).show()
            return
        }
        if (entry.ocrText().isEmpty()) {
            Toast.makeText(this, R.string.detail_need_ocr, Toast.LENGTH_LONG).show()
            return
        }
        entry.setCustomInstructions(customInstructions)
        entry.requestReprocess()
        Prefs.setLastProcError(this, null)
        ProcessWorker.enqueue(this, entry.id)
        render()
        Toast.makeText(this, R.string.detail_reprocess_queued, Toast.LENGTH_SHORT).show()
    }

    private fun fieldRow(label: String, value: String): View {
        val row = LinearLayout(this)
        row.orientation = LinearLayout.HORIZONTAL
        row.setPadding(0, 3, 0, 3)
        val l = TextView(this)
        l.typeface = android.graphics.Typeface.MONOSPACE
        l.textSize = 11f
        l.setTextColor(getColor(R.color.whl_ink_dim))
        l.text = label.uppercase().padEnd(10)
        val v = TextView(this)
        v.typeface = android.graphics.Typeface.MONOSPACE
        v.textSize = 12f
        v.setTextColor(getColor(R.color.whl_ink))
        v.text = value
        row.addView(l, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        row.addView(v, LinearLayout.LayoutParams(
            0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        return row
    }
}
