package org.whl.bookcapture

import android.Manifest
import android.animation.LayoutTransition
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.RenderEffect
import android.graphics.RuntimeShader
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.util.Size
import android.view.GestureDetector
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.Surface
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.RadioGroup
import android.widget.SeekBar
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalZeroShutterLag
import androidx.camera.core.FocusMeteringAction
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
import com.google.android.material.checkbox.MaterialCheckBox
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityMainBinding
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors

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
private val CAPTURE_COMMIT_EXECUTOR = Executors.newSingleThreadExecutor { task ->
    Thread(task, "whl-capture-commit")
}

/**
 * Hands-free book capture:
 *
 *   say "start"  — begin a book entry        (or tap ▶)
 *   say "photo"  — photograph the shown page (●)
 *   say "done"   — seal the entry locally    (✓)
 *   say "cancel" — void the entry            (✕)
 *   say "edit"   — reopen the last local scan
 *
 * The camera preview fills the screen under a thin CAD-style chrome: the
 * entry state ("OPEN (3)") lives in the top bar, captured pages run as a
 * thumbnail strip, and the last submitted book remains above the controls
 * until the next book is sealed for processing.
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
    private var extraFieldsDialog: AlertDialog? = null
    private var cameraSettingsDialog: AlertDialog? = null
    private var lastBookPreviewJob: Job? = null
    private var lastBookPreviewRefreshPending = false
    private var lastBookPreviewFingerprint: String? = null
    private var lastBookPreviewBitmap: android.graphics.Bitmap? = null
    private var backgroundRefreshJob: Job? = null
    private val thumbnailDecodeGate = Semaphore(permits = 2)
    private val thumbnailBitmaps = linkedMapOf<ImageView, android.graphics.Bitmap>()
    private val captureQueue = ShallowCaptureQueue()
    private val acceptedShots = mutableMapOf<Long, AcceptedShot>()
    private var pendingCommand: String? = null   // terminal voice command accepted mid-shot
    private var pendingUndoTargetPage: Int? = null
    private var deferredCaptureSubmission = false
    private var finishAfterAcceptedCaptures = false
    private var waitingForPriorCaptureWrites = false
    private var notePermissionRequested = false
    private var voiceNoteDraft: StructuredNote? = null
    private var voiceNoteId: String? = null
    private var voiceNoteStartedAt = 0L
    private var voiceNoteFinalizing = false
    private var voiceNoteDiscardPending = false
    private var voiceNoteTranscriber: MistralRealtimeTranscriber? = null
    private var voiceNoteGeneration = 0L
    private var captureMutationInFlight = false

    private data class AcceptedShot(
        var reservation: CaptureSession.PhotoReservation,
        val acceptedAtNanos: Long,
        var startedAtNanos: Long? = null,
    )

    private data class CameraBindingSnapshot(
        val profile: CameraCaptureProfile,
        val sharpenPreview: Boolean,
        val torchEnabled: Boolean,
        val orientation: CameraCaptureOrientation,
    )

    private data class BoundCameraUseCases(
        val camera: Camera,
        val capture: ImageCapture,
    )

    private data class LastBookPreviewLoad(
        val entry: Entries.Entry?,
        val bitmap: android.graphics.Bitmap?,
        val fingerprint: String?,
        val thumbnailChanged: Boolean,
    )

    private sealed interface CaptureUndoOutcome {
        data object NoteDiscarded : CaptureUndoOutcome
        data class Photo(val result: LastCommittedPhotoUndoResult) : CaptureUndoOutcome
        data object Empty : CaptureUndoOutcome
        data object Failed : CaptureUndoOutcome
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (captureQueue.busy || captureMutationInFlight) {
                    finishAfterAcceptedCaptures = true
                    setStatus(RemoteUiCatalog.text(
                        this@MainActivity,
                        if (captureMutationInFlight) R.string.capture_finishing_change
                        else R.string.capture_finishing_photos,
                    ))
                    updateUi()
                } else {
                    finish()
                }
            }
        })
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        session = CaptureSession(this)
        pendingCommand = Prefs.pendingCaptureCommand(this, session.entryId)
        pendingUndoTargetPage = Prefs.pendingCaptureTargetPage(this, session.entryId)
        cues = AudioCues(this)
        binding.thumbs.layoutTransition = LayoutTransition()   // pages land, not pop

        binding.btnStart.setOnClickListener { command("start") }
        binding.btnPhoto.setOnClickListener { command("photo") }
        binding.btnDone.setOnClickListener { command("done") }
        binding.btnCancel.setOnClickListener { command("cancel") }
        binding.btnNote.setOnClickListener {
            if (voiceNoteDraft == null) requestStartVoiceNote()
            else finishVoiceNote(save = !voiceNoteDiscardPending)
        }
        binding.btnCameraSettings.setOnClickListener { showCameraSettings() }
        binding.btnCaptureOrientation.setOnClickListener {
            val current = cameraCaptureOrientation(Prefs.cameraCaptureOrientation(this))
            val next = when (current) {
                CameraCaptureOrientation.PORTRAIT -> CameraCaptureOrientation.LANDSCAPE
                CameraCaptureOrientation.LANDSCAPE -> CameraCaptureOrientation.PORTRAIT
            }
            Prefs.setCameraCaptureOrientation(this, next)
            updateCaptureOrientationUi()
            applyCameraPreferenceChanges()
            setStatus(getString(
                if (next == CameraCaptureOrientation.PORTRAIT)
                    R.string.camera_orientation_portrait_status
                else R.string.camera_orientation_landscape_status,
            ))
        }
        val focusGesture = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(event: MotionEvent): Boolean = true

            override fun onSingleTapUp(event: MotionEvent): Boolean {
                focusAtPreviewPoint(event.x, event.y, announce = true)
                return true
            }
        })
        binding.preview.setOnTouchListener { _, event -> focusGesture.onTouchEvent(event) }
        updateCaptureOrientationUi()
        binding.configWarning.setOnClickListener {
            startActivity(Intent(this, LoginActivity::class.java))
        }
        // Background work landing (OCR done, upload done) refreshes the
        // persistent previous-book preview in place.
        WorkManager.getInstance(this)
            .getWorkInfosLiveData(activeUniqueWorkQuery(
                ProcessWorker.UNIQUE_WORK_NAME,
                ProcessWorker.BACKLOG_WORK_NAME,
                "capture-upload",
            ))
            .observe(this) { scheduleBackgroundRefresh() }
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
        refreshLastCapturedBook()
    }

    /** After a config change / process death, CaptureSession re-adopts the open
     *  entry but the thumbnail strip (view state) is gone — repaint it from the
     *  photos on disk so "OPEN (3)" still shows three pages. */
    private fun restoreThumbnailsIfNeeded() {
        session.refreshPhotoCount()
        if (binding.thumbs.childCount >= session.photoCount) return
        renderCurrentThumbnails()
    }

    private fun renderCurrentThumbnails() {
        val id = session.entryId
        clearThumbnailStrip()
        if (id == null) return
        session.refreshPhotoCount()
        session.entryDir(id).listFiles { f -> f.isFile && f.name.matches(PHOTO_NAME) }
            ?.sortedBy { photoNumber(it.name) }
            ?.forEach { addThumbnail(it.absolutePath) }
    }

    override fun onPause() {
        if (voiceNoteDraft != null) {
            finishVoiceNote(
                save = true,
                successMessage = getString(R.string.capture_note_interrupted),
            )
        }
        super.onPause()
        voice?.setPaused(true)
    }

    override fun onStop() {
        // Normally an unsubmitted request should not fire after the user leaves.
        // Done is different: it explicitly promises to finish every accepted
        // capture, so keep its one queued reservation for the next resume.
        if (pendingCommand != "done") cancelQueuedCapture()
        backgroundRefreshJob?.cancel()
        backgroundRefreshJob = null
        super.onStop()
    }

    override fun onDestroy() {
        // Never delete an output still owned by a CameraX callback. Back is
        // deferred until the queue drains; Activity recreation is covered by
        // CaptureSession's process-local temporary-file ownership registry.
        abortUnsubmittedDeferredCapture()
        if (!captureQueue.busy) discardAllCaptureRequests()
        extraFieldsDialog?.dismiss()
        extraFieldsDialog = null
        cameraSettingsDialog?.dismiss()
        cameraSettingsDialog = null
        lastBookPreviewRefreshPending = false
        lastBookPreviewJob?.cancel()
        lastBookPreviewJob = null
        binding.lastBookThumb.setImageDrawable(null)
        lastBookPreviewBitmap?.takeIf { !it.isRecycled }?.recycle()
        lastBookPreviewBitmap = null
        clearThumbnailStrip()
        boundCamera = null
        boundCameraConfig = null
        cameraBindingInFlight = null
        if (!captureQueue.busy) deferredCaptureSubmission = false
        // Home, Back, and configuration changes can destroy this Activity
        // before Mistral releases its target-delay buffer. Keep only that
        // bounded (two-second) drain alive; its callback persists the note but
        // skips dead-Activity UI work. Every non-draining transcriber is still
        // stopped immediately below.
        if (!voiceNoteFinalizing || voiceNoteTranscriber == null) {
            voiceNoteGeneration += 1
            voiceNoteTranscriber?.stop()
            voiceNoteTranscriber = null
            voiceNoteDraft = null
            voiceNoteId = null
            voiceNoteFinalizing = false
            voiceNoteDiscardPending = false
        }
        super.onDestroy()
        voice?.stop()
        cues.shutdown()
    }

    /** A queued ticket promoted while the Activity is stopped may deliberately
     * remain unsubmitted. Unlike a CameraX-owned output, that reservation is
     * safe to abort on recreation; leaving it in the process registry would
     * make the replacement Activity wait forever for a callback that cannot
     * occur. The durable terminal command is preserved and reconciles next. */
    private fun abortUnsubmittedDeferredCapture() {
        if (!deferredCaptureSubmission) return
        captureQueue.cancelAll().forEach { ticket ->
            acceptedShots.remove(ticket.id)?.let { session.abortPhoto(it.reservation) }
        }
        acceptedShots.values.forEach { session.abortPhoto(it.reservation) }
        acceptedShots.clear()
        deferredCaptureSubmission = false
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
                val requestedForNote = notePermissionRequested
                val requestedForVoice = voicePermissionRequestedForEnablement
                notePermissionRequested = false
                voicePermissionRequestedForEnablement = false
                if (voicePermissionGranted()) {
                    if (requestedForVoice && Prefs.voiceEnabled(this)) startVoice()
                    if (requestedForNote) startVoiceNote()
                } else if (requestedForNote) {
                    setStatus(getString(R.string.capture_note_permission_denied))
                } else if (requestedForVoice) {
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
                if (!notePermissionRequested) requestMicrophonePermission()
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

    private fun requestStartVoiceNote() {
        if (voiceNoteDraft != null) return
        when {
            !session.active -> {
                cues.error("no entry open")
                setStatus(getString(R.string.capture_note_requires_entry))
            }
            captureQueue.busy || captureMutationInFlight || pendingCommand != null -> {
                cues.error("capture is busy")
                setStatus(getString(R.string.capture_note_wait_for_capture))
            }
            Prefs.mistralKey(this).isBlank() -> {
                cues.error("Mistral key required")
                setStatus(getString(R.string.capture_note_requires_key))
            }
            !voicePermissionGranted() -> {
                notePermissionRequested = true
                if (!voicePermissionRequestedForEnablement) requestMicrophonePermission()
            }
            else -> startVoiceNote()
        }
        updateUi()
    }

    /** Mistral and Vosk cannot own Android's microphone at the same time. The
     * offline command listener is paused for the duration of the cloud note;
     * trailing note commands are recognized from Mistral's settled deltas. */
    private fun startVoiceNote() {
        if (voiceNoteDraft != null || !session.active || !voicePermissionGranted() ||
            captureQueue.busy || captureMutationInFlight || pendingCommand != null
        ) return
        val apiKey = Prefs.mistralKey(this)
        if (apiKey.isBlank()) {
            setStatus(getString(R.string.capture_note_requires_key))
            return
        }

        voice?.setPaused(true)
        val generation = ++voiceNoteGeneration
        voiceNoteId = UUID.randomUUID().toString()
        voiceNoteStartedAt = System.currentTimeMillis()
        voiceNoteFinalizing = false
        voiceNoteDiscardPending = false
        voiceNoteDraft = StructuredNote.inProgress()
        binding.noteStatus.setText(R.string.capture_note_connecting)
        renderVoiceNote()

        val transcriber = MistralRealtimeTranscriber(
            context = applicationContext,
            apiKey = apiKey,
            listener = object : MistralRealtimeTranscriber.Listener {
                override fun onConnected() = runOnUiThread {
                    if (generation != voiceNoteGeneration || voiceNoteDraft == null ||
                        voiceNoteFinalizing
                    ) return@runOnUiThread
                    binding.noteStatus.setText(R.string.capture_note_listening)
                }

                override fun onTranscript(text: String, final: Boolean) = runOnUiThread {
                    if (generation != voiceNoteGeneration || voiceNoteDraft == null) return@runOnUiThread
                    handleVoiceNoteTranscript(text)
                }

                override fun onError(message: String) = runOnUiThread {
                    if (generation != voiceNoteGeneration || voiceNoteDraft == null) return@runOnUiThread
                    val hasTranscript = voiceNoteDraft?.transcript?.isNotBlank() == true
                    finishVoiceNote(
                        save = hasTranscript,
                        successMessage = message,
                        drain = false,
                    )
                }
            },
        )
        voiceNoteTranscriber = transcriber
        if (!transcriber.start()) {
            voiceNoteGeneration += 1
            voiceNoteTranscriber = null
            voiceNoteDraft = null
            voiceNoteId = null
            voiceNoteStartedAt = 0L
            voiceNoteFinalizing = false
            voiceNoteDiscardPending = false
            renderVoiceNote()
            resumeOfflineVoiceAfterNote()
            setStatus(getString(R.string.capture_note_start_failed))
        }
        updateUi()
    }

    private fun handleVoiceNoteTranscript(transcript: String) {
        val draft = voiceNoteDraft ?: return
        if (voiceNoteFinalizing) {
            voiceNoteDraft = draft.updateTranscript(transcript)
            checkpointVoiceNote(checkNotNull(voiceNoteDraft))
            renderVoiceNote()
            return
        }
        val policy = StateAwareVoiceCommandPolicy.evaluate(
            transcript = transcript,
            state = VoiceCommandState.NOTE_ACTIVE,
            // Realtime text deltas are append-only settled text. Treating them
            // as final lets a trailing "end notes" stop the same stream that is
            // currently using the microphone.
            stability = VoiceRecognitionStability.FINAL,
        )
        when (policy?.command) {
            PolicyVoiceCommand.END_NOTES -> finishVoiceNote(
                save = true,
                transcriptOverride = policy.transcriptBeforeCommand,
                drain = false,
            )
            PolicyVoiceCommand.UNDO -> finishVoiceNote(
                save = false,
                transcriptOverride = policy.transcriptBeforeCommand,
                successMessage = getString(R.string.capture_note_discarded),
            )
            PolicyVoiceCommand.RESTART -> {
                finishVoiceNote(
                    save = false,
                    transcriptOverride = policy.transcriptBeforeCommand,
                    successMessage = null,
                )
                command("restart")
            }
            else -> {
                voiceNoteDraft = draft.updateTranscript(transcript)
                checkpointVoiceNote(checkNotNull(voiceNoteDraft))
                renderVoiceNote()
            }
        }
    }

    /** Stop microphone ownership before publishing the note. Button and
     * lifecycle stops drain Mistral's target-delay buffer; settled voice
     * commands and errors can finalize their supplied transcript directly. */
    private fun finishVoiceNote(
        save: Boolean,
        transcriptOverride: String? = null,
        successMessage: String? = null,
        drain: Boolean = true,
    ) {
        val draft = voiceNoteDraft ?: return
        val transcriber = voiceNoteTranscriber
        // Once a user has requested discard, no lifecycle or button path may
        // reinterpret that draft as accepted metadata. A failed removal stays
        // in retry-discard mode until its exact checkpoint is gone.
        val shouldSave = save && !voiceNoteDiscardPending
        if (shouldSave && drain && transcriptOverride == null && transcriber != null) {
            if (voiceNoteFinalizing) return
            voiceNoteFinalizing = true
            checkpointVoiceNote(draft)
            binding.noteStatus.setText(R.string.capture_note_finalizing)
            updateUi()
            val generation = voiceNoteGeneration
            transcriber.finish { finalTranscript ->
                runOnUiThread {
                    if (generation != voiceNoteGeneration || voiceNoteDraft == null) {
                        return@runOnUiThread
                    }
                    val refined = finalTranscript.takeIf { it.isNotBlank() }
                        ?: voiceNoteDraft?.transcript.orEmpty()
                    voiceNoteDraft = voiceNoteDraft?.updateTranscript(refined)
                    completeVoiceNoteNow(save = true, successMessage = successMessage)
                }
            }
            return
        }
        completeVoiceNoteNow(shouldSave, transcriptOverride, successMessage)
    }

    private fun checkpointVoiceNote(note: StructuredNote) {
        if (note.transcript.isBlank() || note.isCompleted) return
        val noteId = voiceNoteId
        val entryId = session.entryId
        if (noteId == null || entryId == null) return
        runCatching {
            CaptureNotes.save(
                dir = session.entryDir(entryId),
                noteId = noteId,
                note = note,
                startedAtMs = voiceNoteStartedAt,
                updatedAtMs = System.currentTimeMillis(),
                provider = "mistral",
                model = MISTRAL_REALTIME_TRANSCRIPTION_MODEL,
            )
        }.onFailure { error ->
            Log.w(CAMERA_LOG_TAG, "Could not checkpoint voice note", error)
        }
    }

    /** Complete exactly once. Advancing the generation before closing means a
     * WebSocket callback already queued on the main looper cannot rewrite an
     * accepted snapshot. A failed disk write keeps the draft visible for retry. */
    private fun completeVoiceNoteNow(
        save: Boolean,
        transcriptOverride: String? = null,
        successMessage: String? = null,
    ) {
        val draft = voiceNoteDraft ?: return
        val noteId = voiceNoteId
        val entryId = session.entryId
        voiceNoteGeneration += 1
        voiceNoteTranscriber?.stop()
        voiceNoteTranscriber = null
        voiceNoteFinalizing = false

        val completed = draft.complete(transcriptOverride ?: draft.transcript)
        var saved = false
        var saveFailed = false
        var discardFailed = false
        if (save && completed.transcript.isNotBlank()) {
            if (noteId == null || entryId == null) {
                saveFailed = true
            } else {
                saved = runCatching {
                    CaptureNotes.save(
                        dir = session.entryDir(entryId),
                        noteId = noteId,
                        note = completed,
                        startedAtMs = voiceNoteStartedAt,
                        updatedAtMs = System.currentTimeMillis(),
                        provider = "mistral",
                        model = MISTRAL_REALTIME_TRANSCRIPTION_MODEL,
                    )
                }.isSuccess
                saveFailed = !saved
            }
        }

        if (!save && noteId != null && entryId != null) {
            runCatching {
                CaptureNotes.remove(session.entryDir(entryId), noteId)
            }.onFailure { error ->
                discardFailed = true
                Log.w(CAMERA_LOG_TAG, "Could not remove discarded note checkpoint", error)
            }
        }

        if (saveFailed || discardFailed) {
            voiceNoteDraft = completed
            voiceNoteDiscardPending = discardFailed
            if (!isDestroyed) {
                renderVoiceNote()
                setStatus(
                    getString(
                        if (saveFailed) R.string.capture_note_save_failed
                        else R.string.capture_note_discard_failed,
                    ),
                )
                if (discardFailed) resumeOfflineVoiceAfterNote()
                updateUi()
            }
            return
        }

        voiceNoteDraft = null
        voiceNoteId = null
        voiceNoteStartedAt = 0L
        voiceNoteDiscardPending = false
        if (!isDestroyed) {
            renderVoiceNote()
            resumeOfflineVoiceAfterNote()
            when {
                successMessage != null -> setStatus(successMessage)
                saved -> setStatus(getString(R.string.capture_note_saved))
                save -> setStatus(getString(R.string.capture_note_empty))
                else -> setStatus(getString(R.string.capture_note_discarded))
            }
            updateUi()
        }
    }

    private fun resumeOfflineVoiceAfterNote() {
        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED) ||
            !Prefs.voiceEnabled(this) || !voicePermissionGranted()
        ) return
        if (voice == null) startVoice() else voice?.setPaused(false)
    }

    private fun renderVoiceNote() {
        val note = voiceNoteDraft
        val active = note != null
        binding.noteOverlay.visibility = if (active) View.VISIBLE else View.GONE
        binding.btnNote.isActivated = active
        binding.btnNote.isSelected = active
        binding.btnNote.contentDescription = getString(
            when {
                voiceNoteDiscardPending -> R.string.capture_note_retry_discard
                active -> R.string.capture_note_end
                else -> R.string.capture_note_start
            },
        )
        binding.btnNote.backgroundTintList = ColorStateList.valueOf(
            getColor(if (active) R.color.whl_red else R.color.whl_green),
        )
        binding.noteRows.removeAllViews()
        if (note == null) {
            binding.noteUnclassified.visibility = View.GONE
            binding.noteUnclassified.text = ""
            return
        }

        binding.noteUnclassified.visibility =
            if (note.unclassifiedText.isBlank()) View.GONE else View.VISIBLE
        binding.noteUnclassified.text = getString(
            R.string.capture_note_unclassified,
            note.unclassifiedText,
        )
        note.rows.forEach { noteRow ->
            val row = LayoutInflater.from(this)
                .inflate(R.layout.item_capture_note_row, binding.noteRows, false)
            val field = row.findViewById<TextView>(R.id.noteField)
            val value = row.findViewById<TextView>(R.id.noteValue)
            field.setText(noteFieldLabel(noteRow.field))
            field.backgroundTintList = ColorStateList.valueOf(getColor(noteFieldColor(noteRow.field)))
            value.text = noteRow.value.ifBlank { getString(R.string.capture_note_value_pending) }
            row.contentDescription = "${field.text}: ${value.text}"
            RemoteUiCatalog.apply(row)
            binding.noteRows.addView(row)
        }
    }

    private fun noteFieldLabel(field: StructuredNoteField): Int = when (field) {
        StructuredNoteField.PRICE -> R.string.capture_note_price
        StructuredNoteField.PAGES -> R.string.capture_note_pages
        StructuredNoteField.CONDITION -> R.string.capture_note_condition
        StructuredNoteField.ILLUSTRATIONS -> R.string.capture_note_illustrations
        StructuredNoteField.REMARK -> R.string.capture_note_remark
    }

    private fun noteFieldColor(field: StructuredNoteField): Int = when (field) {
        StructuredNoteField.PRICE -> R.color.whl_green
        StructuredNoteField.PAGES -> R.color.whl_cyan
        StructuredNoteField.CONDITION -> R.color.whl_amber
        StructuredNoteField.ILLUSTRATIONS -> R.color.whl_blue2
        StructuredNoteField.REMARK -> R.color.whl_red
    }

    private fun desiredCameraBindingSnapshot(): CameraBindingSnapshot = CameraBindingSnapshot(
        profile = cameraCaptureProfile(Prefs.cameraProfile(this)),
        sharpenPreview = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            Prefs.sharpenPreview(this),
        torchEnabled = Prefs.torchEnabled(this),
        orientation = cameraCaptureOrientation(Prefs.cameraCaptureOrientation(this)),
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
                        targetRotation = captureTargetRotation(requestedBinding.orientation),
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
                            targetRotation = captureTargetRotation(requestedBinding.orientation),
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
                applyStoredCameraControls(bound.camera)
                applyTorchAndPersistDiagnostics(
                    requestedBinding = requestedBinding,
                    config = config,
                    capture = bound.capture,
                    camera = bound.camera,
                    zslSupported = zslSupported,
                    zslActive = zslActiveExpected,
                    usedBindFallback = usedBindFallback,
                )
                updateCaptureOrientationUi()
                updateUi()
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
        targetRotation: Int,
    ): BoundCameraUseCases {
        val preview = Preview.Builder()
            .setTargetRotation(targetRotation)
            .build().also {
            it.setSurfaceProvider(binding.preview.surfaceProvider)
        }
        val builder = ImageCapture.Builder()
            .setCaptureMode(
                if (requestZsl) ImageCapture.CAPTURE_MODE_ZERO_SHUTTER_LAG
                else ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY,
            )
                .setFlashMode(ImageCapture.FLASH_MODE_OFF)
            .setTargetRotation(targetRotation)
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

    private fun captureTargetRotation(orientation: CameraCaptureOrientation): Int =
        when (orientation) {
            CameraCaptureOrientation.PORTRAIT -> Surface.ROTATION_0
            CameraCaptureOrientation.LANDSCAPE -> Surface.ROTATION_90
        }

    private fun updateCaptureOrientationUi() {
        val orientation = cameraCaptureOrientation(Prefs.cameraCaptureOrientation(this))
        binding.pageMarginOverlay.captureOrientation = orientation
        binding.pageMarginOverlay.setLockedFocusPoint(
            Prefs.cameraFocusPointX(this),
            Prefs.cameraFocusPointY(this),
            Prefs.cameraFocusLocked(this),
        )
        when (orientation) {
            CameraCaptureOrientation.PORTRAIT -> {
                binding.btnCaptureOrientation.setImageResource(R.drawable.ic_capture_portrait)
                binding.btnCaptureOrientation.contentDescription =
                    getString(R.string.camera_orientation_portrait)
            }
            CameraCaptureOrientation.LANDSCAPE -> {
                binding.btnCaptureOrientation.setImageResource(R.drawable.ic_capture_landscape)
                binding.btnCaptureOrientation.contentDescription =
                    getString(R.string.camera_orientation_landscape)
            }
        }
        RemoteUiCatalog.apply(binding.btnCaptureOrientation)
    }

    /** Zoom and exposure are ordinary CameraX controls and are reapplied after
     * every lifecycle rebind. Focus lock is posted until PreviewView has real
     * dimensions so its normalized saved point maps to the correct sensor area. */
    private fun applyStoredCameraControls(camera: Camera) {
        camera.cameraInfo.zoomState.value?.let { state ->
            val requested = normalizedZoomRatio(
                Prefs.cameraZoomRatio(this),
                state.minZoomRatio,
                state.maxZoomRatio,
            )
            Prefs.setCameraZoomRatio(this, requested)
            val future = camera.cameraControl.setZoomRatio(requested)
            future.addListener({
                runCatching { future.get() }
                    .onFailure { Log.w(CAMERA_LOG_TAG, "Could not restore zoom", it) }
            }, ContextCompat.getMainExecutor(this))
        }

        val exposure = camera.cameraInfo.exposureState
        if (exposure.isExposureCompensationSupported) {
            val range = exposure.exposureCompensationRange
            val requested = normalizedExposureIndex(
                Prefs.cameraExposureIndex(this),
                range.lower,
                range.upper,
            )
            Prefs.setCameraExposureIndex(this, requested)
            val future = camera.cameraControl.setExposureCompensationIndex(requested)
            future.addListener({
                runCatching { future.get() }
                    .onFailure { Log.w(CAMERA_LOG_TAG, "Could not restore exposure", it) }
            }, ContextCompat.getMainExecutor(this))
        }

        binding.preview.post {
            if (boundCamera !== camera || isDestroyed) return@post
            val locked = Prefs.cameraFocusLocked(this)
            binding.pageMarginOverlay.setLockedFocusPoint(
                Prefs.cameraFocusPointX(this),
                Prefs.cameraFocusPointY(this),
                locked,
            )
            if (locked) applyFocusPoint(camera, announce = false)
        }
    }

    private fun focusAtPreviewPoint(x: Float, y: Float, announce: Boolean) {
        val camera = boundCamera ?: return
        if (binding.preview.width <= 0 || binding.preview.height <= 0) return
        Prefs.setCameraFocusPoint(
            this,
            x / binding.preview.width,
            y / binding.preview.height,
        )
        applyFocusPoint(camera, announce)
    }

    private fun focusAction(locked: Boolean): FocusMeteringAction {
        val point = binding.preview.meteringPointFactory.createPoint(
            Prefs.cameraFocusPointX(this) * binding.preview.width.coerceAtLeast(1),
            Prefs.cameraFocusPointY(this) * binding.preview.height.coerceAtLeast(1),
            0.15f,
        )
        return FocusMeteringAction.Builder(point, FocusMeteringAction.FLAG_AF)
            .apply { if (locked) disableAutoCancel() }
            .build()
    }

    private fun focusControlSupported(camera: Camera): Boolean = runCatching {
        camera.cameraInfo.isFocusMeteringSupported(focusAction(locked = true))
    }.getOrDefault(false)

    private fun applyFocusPoint(camera: Camera, announce: Boolean) {
        val locked = Prefs.cameraFocusLocked(this)
        val action = focusAction(locked)
        if (!camera.cameraInfo.isFocusMeteringSupported(action)) {
            binding.pageMarginOverlay.setLockedFocusPoint(
                Prefs.cameraFocusPointX(this),
                Prefs.cameraFocusPointY(this),
                visible = false,
            )
            if (announce) setStatus(getString(R.string.camera_settings_focus_unavailable))
            return
        }

        binding.pageMarginOverlay.setLockedFocusPoint(
            Prefs.cameraFocusPointX(this),
            Prefs.cameraFocusPointY(this),
            visible = locked,
        )
        val future = camera.cameraControl.startFocusAndMetering(action)
        future.addListener({
            if (boundCamera !== camera || !canUpdateCaptureUi()) return@addListener
            val successful = runCatching { future.get().isFocusSuccessful }
                .onFailure { Log.w(CAMERA_LOG_TAG, "Focus/metering request failed", it) }
                .getOrDefault(false)
            if (announce) {
                setStatus(getString(when {
                    !successful -> R.string.camera_focus_failed_status
                    locked -> R.string.camera_focus_locked_status
                    else -> R.string.camera_focus_set_status
                }))
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun clearFocusLock(camera: Camera?) {
        Prefs.setCameraFocusLocked(this, false)
        binding.pageMarginOverlay.setLockedFocusPoint(
            Prefs.cameraFocusPointX(this),
            Prefs.cameraFocusPointY(this),
            visible = false,
        )
        camera ?: return
        val future = camera.cameraControl.cancelFocusAndMetering()
        future.addListener({
            runCatching { future.get() }
                .onFailure { Log.w(CAMERA_LOG_TAG, "Could not restore continuous focus", it) }
        }, ContextCompat.getMainExecutor(this))
    }

    /** This dialog intentionally contains no account, network, update, or app
     * preferences. It is a short, capability-aware surface for the live camera
     * and scan-quality controls needed while a book is on the copy stand. */
    private fun showCameraSettings() {
        cameraSettingsDialog?.takeIf { it.isShowing }?.let { return }
        val camera = boundCamera ?: run {
            setStatus(getString(R.string.camera_settings_starting))
            return
        }
        val content = LayoutInflater.from(this)
            .inflate(R.layout.dialog_capture_camera_settings, null)
        val focusLock = content.findViewById<MaterialCheckBox>(R.id.cameraFocusLock)
        val focusState = content.findViewById<TextView>(R.id.cameraFocusState)
        val zoom = content.findViewById<SeekBar>(R.id.cameraZoom)
        val zoomLabel = content.findViewById<TextView>(R.id.cameraZoomLabel)
        val exposure = content.findViewById<SeekBar>(R.id.cameraExposure)
        val exposureLabel = content.findViewById<TextView>(R.id.cameraExposureLabel)
        val torch = content.findViewById<MaterialCheckBox>(R.id.cameraTorch)
        val profile = content.findViewById<RadioGroup>(R.id.cameraProfile)
        val sharpen = content.findViewById<MaterialCheckBox>(R.id.cameraSharpen)

        val focusSupported = focusControlSupported(camera)
        focusLock.isEnabled = focusSupported
        focusLock.isChecked = focusSupported && Prefs.cameraFocusLocked(this)
        focusState.setText(when {
            !focusSupported -> R.string.camera_settings_focus_unavailable
            focusLock.isChecked -> R.string.camera_settings_focus_locked
            else -> R.string.camera_settings_focus_auto
        })
        focusLock.setOnCheckedChangeListener { _, checked ->
            if (!focusSupported) return@setOnCheckedChangeListener
            Prefs.setCameraFocusLocked(this, checked)
            focusState.setText(
                if (checked) R.string.camera_settings_focus_locked
                else R.string.camera_settings_focus_auto,
            )
            if (checked) applyFocusPoint(camera, announce = true) else clearFocusLock(camera)
        }

        val zoomState = camera.cameraInfo.zoomState.value
        val minimumZoom = zoomState?.minZoomRatio ?: 1f
        val maximumZoom = zoomState?.maxZoomRatio ?: minimumZoom
        val requestedZoom = normalizedZoomRatio(
            Prefs.cameraZoomRatio(this),
            minimumZoom,
            maximumZoom,
        )
        zoom.max = 1000
        zoom.progress = zoomProgressFromRatio(
            requestedZoom,
            zoom.max,
            minimumZoom,
            maximumZoom,
        )
        zoom.isEnabled = maximumZoom > minimumZoom
        zoomLabel.text = if (zoom.isEnabled) {
            getString(R.string.camera_settings_zoom_value, requestedZoom)
        } else {
            getString(R.string.camera_settings_zoom_unavailable)
        }
        zoom.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) {
                if (!fromUser || !seekBar.isEnabled) return
                val ratio = zoomRatioFromProgress(
                    progress,
                    seekBar.max,
                    minimumZoom,
                    maximumZoom,
                )
                Prefs.setCameraZoomRatio(this@MainActivity, ratio)
                zoomLabel.text = getString(R.string.camera_settings_zoom_value, ratio)
                val future = camera.cameraControl.setZoomRatio(ratio)
                future.addListener({
                    runCatching { future.get() }
                        .onFailure { Log.w(CAMERA_LOG_TAG, "Could not set zoom", it) }
                }, ContextCompat.getMainExecutor(this@MainActivity))
            }

            override fun onStartTrackingTouch(seekBar: SeekBar) = Unit
            override fun onStopTrackingTouch(seekBar: SeekBar) = Unit
        })

        val exposureState = camera.cameraInfo.exposureState
        val exposureRange = exposureState.exposureCompensationRange
        val exposureSupported = exposureState.isExposureCompensationSupported &&
            exposureRange.lower <= exposureRange.upper
        val requestedExposure = normalizedExposureIndex(
            Prefs.cameraExposureIndex(this),
            exposureRange.lower,
            exposureRange.upper,
        )
        exposure.max = (exposureRange.upper - exposureRange.lower).coerceAtLeast(0)
        exposure.progress = (requestedExposure - exposureRange.lower).coerceIn(0, exposure.max)
        exposure.isEnabled = exposureSupported && exposure.max > 0
        val exposureStep = exposureState.exposureCompensationStep.toFloat()
        exposureLabel.text = if (exposureSupported) {
            getString(
                R.string.camera_settings_exposure_value,
                requestedExposure * exposureStep,
            )
        } else {
            getString(R.string.camera_settings_exposure_unavailable)
        }
        exposure.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) {
                if (!fromUser || !seekBar.isEnabled) return
                val index = normalizedExposureIndex(
                    exposureRange.lower + progress,
                    exposureRange.lower,
                    exposureRange.upper,
                )
                Prefs.setCameraExposureIndex(this@MainActivity, index)
                exposureLabel.text = getString(
                    R.string.camera_settings_exposure_value,
                    index * exposureStep,
                )
                val future = camera.cameraControl.setExposureCompensationIndex(index)
                future.addListener({
                    runCatching { future.get() }
                        .onFailure { Log.w(CAMERA_LOG_TAG, "Could not set exposure", it) }
                }, ContextCompat.getMainExecutor(this@MainActivity))
            }

            override fun onStartTrackingTouch(seekBar: SeekBar) = Unit
            override fun onStopTrackingTouch(seekBar: SeekBar) = Unit
        })

        val torchSupported = camera.cameraInfo.hasFlashUnit()
        torch.isEnabled = torchSupported
        torch.isChecked = torchSupported && Prefs.torchEnabled(this)
        if (!torchSupported) torch.setText(R.string.camera_settings_light_unavailable)
        torch.setOnCheckedChangeListener { _, checked ->
            if (!torchSupported) return@setOnCheckedChangeListener
            Prefs.setTorchEnabled(this, checked)
            applyCameraPreferenceChanges()
        }

        profile.check(
            when (Prefs.cameraProfile(this)) {
                Prefs.CAMERA_PROFILE_LOW -> R.id.cameraProfileLow
                Prefs.CAMERA_PROFILE_DETAIL -> R.id.cameraProfileDetail
                else -> R.id.cameraProfileFast
            },
        )
        profile.setOnCheckedChangeListener { _, checkedId ->
            Prefs.setCameraProfile(
                this,
                when (checkedId) {
                    R.id.cameraProfileLow -> Prefs.CAMERA_PROFILE_LOW
                    R.id.cameraProfileDetail -> Prefs.CAMERA_PROFILE_DETAIL
                    else -> Prefs.CAMERA_PROFILE_FAST
                },
            )
        }

        sharpen.isEnabled = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
        sharpen.isChecked = sharpen.isEnabled && Prefs.sharpenPreview(this)
        sharpen.setOnCheckedChangeListener { _, checked ->
            if (sharpen.isEnabled) Prefs.setSharpenPreview(this, checked)
        }

        RemoteUiCatalog.apply(content)
        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(RemoteUiCatalog.text(this, R.string.camera_settings_title))
            .setView(content)
            .setNeutralButton(
                RemoteUiCatalog.text(this, R.string.camera_settings_reset),
            ) { _, _ ->
                clearFocusLock(camera)
                Prefs.resetCameraOptions(this)
                updateCaptureOrientationUi()
                applyCameraPreferenceChanges()
            }
            .setPositiveButton(android.R.string.ok, null)
            .create()
        cameraSettingsDialog = dialog
        dialog.setOnDismissListener {
            if (cameraSettingsDialog === dialog) cameraSettingsDialog = null
            applyCameraPreferenceChanges()
        }
        dialog.show()
        RemoteUiCatalog.apply(dialog)
    }

    // --- the command state machine --------------------------------------------

    private fun command(word: String) {
        if (captureMutationInFlight) {
            cues.error("capture is being updated")
            return
        }
        // Sealing while an accepted shot is being exposed/written would lose
        // it. Done waits for both accepted shots. Cancel/restart drop the
        // queued shot. Undo drops the queued shot immediately, or waits for the
        // active shot and then removes that newly committed final page.
        val waitsForCapture = word in setOf("done", "cancel", "restart", "undo")
        if ((captureQueue.busy || session.hasActiveCaptureWrites()) && waitsForCapture) {
            if (word == "undo" && captureQueue.queued != null) {
                cancelQueuedCapture()
                setStatus(getString(R.string.capture_undo_photo))
                updateUi()
                return
            }
            pendingCommand = word
            pendingUndoTargetPage = if (word == "undo") {
                captureQueue.active?.pageNumber ?: (session.photoCount + 1)
            } else null
            Prefs.setPendingCaptureCommand(
                this,
                session.entryId,
                word,
                targetPage = pendingUndoTargetPage,
            )
            if (word != "done") cancelQueuedCapture()
            setStatus(
                when (word) {
                    "done" -> getString(R.string.capture_waiting_done)
                    "cancel" -> getString(R.string.capture_waiting_cancel)
                    "restart" -> getString(R.string.capture_waiting_restart)
                    else -> getString(R.string.capture_waiting_undo)
                },
            )
            updateUi()
            return
        }
        when (word) {
            "notes" -> requestStartVoiceNote()
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
                    val hasNotes = session.entryId?.let { id ->
                        CaptureNotes.read(session.entryDir(id)).notes.isNotEmpty()
                    } == true
                    val id = session.done()
                    when {
                        id != null -> {
                            cues.saved(photos)
                            ProcessWorker.enqueuePending(this, id)
                            UploadWorker.enqueue(this)
                            refreshLastCapturedBook()
                        }
                        // null + still active = the manifest write failed
                        // (disk full); the pages are kept and "done" can retry
                        session.active && photos == 0 && hasNotes -> {
                            cues.error("take a page photo before finishing")
                            setStatus(getString(R.string.capture_note_needs_photo))
                        }
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
            "restart" -> restartCapture()
            "undo" -> undoLastCaptureOrNote()
            "edit" -> reopenLastScannedBook()
        }
        updateUi()
    }

    /** Reopen the newest sealed capture which has not left this device. The
     * manifest is detached and extraction is marked stale under the same lock
     * used by processing/delivery, then a fresh CaptureSession adopts the
     * durable current-entry pointer. Page OCR, metadata, and originals remain. */
    private fun reopenLastScannedBook() {
        when {
            session.active -> {
                cues.error(RemoteUiCatalog.text(this, R.string.capture_edit_already_open_error))
                setStatus(RemoteUiCatalog.text(this, R.string.capture_edit_finish_current))
                return
            }
            captureQueue.busy || session.hasActiveCaptureWrites() -> {
                cues.error(RemoteUiCatalog.text(this, R.string.capture_edit_busy_error))
                setStatus(RemoteUiCatalog.text(this, R.string.capture_edit_busy_status))
                return
            }
        }
        captureMutationInFlight = true
        setStatus(RemoteUiCatalog.text(this, R.string.capture_edit_reopening))
        updateUi()
        lifecycleScope.launch {
            val (entryId, result) = withContext(Dispatchers.IO) {
                val candidate = selectLastEditableEntry(Entries.recent(this@MainActivity))
                    ?: return@withContext null to null
                candidate.id to EntryOperationLocks.withLock(candidate.id) {
                    CaptureQueueLifecycle.exclusive {
                        if (Prefs.currentEntryId(this@MainActivity) != null) {
                            return@exclusive CaptureReopenResult.InvalidCapture
                        }
                        if (Prefs.activeCaptureSyncRecord(this@MainActivity)
                                ?.targetIds?.contains(candidate.id) == true
                        ) return@exclusive CaptureReopenResult.InvalidCapture
                        val current = Entries.find(this@MainActivity, candidate.id)
                        if (current == null || !current.sealed || current.uploaded ||
                            current.dir.canonicalFile != candidate.dir.canonicalFile
                        ) return@exclusive CaptureReopenResult.InvalidCapture
                        prepareCaptureForEditing(current.dir).also { prepared ->
                            if (prepared is CaptureReopenResult.Reopened) {
                                Prefs.setCurrentEntryId(this@MainActivity, current.id)
                            }
                        }
                    }
                }
            }
            try {
                when (result) {
                    is CaptureReopenResult.Reopened -> {
                        val replacement = CaptureSession(this@MainActivity)
                        if (replacement.entryId != entryId) {
                            cues.error(RemoteUiCatalog.text(
                                this@MainActivity,
                                R.string.capture_edit_failed_error,
                            ))
                            setStatus(RemoteUiCatalog.text(
                                this@MainActivity,
                                R.string.capture_edit_failed_status,
                            ))
                        } else {
                            session = replacement
                            renderCurrentThumbnails()
                            cues.started()
                            if (!result.cleanupComplete) {
                                Log.w(
                                    CAMERA_LOG_TAG,
                                    "Reopened $entryId; hidden stale extraction cleanup remains",
                                )
                            }
                            setStatus(RemoteUiCatalog.text(
                                this@MainActivity,
                                R.string.capture_edit_reopened,
                            ))
                        }
                    }
                    null -> {
                        cues.error(RemoteUiCatalog.text(
                            this@MainActivity,
                            R.string.capture_edit_none_error,
                        ))
                        setStatus(RemoteUiCatalog.text(
                            this@MainActivity,
                            R.string.capture_edit_none_status,
                        ))
                    }
                    CaptureReopenResult.NotSealed,
                    CaptureReopenResult.InvalidCapture,
                    CaptureReopenResult.StorageFailure -> {
                        cues.error(RemoteUiCatalog.text(
                            this@MainActivity,
                            R.string.capture_edit_failed_error,
                        ))
                        setStatus(RemoteUiCatalog.text(
                            this@MainActivity,
                            R.string.capture_edit_failed_status,
                        ))
                    }
                }
            } finally {
                captureMutationInFlight = false
                if (result is CaptureReopenResult.Reopened) refreshLastCapturedBook()
                updateUi()
            }
        }
    }

    private fun restartCapture() {
        val collection = Collections.current(this) ?: session.provenance?.let { provenance ->
            BookCollection(
                id = provenance.collectionId,
                name = provenance.collectionName,
                from = provenance.from,
            )
        }
        if (collection == null) {
            cues.error("choose a collection first")
            setStatus(getString(R.string.collections_choose_first))
            return
        }

        captureMutationInFlight = true
        val discarded = if (session.active) session.cancel() else null
        if (session.active) {
            captureMutationInFlight = false
            cues.error("could not discard current capture")
            setStatus(getString(R.string.capture_restart_failed, "could not discard current capture"))
            return
        }
        val result = runCatching { session.start(collection) }
        if (result.isSuccess) {
            clearThumbnailStrip()
            cues.started()
            setStatus(getString(R.string.capture_restart_complete))
        } else {
            discarded?.let(session::restoreFromTrash)
            cues.error("could not restart capture")
            setStatus(getString(
                R.string.capture_restart_failed,
                result.exceptionOrNull()?.message ?: "storage unavailable",
            ))
        }
        captureMutationInFlight = false
    }

    private fun undoLastCaptureOrNote(
        forcePhoto: Boolean = false,
        durableEntryId: String? = null,
        targetPage: Int? = null,
    ) {
        if (!forcePhoto && voiceNoteDraft != null) {
            finishVoiceNote(save = false)
            return
        }
        val entryId = session.entryId
        if (entryId == null || durableEntryId != null && durableEntryId != entryId) {
            cues.error("nothing to undo")
            setStatus(getString(R.string.capture_undo_empty))
            durableEntryId?.let { Prefs.clearPendingCaptureCommand(this, it) }
            return
        }

        captureMutationInFlight = true
        updateUi()
        lifecycleScope.launch {
            var terminal = false
            var retryCaptureWrite = false
            try {
                val outcome = withContext(Dispatchers.IO) {
                    try {
                        val dir = session.entryDir(entryId)
                        if (forcePhoto) {
                            val page = targetPage
                            if (page == null || !File(dir, "photo_$page.jpg").isFile ||
                                session.refreshPhotoCount() != page
                            ) {
                                CaptureUndoOutcome.Empty
                            } else {
                                CaptureUndoOutcome.Photo(session.discardLastCommittedPhoto())
                            }
                        } else {
                            val lastNote = CaptureNotes.read(dir).notes.lastOrNull()
                            val lastPhoto = PhotoAssetStore.descriptors(dir)
                                .maxByOrNull { it.captureOrder }
                            // Processing may atomically replace photo_N and
                            // advance its mtime. The preserved camera original
                            // remains immutable and reflects the user action.
                            val lastPhotoCapturedAt = lastPhoto?.rawFile?.lastModified()
                                ?.takeIf { it > 0L }
                                ?: lastPhoto?.captureFile?.lastModified()?.coerceAtLeast(0L)
                                ?: 0L
                            if (lastNote != null &&
                                (lastPhoto == null || lastNote.updatedAtMs >= lastPhotoCapturedAt)
                            ) {
                                if (CaptureNotes.removeLast(dir) != null) {
                                    CaptureUndoOutcome.NoteDiscarded
                                } else CaptureUndoOutcome.Empty
                            } else if (lastPhoto != null) {
                                CaptureUndoOutcome.Photo(session.discardLastCommittedPhoto())
                            } else {
                                CaptureUndoOutcome.Empty
                            }
                        }
                    } catch (cancelled: kotlinx.coroutines.CancellationException) {
                        throw cancelled
                    } catch (error: Exception) {
                        Log.e(CAMERA_LOG_TAG, "Could not undo capture action", error)
                        CaptureUndoOutcome.Failed
                    }
                }
                if (session.entryId != entryId) {
                    terminal = true
                    return@launch
                }
                when (outcome) {
                    CaptureUndoOutcome.NoteDiscarded -> {
                        terminal = true
                        setStatus(getString(R.string.capture_undo_note))
                    }
                    is CaptureUndoOutcome.Photo -> when (val result = outcome.result) {
                        is LastCommittedPhotoUndoResult.Discarded -> {
                            terminal = true
                            renderCurrentThumbnails()
                            if (!result.cleanupComplete) {
                                Log.w(
                                    CAMERA_LOG_TAG,
                                    "Undo detached page ${result.pageNumber}; staged cleanup remains",
                                )
                            }
                            if (result.remainingPhotoCount > 0) {
                                ProcessWorker.enqueue(this@MainActivity)
                            }
                            setStatus(getString(R.string.capture_undo_photo))
                        }
                        LastCommittedPhotoUndoResult.CaptureWriteInProgress -> {
                            if (durableEntryId != null && targetPage != null) {
                                retryCaptureWrite = true
                                setStatus(getString(R.string.capture_waiting_undo))
                            } else {
                                terminal = true
                                setStatus(getString(R.string.capture_undo_failed))
                            }
                        }
                        LastCommittedPhotoUndoResult.NoActiveCapture,
                        LastCommittedPhotoUndoResult.NoCommittedPhoto -> {
                            terminal = true
                            setStatus(getString(
                                if (forcePhoto) R.string.capture_undo_photo
                                else R.string.capture_undo_empty,
                            ))
                        }
                        LastCommittedPhotoUndoResult.CaptureChanged,
                        LastCommittedPhotoUndoResult.InvalidPhotoSequence,
                        LastCommittedPhotoUndoResult.InvalidPhotoContract,
                        LastCommittedPhotoUndoResult.StorageFailure -> {
                            terminal = true
                            cues.error("could not undo last capture")
                            setStatus(getString(R.string.capture_undo_failed))
                        }
                    }
                    CaptureUndoOutcome.Empty -> {
                        terminal = true
                        setStatus(getString(
                            if (forcePhoto) R.string.capture_undo_photo
                            else R.string.capture_undo_empty,
                        ))
                    }
                    CaptureUndoOutcome.Failed -> {
                        terminal = true
                        cues.error("could not undo last capture")
                        setStatus(getString(R.string.capture_undo_failed))
                    }
                }
            } finally {
                captureMutationInFlight = false
                if (terminal && durableEntryId != null) {
                    Prefs.clearPendingCaptureCommand(this@MainActivity, durableEntryId)
                    pendingUndoTargetPage = null
                }
                if (retryCaptureWrite && durableEntryId != null && targetPage != null) {
                    pendingCommand = "undo"
                    pendingUndoTargetPage = targetPage
                    Prefs.setPendingCaptureCommand(
                        this@MainActivity,
                        durableEntryId,
                        "undo",
                        targetPage,
                    )
                    startCameraAfterPriorWrites()
                }
                if (!isDestroyed) updateUi()
            }
        }
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

        // registerCapturedPhoto reconciles and persists the complete photo
        // contract and may copy the original JPEG. Keep that filesystem/JSON
        // work off CameraX's main-thread callback while the shallow capture
        // queue preserves page ordering.
        if (canUpdateCaptureUi()) setStatus("Saving capture\u2026")
        CAPTURE_COMMIT_EXECUTOR.execute {
            val commitError = try {
                if (session.commitPhoto(reservation)) null
                else IllegalStateException("could not finalize capture file")
            } catch (error: Exception) {
                error
            }
            ContextCompat.getMainExecutor(this).execute {
                finishCaptureCommit(ticket, reservation, commitError)
            }
        }
    }

    private fun finishCaptureCommit(
        ticket: ShallowCaptureQueue.Ticket,
        reservation: CaptureSession.PhotoReservation,
        commitError: Exception?,
    ) {
        val shot = acceptedShots[ticket.id]
        if (captureQueue.active?.id != ticket.id || shot?.reservation != reservation) {
            if (commitError != null) session.abortPhoto(reservation)
            return
        }
        if (commitError != null) {
            handleCaptureError(ticket, reservation, commitError)
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
        // Pending terminal commands are durable. Activity destruction must not
        // erase an accepted Done/Cancel/Restart/Undo before its replacement can
        // reconcile the target entry.
        deferredCaptureSubmission = false
    }

    private fun canUpdateCaptureUi(): Boolean =
        !isDestroyed && !isFinishing && lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    private fun nanosToMillis(nanos: Long): String = "%.1f".format(nanos / 1_000_000.0)

    private fun runPending() {
        if (captureQueue.busy || session.hasActiveCaptureWrites() || isDestroyed) return
        val cmd = pendingCommand ?: return
        val pendingEntry = session.entryId ?: run {
            pendingCommand = null
            pendingUndoTargetPage = null
            Prefs.setPendingCaptureCommand(this, null, null)
            return
        }
        if (cmd == "undo") {
            val targetPage = pendingUndoTargetPage
                ?: Prefs.pendingCaptureTargetPage(this, pendingEntry)
            pendingCommand = null
            if (targetPage == null) {
                pendingUndoTargetPage = null
                Prefs.clearPendingCaptureCommand(this, pendingEntry)
                setStatus(getString(R.string.capture_undo_failed))
                updateUi()
                return
            }
            session.refreshPhotoCount()
            val targetExists = File(session.entryDir(pendingEntry), "photo_$targetPage.jpg").isFile
            when (deferredUndoDisposition(targetPage, session.photoCount, targetExists)) {
                DeferredUndoDisposition.ALREADY_UNDONE -> {
                // The accepted shot failed or its temporary was reclaimed
                // after process death. That is already the requested Undo;
                // never fall through to an older committed action.
                    pendingUndoTargetPage = null
                    Prefs.clearPendingCaptureCommand(this, pendingEntry)
                    setStatus(getString(R.string.capture_undo_photo))
                    updateUi()
                    return
                }
                DeferredUndoDisposition.INVALID -> {
                    pendingUndoTargetPage = null
                    Prefs.clearPendingCaptureCommand(this, pendingEntry)
                    setStatus(getString(R.string.capture_undo_failed))
                    updateUi()
                    return
                }
                DeferredUndoDisposition.REMOVE_TARGET -> Unit
            }
            undoLastCaptureOrNote(
                forcePhoto = true,
                durableEntryId = pendingEntry,
                targetPage = targetPage,
            )
            return
        }
        pendingCommand = null
        pendingUndoTargetPage = null
        command(cmd)
        Prefs.clearPendingCaptureCommand(this, pendingEntry)
    }

    private fun finishAfterAcceptedCapturesIfReady() {
        if (!finishAfterAcceptedCaptures || captureQueue.busy ||
            captureMutationInFlight || pendingCommand != null
        ) return
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
        val pageNumber = photoNumber(File(path).name)
        iv.contentDescription = RemoteUiCatalog.text(
            this,
            R.string.capture_thumbnail_delete_description,
            pageNumber,
        )
        iv.setOnLongClickListener {
            removeCaptureThumbnail(pageNumber)
            true
        }
        binding.thumbs.addView(iv)
        updateThumbnailStripVisibility()
        lifecycleScope.launch(Dispatchers.IO) {
            var unclaimedBitmap: android.graphics.Bitmap? = null
            try {
                unclaimedBitmap = thumbnailDecodeGate.withPermit {
                    decodeSampledOriented(
                        File(path),
                        maxWidth = h * 2,
                        maxHeight = h,
                    )
                }
                withContext(Dispatchers.Main) {
                    val bitmap = unclaimedBitmap
                    if (iv.parent !== binding.thumbs) return@withContext
                    if (bitmap == null) {
                        binding.thumbs.removeView(iv)
                        updateThumbnailStripVisibility()
                        return@withContext
                    }
                    val w = h * bitmap.width / bitmap.height.coerceAtLeast(1)
                    iv.layoutParams = LinearLayout.LayoutParams(w, h).apply { marginEnd = gap }
                    iv.setImageBitmap(bitmap)
                    thumbnailBitmaps[iv] = bitmap
                    unclaimedBitmap = null
                }
            } finally {
                unclaimedBitmap?.takeIf { !it.isRecycled }?.recycle()
            }
        }
    }

    private fun clearThumbnailStrip() {
        for (index in 0 until binding.thumbs.childCount) {
            (binding.thumbs.getChildAt(index) as? ImageView)?.setImageDrawable(null)
        }
        binding.thumbs.removeAllViews()
        thumbnailBitmaps.values.forEach { bitmap ->
            if (!bitmap.isRecycled) bitmap.recycle()
        }
        thumbnailBitmaps.clear()
        updateThumbnailStripVisibility()
    }

    /** Long-press deletion has no confirmation, but cannot race an accepted
     * CameraX output. Once admitted on the main thread, capture buttons and
     * voice mutations remain disabled until the entry lock publishes the new
     * dense page sequence. */
    private fun removeCaptureThumbnail(pageNumber: Int) {
        val entryId = session.entryId
        if (entryId == null || pageNumber <= 0) return
        if (captureMutationInFlight || captureQueue.busy ||
            session.hasActiveCaptureWrites() || pendingCommand != null
        ) {
            cues.error(RemoteUiCatalog.text(this, R.string.capture_thumbnail_remove_busy))
            setStatus(RemoteUiCatalog.text(this, R.string.capture_thumbnail_remove_busy))
            return
        }
        captureMutationInFlight = true
        setStatus(RemoteUiCatalog.text(this, R.string.capture_thumbnail_removing))
        updateUi()
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                EntryOperationLocks.withLock(entryId) {
                    if (session.entryId != entryId || session.hasActiveCaptureWrites()) {
                        CaptureThumbnailDeleteResult.InvalidCapture
                    } else {
                        deleteCaptureThumbnail(session.entryDir(entryId), pageNumber)
                    }
                }
            }
            try {
                if (session.entryId != entryId) return@launch
                when (result) {
                    is CaptureThumbnailDeleteResult.Deleted -> {
                        session.refreshPhotoCount()
                        renderCurrentThumbnails()
                        if (!result.cleanupComplete) {
                            Log.w(
                                CAMERA_LOG_TAG,
                                "Removed page ${result.pageNumber}; hidden file cleanup remains",
                            )
                        }
                        if (result.remainingPhotoCount > 0) {
                            ProcessWorker.enqueue(this@MainActivity)
                        }
                        setStatus(RemoteUiCatalog.text(this@MainActivity, R.string.capture_thumbnail_removed))
                    }
                    CaptureThumbnailDeleteResult.NoPhoto -> {
                        session.refreshPhotoCount()
                        renderCurrentThumbnails()
                    }
                    CaptureThumbnailDeleteResult.SealedCapture,
                    CaptureThumbnailDeleteResult.InvalidCapture,
                    CaptureThumbnailDeleteResult.StorageFailure -> {
                        cues.error(RemoteUiCatalog.text(
                            this@MainActivity,
                            R.string.capture_thumbnail_remove_error,
                        ))
                        setStatus(RemoteUiCatalog.text(
                            this@MainActivity,
                            R.string.capture_thumbnail_remove_failed,
                        ))
                    }
                }
            } finally {
                captureMutationInFlight = false
                updateUi()
            }
        }
    }

    /** Do not reserve a fixed-height blank band underneath the viewfinder. */
    private fun updateThumbnailStripVisibility() {
        binding.thumbsScroll.visibility =
            if (binding.thumbs.childCount == 0) View.GONE else View.VISIBLE
    }

    private fun setStatus(msg: String) {
        binding.status.text = msg
    }

    private fun scheduleBackgroundRefresh() {
        backgroundRefreshJob?.cancel()
        backgroundRefreshJob = lifecycleScope.launch {
            delay(200)
            updateUi()
            refreshLastCapturedBook()
        }
    }

    private fun updateUi() {
        val active = session.active
        val noteActive = voiceNoteDraft != null
        val captureAvailable = !captureMutationInFlight && pendingCommand == null
        binding.entryState.text =
            if (active) getString(R.string.entry_active, session.photoCount)
            else getString(R.string.entry_idle)
        binding.entryState.setTextColor(
            getColor(if (active) R.color.whl_green else R.color.whl_ink_dim))
        binding.btnStart.isEnabled = !active && captureAvailable && !noteActive
        binding.btnPhoto.isEnabled = active && captureAvailable && !noteActive && !captureQueue.full
        binding.btnDone.isEnabled = active && captureAvailable && !noteActive
        binding.btnCancel.isEnabled = active && captureAvailable && !noteActive
        binding.btnNote.isEnabled = active &&
            !voiceNoteFinalizing && (noteActive || captureAvailable && !captureQueue.busy)
        val cameraControlsAvailable = boundCamera != null && !captureQueue.busy &&
            !captureMutationInFlight && !noteActive
        binding.btnCameraSettings.isEnabled = cameraControlsAvailable
        binding.btnCaptureOrientation.isEnabled = cameraControlsAvailable
        binding.configWarning.isEnabled = !captureQueue.busy && !captureMutationInFlight && !noteActive
        if (!active && binding.thumbs.childCount > 0) clearThumbnailStrip()
        updateThumbnailStripVisibility()
        updateCaptureOrientationUi()

        val signedIn = Auth.signedIn(this)
        val err = Prefs.lastUploadError(this) ?: Prefs.lastProcError(this)
        val pending = session.pendingUploads().size
        binding.configWarning.visibility = if (signedIn) View.GONE else View.VISIBLE
        binding.configWarning.text = getString(R.string.not_signed_in)
        if (err != null && pending > 0)
            setStatus(resources.getQuantityString(
                R.plurals.uploads_stuck, pending, pending, err))
        finishAfterAcceptedCapturesIfReady()
    }

    /** An open capture is intentionally excluded. The prior submitted book
     * remains useful while the operator photographs the next one, and Done
     * replaces it immediately because sealing precedes ProcessWorker enqueue. */
    private fun refreshLastCapturedBook() {
        lastBookPreviewRefreshPending = true
        if (lastBookPreviewJob?.isActive == true) return
        lastBookPreviewJob = lifecycleScope.launch {
            delay(100)
            while (lastBookPreviewRefreshPending) {
                lastBookPreviewRefreshPending = false
                val previousFingerprint = lastBookPreviewFingerprint
                var unclaimedBitmap: android.graphics.Bitmap? = null
                try {
                    val load = withContext(Dispatchers.IO) {
                        val latest = selectLastSubmittedEntry(Entries.recent(this@MainActivity))
                        val photo = latest?.thumbnailDescriptor()?.displayFile
                        val fingerprint = latest?.let { entry ->
                            listOf(
                                entry.id,
                                photo?.absolutePath.orEmpty(),
                                photo?.length() ?: 0L,
                                photo?.lastModified() ?: 0L,
                            ).joinToString("|")
                        }
                        val changed = fingerprint != previousFingerprint
                        val bitmap = if (changed && photo != null) {
                            decodeSampledOriented(photo, maxWidth = 360, maxHeight = 480)
                        } else null
                        unclaimedBitmap = bitmap
                        LastBookPreviewLoad(
                            entry = latest,
                            bitmap = bitmap,
                            fingerprint = fingerprint,
                            thumbnailChanged = changed,
                        )
                    }
                    renderLastCapturedBook(load)
                    unclaimedBitmap = null
                } finally {
                    unclaimedBitmap?.takeIf { !it.isRecycled }?.recycle()
                }
            }
        }
    }

    private fun requestMicrophonePermission() {
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            VOICE_PERMISSION_REQUEST,
        )
    }

    private fun renderLastCapturedBook(load: LastBookPreviewLoad) {
        val entry = load.entry
        if (load.thumbnailChanged) {
            binding.lastBookThumb.setImageDrawable(null)
            lastBookPreviewBitmap?.takeIf { !it.isRecycled }?.recycle()
            lastBookPreviewBitmap = load.bitmap
            lastBookPreviewFingerprint = load.fingerprint
            if (load.bitmap == null) {
                binding.lastBookThumb.setImageResource(R.drawable.ic_launcher_safe_fg)
            } else {
                binding.lastBookThumb.setImageBitmap(load.bitmap)
            }
        }
        if (entry == null) {
            if (!load.thumbnailChanged) {
                binding.lastBookThumb.setImageResource(R.drawable.ic_launcher_safe_fg)
            }
            binding.lastBookThumb.contentDescription = getString(R.string.capture_last_book_empty)
            binding.lastBookTitle.setText(R.string.capture_last_book_empty)
            binding.lastBookAuthor.setText(R.string.capture_last_book_author_empty)
            binding.lastBookYear.setText(R.string.capture_last_book_year_empty)
            binding.lastBookAttention.visibility = View.GONE
            binding.lastBookExtrasGroup.visibility = View.GONE
            binding.lastBookPreview.alpha = 0.72f
            binding.lastBookPrimary.contentDescription = getString(R.string.capture_last_book_empty)
            binding.lastBookPrimary.isEnabled = false
            binding.lastBookPrimary.isClickable = false
            binding.lastBookPrimary.isFocusable = false
            binding.lastBookPrimary.setOnClickListener(null)
            binding.lastBookPrimary.setOnLongClickListener(null)
            binding.lastBookPreview.isClickable = false
            binding.lastBookPreview.isFocusable = false
            binding.lastBookPreview.setOnClickListener(null)
            binding.lastBookPreview.setOnLongClickListener(null)
            binding.lastBookPreview.isLongClickable = false
            binding.lastBookExtras.setOnClickListener(null)
            binding.lastBookExtras.setOnLongClickListener(null)
            return
        }

        // Entries.titleLabel is valuable while extraction is pending, but its
        // populated-metadata fallback may use a secondary/extra field. Once
        // metadata exists, keep this compact card strictly primary-only.
        val title = when {
            entry.title.isNotEmpty() -> entry.title
            entry.meta == null -> Entries.titleLabel(this, entry)
            else -> getString(R.string.capture_last_book_title_missing)
        }
        binding.lastBookThumb.contentDescription = getString(
            R.string.capture_last_book_thumbnail_description,
            title,
        )
        binding.lastBookTitle.text = title
        binding.lastBookAuthor.text = if (entry.author.isNotEmpty())
            getString(R.string.capture_last_book_author, entry.author)
        else getString(R.string.capture_last_book_author_empty)
        binding.lastBookYear.text = if (entry.year.isNotEmpty())
            getString(R.string.capture_last_book_year, entry.year)
        else getString(R.string.capture_last_book_year_empty)

        val available = !captureQueue.busy
        binding.lastBookPreview.alpha = 1f
        binding.lastBookPrimary.isEnabled = available
        binding.lastBookPrimary.isClickable = available
        binding.lastBookPrimary.isFocusable = available
        val bookDescription = getString(
            R.string.capture_last_book_description,
            title,
            entry.author.ifEmpty { getString(R.string.capture_last_book_field_missing) },
            entry.year.ifEmpty { getString(R.string.capture_last_book_field_missing) },
        )
        binding.lastBookPrimary.contentDescription = bookDescription
        binding.lastBookPreview.contentDescription = bookDescription
        val openBook = View.OnClickListener {
            if (!captureQueue.busy) {
                startActivity(Intent(this, EntryDetailActivity::class.java)
                    .putExtra(EntryDetailActivity.EXTRA_ID, entry.id))
            }
        }
        binding.lastBookPrimary.setOnClickListener(openBook)
        binding.lastBookPreview.isClickable = available
        // Keep one keyboard/screen-reader target for opening the book. The
        // outer card remains touch-clickable so its padding is not a dead zone.
        binding.lastBookPreview.isFocusable = false
        binding.lastBookPreview.setOnClickListener(openBook)
        val attentionListener = View.OnLongClickListener {
            showEntryAttentionDialog(this, entry.id) { refreshLastCapturedBook() }
            true
        }
        // lastBookPrimary owns most touch events inside the card; keep the
        // listener on both it and the outer preview so every part of the card
        // offers the same desktop-style review gesture.
        binding.lastBookPrimary.setOnLongClickListener(attentionListener)
        binding.lastBookPreview.isLongClickable = true
        binding.lastBookPreview.setOnLongClickListener(attentionListener)

        val localReview = entry.captureReview
        val needsReview = localReview?.needsReview == true
        val needsAttention = localReview?.needsAttention == true || needsReview
        val attentionReason = localReview?.attentionReason.orEmpty()
        binding.lastBookAttention.apply {
            visibility = if (needsAttention) View.VISIBLE else View.GONE
            setColorFilter(getColor(if (needsReview) R.color.whl_red else R.color.whl_amber))
            contentDescription = buildString {
                append(getString(
                    if (needsReview) R.string.home_needs_review else R.string.home_needs_attention,
                ))
                if (attentionReason.isNotBlank()) append(": ").append(attentionReason)
            }
        }

        val extras = captureExtraFields(entry.meta)
        binding.lastBookExtrasGroup.visibility =
            if (extras.isEmpty()) View.GONE else View.VISIBLE
        if (extras.isNotEmpty()) {
            binding.lastBookExtraCount.text =
                getString(R.string.capture_extra_fields_count, extras.size)
            binding.lastBookExtras.contentDescription = resources.getQuantityString(
                R.plurals.capture_extra_fields_description,
                extras.size,
                extras.size,
            )
            binding.lastBookExtras.isEnabled = available
            binding.lastBookExtras.setOnClickListener { showCaptureExtras(entry.id) }
            binding.lastBookExtras.setOnLongClickListener(attentionListener)
        } else {
            binding.lastBookExtras.setOnClickListener(null)
            binding.lastBookExtras.setOnLongClickListener(null)
        }
    }

    /** Re-read by id so a processing callback cannot leave a stale extra-field
     * snapshot in a dialog opened at the same moment. */
    private fun showCaptureExtras(entryId: String) {
        val fields = captureExtraFields(Entries.find(this, entryId)?.meta)
        if (fields.isEmpty()) {
            refreshLastCapturedBook()
            return
        }
        extraFieldsDialog?.dismiss()
        val content = LayoutInflater.from(this).inflate(R.layout.dialog_capture_extras, null)
        val list = content.findViewById<LinearLayout>(R.id.captureExtrasList)
        for (field in fields) {
            val row = LayoutInflater.from(this).inflate(R.layout.item_capture_extra, list, false)
            row.findViewById<TextView>(R.id.extraLabel).text = field.label
            row.findViewById<TextView>(R.id.extraValue).text = field.value
            row.contentDescription =
                getString(R.string.capture_extra_field_description, field.label, field.value)
            list.addView(row)
        }
        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.capture_extra_fields_title)
            .setView(content)
            .setPositiveButton(R.string.close, null)
            .create()
        extraFieldsDialog = dialog
        dialog.setOnDismissListener {
            if (extraFieldsDialog === dialog) extraFieldsDialog = null
        }
        dialog.show()
        RemoteUiCatalog.apply(dialog)
    }
}
