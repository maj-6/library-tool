package org.whl.bookcapture

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityCoverScannerBinding
import java.io.File
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Mistral cover/title-page reader for Inspect and physical-scan search.
 *
 * It owns no CaptureSession and never creates a book or page. CameraX writes a
 * temporary JPEG, the existing authenticated Mistral pipeline reads it with OCR
 * 4.1, and only bounded text plus a non-reversible cover signature survives.
 * Physical-scan mode is hands-free: cover/title take successive shots and
 * A/B/C routes the whole session without a per-shot dialog or confirmation.
 */
class CoverScannerActivity : AppCompatActivity() {
    private lateinit var binding: ActivityCoverScannerBinding
    private var cameraProvider: ProcessCameraProvider? = null
    private var imageCapture: ImageCapture? = null
    private var recognitionJob: Job? = null
    private var activeTempFile: File? = null
    private var voice: VoiceController? = null
    private lateinit var cues: AudioCues
    private var queuedCount = 0
    private var pendingRouteSlot: ScanCollectionSlot? = null
    private var microphonePermissionRequested = false
    private val queueMode: Boolean by lazy {
        intent.getBooleanExtra(EXTRA_QUEUE_SESSION, false)
    }
    private val queueSessionId: String by lazy {
        intent.getStringExtra(EXTRA_SESSION_ID)
            ?.trim()
            ?.lowercase()
            ?.takeIf(SAFE_CAPTURE_SYNC_ID::matches)
            ?: UUID.randomUUID().toString()
    }
    private val photoRole: ScanSearchPhotoRole by lazy {
        ScanSearchPhotoRole.fromWire(intent.getStringExtra(EXTRA_PHOTO_ROLE).orEmpty())
            ?: ScanSearchPhotoRole.COVER
    }

    /** Exactly one capture may own the temporary file and recognition task. */
    private val captureInFlight = AtomicBoolean(false)

    /** Once true, every late CameraX/Mistral completion is cleanup-only. */
    private val terminal = AtomicBoolean(false)

    private val cameraPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            bindCamera()
        } else {
            Toast.makeText(
                this,
                if (queueMode) R.string.scan_queue_camera_permission_denied
                else R.string.cover_scanner_permission_denied,
                Toast.LENGTH_LONG,
            ).show()
            finishCancelled()
        }
    }

    private val microphonePermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        microphonePermissionRequested = false
        if (granted) startQueueVoice()
        else {
            Toast.makeText(this, R.string.scan_queue_voice_permission_denied, Toast.LENGTH_LONG)
                .show()
            finishCancelled()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCoverScannerBinding.inflate(layoutInflater)
        setContentView(binding.root)
        cues = AudioCues(this)

        if (queueMode) {
            binding.coverScannerTitle.setText(R.string.scan_queue_capture_title)
            binding.coverScannerHelpPanel.visibility = View.GONE
            binding.captureCover.visibility = View.GONE
            binding.closeCoverScanner.contentDescription =
                getString(R.string.scan_queue_close)
            queuedCount = ScanSearchQueue.read(this).items.count {
                it.sessionId == queueSessionId && it.scanCollectionId.isEmpty()
            }
        } else if (photoRole == ScanSearchPhotoRole.TITLE_PAGE) {
            binding.coverScannerTitle.setText(R.string.title_page_scanner_title)
            binding.coverScannerHelp.setText(R.string.title_page_scanner_help)
            binding.captureCover.setText(R.string.title_page_scanner_capture)
            binding.captureCover.setContentDescription(
                getString(R.string.title_page_scanner_capture_description),
            )
        }

        binding.closeCoverScanner.setOnClickListener { finishCancelled() }
        binding.captureCover.setOnClickListener { captureCover(photoRole) }
        binding.captureCover.isEnabled = false
        renderStatus(R.string.cover_scanner_starting, busy = true)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = finishCancelled()
        })

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            bindCamera()
        } else {
            cameraPermission.launch(Manifest.permission.CAMERA)
        }
        if (queueMode) ensureQueueVoice()
    }

    override fun onResume() {
        super.onResume()
        if (queueMode) ensureQueueVoice()
    }

    override fun onPause() {
        voice?.setPaused(true)
        super.onPause()
    }

    private fun ensureQueueVoice() {
        if (!queueMode || terminal.get()) return
        voice?.let {
            it.setPaused(false)
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            if (!microphonePermissionRequested) {
                microphonePermissionRequested = true
                microphonePermission.launch(Manifest.permission.RECORD_AUDIO)
            }
            return
        }
        startQueueVoice()
    }

    private fun startQueueVoice() {
        if (!queueMode || terminal.get() || voice != null) return
        val controller = VoiceController(
            this,
            onCommand = { command -> runOnUiThread { handleQueueVoiceCommand(command) } },
            onState = { state ->
                runOnUiThread {
                    if (!captureInFlight.get() && !terminal.get()) {
                        binding.coverScannerStatus.text = state
                    }
                }
            },
        )
        voice = controller
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                if (!controller.modelReady) {
                    controller.downloadModel { progress ->
                        runOnUiThread { binding.coverScannerStatus.text = progress }
                    }
                }
                if (voice === controller && !terminal.get()) controller.start()
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (voice === controller && !terminal.get()) {
                        Toast.makeText(
                            this@CoverScannerActivity,
                            getString(R.string.model_download_failed, e.message ?: "?"),
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                }
            }
        }
    }

    private fun handleQueueVoiceCommand(command: String) {
        when (command) {
            "cover" -> captureCover(ScanSearchPhotoRole.COVER)
            "title" -> captureCover(ScanSearchPhotoRole.TITLE_PAGE)
            "a", "b", "c" -> routeQueueSession(
                checkNotNull(ScanCollectionSlot.fromWire(command)),
            )
            "cancel" -> finishCancelled()
        }
    }

    private fun bindCamera() {
        if (terminal.get()) return
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            if (terminal.get() || isFinishing || isDestroyed) return@addListener
            try {
                val provider = providerFuture.get()
                val preview = Preview.Builder().build().also {
                    it.surfaceProvider = binding.coverPreview.surfaceProvider
                }
                val capture = ImageCapture.Builder()
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                    .setFlashMode(ImageCapture.FLASH_MODE_OFF)
                    .build()
                provider.unbindAll()
                provider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    capture,
                )
                cameraProvider = provider
                imageCapture = capture
                binding.captureCover.isEnabled = true
                renderStatus(readyStatus(), busy = false)
            } catch (_: Exception) {
                Toast.makeText(
                    this,
                    if (queueMode) R.string.scan_queue_camera_unavailable
                    else R.string.cover_scanner_unavailable,
                    Toast.LENGTH_LONG,
                ).show()
                finishCancelled()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun captureCover(role: ScanSearchPhotoRole) {
        if (terminal.get()) return
        val capture = imageCapture ?: return
        val mistralKey = Prefs.mistralKey(this).trim()
        if (mistralKey.isEmpty()) {
            Toast.makeText(
                this,
                if (queueMode) R.string.scan_queue_requires_mistral_key
                else R.string.cover_scanner_requires_mistral_key,
                Toast.LENGTH_LONG,
            ).show()
            return
        }
        if (!captureInFlight.compareAndSet(false, true)) return
        cues.photoHeard()

        binding.captureCover.isEnabled = false
        renderStatus(
            if (queueMode) {
                R.string.scan_queue_capturing
            } else if (role == ScanSearchPhotoRole.TITLE_PAGE) {
                R.string.title_page_scanner_capturing
            } else {
                R.string.cover_scanner_capturing
            },
            busy = true,
        )
        val target = try {
            File.createTempFile(COVER_TEMP_PREFIX, ".jpg", cacheDir)
        } catch (_: Exception) {
            recoverAttempt(null, captureFailureMessage())
            return
        }
        activeTempFile = target
        val output = ImageCapture.OutputFileOptions.Builder(target).build()
        try {
            capture.takePicture(
                output,
                ContextCompat.getMainExecutor(this),
                object : ImageCapture.OnImageSavedCallback {
                    override fun onImageSaved(outputFileResults: ImageCapture.OutputFileResults) {
                        if (terminal.get()) {
                            discardTemp(target)
                            captureInFlight.set(false)
                            return
                        }
                        cues.photoStarted()
                        recognizeCover(target, mistralKey, role)
                    }

                    override fun onError(exception: ImageCaptureException) {
                        recoverAttempt(target, captureFailureMessage())
                    }
                },
            )
        } catch (_: Exception) {
            recoverAttempt(target, captureFailureMessage())
        }
    }

    private fun recognizeCover(
        target: File,
        mistralKey: String,
        role: ScanSearchPhotoRole,
    ) {
        if (terminal.get()) {
            discardTemp(target)
            captureInFlight.set(false)
            return
        }
        renderStatus(
            if (queueMode) {
                R.string.scan_queue_reading
            } else if (role == ScanSearchPhotoRole.TITLE_PAGE) {
                R.string.title_page_scanner_reading
            } else {
                R.string.cover_scanner_reading
            },
            busy = true,
        )
        recognitionJob = lifecycleScope.launch {
            val result = try {
                withContext(Dispatchers.IO) {
                    // This is a disposable cache image. Bound its upload size
                    // without touching any capture/session data.
                    Pipeline.standardizeInPlace(target)
                    val signature = if (role == ScanSearchPhotoRole.COVER) {
                        extractCoverVisualSignature(target).orEmpty()
                    } else {
                        ""
                    }
                    val recognized = try {
                        boundedCoverText(Pipeline.coverOcr(target, mistralKey))
                    } catch (error: Exception) {
                        if (signature.isEmpty()) throw error else ""
                    }
                    recognized to signature
                }
            } catch (cancelled: CancellationException) {
                discardTemp(target)
                captureInFlight.compareAndSet(true, false)
                throw cancelled
            } catch (_: Exception) {
                recoverAttempt(target, readFailureMessage())
                return@launch
            } finally {
                // standardizeInPlace uses this sibling for its atomic rewrite;
                // every terminal path must clean both disposable cache files.
                discardTemp(target)
            }

            if (terminal.get() || isFinishing || isDestroyed) {
                discardTemp(target)
                captureInFlight.compareAndSet(true, false)
                return@launch
            }
            val (recognized, signature) = result
            if (!hasReadableCoverText(recognized) && signature.isEmpty()) {
                recoverAttempt(
                    target,
                    if (queueMode) R.string.scan_queue_no_evidence
                    else R.string.cover_scanner_no_text,
                )
            } else if (queueMode) {
                queueResult(target, role, recognized, signature)
            } else {
                deliverResult(target, recognized)
            }
        }
    }

    private fun queueResult(
        target: File,
        role: ScanSearchPhotoRole,
        recognized: String,
        signature: String,
    ) {
        if (!captureInFlight.compareAndSet(true, false)) {
            discardTemp(target)
            return
        }
        discardTemp(target)
        val queued = ScanSearchQueue.enqueueDraft(
            this,
            sessionId = queueSessionId,
            photoRole = role,
            ocrText = recognized,
            visualSignature = signature,
        )
        if (queued == null) {
            recoverQueuedSaveFailure()
            return
        }
        queuedCount += 1
        cues.saved(queuedCount)
        val route = pendingRouteSlot
        pendingRouteSlot = null
        if (route != null) {
            routeQueueSession(route)
        } else {
            binding.captureCover.isEnabled = imageCapture != null
            binding.coverScannerStatus.text = getString(
                R.string.scan_queue_capture_saved,
                queuedCount,
            )
            binding.coverScannerProgress.visibility = View.GONE
        }
    }

    private fun recoverQueuedSaveFailure() {
        pendingRouteSlot = null
        Toast.makeText(this, R.string.scan_queue_save_failed, Toast.LENGTH_LONG).show()
        binding.captureCover.isEnabled = imageCapture != null
        renderStatus(readyStatus(), busy = false)
    }

    private fun captureFailureMessage(): Int = if (queueMode) {
        R.string.scan_queue_capture_failed
    } else {
        R.string.cover_scanner_capture_failed
    }

    private fun readFailureMessage(): Int = if (queueMode) {
        R.string.scan_queue_read_failed
    } else {
        R.string.cover_scanner_read_failed
    }

    private fun routeQueueSession(slot: ScanCollectionSlot) {
        if (terminal.get()) return
        if (captureInFlight.get()) {
            pendingRouteSlot = slot
            binding.coverScannerStatus.setText(R.string.scan_queue_finishing_capture)
            return
        }
        if (queuedCount <= 0) {
            cues.error("identifying image required")
            binding.coverScannerStatus.setText(R.string.scan_queue_capture_first)
            return
        }
        val destination = Collections.currentScan(this, slot)
        if (destination == null) {
            cues.error("scan slot ${slot.name} is not assigned")
            binding.coverScannerStatus.text = getString(
                R.string.scan_queue_slot_unassigned,
                slot.name,
            )
            return
        }
        val routed = ScanSearchQueue.routeSession(this, queueSessionId, destination.id)
        if (routed == null || routed.isEmpty()) {
            cues.error("scan queue could not be saved")
            Toast.makeText(this, R.string.scan_queue_save_failed, Toast.LENGTH_LONG).show()
            return
        }
        ScanSearchQueueSyncWorker.enqueue(this)
        cues.saved(routed.size)
        if (!terminal.compareAndSet(false, true)) return
        setResult(
            Activity.RESULT_OK,
            Intent()
                .putExtra(EXTRA_SESSION_ID, queueSessionId)
                .putExtra(EXTRA_CAPTURE_COUNT, routed.size)
                .putExtra(EXTRA_SCAN_SLOT, slot.wireValue)
                .putExtra(EXTRA_SCAN_COLLECTION_ID, destination.id),
        )
        finish()
    }

    private fun deliverResult(target: File, recognized: String) {
        if (!captureInFlight.compareAndSet(true, false)) {
            discardTemp(target)
            return
        }
        discardTemp(target)
        if (!terminal.compareAndSet(false, true)) return
        setResult(
            Activity.RESULT_OK,
            Intent()
                .putExtra(EXTRA_RECOGNIZED_TEXT, recognized)
                .putExtra(EXTRA_PHOTO_ROLE, photoRole.wireValue),
        )
        finish()
    }

    /** Return to a ready preview after a recoverable capture/read failure. */
    private fun recoverAttempt(target: File?, message: Int) {
        target?.let(::discardTemp)
        if (!captureInFlight.compareAndSet(true, false)) return
        pendingRouteSlot = null
        if (terminal.get() || isFinishing || isDestroyed) return
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        binding.captureCover.isEnabled = imageCapture != null
        renderStatus(readyStatus(), busy = false)
    }

    private fun readyStatus(): Int = if (queueMode) {
        R.string.scan_queue_voice_ready
    } else if (photoRole == ScanSearchPhotoRole.TITLE_PAGE) {
        R.string.title_page_scanner_ready
    } else {
        R.string.cover_scanner_ready
    }

    private fun renderStatus(message: Int, busy: Boolean) {
        binding.coverScannerStatus.setText(message)
        binding.coverScannerProgress.visibility = if (busy) View.VISIBLE else View.GONE
    }

    private fun finishCancelled() {
        if (!terminal.compareAndSet(false, true)) return
        setResult(Activity.RESULT_CANCELED)
        finish()
    }

    private fun discardTemp(target: File) {
        if (activeTempFile?.absolutePath == target.absolutePath) activeTempFile = null
        runCatching { if (target.exists()) target.delete() }
        val standardizeTemp = File(target.parentFile, target.name + ".tmp")
        runCatching { if (standardizeTemp.exists()) standardizeTemp.delete() }
    }

    override fun onDestroy() {
        terminal.set(true)
        captureInFlight.set(false)
        imageCapture = null
        cameraProvider?.unbindAll()
        cameraProvider = null
        recognitionJob?.cancel()
        recognitionJob = null
        voice?.stop()
        voice = null
        cues.shutdown()
        activeTempFile?.let(::discardTemp)
        super.onDestroy()
    }

    companion object {
        const val EXTRA_RECOGNIZED_TEXT = "recognized_cover_text"
        const val EXTRA_PHOTO_ROLE = "scan_search_photo_role"
        const val EXTRA_QUEUE_SESSION = "scan_search_queue_session"
        const val EXTRA_SESSION_ID = "scan_search_session_id"
        const val EXTRA_CAPTURE_COUNT = "scan_search_capture_count"
        const val EXTRA_SCAN_SLOT = "scan_search_slot"
        const val EXTRA_SCAN_COLLECTION_ID = "scan_search_collection_id"
        internal const val MAX_RECOGNIZED_TEXT_CHARS = 16_000
        internal const val MIN_COVER_READABLE_CHARS = 4
        private const val COVER_TEMP_PREFIX = "inspect-cover-"

        internal fun boundedCoverText(value: String): String = value
            .replace('\u0000', ' ')
            .replace("\r\n", "\n")
            .replace('\r', '\n')
            .trim()
            .take(MAX_RECOGNIZED_TEXT_CHARS)

        internal fun hasReadableCoverText(value: String): Boolean =
            Pipeline.readableOcrChars(value) >= MIN_COVER_READABLE_CHARS
    }
}
