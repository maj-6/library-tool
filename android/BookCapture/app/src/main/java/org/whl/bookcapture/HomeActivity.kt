package org.whl.bookcapture

import android.content.Intent
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityHomeBinding

/**
 * The landing screen. Launching the app opens HERE, not the camera: a list of
 * recent scans — each a page thumbnail, the extracted title / author / year (or
 * "Processing…" until the pipeline catches up), and its status (pending upload,
 * uploaded, imported). Tapping a scan opens the full detail (all photos, OCR
 * text, every field). "New scan" is the way into capture.
 *
 * This screen also owns the sign-in gate (it is the entry point) and nudges the
 * upload queue so returning to it drains anything waiting.
 */
class HomeActivity : AppCompatActivity() {

    private lateinit var binding: ActivityHomeBinding
    private var thumbJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityHomeBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.newScan.setOnClickListener {
            startActivity(Intent(this, MainActivity::class.java))
        }
        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        // when background OCR / upload lands, the list re-renders itself
        for (name in listOf("capture-process", "capture-upload"))
            WorkManager.getInstance(this)
                .getWorkInfosForUniqueWorkLiveData(name)
                .observe(this) { if (Auth.signedIn(this)) refreshHome() }
    }

    override fun onResume() {
        super.onResume()
        if (!Auth.signedIn(this)) {
            // the entry point gates sign-in; finishing means backing out of the
            // login form exits the app instead of looping back here
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
            return
        }
        binding.configWarning.visibility = View.GONE
        // returning to Home is a good moment to drain the queue and process
        // anything a previous run left un-OCR'd
        if (CaptureSession(this).pendingUploads().isNotEmpty()) UploadWorker.kick(this)
        ProcessWorker.enqueue(this)
        refreshHome()
    }

    override fun onDestroy() {
        super.onDestroy()
        thumbJob?.cancel()
    }

    private fun refreshHome() {
        val list = binding.homeList
        list.removeAllViews()
        thumbJob?.cancel()
        val entries = Entries.recent(this)
        if (entries.isEmpty()) {
            val empty = TextView(this)
            empty.typeface = Typeface.MONOSPACE
            empty.textSize = 13f
            empty.setTextColor(getColor(R.color.whl_ink_dim))
            empty.setPadding(28, 40, 28, 28)
            empty.text = getString(R.string.home_empty)
            list.addView(empty)
            return
        }
        val inflater = LayoutInflater.from(this)
        val thumbs = ArrayList<Pair<ImageView, java.io.File>>()
        for (e in entries) {
            val row = inflater.inflate(R.layout.item_home, list, false)
            row.findViewById<TextView>(R.id.title).text = Entries.titleLabel(this, e)
            row.findViewById<TextView>(R.id.sub).text =
                listOf(e.author, e.year, "${e.photoCount} page(s)")
                    .filter { it.isNotEmpty() }.joinToString(" · ")
            val state = Entries.statusLabel(this, e)
            row.findViewById<TextView>(R.id.state).text = state
            row.findViewById<View>(R.id.marker).setBackgroundColor(getColor(markerColor(state)))
            val thumb = row.findViewById<ImageView>(R.id.thumb)
            e.photos().firstOrNull()?.let { thumbs.add(thumb to it) }
            row.setOnClickListener {
                startActivity(Intent(this, EntryDetailActivity::class.java)
                    .putExtra(EntryDetailActivity.EXTRA_ID, e.id))
            }
            list.addView(row)
        }
        // decode the page thumbnails off the UI thread, in list order
        thumbJob = lifecycleScope.launch {
            for ((iv, file) in thumbs) {
                val bmp = withContext(Dispatchers.IO) {
                    BitmapFactory.decodeFile(file.absolutePath,
                        BitmapFactory.Options().apply { inSampleSize = 8 })
                } ?: continue
                iv.setImageBitmap(bmp)
            }
        }
    }

    private fun markerColor(state: String): Int = when (state) {
        "capturing" -> R.color.whl_green
        "pending upload" -> R.color.whl_amber
        "uploaded" -> R.color.whl_blue
        "imported" -> R.color.whl_cyan
        else -> R.color.whl_face_sh2
    }
}
