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
import java.util.concurrent.atomic.AtomicBoolean

/**
 * One-shot Mistral cover/title-page reader for Inspect.
 *
 * It owns no CaptureSession and never creates a book or page. CameraX writes a
 * temporary JPEG, the existing authenticated Mistral pipeline reads it with OCR
 * 4.1, and only bounded text is returned to Home for inventory matching.
 */
class CoverScannerActivity : AppCompatActivity() {
    private lateinit var binding: ActivityCoverScannerBinding
    private var cameraProvider: ProcessCameraProvider? = null
    private var imageCapture: ImageCapture? = null
    private var recognitionJob: Job? = null
    private var activeTempFile: File? = null
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
                R.string.cover_scanner_permission_denied,
                Toast.LENGTH_LONG,
            ).show()
            finishCancelled()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCoverScannerBinding.inflate(layoutInflater)
        setContentView(binding.root)

        if (photoRole == ScanSearchPhotoRole.TITLE_PAGE) {
            binding.coverScannerTitle.setText(R.string.title_page_scanner_title)
            binding.coverScannerHelp.setText(R.string.title_page_scanner_help)
            binding.captureCover.setText(R.string.title_page_scanner_capture)
            binding.captureCover.setContentDescription(
                getString(R.string.title_page_scanner_capture_description),
            )
        }

        binding.closeCoverScanner.setOnClickListener { finishCancelled() }
        binding.captureCover.setOnClickListener { captureCover() }
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
                    R.string.cover_scanner_unavailable,
                    Toast.LENGTH_LONG,
                ).show()
                finishCancelled()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun captureCover() {
        if (terminal.get()) return
        val capture = imageCapture ?: return
        val mistralKey = Prefs.mistralKey(this).trim()
        if (mistralKey.isEmpty()) {
            Toast.makeText(
                this,
                R.string.cover_scanner_requires_mistral_key,
                Toast.LENGTH_LONG,
            ).show()
            return
        }
        if (!captureInFlight.compareAndSet(false, true)) return

        binding.captureCover.isEnabled = false
        renderStatus(
            if (photoRole == ScanSearchPhotoRole.TITLE_PAGE) {
                R.string.title_page_scanner_capturing
            } else {
                R.string.cover_scanner_capturing
            },
            busy = true,
        )
        val target = try {
            File.createTempFile(COVER_TEMP_PREFIX, ".jpg", cacheDir)
        } catch (_: Exception) {
            recoverAttempt(null, R.string.cover_scanner_capture_failed)
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
                        recognizeCover(target, mistralKey)
                    }

                    override fun onError(exception: ImageCaptureException) {
                        recoverAttempt(target, R.string.cover_scanner_capture_failed)
                    }
                },
            )
        } catch (_: Exception) {
            recoverAttempt(target, R.string.cover_scanner_capture_failed)
        }
    }

    private fun recognizeCover(target: File, mistralKey: String) {
        if (terminal.get()) {
            discardTemp(target)
            captureInFlight.set(false)
            return
        }
        renderStatus(
            if (photoRole == ScanSearchPhotoRole.TITLE_PAGE) {
                R.string.title_page_scanner_reading
            } else {
                R.string.cover_scanner_reading
            },
            busy = true,
        )
        recognitionJob = lifecycleScope.launch {
            val recognized = try {
                withContext(Dispatchers.IO) {
                    // This is a disposable cache image. Bound its upload size
                    // without touching any capture/session data.
                    Pipeline.standardizeInPlace(target)
                    boundedCoverText(Pipeline.coverOcr(target, mistralKey))
                }
            } catch (cancelled: CancellationException) {
                discardTemp(target)
                captureInFlight.compareAndSet(true, false)
                throw cancelled
            } catch (_: Exception) {
                recoverAttempt(target, R.string.cover_scanner_read_failed)
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
            if (!hasReadableCoverText(recognized)) {
                recoverAttempt(target, R.string.cover_scanner_no_text)
            } else {
                deliverResult(target, recognized)
            }
        }
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
        if (terminal.get() || isFinishing || isDestroyed) return
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        binding.captureCover.isEnabled = imageCapture != null
        renderStatus(readyStatus(), busy = false)
    }

    private fun readyStatus(): Int = if (photoRole == ScanSearchPhotoRole.TITLE_PAGE) {
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
        activeTempFile?.let(::discardTemp)
        super.onDestroy()
    }

    companion object {
        const val EXTRA_RECOGNIZED_TEXT = "recognized_cover_text"
        const val EXTRA_PHOTO_ROLE = "scan_search_photo_role"
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
