package org.whl.bookcapture

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityMainBinding

/**
 * Hands-free book capture:
 *
 *   say "start"  — begin a book entry        (or tap the button)
 *   say "photo"  — photograph the shown page
 *   say "done"   — seal + upload the entry
 *   say "cancel" — void the entry
 *
 * The camera preview fills the screen; captured pages appear as a thumbnail
 * strip. Every registered command is confirmed with a tone + spoken word.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var session: CaptureSession
    private lateinit var cues: AudioCues
    private var voice: VoiceController? = null
    private var imageCapture: ImageCapture? = null
    private var busy = false                  // a shot is being written
    private var pendingCommand: String? = null   // "done"/"cancel" said mid-shot

    private val permissions = arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        session = CaptureSession(this)
        cues = AudioCues(this) { speaking -> voice?.suppress(speaking) }

        binding.btnStart.setOnClickListener { command("start") }
        binding.btnPhoto.setOnClickListener { command("photo") }
        binding.btnDone.setOnClickListener { command("done") }
        binding.btnCancel.setOnClickListener { command("cancel") }
        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        if (permissions.all {
                ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
            }) initAfterPermissions()
        else ActivityCompat.requestPermissions(this, permissions, 1)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, perms, results)
        if (results.isNotEmpty() && results.all { it == PackageManager.PERMISSION_GRANTED })
            initAfterPermissions()
        else setStatus(getString(R.string.need_permissions))
    }

    private fun initAfterPermissions() {
        startCamera()
        UploadWorker.enqueue(this)                 // drain anything left from last time
        val v = VoiceController(this,
            onCommand = { word -> runOnUiThread { command(word) } },
            onState = { msg -> runOnUiThread { setStatus(msg) } })
        voice = v
        lifecycleScope.launch(Dispatchers.IO) {    // model download/load off the UI thread
            try {
                if (!v.modelReady)
                    v.downloadModel { p -> runOnUiThread { setStatus(p) } }
                v.start()
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
            val provider = future.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.preview.surfaceProvider)
            }
            val capture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                .setJpegQuality(88)               // "lightly compressed" upload originals
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
                    if (id == null) cues.error("no pages, entry dropped")
                    else {
                        cues.saved(photos)
                        UploadWorker.enqueue(this)
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
        iv.layoutParams = android.widget.LinearLayout.LayoutParams(w, h).apply { marginEnd = 8 }
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
        binding.entryState.setBackgroundResource(
            if (active) R.color.entry_active else R.color.entry_idle)
        binding.btnStart.isEnabled = !active
        binding.btnPhoto.isEnabled = active
        binding.btnDone.isEnabled = active
        binding.btnCancel.isEnabled = active
        if (!active) binding.thumbs.removeAllViews()
        binding.configWarning.visibility =
            if (Prefs.configured(this)) View.GONE else View.VISIBLE
        val pending = session.pendingUploads().size
        binding.queueState.text =
            if (pending > 0) getString(R.string.uploads_pending, pending) else ""
    }

    override fun onResume() {
        super.onResume()
        voice?.setPaused(false)               // mic live only while on screen
        updateUi()
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
}
