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
import com.google.android.material.dialog.MaterialAlertDialogBuilder
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
    private var discardDialog: AlertDialog? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityEntryDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.toolbar.setNavigationOnClickListener { finish() }
        binding.deepseekInstructions.setOnClickListener { showDeepseekInstructions() }
        val entryId = intent.getStringExtra(EXTRA_ID).orEmpty()
        for (workName in listOf(
            ProcessWorker.UNIQUE_WORK_NAME,
            ProcessWorker.BACKLOG_WORK_NAME,
            ProcessWorker.workNameForEntry(entryId),
        )) {
            WorkManager.getInstance(this)
                .getWorkInfosForUniqueWorkLiveData(workName)
                .observe(this) { render() }
        }
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
        discardDialog?.dismiss()
        discardDialog = null
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

        val ownership = cloudUploadOwnership(
            readCaptureCreator(this, entry.dir),
            Prefs.userId(this),
        )
        binding.ownershipNotice.visibility =
            if (!entry.uploaded && ownership != CloudUploadOwnership.ALLOWED) View.VISIBLE
            else View.GONE
        binding.ownershipNotice.text = when (ownership) {
            CloudUploadOwnership.ALLOWED -> ""
            CloudUploadOwnership.NEEDS_CLAIM -> getString(
                if (Auth.signedIn(this)) R.string.detail_local_claim_available
                else R.string.detail_local_sign_in_to_claim)
            CloudUploadOwnership.DIFFERENT_ACCOUNT ->
                getString(R.string.detail_different_account)
        }
        binding.claimCloud.visibility = if (
            !entry.uploaded && ownership == CloudUploadOwnership.NEEDS_CLAIM && Auth.signedIn(this)
        ) View.VISIBLE else View.GONE
        binding.claimCloud.setOnClickListener { showClaimConfirmation(entry.id) }

        // extracted fields, mono rows, only what exists
        binding.fields.removeAllViews()
        renderProvenance(entry)
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
        binding.deepseekInstructions.isEnabled = !entry.uploaded
        binding.deepseekInstructions.alpha = if (entry.uploaded) .55f else 1f
        binding.reprocessAvailability.visibility = if (entry.uploaded) View.VISIBLE else View.GONE
        binding.reprocessAvailability.text = if (entry.uploaded)
            getString(R.string.detail_reprocess_desktop) else ""

        // pages, decoded off the UI thread at thumbnail scale. Cancel any decode
        // still running from a previous render (a second onResume) so its views
        // can't append onto the freshly-cleared strip and duplicate pages.
        photoJob?.cancel()
        binding.photos.removeAllViews()
        val photos = entry.photos()
        photoJob = lifecycleScope.launch {
            for ((index, p) in photos.withIndex()) {
                val bmp = withContext(Dispatchers.IO) {
                    BitmapFactory.decodeFile(p.absolutePath,
                        BitmapFactory.Options().apply { inSampleSize = 4 })
                } ?: continue
                val iv = ImageView(this@EntryDetailActivity)
                val h = resources.getDimensionPixelSize(R.dimen.detail_thumbnail_height)
                val w = h * bmp.width / bmp.height.coerceAtLeast(1)
                iv.layoutParams = LinearLayout.LayoutParams(w, h).apply {
                    marginEnd = resources.getDimensionPixelSize(R.dimen.detail_thumbnail_gap)
                }
                iv.scaleType = ImageView.ScaleType.CENTER_CROP
                iv.contentDescription = getString(R.string.detail_photo_description, index + 1)
                iv.setImageBitmap(bmp)
                binding.photos.addView(iv)
            }
        }

        // discard is for what has not shipped; the cloud copy is the desktop's
        val isActiveCapture = Prefs.currentEntryId(this) == entry.id
        binding.discard.visibility =
            if (entry.uploaded || isActiveCapture) View.GONE else View.VISIBLE
        binding.discard.setOnClickListener { showDiscardConfirmation(entry) }
    }

    private fun showDiscardConfirmation(entry: Entries.Entry) {
        if (discardDialog?.isShowing == true) return
        discardDialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.detail_discard_title)
            .setMessage(R.string.detail_discard_message)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.detail_discard_confirm) { _, _ ->
                deleteEntry(entry.id)
            }
            .create()
            .also { dialog ->
                dialog.setOnShowListener {
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                        .setTextColor(getColor(R.color.whl_red))
                }
                dialog.setOnDismissListener { discardDialog = null }
                dialog.show()
            }
    }

    private fun showClaimConfirmation(entryId: String) {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.detail_claim_title)
            .setMessage(getString(R.string.detail_claim_message, Prefs.email(this)))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.detail_claim_confirm) { _, _ ->
                lifecycleScope.launch {
                    val result = withContext(Dispatchers.IO) {
                        claimCaptureForCloud(this@EntryDetailActivity, entryId)
                    }
                    when (result) {
                        ClaimCaptureResult.CLAIMED,
                        ClaimCaptureResult.ALREADY_OWNED -> {
                            Prefs.setLastUploadError(this@EntryDetailActivity, null)
                            UploadWorker.enqueue(this@EntryDetailActivity)
                            Toast.makeText(
                                this@EntryDetailActivity,
                                R.string.detail_claim_queued,
                                Toast.LENGTH_SHORT,
                            ).show()
                            render()
                        }
                        ClaimCaptureResult.DIFFERENT_ACCOUNT -> Toast.makeText(
                            this@EntryDetailActivity,
                            R.string.detail_different_account,
                            Toast.LENGTH_LONG,
                        ).show()
                        ClaimCaptureResult.SIGNED_OUT -> Toast.makeText(
                            this@EntryDetailActivity,
                            R.string.detail_local_sign_in_to_claim,
                            Toast.LENGTH_LONG,
                        ).show()
                        ClaimCaptureResult.MISSING -> finish()
                    }
                }
            }
            .show()
    }

    private fun deleteEntry(entryId: String) {
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                Entries.deleteLocalSafely(
                    this@EntryDetailActivity,
                    entryId,
                    allowUploaded = false,
                )
            }
            when (result) {
                Entries.DeleteResult.DELETED,
                Entries.DeleteResult.MISSING -> finish()
                Entries.DeleteResult.ACTIVE_CAPTURE -> {
                    Toast.makeText(
                        this@EntryDetailActivity,
                        R.string.detail_discard_active,
                        Toast.LENGTH_LONG,
                    ).show()
                    finish()
                }
                Entries.DeleteResult.ALREADY_UPLOADED -> {
                    Toast.makeText(
                        this@EntryDetailActivity,
                        R.string.detail_discard_uploaded,
                        Toast.LENGTH_LONG,
                    ).show()
                    render()
                }
                Entries.DeleteResult.DELETE_FAILED -> Toast.makeText(
                    this@EntryDetailActivity,
                    R.string.detail_delete_failed,
                    Toast.LENGTH_LONG,
                ).show()
            }
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
        dialogBinding.resubmit.isEnabled = !pending && !entry.uploaded
        dialogBinding.reprocessState.text = when {
            pending -> getString(R.string.detail_reprocessing)
            error.isNotEmpty() -> getString(R.string.detail_reprocess_error, error)
            else -> ""
        }
    }

    private fun resubmit(customInstructions: String) {
        val entry = Entries.find(this, intent.getStringExtra(EXTRA_ID) ?: "") ?: return
        if (entry.uploaded) {
            Toast.makeText(this, R.string.detail_reprocess_desktop, Toast.LENGTH_LONG).show()
            return
        }
        if (Prefs.deepseekKey(this).isEmpty()) {
            Toast.makeText(this, R.string.detail_need_deepseek, Toast.LENGTH_LONG).show()
            return
        }
        if (entry.ocrText().isEmpty()) {
            Toast.makeText(this, R.string.detail_need_ocr, Toast.LENGTH_LONG).show()
            return
        }
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                EntryOperationLocks.withLock(entry.id) {
                    val current = Entries.find(this@EntryDetailActivity, entry.id)
                        ?: return@withLock ReprocessRequestResult.MISSING
                    if (current.uploaded) return@withLock ReprocessRequestResult.UPLOADED
                    current.setCustomInstructions(customInstructions)
                    if (!current.requestReprocess()) {
                        return@withLock ReprocessRequestResult.LOCAL_WRITE_FAILED
                    }
                    Prefs.setLastProcError(this@EntryDetailActivity, null)
                    ProcessWorker.enqueue(this@EntryDetailActivity, current.id)
                    ReprocessRequestResult.QUEUED
                }
            }
            when (result) {
                ReprocessRequestResult.QUEUED -> {
                    render()
                    Toast.makeText(
                        this@EntryDetailActivity,
                        R.string.detail_reprocess_queued,
                        Toast.LENGTH_SHORT,
                    ).show()
                }
                ReprocessRequestResult.UPLOADED -> {
                    render()
                    Toast.makeText(
                        this@EntryDetailActivity,
                        R.string.detail_reprocess_desktop,
                        Toast.LENGTH_LONG,
                    ).show()
                }
                ReprocessRequestResult.MISSING -> finish()
                ReprocessRequestResult.LOCAL_WRITE_FAILED -> Toast.makeText(
                    this@EntryDetailActivity,
                    R.string.detail_reprocess_local_write_failed,
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private enum class ReprocessRequestResult {
        QUEUED, UPLOADED, MISSING, LOCAL_WRITE_FAILED
    }

    /**
     * Where this book came from, above the extracted bibliography — it describes
     * the scan, not the book. "From" is editable until the capture uploads:
     * after that the cloud row is insert-only, so a local edit would disagree
     * with what the desktop already holds.
     */
    private fun renderProvenance(entry: Entries.Entry) {
        val provenance = entry.provenance ?: return
        binding.fields.addView(
            fieldRow(getString(R.string.collections_field_name), provenance.collectionName))
        val fromRow = fieldRow(
            getString(R.string.detail_from),
            provenance.from.ifEmpty { getString(R.string.detail_from_none) })
        if (entry.uploaded) {
            fromRow.setOnClickListener {
                Toast.makeText(this, R.string.detail_from_locked, Toast.LENGTH_LONG).show()
            }
            fromRow.alpha = .7f
        } else {
            // Nothing else in this list is tappable, so the row needs to say so
            // — an unmarked one reads as another read-only field.
            (fromRow as LinearLayout).addView(TextView(this).apply {
                typeface = android.graphics.Typeface.MONOSPACE
                textSize = 10f
                setTextColor(getColor(R.color.whl_cyan))
                setText(R.string.detail_from_edit)
            })
            fromRow.setOnClickListener { editFrom(entry) }
        }
        fromRow.contentDescription = getString(
            R.string.detail_from_description,
            provenance.from.ifEmpty { getString(R.string.detail_from_none) })
        binding.fields.addView(fromRow)
    }

    private fun editFrom(entry: Entries.Entry) {
        val field = android.widget.EditText(this).apply {
            setText(entry.from)
            setSingleLine()
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_FLAG_CAP_WORDS
            setHint(R.string.detail_from_hint)
            setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO)
        }
        val frame = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
            addView(field)
            addView(TextView(this@EntryDetailActivity).apply {
                typeface = android.graphics.Typeface.MONOSPACE
                textSize = 11f
                setTextColor(getColor(R.color.whl_ink_dim))
                setPadding(0, 16, 0, 0)
                setText(R.string.detail_from_help)
            })
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.detail_from_title)
            .setView(frame)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.collections_save) { _, _ -> saveFrom(entry, field.text.toString()) }
            .show()
    }

    /**
     * Same discipline as [requestReprocess]: off the UI thread, under the
     * entry's lock, re-reading the entry inside it. `entry.uploaded` in
     * [renderProvenance] is a snapshot from render() time, which does not cover
     * an upload that starts while the dialog is open — without the lock this
     * can interleave with UploadWorker's read-modify-write of the same
     * manifest, leaving the phone showing an origin the cloud never received.
     */
    private fun saveFrom(entry: Entries.Entry, from: String) {
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                EntryOperationLocks.withLock(entry.id) {
                    val current = Entries.find(this@EntryDetailActivity, entry.id)
                        ?: return@withLock FromSaveResult.MISSING
                    if (current.uploaded) return@withLock FromSaveResult.UPLOADED
                    if (overrideEntryFrom(current.dir, from)) FromSaveResult.SAVED
                    else FromSaveResult.WRITE_FAILED
                }
            }
            // Each outcome needs its own words. "Free storage and try again" is
            // actively wrong advice for a book that uploaded while the dialog
            // was open — retrying can never succeed. MISSING says nothing at
            // all: render() finishes the Activity, and a Toast would outlive it.
            when (result) {
                FromSaveResult.SAVED, FromSaveResult.MISSING -> Unit
                FromSaveResult.UPLOADED -> Toast.makeText(
                    this@EntryDetailActivity,
                    R.string.detail_from_locked,
                    Toast.LENGTH_LONG,
                ).show()
                FromSaveResult.WRITE_FAILED -> Toast.makeText(
                    this@EntryDetailActivity,
                    R.string.detail_from_failed,
                    Toast.LENGTH_LONG,
                ).show()
            }
            render()
        }
    }

    private enum class FromSaveResult { SAVED, UPLOADED, MISSING, WRITE_FAILED }

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
