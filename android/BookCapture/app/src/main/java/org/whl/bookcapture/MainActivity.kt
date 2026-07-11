package org.whl.bookcapture

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityMainBinding

/**
 * Hands-free book capture:
 *
 *   say "start"  — begin a book entry        (or tap ▶)
 *   say "photo"  — photograph the shown page (●)
 *   say "done"   — seal + upload the entry   (✓)
 *   say "cancel" — void the entry            (✕)
 *
 * The camera preview fills the screen under a thin CAD-style chrome: the
 * entry state ("OPEN (3)") and a recent-scans dropdown live in the top bar,
 * captured pages run as a thumbnail strip above the controls. Photos are
 * OCR'd in the background as they land, so a book often has its title in the
 * list before its folder finishes uploading.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var session: CaptureSession
    private lateinit var cues: AudioCues
    private var voice: VoiceController? = null
    private var imageCapture: ImageCapture? = null
    private var initialized = false
    private var busy = false                  // a shot is being written
    private var pendingCommand: String? = null   // "done"/"cancel" said mid-shot

    private val permissions = arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        session = CaptureSession(this)
        cues = AudioCues()

        binding.btnStart.setOnClickListener { command("start") }
        binding.btnPhoto.setOnClickListener { command("photo") }
        binding.btnDone.setOnClickListener { command("done") }
        binding.btnCancel.setOnClickListener { command("cancel") }
        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.queueChip.setOnClickListener {
            binding.recentPanel.visibility =
                if (binding.recentPanel.visibility == View.VISIBLE) View.GONE else View.VISIBLE
            if (binding.recentPanel.visibility == View.VISIBLE) refreshRecent()
        }

        // background work landing (OCR done, upload done) refreshes the list
        for (name in listOf("capture-process", "capture-upload"))
            WorkManager.getInstance(this)
                .getWorkInfosForUniqueWorkLiveData(name)
                .observe(this) { updateUi() }
    }

    override fun onResume() {
        super.onResume()
        if (!Auth.signedIn(this)) {
            // finish, so backing out of the login form exits the app instead of
            // bouncing straight back here and relaunching login forever
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
            return
        }
        if (!initialized) {
            if (permissions.all {
                    ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
                }) initAfterPermissions()
            else ActivityCompat.requestPermissions(this, permissions, 1)
        }
        voice?.setPaused(false)               // mic live only while on screen
        if (session.pendingUploads().isNotEmpty()) UploadWorker.kick(this)
        restoreThumbnailsIfNeeded()           // an entry re-adopted after recreation
        updateUi()
    }

    /** After a config change / process death, CaptureSession re-adopts the open
     *  entry but the thumbnail strip (view state) is gone — repaint it from the
     *  photos on disk so "OPEN (3)" still shows three pages. */
    private fun restoreThumbnailsIfNeeded() {
        val id = session.entryId ?: return
        if (binding.thumbs.childCount >= session.photoCount) return
        binding.thumbs.removeAllViews()
        session.entryDir(id).listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
            ?.sortedBy { photoNumber(it.name) }
            ?.forEach { addThumbnail(it.absolutePath) }
    }

    override fun onPause() {
        super.onPause()
        voice?.setPaused(true)
    }

    override fun onDestroy() {
        super.onDestroy()
        voice?.stop()
        cues.shutdown()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, perms, results)
        if (results.isNotEmpty() && results.all { it == PackageManager.PERMISSION_GRANTED })
            initAfterPermissions()
        else setStatus(getString(R.string.need_permissions))
    }

    private fun initAfterPermissions() {
        if (initialized) return
        initialized = true
        startCamera()
        UploadWorker.enqueue(this)                 // drain anything left from last time
        ProcessWorker.enqueue(this)
        val v = VoiceController(this,
            onCommand = { word -> runOnUiThread { command(word) } },
            onState = { msg -> runOnUiThread { setStatus(msg) } })
        voice = v
        lifecycleScope.launch(Dispatchers.IO) {    // model download/load off the UI thread
            try {
                if (!v.modelReady)
                    v.downloadModel { p -> runOnUiThread { setStatus(p) } }
                // the download has no suspension points, so a destroy during it
                // doesn't cancel us — don't grab the mic for a dead activity
                // (VoiceController's stopped latch catches the narrower race)
                if (isActive) v.start()
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    setStatus(getString(R.string.model_download_failed, e.message ?: "?"))
                }
            }
        }
        updateUi()
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            if (lifecycle.currentState == Lifecycle.State.DESTROYED) return@addListener
            val provider = future.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.preview.surfaceProvider)
            }
            val capture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                .setJpegQuality(92)               // the pipeline recompresses to the standard
                .build()
            imageCapture = capture
            provider.unbindAll()
            provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, capture)
        }, ContextCompat.getMainExecutor(this))
    }

    // --- the command state machine --------------------------------------------

    private fun command(word: String) {
        // a shot is still being written: sealing/voiding now would lose it, so
        // remember the command and run it the moment the shot lands
        if (busy && (word == "done" || word == "cancel")) {
            pendingCommand = word
            return
        }
        when (word) {
            "start" -> {
                if (session.active) cues.error("entry already open")
                else {
                    session.start()
                    cues.started()
                }
            }
            "photo" -> {
                if (!session.active) cues.error("no entry open")
                else takePhoto()
            }
            "done" -> {
                if (!session.active) cues.error("nothing to save")
                else {
                    val photos = session.photoCount
                    val id = session.done()
                    when {
                        id != null -> {
                            cues.saved(photos)
                            ProcessWorker.enqueue(this)
                            UploadWorker.enqueue(this)
                        }
                        // null + still active = the manifest write failed
                        // (disk full); the pages are kept and "done" can retry
                        session.active -> cues.error("could not save, still open")
                        else -> cues.error("no pages, entry dropped")
                    }
                }
            }
            "cancel" -> {
                if (session.cancel()) cues.cancelled()
                else cues.error("nothing to discard")
            }
        }
        updateUi()
    }

    private fun takePhoto() {
        val capture = imageCapture ?: return cues.error("camera not ready")
        if (busy) { cues.error("hold on"); return }   // audible, never silent
        val file = session.nextPhotoFile() ?: return
        busy = true
        val opts = ImageCapture.OutputFileOptions.Builder(file).build()
        capture.takePicture(opts, ContextCompat.getMainExecutor(this),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(results: ImageCapture.OutputFileResults) {
                    busy = false
                    session.photoSaved()
                    cues.photo(session.photoCount)
                    addThumbnail(file.absolutePath)
                    // standardize + OCR while the user flips to the next page
                    ProcessWorker.enqueue(this@MainActivity)
                    updateUi()
                    runPending()
                }
                override fun onError(e: ImageCaptureException) {
                    busy = false
                    cues.error("capture failed")
                    setStatus("Capture error: ${e.message}")
                    runPending()
                }
            })
    }

    private fun runPending() {
        val cmd = pendingCommand ?: return
        pendingCommand = null
        command(cmd)
    }

    // --- UI ---------------------------------------------------------------------

    private fun addThumbnail(path: String) {
        val opts = BitmapFactory.Options().apply { inSampleSize = 8 }
        val bmp = BitmapFactory.decodeFile(path, opts) ?: return
        val iv = ImageView(this)
        val h = binding.thumbs.height.coerceAtLeast(160)
        val w = h * bmp.width / bmp.height.coerceAtLeast(1)
        iv.layoutParams = LinearLayout.LayoutParams(w, h).apply { marginEnd = 8 }
        iv.scaleType = ImageView.ScaleType.CENTER_CROP
        iv.setImageBitmap(bmp)
        binding.thumbs.addView(iv)
    }

    private fun setStatus(msg: String) {
        binding.status.text = msg
    }

    private fun updateUi() {
        val active = session.active
        binding.entryState.text =
            if (active) getString(R.string.entry_active, session.photoCount)
            else getString(R.string.entry_idle)
        binding.entryState.setTextColor(
            getColor(if (active) R.color.whl_green else R.color.whl_ink_dim))
        binding.btnStart.isEnabled = !active
        binding.btnPhoto.isEnabled = active
        binding.btnDone.isEnabled = active
        binding.btnCancel.isEnabled = active
        if (!active) binding.thumbs.removeAllViews()

        val signedIn = Auth.signedIn(this)
        val err = Prefs.lastUploadError(this) ?: Prefs.lastProcError(this)
        val pending = session.pendingUploads().size
        binding.configWarning.visibility = if (signedIn) View.GONE else View.VISIBLE
        binding.configWarning.text = getString(R.string.not_signed_in)
        binding.queueChip.text =
            if (pending > 0) getString(R.string.recent_chip, pending)
            else getString(R.string.recent_chip_empty)
        binding.queueChip.setTextColor(
            getColor(when {
                err != null -> R.color.whl_red
                pending > 0 -> R.color.whl_amber
                else -> R.color.whl_ink_dim
            }))
        if (err != null && pending > 0)
            setStatus(getString(R.string.uploads_stuck, pending, err))
        if (binding.recentPanel.visibility == View.VISIBLE) refreshRecent()
    }

    /** Rebuild the dropdown: newest first, "Processing…" until the pipeline
     *  turns a folder of photos into a book record. */
    private fun refreshRecent() {
        val list = binding.recentList
        list.removeAllViews()
        val entries = Entries.recent(this)
        if (entries.isEmpty()) {
            val empty = TextView(this)
            empty.typeface = android.graphics.Typeface.MONOSPACE
            empty.textSize = 12f
            empty.setTextColor(getColor(R.color.whl_ink_dim))
            empty.setPadding(24, 18, 24, 18)
            empty.text = getString(R.string.recent_none)
            list.addView(empty)
            return
        }
        val inflater = LayoutInflater.from(this)
        for (e in entries) {
            val row = inflater.inflate(R.layout.item_recent, list, false)
            row.findViewById<TextView>(R.id.title).text = Entries.titleLabel(this, e)
            row.findViewById<TextView>(R.id.sub).text =
                listOf(e.author, e.year, "${e.photoCount}p")
                    .filter { it.isNotEmpty() }.joinToString(" · ")
            val state = Entries.statusLabel(this, e)
            row.findViewById<TextView>(R.id.state).text = state
            row.findViewById<View>(R.id.marker).setBackgroundColor(getColor(when (state) {
                "capturing" -> R.color.whl_green
                "pending upload" -> R.color.whl_amber
                "uploaded" -> R.color.whl_blue
                "imported" -> R.color.whl_cyan
                else -> R.color.whl_face_sh2
            }))
            row.setOnClickListener {
                startActivity(Intent(this, EntryDetailActivity::class.java)
                    .putExtra(EntryDetailActivity.EXTRA_ID, e.id))
            }
            list.addView(row)
        }
    }
}
