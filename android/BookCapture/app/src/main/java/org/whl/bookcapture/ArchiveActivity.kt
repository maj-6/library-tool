package org.whl.bookcapture

import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.view.LayoutInflater
import android.widget.ImageView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityArchiveBinding
import org.whl.bookcapture.databinding.ItemArchiveBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Reading the retention archive.
 *
 * Retention displaces captures out of the browsing list rather than deleting
 * them (see [CaptureArchive]); without somewhere to look at the result, that
 * guarantee is invisible and untestable by the person relying on it. This
 * screen is deliberately read-only. The archive is not a sync source and no
 * worker enumerates it, so offering edits here would produce changes that
 * silently never leave the phone.
 */
class ArchiveActivity : AppCompatActivity() {

    private lateinit var binding: ActivityArchiveBinding
    private var thumbJob: Job? = null

    private data class ArchiveThumbnail(
        val entry: Entries.Entry,
        val image: ImageView,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityArchiveBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.toolbar.setNavigationOnClickListener { finish() }
        RemoteUiCatalog.apply(binding.root)
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onPause() {
        thumbJob?.cancel()
        thumbJob = null
        super.onPause()
    }

    private fun render() {
        lifecycleScope.launch {
            val entries = withContext(Dispatchers.IO) { Entries.archived(this@ArchiveActivity) }
            val bytes = withContext(Dispatchers.IO) {
                CaptureArchive.archiveBytes(this@ArchiveActivity)
            }
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
            binding.archiveSummary.text = getString(
                R.string.archive_summary,
                entries.size,
                formatByteSize(bytes),
            )
            renderRows(entries)
        }
    }

    private fun renderRows(entries: List<Entries.Entry>) {
        val list = binding.archiveList
        list.removeAllViews()
        if (entries.isEmpty()) {
            val notice = android.widget.TextView(this).apply {
                typeface = android.graphics.Typeface.MONOSPACE
                textSize = 13f
                setTextColor(getColor(R.color.whl_ink_dim))
                setPadding(28, 40, 28, 28)
                text = getString(R.string.archive_empty)
            }
            RemoteUiCatalog.apply(notice)
            list.addView(notice)
            return
        }
        val inflater = LayoutInflater.from(this)
        val thumbnails = mutableListOf<ArchiveThumbnail>()
        val dateFormat = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        for (entry in entries) {
            val row = ItemArchiveBinding.inflate(inflater, list, false)
            row.archiveEntryTitle.text = Entries.titleLabel(this, entry)
            val stamp = CaptureArchive.readStamp(entry.dir)
            row.archiveEntrySubtitle.text = archiveRowSubtitle(
                archivedOn = stamp?.archivedAt?.let { dateFormat.format(Date(it)) }.orEmpty(),
                photoCount = entry.photoCount,
                recordOnly = stamp?.photosRetained == false,
                statusLabel = Entries.statusLabel(this, entry),
            )
            val open = {
                startActivity(
                    Intent(this, EntryDetailActivity::class.java)
                        .putExtra(EntryDetailActivity.EXTRA_ID, entry.id),
                )
            }
            row.root.setOnClickListener { open() }
            row.archiveOpen.setOnClickListener { open() }
            RemoteUiCatalog.apply(row.root)
            list.addView(row.root)
            if (entry.photoCount > 0) thumbnails += ArchiveThumbnail(entry, row.archiveThumb)
        }
        startThumbnailLoading(thumbnails)
    }

    private fun startThumbnailLoading(requests: List<ArchiveThumbnail>) {
        thumbJob?.cancel()
        if (requests.isEmpty()) return
        thumbJob = lifecycleScope.launch(Dispatchers.IO) {
            for (request in requests) {
                val loadContext = currentCoroutineContext()
                loadContext.ensureActive()
                val descriptor = request.entry.thumbnailDescriptor() ?: continue
                val decoded: Bitmap = decodeSampledOriented(
                    descriptor.displayFile,
                    maxWidth = THUMB_PX,
                    maxHeight = THUMB_PX,
                ) ?: continue
                loadContext.ensureActive()
                withContext(Dispatchers.Main) {
                    if (!request.image.isAttachedToWindow ||
                        !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
                    ) return@withContext
                    request.image.setImageBitmap(decoded)
                }
            }
        }
    }

    private companion object {
        const val THUMB_PX = 240
    }
}
