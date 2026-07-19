package org.whl.bookcapture

import android.Manifest
import android.animation.LayoutTransition
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.graphics.RenderEffect
import android.graphics.RuntimeShader
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.transition.TransitionManager
import android.util.Log
import android.util.Size
import android.view.LayoutInflater
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalZeroShutterLag
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import com.google.android.material.snackbar.Snackbar
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import org.whl.bookcapture.databinding.ActivityMainBinding

// A 3x3 unsharp/Laplacian kernel: center*3 - 0.5*(4 neighbours). Flat regions
// keep their value (3 - 0.5*4 = 1), edges get boosted. Coords are in pixels.
private const val SHARPEN_AGSL = """
uniform shader content;
half4 main(float2 coord) {
    half4 c = content.eval(coord);
    half4 sum = content.eval(coord + float2(0.0, -1.0))
              + content.eval(coord + float2(0.0, 1.0))
              + content.eval(coord + float2(-1.0, 0.0))
              + content.eval(coord + float2(1.0, 0.0));
    half3 rgb = clamp(c.rgb * 3.0 - sum.rgb * 0.5, 0.0, 1.0);
    return half4(rgb, c.a);
}
"""

private const val CAMERA_LOG_TAG = "BookCaptureCamera"
private const val CAMERA_PERMISSION_REQUEST = 1
private const val VOICE_PERMISSION_REQUEST = 2
private val TORCH_DIAGNOSTICS = Regex("torch requested=[^;]+")

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
    private var boundCamera: Camera? = null
    private var boundCameraConfig: CameraBindingSnapshot? = null
    private var cameraBindingInFlight: CameraBindingSnapshot? = null
    private var initialized = false
    private var voicePermissionRequestedForEnablement = false
    private var discardDialog: AlertDialog? = null
    private val captureQueue = ShallowCaptureQueue()
    private val acceptedShots = mutableMapOf<Long, AcceptedShot>()
    private var pendingCommand: String? = null   // "done"/"cancel" said mid-shot
    private var deferredCaptureSubmission = false
    private var finishAfterAcceptedCaptures = false
    private var waitingForPriorCaptureWrites = false

    private data class AcceptedShot(
        var reservation: CaptureSession.PhotoReservation,
        val acceptedAtNanos: Long,
        var startedAtNanos: Long? = null,
    )

    private data class CameraBindingSnapshot(
        val profile: CameraCaptureProfile,
        val sharpenPreview: Boolean,
        val torchEnabled: Boolean,
    )

    private data class BoundCameraUseCases(
        val camera: Camera,
        val capture: ImageCapture,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (captureQueue.busy) {
                    finishAfterAcceptedCaptures = true
                    setStatus("Finishing accepted page photos…")
                    updateUi()
                } else {
                    finish()
                }
            }
        })
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        session = CaptureSession(this)
        pendingCommand = Prefs.pendingCaptureCommand(this, session.entryId)
        cues = AudioCues(this)
        binding.thumbs.layoutTransition = LayoutTransition()   // pages land, not pop

        binding.btnStart.setOnClickListener { command("start") }
        binding.btnPhoto.setOnClickListener { command("photo") }
        binding.btnDone.setOnClickListener { command("done") }
        binding.btnCancel.setOnClickListener { command("cancel") }
        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.configWarning.setOnClickListener {
            startActivity(Intent(this, LoginActivity::class.java))
        }
        binding.queueChip.setOnClickListener {
            TransitionManager.beginDelayedTransition(binding.root)
            binding.recentPanel.visibility =
                if (binding.recentPanel.visibility == View.VISIBLE) View.GONE else View.VISIBLE
            if (binding.recentPanel.visibility == View.VISIBLE) refreshRecent()
        }

        // background work landing (OCR done, upload done) refreshes the list
        for (name in listOf(
            ProcessWorker.UNIQUE_WORK_NAME,
            ProcessWorker.BACKLOG_WORK_NAME,
            "capture-upload",
        ))
            WorkManager.getInstance(this)
                .getWorkInfosForUniqueWorkLiveData(name)
                .observe(this) { updateUi() }
    }

    override fun onResume() {
        super.onResume()
        if (!initialized) {
            if (cameraPermissionGranted()) initAfterCameraPermission()
            else ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.CAMERA), CAMERA_PERMISSION_REQUEST)
        } else {
            syncVoicePreference()
        }
        if (session.pendingUploads().isNotEmpty() &&
            (Auth.signedIn(this) || Prefs.transport(this) != "cloud")) {
            UploadWorker.kick(this)
        }
        restoreThumbnailsIfNeeded()           // an entry re-adopted after recreation
        // Settings can change the capture profile, continuous light, or preview
        // implementation. Profile/preview changes wait for accepted captures;
        // continuous light can be changed through CameraControl in place.
        applyCameraPreferenceChanges()
        submitDeferredCaptureIfReady()
        updateUi()
    }

    /** After a config change / process death, CaptureSession re-adopts the open
     *  entry but the thumbnail strip (view state) is gone — repaint it from the
     *  photos on disk so "OPEN (3)" still shows three pages. */
    private fun restoreThumbnailsIfNeeded() {
        val id = session.entryId ?: return
        session.refreshPhotoCount()
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

    override fun onStop() {
        // Normally an unsubmitted request should not fire after the user leaves.
        // Done is different: it explicitly promises to finish every accepted
        // capture, so keep its one queued reservation for the next resume.
        if (pendingCommand != "done") cancelQueuedCapture()
        super.onStop()
    }

    override fun onDestroy() {
        // Never delete an output still owned by a CameraX callback. Back is
        // deferred until the queue drains; Activity recreation is covered by
        // CaptureSession's process-local temporary-file ownership registry.
        if (!captureQueue.busy) discardAllCaptureRequests()
        discardDialog?.dismiss()
        discardDialog = null
        boundCamera = null
        boundCameraConfig = null
        cameraBindingInFlight = null
        if (!captureQueue.busy) deferredCaptureSubmission = false
        super.onDestroy()
        voice?.stop()
        cues.shutdown()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, perms, results)
        when (requestCode) {
            CAMERA_PERMISSION_REQUEST -> {
                if (cameraPermissionGranted()) initAfterCameraPermission()
                else setStatus(getString(R.string.need_permissions))
            }
            VOICE_PERMISSION_REQUEST -> {
                voicePermissionRequestedForEnablement = false
                if (voicePermissionGranted() && Prefs.voiceEnabled(this)) startVoice()
                else {
                    Prefs.setVoiceEnabled(this, false)
                    setStatus(getString(R.string.voice_permission_denied))
                }
            }
        }
    }

    private fun initAfterCameraPermission() {
        if (initialized) return
        initialized = true
        startCameraAfterPriorWrites()
        UploadWorker.enqueue(this)                 // drain anything left from last time
        ProcessWorker.enqueue(this)
        syncVoicePreference()
        updateUi()
    }

    /** An Activity can be replaced while the old CameraX callback is still
     * completing. Binding the replacement immediately calls unbindAll(), so
     * wait for the process-owned temporary to be committed or aborted first. */
    private fun startCameraAfterPriorWrites() {
        if (!session.hasActiveCaptureWrites()) {
            runPending()
            startCamera()
            return
        }
        if (waitingForPriorCaptureWrites) return
        waitingForPriorCaptureWrites = true
        setStatus("Finishing accepted page photo…")
        lifecycleScope.launch {
            while (isActive && session.hasActiveCaptureWrites()) delay(50)
            if (!isActive) return@launch
            waitingForPriorCaptureWrites = false
            session.refreshPhotoCount()
            restoreThumbnailsIfNeeded()
            runPending()
            if (!isDestroyed && !isFinishing) startCamera()
        }
    }

    private fun cameraPermissionGranted(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    private fun voicePermissionGranted(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    /** Voice is an optional enhancement. Camera initialization never waits on
     *  microphone permission or the offline model download. */
    private fun syncVoicePreference() {
        if (!Prefs.voiceEnabled(this)) {
            voicePermissionRequestedForEnablement = false
            voice?.stop()
            voice = null
            return
        }
        if (!voicePermissionGranted()) {
            if (!voicePermissionRequestedForEnablement) {
                voicePermissionRequestedForEnablement = true
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.RECORD_AUDIO),
                    VOICE_PERMISSION_REQUEST,
                )
            }
            return
        }
        voicePermissionRequestedForEnablement = false
        if (voice == null) startVoice() else voice?.setPaused(false)
    }

    private fun startVoice() {
        if (!Prefs.voiceEnabled(this) || !voicePermissionGranted() || voice != null) return
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
                if (isActive && voice === v && Prefs.voiceEnabled(this@MainActivity)) v.start()
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (voice === v)
                        setStatus(getString(R.string.model_download_failed, e.message ?: "?"))
                }
            }
        }
    }

    private fun desiredCameraBindingSnapshot(): CameraBindingSnapshot = CameraBindingSnapshot(
        profile = cameraCaptureProfile(Prefs.cameraProfile(this)),
        sharpenPreview = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            Prefs.sharpenPreview(this),
        torchEnabled = Prefs.torchEnabled(this),
    )

    @androidx.annotation.OptIn(markerClass = [ExperimentalZeroShutterLag::class])
    private fun startCamera() {
        if (captureQueue.busy) return
        val requestedBinding = desiredCameraBindingSnapshot()
        cameraBindingInFlight = requestedBinding
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            if (lifecycle.currentState == Lifecycle.State.DESTROYED) return@addListener
            // A newer settings snapshot superseded this asynchronous request.
            if (cameraBindingInFlight != requestedBinding) return@addListener
            // The user may have accepted a capture while the provider future was
            // resolving. Never unbind its use cases from underneath CameraX.
            if (captureQueue.busy) {
                cameraBindingInFlight = null
                return@addListener
            }
            try {
                val provider = future.get()
                // optional viewfinder sharpen (Android 13+): a RenderEffect only
                // composites over the preview in COMPATIBLE mode, so pick the
                // implementation mode before the surface is provided.
                val sharpen = requestedBinding.sharpenPreview
                binding.preview.implementationMode =
                    if (sharpen) PreviewView.ImplementationMode.COMPATIBLE
                    else PreviewView.ImplementationMode.PERFORMANCE

                val profile = requestedBinding.profile
                val config = cameraCaptureConfig(profile)
                val selector = CameraSelector.DEFAULT_BACK_CAMERA
                val zslSupported = try {
                    provider.getCameraInfo(selector).isZslSupported
                } catch (e: Exception) {
                    Log.w(CAMERA_LOG_TAG, "Could not query ZSL support", e)
                    false
                }
                val requestZsl = config.zslEligible && zslSupported

                var usedBindFallback = false
                var zslActiveExpected = requestZsl
                val bound = try {
                    bindCamera(
                        provider = provider,
                        selector = selector,
                        config = config,
                        requestZsl = requestZsl,
                        constrainResolution = true,
                    )
                } catch (primary: Exception) {
                    Log.w(
                        CAMERA_LOG_TAG,
                        "Profile bind failed; retrying safe CameraX configuration",
                        primary,
                    )
                    usedBindFallback = true
                    zslActiveExpected = false
                    try {
                        bindCamera(
                            provider = provider,
                            selector = selector,
                            config = config,
                            requestZsl = false,
                            constrainResolution = false,
                        )
                    } catch (fallback: Exception) {
                        fallback.addSuppressed(primary)
                        throw fallback
                    }
                }

                imageCapture = bound.capture
                boundCamera = bound.camera
                boundCameraConfig = requestedBinding
                cameraBindingInFlight = null
                applyPreviewSharpen(sharpen)
                applyTorchAndPersistDiagnostics(
                    requestedBinding = requestedBinding,
                    config = config,
                    capture = bound.capture,
                    camera = bound.camera,
                    zslSupported = zslSupported,
                    zslActive = zslActiveExpected,
                    usedBindFallback = usedBindFallback,
                )
                submitDeferredCaptureIfReady()
            } catch (e: Exception) {
                imageCapture = null
                boundCamera = null
                boundCameraConfig = null
                cameraBindingInFlight = null
                Log.e(CAMERA_LOG_TAG, "Camera bind failed with primary and fallback configs", e)
                val message = "Camera unavailable: ${e.message ?: e.javaClass.simpleName}"
                Prefs.setCameraDiagnostics(this, message)
                setStatus(message)
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun applyCameraPreferenceChanges() {
        if (!initialized || isDestroyed) return
        if (waitingForPriorCaptureWrites || session.hasActiveCaptureWrites()) return
        val desired = desiredCameraBindingSnapshot()
        val current = boundCameraConfig
        // Returning settings to the already-bound values must also invalidate
        // an older asynchronous request, or that stale request could rebind the
        // camera to values the user just changed away from.
        if (cameraBindingInFlight != null && cameraBindingInFlight != desired) {
            cameraBindingInFlight = null
        }

        // A torch toggle does not change either use case. Avoid a visible
        // preview interruption (and, critically, avoid unbinding mid-capture).
        if (current != null &&
            current.profile == desired.profile &&
            current.sharpenPreview == desired.sharpenPreview &&
            current.torchEnabled != desired.torchEnabled
        ) {
            applyTorchOnly(desired)
            return
        }

        if (desired == current || desired == cameraBindingInFlight) {
            return
        }
        if (captureQueue.busy ||
            !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
        ) {
            return
        }
        startCamera()
    }

    private fun applyTorchOnly(desired: CameraBindingSnapshot) {
        val camera = boundCamera ?: return
        val supported = camera.cameraInfo.hasFlashUnit()
        boundCameraConfig = desired
        if (!supported) {
            persistTorchDiagnostics(
                requested = desired.torchEnabled,
                supported = false,
                active = false,
            )
            if (desired.torchEnabled && canUpdateCaptureUi()) {
                setStatus("Continuous light is unavailable on this camera")
            }
            return
        }

        persistTorchDiagnostics(
            requested = desired.torchEnabled,
            supported = true,
            active = null,
        )
        val future = camera.cameraControl.enableTorch(desired.torchEnabled)
        future.addListener({
            if (boundCamera !== camera || boundCameraConfig != desired) return@addListener
            try {
                future.get()
                persistTorchDiagnostics(
                    requested = desired.torchEnabled,
                    supported = true,
                    active = desired.torchEnabled,
                )
            } catch (e: Exception) {
                Log.w(CAMERA_LOG_TAG, "Could not apply continuous light in place", e)
                // Keep the requested preference, but mark this binding snapshot
                // stale so a later resume can retry without unbinding now.
                boundCameraConfig = desired.copy(torchEnabled = !desired.torchEnabled)
                persistTorchDiagnostics(
                    requested = desired.torchEnabled,
                    supported = true,
                    active = !desired.torchEnabled,
                )
                if (desired.torchEnabled && canUpdateCaptureUi()) {
                    setStatus("Continuous light failed: ${e.message ?: e.javaClass.simpleName}")
                }
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun persistTorchDiagnostics(
        requested: Boolean,
        supported: Boolean,
        active: Boolean?,
    ) {
        val clause =
            "torch requested=$requested supported=$supported " +
                "active=${active?.toString() ?: "pending"}"
        val current = Prefs.cameraDiagnostics(this)
        val diagnostics = when {
            TORCH_DIAGNOSTICS.containsMatchIn(current) ->
                current.replace(TORCH_DIAGNOSTICS, clause)
            current.isBlank() -> clause
            else -> "$current; $clause"
        }
        Log.i(CAMERA_LOG_TAG, diagnostics)
        Prefs.setCameraDiagnostics(this, diagnostics)
    }

    @androidx.annotation.OptIn(markerClass = [ExperimentalZeroShutterLag::class])
    private fun bindCamera(
        provider: ProcessCameraProvider,
        selector: CameraSelector,
        config: CameraCaptureConfig,
        requestZsl: Boolean,
        constrainResolution: Boolean,
    ): BoundCameraUseCases {
        val preview = Preview.Builder().build().also {
            it.setSurfaceProvider(binding.preview.surfaceProvider)
        }
        val builder = ImageCapture.Builder()
            .setCaptureMode(
                if (requestZsl) ImageCapture.CAPTURE_MODE_ZERO_SHUTTER_LAG
                else ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY,
            )
                .setFlashMode(ImageCapture.FLASH_MODE_OFF)
            .setJpegQuality(config.jpegQuality)
        if (constrainResolution) {
            val fallbackRule = when (config.resolutionFallback) {
                CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER ->
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER
            }
            builder.setResolutionSelector(
                    ResolutionSelector.Builder()
                        .setResolutionStrategy(
                            ResolutionStrategy(
                            Size(config.targetWidth, config.targetHeight),
                            fallbackRule,
                        ),
                    )
                    .build(),
            )
        }
        val capture = builder.build()
        provider.unbindAll()
        val camera = provider.bindToLifecycle(this, selector, preview, capture)
        return BoundCameraUseCases(camera, capture)
    }

    private fun applyTorchAndPersistDiagnostics(
        requestedBinding: CameraBindingSnapshot,
        config: CameraCaptureConfig,
        capture: ImageCapture,
        camera: Camera,
        zslSupported: Boolean,
        zslActive: Boolean,
        usedBindFallback: Boolean,
    ) {
        val torchSupported = camera.cameraInfo.hasFlashUnit()
        if (!torchSupported) {
            logCameraDiagnostics(
                profile = requestedBinding.profile,
                config = config,
                capture = capture,
                zslSupported = zslSupported,
                zslActive = zslActive,
                usedBindFallback = usedBindFallback,
                torchRequested = requestedBinding.torchEnabled,
                torchSupported = false,
                torchActive = false,
            )
            if (requestedBinding.torchEnabled) {
                setStatus("Continuous light is unavailable on this camera")
            }
            return
        }

        logCameraDiagnostics(
            profile = requestedBinding.profile,
            config = config,
            capture = capture,
            zslSupported = zslSupported,
            zslActive = zslActive,
            usedBindFallback = usedBindFallback,
            torchRequested = requestedBinding.torchEnabled,
            torchSupported = true,
            torchActive = null,
        )
        val torchFuture = camera.cameraControl.enableTorch(requestedBinding.torchEnabled)
        torchFuture.addListener({
            if (boundCamera !== camera || boundCameraConfig != requestedBinding) return@addListener
            try {
                torchFuture.get()
                logCameraDiagnostics(
                    profile = requestedBinding.profile,
                    config = config,
                    capture = capture,
                    zslSupported = zslSupported,
                    zslActive = zslActive,
                    usedBindFallback = usedBindFallback,
                    torchRequested = requestedBinding.torchEnabled,
                    torchSupported = true,
                    torchActive = requestedBinding.torchEnabled,
                )
            } catch (e: Exception) {
                Log.w(CAMERA_LOG_TAG, "Could not apply continuous light", e)
                logCameraDiagnostics(
                    profile = requestedBinding.profile,
                    config = config,
                    capture = capture,
                    zslSupported = zslSupported,
                    zslActive = zslActive,
                    usedBindFallback = usedBindFallback,
                    torchRequested = requestedBinding.torchEnabled,
                    torchSupported = true,
                    torchActive = false,
                )
                if (requestedBinding.torchEnabled && canUpdateCaptureUi()) {
                    setStatus("Continuous light failed: ${e.message ?: e.javaClass.simpleName}")
                }
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun logCameraDiagnostics(
        profile: CameraCaptureProfile,
        config: CameraCaptureConfig,
        capture: ImageCapture,
        zslSupported: Boolean,
        zslActive: Boolean,
        usedBindFallback: Boolean,
        torchRequested: Boolean,
        torchSupported: Boolean,
        torchActive: Boolean?,
    ) {
        fun logResolution() {
            val resolved = capture.resolutionInfo?.resolution
            val resolvedText = resolved?.let { "${it.width}x${it.height}" } ?: "pending"
            val diagnostics =
                "Profile=${profile.name}; requested=${config.targetWidth}x${config.targetHeight}; " +
                    "resolved=$resolvedText; ZSL supported=$zslSupported active=$zslActive; " +
                    "torch requested=$torchRequested supported=$torchSupported " +
                    "active=${torchActive?.toString() ?: "pending"}; " +
                    "safe bind fallback=$usedBindFallback"
            Log.i(
                CAMERA_LOG_TAG,
                "$diagnostics fallback=${config.resolutionFallback} " +
                    "captureMode=${capture.captureMode} jpegQuality=${capture.jpegQuality}",
            )
            Prefs.setCameraDiagnostics(this, diagnostics)
        }
        logResolution()
        if (capture.resolutionInfo == null) binding.preview.post {
            if (!isDestroyed && imageCapture === capture) logResolution()
        }
    }

    /** Set or clear the AGSL sharpen RenderEffect on the preview (Android 13+). */
    private fun applyPreviewSharpen(on: Boolean) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        binding.preview.setRenderEffect(
            if (on) RenderEffect.createRuntimeShaderEffect(RuntimeShader(SHARPEN_AGSL), "content")
            else null)
    }

    // --- the command state machine --------------------------------------------

    private fun command(word: String, discardConfirmed: Boolean = false) {
        if (word == "cancel" && session.active && !discardConfirmed) {
            showDiscardConfirmation()
            return
        }
        // Sealing while an accepted shot is being exposed/written would lose
        // it. Done waits for the active and queued shots. Cancel drops the
        // not-yet-submitted shot, then deletes the entry after the active
        // callback releases its file.
        if (captureQueue.busy && (word == "done" || word == "cancel")) {
            pendingCommand = word
            Prefs.setPendingCaptureCommand(this, session.entryId, word)
            if (word == "cancel") cancelQueuedCapture()
            setStatus(
                if (word == "done") "Finishing accepted captures…"
                else "Cancelling after active capture…",
            )
            updateUi()
            return
        }
        when (word) {
            "start" -> {
                // Gated here rather than only on the Home button so the voice
                // word cannot open a book with no collection behind it.
                val collection = Collections.current(this)
                when {
                    session.active -> cues.error("entry already open")
                    collection == null -> {
                        cues.error("choose a collection first")
                        setStatus(getString(R.string.collections_choose_first))
                    }
                    else -> {
                        session.start(collection)
                        cues.started()
                    }
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
                            ProcessWorker.enqueuePending(this, id)
                            UploadWorker.enqueue(this)
                        }
                        // null + still active = the manifest write failed
                        // (disk full); the pages are kept and "done" can retry
                        session.active -> cues.error("could not save, still open")
                        else -> cues.error("no captures, entry dropped")
                    }
                }
            }
            "cancel" -> {
                val discarded = session.cancel()
                if (discarded != null) {
                    cues.cancelled()
                    showUndoDiscard(discarded)
                } else if (session.active) {
                    cues.error("could not discard, still open")
                } else {
                    cues.error("nothing to discard")
                }
            }
        }
        updateUi()
    }

    private fun showDiscardConfirmation() {
        if (discardDialog?.isShowing == true) return
        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.capture_discard_title)
            .setMessage(R.string.capture_discard_message)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.capture_discard_confirm) { _, _ ->
                command("cancel", discardConfirmed = true)
            }
            .create()
        discardDialog = dialog
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                .setTextColor(getColor(R.color.whl_red))
        }
        dialog.setOnDismissListener { if (discardDialog === dialog) discardDialog = null }
        dialog.show()
    }

    private fun takePhoto() {
        if (imageCapture == null) return cues.error("camera not ready")
        if (pendingCommand != null) return cues.error("capture is finishing")
        session.refreshPhotoCount()

        when (val acceptance = captureQueue.accept(session.photoCount + 1)) {
            is ShallowCaptureQueue.Acceptance.Started -> {
                if (!reserveAcceptedShot(acceptance.ticket)) {
                    captureQueue.finishActive(success = false)
                    cues.error("could not reserve capture")
                    setStatus("Capture error: could not reserve a file")
                    return
                }
                cues.photoHeard()
                setStatus("Capture accepted")
                updateUi()
                submitCapture(acceptance.ticket)
            }
            is ShallowCaptureQueue.Acceptance.Queued -> {
                if (!reserveAcceptedShot(acceptance.ticket)) {
                    captureQueue.cancelQueued()
                    cues.error("could not queue capture")
                    setStatus("Capture error: could not reserve a queued file")
                    return
                }
                cues.photoHeard()
                setStatus("Capture queued")
                updateUi()
            }
            ShallowCaptureQueue.Acceptance.Rejected -> {
                cues.error("capture queue full")
                setStatus("Capture queue full")
            }
        }
    }

    private fun reserveAcceptedShot(ticket: ShallowCaptureQueue.Ticket): Boolean {
        val reservation = session.reservePhoto(ticket.pageNumber) ?: return false
        acceptedShots[ticket.id] = AcceptedShot(
            reservation = reservation,
            acceptedAtNanos = SystemClock.elapsedRealtimeNanos(),
        )
        return true
    }

    private fun submitCapture(ticket: ShallowCaptureQueue.Ticket) {
        val shot = acceptedShots[ticket.id] ?: return failUnsubmittedCapture(
            ticket,
            "capture reservation was lost",
        )
        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
            deferredCaptureSubmission = true
            Log.i(
                CAMERA_LOG_TAG,
                "Deferred accepted capture page=${ticket.pageNumber} until camera lifecycle resumes",
            )
            return
        }
        val capture = imageCapture ?: return failUnsubmittedCapture(ticket, "camera not ready")
        deferredCaptureSubmission = false
        val reservation = shot.reservation
        val opts = ImageCapture.OutputFileOptions.Builder(reservation.tempFile).build()
        try {
            capture.takePicture(
                opts,
                ContextCompat.getMainExecutor(this),
                object : ImageCapture.OnImageSavedCallback {
                    override fun onCaptureStarted() {
                        handleCaptureStarted(ticket, reservation)
                    }

                    override fun onImageSaved(results: ImageCapture.OutputFileResults) {
                        handleCaptureSaved(ticket, reservation)
                    }

                    override fun onError(e: ImageCaptureException) {
                        handleCaptureError(ticket, reservation, e)
                    }
                },
            )
        } catch (e: Exception) {
            handleCaptureError(ticket, reservation, e)
        }
    }

    private fun handleCaptureStarted(
        ticket: ShallowCaptureQueue.Ticket,
        reservation: CaptureSession.PhotoReservation,
    ) {
        val shot = acceptedShots[ticket.id]
        if (captureQueue.active?.id != ticket.id || shot?.reservation != reservation) return
        val started = SystemClock.elapsedRealtimeNanos()
        shot.startedAtNanos = started
        Log.i(
            CAMERA_LOG_TAG,
            "capture page=${ticket.pageNumber} acceptedToStartedMs=" +
                nanosToMillis(started - shot.acceptedAtNanos),
        )
        if (canUpdateCaptureUi()) {
            cues.photoStarted()
            shutterFlash()
            setStatus("Capture started")
        }
    }

    private fun handleCaptureSaved(
        ticket: ShallowCaptureQueue.Ticket,
        reservation: CaptureSession.PhotoReservation,
    ) {
        val shot = acceptedShots[ticket.id]
        if (captureQueue.active?.id != ticket.id || shot?.reservation != reservation) {
            session.abortPhoto(reservation)
            return
        }
        if (!session.commitPhoto(reservation)) {
            handleCaptureError(
                ticket,
                reservation,
                IllegalStateException("could not finalize capture file"),
            )
            return
        }

        val saved = SystemClock.elapsedRealtimeNanos()
        Log.i(
            CAMERA_LOG_TAG,
            "capture page=${ticket.pageNumber} startedToSavedMs=" +
                nanosToMillis(saved - (shot.startedAtNanos ?: shot.acceptedAtNanos)),
        )
        if (canUpdateCaptureUi()) {
            setStatus("Capture saved")
            addThumbnail(reservation.finalFile.absolutePath)
        }
        // Standardize + OCR while the user flips to the next page. Thumbnail
        // decoding also remains outside the CameraX callback's synchronous work.
        ProcessWorker.enqueue(this)
        completeCapture(ticket, success = true)
    }

    private fun handleCaptureError(
        ticket: ShallowCaptureQueue.Ticket,
        reservation: CaptureSession.PhotoReservation,
        error: Exception,
    ) {
        session.abortPhoto(reservation)
        if (captureQueue.active?.id != ticket.id) return
        Log.e(CAMERA_LOG_TAG, "Capture page=${ticket.pageNumber} failed", error)
        if (canUpdateCaptureUi()) {
            cues.error("capture failed")
            setStatus("Capture error: ${error.message ?: error.javaClass.simpleName}")
        }
        completeCapture(ticket, success = false)
    }

    private fun completeCapture(ticket: ShallowCaptureQueue.Ticket, success: Boolean) {
        if (captureQueue.active?.id != ticket.id) return
        acceptedShots.remove(ticket.id)
        val next = captureQueue.finishActive(success).next
        if (next == null) {
            deferredCaptureSubmission = false
            if (canUpdateCaptureUi()) updateUi()
            applyCameraPreferenceChanges()
            runPending()
            finishAfterAcceptedCapturesIfReady()
            return
        }

        val waiting = acceptedShots[next.id]
        if (waiting == null) {
            failUnsubmittedCapture(next, "queued capture reservation was lost")
            return
        }
        if (waiting.reservation.pageNumber != next.pageNumber) {
            session.abortPhoto(waiting.reservation)
            val compacted = session.reservePhoto(next.pageNumber)
            if (compacted == null) {
                failUnsubmittedCapture(next, "could not compact queued capture")
                return
            }
            waiting.reservation = compacted
        }
        if (isDestroyed) {
            failUnsubmittedCapture(next, "camera activity was destroyed before queued capture")
            return
        }
        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
            if (pendingCommand == "done") {
                deferredCaptureSubmission = true
                Log.i(
                    CAMERA_LOG_TAG,
                    "Keeping accepted page=${next.pageNumber} for deferred Done submission",
                )
                return
            }
            failUnsubmittedCapture(next, "camera lifecycle stopped before queued capture")
            return
        }
        submitCapture(next)
    }

    private fun submitDeferredCaptureIfReady() {
        if (!deferredCaptureSubmission || isDestroyed ||
            !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED) ||
            imageCapture == null
        ) return
        val ticket = captureQueue.active ?: run {
            deferredCaptureSubmission = false
            runPending()
            return
        }
        if (!acceptedShots.containsKey(ticket.id)) {
            deferredCaptureSubmission = false
            failUnsubmittedCapture(ticket, "deferred capture reservation was lost")
            return
        }
        deferredCaptureSubmission = false
        Log.i(CAMERA_LOG_TAG, "Submitting deferred capture page=${ticket.pageNumber}")
        submitCapture(ticket)
    }

    private fun failUnsubmittedCapture(ticket: ShallowCaptureQueue.Ticket, message: String) {
        if (captureQueue.active?.id == ticket.id) deferredCaptureSubmission = false
        acceptedShots.remove(ticket.id)?.let { session.abortPhoto(it.reservation) }
        if (captureQueue.active?.id == ticket.id) captureQueue.finishActive(success = false)
        Log.w(CAMERA_LOG_TAG, "Capture page=${ticket.pageNumber} not submitted: $message")
        if (canUpdateCaptureUi()) {
            cues.error(message)
            setStatus("Capture error: $message")
            updateUi()
        }
        applyCameraPreferenceChanges()
        runPending()
        finishAfterAcceptedCapturesIfReady()
    }

    private fun cancelQueuedCapture() {
        val ticket = captureQueue.cancelQueued() ?: return
        acceptedShots.remove(ticket.id)?.let { session.abortPhoto(it.reservation) }
        Log.i(CAMERA_LOG_TAG, "Cancelled queued capture page=${ticket.pageNumber}")
    }

    private fun discardAllCaptureRequests() {
        captureQueue.cancelAll().forEach { ticket ->
            acceptedShots.remove(ticket.id)?.let { session.abortPhoto(it.reservation) }
        }
        acceptedShots.values.forEach { session.abortPhoto(it.reservation) }
        acceptedShots.clear()
        pendingCommand = null
        Prefs.clearPendingCaptureCommand(this, session.entryId)
        deferredCaptureSubmission = false
    }

    private fun canUpdateCaptureUi(): Boolean =
        !isDestroyed && !isFinishing && lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    private fun nanosToMillis(nanos: Long): String = "%.1f".format(nanos / 1_000_000.0)

    private fun runPending() {
        if (captureQueue.busy || session.hasActiveCaptureWrites() || isDestroyed) return
        val cmd = pendingCommand ?: return
        val pendingEntry = session.entryId
        pendingCommand = null
        command(cmd, discardConfirmed = cmd == "cancel")
        Prefs.clearPendingCaptureCommand(this, pendingEntry)
    }

    private fun finishAfterAcceptedCapturesIfReady() {
        if (!finishAfterAcceptedCaptures || captureQueue.busy || pendingCommand != null) return
        finishAfterAcceptedCaptures = false
        finish()
    }

    /** After a confirmed discard, offer an UNDO that pulls the entry back out
     *  of the trash. Undo is unavailable once another entry is open because
     *  restoreFromTrash deliberately refuses to clobber it. */
    private fun showUndoDiscard(entryId: String) {
        Snackbar.make(binding.root, getString(R.string.discarded), Snackbar.LENGTH_LONG)
            .setAction(getString(R.string.undo)) {
                if (session.restoreFromTrash(entryId)) {
                    restoreThumbnailsIfNeeded()
                    updateUi()
                } else {
                    setStatus(getString(R.string.undo_unavailable))
                }
            }
            .show()
    }

    // --- UI ---------------------------------------------------------------------

    /** A quick white flash over the preview to mark the shutter. */
    private fun shutterFlash() {
        val v = binding.shutterFlash
        v.animate().cancel()
        v.alpha = 0.6f
        v.animate().alpha(0f).setDuration(180).start()
    }

    /** Reserve the thumbnail's slot synchronously (so pages stay in capture
     *  order), then decode the JPEG off the UI thread and fill it in — the
     *  capture callback never blocks on a bitmap decode. */
    private fun addThumbnail(path: String) {
        val h = resources.getDimensionPixelSize(R.dimen.capture_thumbnail_height)
        val gap = resources.getDimensionPixelSize(R.dimen.capture_thumbnail_gap)
        val iv = ImageView(this)
        iv.layoutParams = LinearLayout.LayoutParams(h, h).apply { marginEnd = gap }
        iv.scaleType = ImageView.ScaleType.CENTER_CROP
        binding.thumbs.addView(iv)
        lifecycleScope.launch(Dispatchers.IO) {
            val opts = BitmapFactory.Options().apply { inSampleSize = 8 }
            val bmp = BitmapFactory.decodeFile(path, opts)
            withContext(Dispatchers.Main) {
                if (bmp == null) { binding.thumbs.removeView(iv); return@withContext }
                val w = h * bmp.width / bmp.height.coerceAtLeast(1)
                iv.layoutParams = LinearLayout.LayoutParams(w, h).apply { marginEnd = gap }
                iv.setImageBitmap(bmp)
            }
        }
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
        binding.btnPhoto.isEnabled = active && !captureQueue.full && pendingCommand == null
        binding.btnDone.isEnabled = active
        binding.btnCancel.isEnabled = active
        binding.btnSettings.isEnabled = !captureQueue.busy
        binding.configWarning.isEnabled = !captureQueue.busy
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
            setStatus(resources.getQuantityString(
                R.plurals.uploads_stuck, pending, pending, err))
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
                listOf(
                    e.author,
                    e.year,
                    resources.getQuantityString(
                        R.plurals.capture_count, e.photoCount, e.photoCount))
                    .filter { it.isNotEmpty() }.joinToString(" · ")
            val state = Entries.statusLabel(this, e)
            row.findViewById<TextView>(R.id.state).text = state
            row.findViewById<View>(R.id.marker).setBackgroundColor(getColor(markerColor(state)))
            row.setOnClickListener {
                startActivity(Intent(this, EntryDetailActivity::class.java)
                    .putExtra(EntryDetailActivity.EXTRA_ID, e.id))
            }
            list.addView(row)
        }
    }

    private fun markerColor(state: String): Int = when {
        state.startsWith("capturing") -> R.color.whl_green
        state == "failed" -> R.color.whl_red
        state == "waiting" || state == "processing" || state == "partial" ||
            state.endsWith("pending upload") || state.endsWith("pending delivery") ||
            state.endsWith("claim for cloud") -> R.color.whl_amber
        state.endsWith("different account") -> R.color.whl_red
        state.endsWith("uploaded") -> R.color.whl_blue
        state.endsWith("imported") -> R.color.whl_cyan
        else -> R.color.whl_face_sh2
    }
}
