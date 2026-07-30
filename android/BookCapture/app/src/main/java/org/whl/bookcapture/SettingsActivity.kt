package org.whl.bookcapture

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.SeekBar
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivitySettingsBinding

/**
 * Account, device label, transport, and processing keys. Local-mode values
 * stay on this phone; signed-in edits are also queued to the account profile.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private var baselineDisplayName = ""
    private var baselineMistral = ""
    private var baselineDeepseek = ""

    /** The grant, not the Uri string, is what authorizes a later background
     * export — so take it persistably here and re-check it at export time. */
    private val exportFolderPicker = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        if (uri == null) return@registerForActivityResult
        val taken = runCatching {
            contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        }.isSuccess
        if (!taken) {
            binding.exportStatus.text = getString(R.string.set_export_permission_lost)
            return@registerForActivityResult
        }
        Prefs.setExportTreeUri(this, uri.toString())
        renderCaptureStorage()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.toolbar.setNavigationOnClickListener { finish() }

        val signedInAtOpen = Auth.signedIn(this)
        binding.accountEmail.text = if (signedInAtOpen) Prefs.email(this)
            else getString(R.string.set_local_mode)
        binding.signOut.text = getString(
            if (signedInAtOpen) R.string.set_sign_out else R.string.set_sign_in)
        binding.displayName.setText(Prefs.displayName(this))
        binding.device.setText(Prefs.deviceName(this))
        binding.compactScanList.isChecked = Prefs.compactScanList(this)
        binding.compactScanList.setOnCheckedChangeListener { _, compact ->
            Prefs.setCompactScanList(this, compact)
        }
        renderOcrOverlaySettings()
        binding.showOcrRegions.setOnCheckedChangeListener { _, show ->
            Prefs.setShowOcrRegions(this, show)
            renderOcrOverlaySettings()
        }
        binding.ocrRegionOpacity.setOnSeekBarChangeListener(
            object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                    if (fromUser) {
                        Prefs.setOcrRegionOpacityPercent(this@SettingsActivity, progress)
                        binding.ocrRegionOpacityLabel.text = getString(
                            R.string.set_ocr_region_opacity_value,
                            Prefs.ocrRegionOpacityPercent(this@SettingsActivity),
                        )
                    }
                }
                override fun onStartTrackingTouch(seekBar: SeekBar?) = Unit
                override fun onStopTrackingTouch(seekBar: SeekBar?) = Unit
            },
        )
        binding.showOcrRegionLabels.setOnCheckedChangeListener { _, show ->
            Prefs.setShowOcrRegionLabels(this, show)
        }
        renderPostProcessingSettings()
        binding.postProcessingPresetGroup.setOnCheckedChangeListener { _, checkedId ->
            val preset = when (checkedId) {
                R.id.postProcessingPresetModern ->
                    PostProcessingPreset.MODERN_1950_AND_LATER
                R.id.postProcessingPresetOlder ->
                    PostProcessingPreset.OLDER_1850_TO_1949
                R.id.postProcessingPresetEarly ->
                    PostProcessingPreset.EARLY_BEFORE_1850
                else -> PostProcessingPreset.AUTOMATIC_BY_DATE
            }
            Prefs.setPostProcessingPreset(this, preset)
        }
        binding.postProcessingDewarp.setOnCheckedChangeListener { _, enabled ->
            Prefs.setPostProcessingDewarp(this, enabled)
        }
        binding.postProcessingMarginCrop.setOnCheckedChangeListener { _, enabled ->
            Prefs.setPostProcessingMarginCrop(this, enabled)
        }
        binding.postProcessingContrast.setOnCheckedChangeListener { _, enabled ->
            Prefs.setPostProcessingContrast(this, enabled)
        }
        binding.postProcessingSpineCrop.setOnCheckedChangeListener { _, enabled ->
            Prefs.setPostProcessingSpineCrop(this, enabled)
        }
        binding.mistralKey.setText(Prefs.mistralKey(this))
        binding.deepseekKey.setText(Prefs.deepseekKey(this))
        binding.extractionInstructions.setText(Prefs.extractionInstructions(this))
        rememberProfileBaseline()

        binding.chooseExportFolder.setOnClickListener { chooseExportFolder() }
        binding.exportNow.setOnClickListener { exportCaptures() }
        binding.browseArchive.setOnClickListener {
            startActivity(Intent(this, ArchiveActivity::class.java))
        }
        renderCaptureStorage()

        // viewfinder sharpen is a GPU shader on the preview — Android 13+ only
        renderCameraSettings()
        binding.cameraProfileGroup.setOnCheckedChangeListener { _, checkedId ->
            Prefs.setCameraProfile(this, when (checkedId) {
                R.id.cameraProfileLow -> Prefs.CAMERA_PROFILE_LOW
                R.id.cameraProfileDetail -> Prefs.CAMERA_PROFILE_DETAIL
                else -> Prefs.CAMERA_PROFILE_FAST
            })
        }
        binding.continuousLight.setOnCheckedChangeListener { _, on ->
            Prefs.setTorchEnabled(this, on)
        }
        binding.sharpenPreview.setOnCheckedChangeListener { _, on ->
            Prefs.setSharpenPreview(this, on)
        }
        binding.resetCamera.setOnClickListener {
            Prefs.resetCameraOptions(this)
            renderCameraSettings()
            binding.msg.text = getString(R.string.saved)
        }

        // Voice control is opt-in: enabling it here is what makes the capture
        // screen ask for the mic and download the model on its next resume.
        binding.voiceControl.isChecked = Prefs.voiceControl(this)
        binding.voiceControl.setOnCheckedChangeListener { _, on ->
            Prefs.setVoiceControl(this, on)
        }

        // transport (Cloud / LAN / Auto) + LAN pairing
        when (Prefs.transport(this)) {
            "lan" -> binding.transportLan.isChecked = true
            "auto" -> binding.transportAuto.isChecked = true
            else -> binding.transportCloud.isChecked = true
        }
        binding.lanHost.setText(Prefs.lanHost(this))
        binding.lanToken.setText(Prefs.lanToken(this))
        binding.lanTest.setOnClickListener {
            Prefs.setLan(this, binding.lanHost.text.toString(), binding.lanToken.text.toString())
            Prefs.setExtractionInstructions(this, binding.extractionInstructions.text.toString())
            binding.msg.text = getString(R.string.testing)
            lifecycleScope.launch {
                val ok = withContext(Dispatchers.IO) {
                    try { LanClient(this@SettingsActivity).ping() } catch (_: Exception) { false }
                }
                binding.msg.text = getString(if (ok) R.string.lan_ok else R.string.lan_unreachable)
            }
        }

        // freshen the cache; another device may have changed the keys. Only
        // overwrite a field the user has NOT touched since it was populated,
        // so a slow network pull can't wipe a key they are mid-typing.
        val shownDisplayName = binding.displayName.text.toString()
        val shownMistral = binding.mistralKey.text.toString()
        val shownDeepseek = binding.deepseekKey.text.toString()
        if (signedInAtOpen) lifecycleScope.launch(Dispatchers.IO) {
            try {
                Auth.pullProfile(this@SettingsActivity)
                withContext(Dispatchers.Main) {
                    if (binding.displayName.text.toString() == shownDisplayName) {
                        baselineDisplayName = Prefs.displayName(this@SettingsActivity)
                        binding.displayName.setText(baselineDisplayName)
                    }
                    if (binding.mistralKey.text.toString() == shownMistral) {
                        baselineMistral = Prefs.mistralKey(this@SettingsActivity)
                        binding.mistralKey.setText(baselineMistral)
                    }
                    if (binding.deepseekKey.text.toString() == shownDeepseek) {
                        baselineDeepseek = Prefs.deepseekKey(this@SettingsActivity)
                        binding.deepseekKey.setText(baselineDeepseek)
                    }
                }
            } catch (_: Exception) { /* offline: the cache stands */ }
        }

        Prefs.profileSyncError(this)?.takeIf { signedInAtOpen }?.let {
            binding.msg.text = getString(R.string.saved_sync_error, it)
        }
        if (signedInAtOpen) ProfileSyncWorker.enqueue(this)

        binding.save.setOnClickListener {
            Prefs.setDeviceName(this, binding.device.text.toString())
            val transport = when {
                binding.transportLan.isChecked -> "lan"
                binding.transportAuto.isChecked -> "auto"
                else -> "cloud"
            }
            Prefs.setTransport(this, transport)
            Prefs.setLan(this, binding.lanHost.text.toString(), binding.lanToken.text.toString())
            val displayName = binding.displayName.text.toString()
            val mistral = binding.mistralKey.text.toString()
            val deepseek = binding.deepseekKey.text.toString()
            val changed = buildSet {
                if (displayName != baselineDisplayName) add(Prefs.PROFILE_DISPLAY_NAME)
                if (mistral != baselineMistral) add(Prefs.PROFILE_MISTRAL)
                if (deepseek != baselineDeepseek) add(Prefs.PROFILE_DEEPSEEK)
            }
            val cloudPending = changed.takeIf {
                Auth.signedIn(this) && Prefs.userId(this).isNotEmpty()
            }.orEmpty()
            // Values and their pending flags are one local transaction, so a
            // concurrent profile pull cannot replace a just-saved edit.
            Prefs.saveProfileLocally(
                this,
                displayName = displayName,
                mistral = mistral,
                deepseek = deepseek,
                pendingFields = cloudPending,
            )
            if (Auth.signedIn(this) || transport != "cloud") {
                UploadWorker.restartExplicitSyncAfterSettingsChange(this)
            }
            if (cloudPending.isNotEmpty()) {
                ProfileSyncWorker.enqueue(this)
                binding.msg.text = getString(R.string.saved_sync_pending)
            } else {
                binding.msg.text = getString(R.string.saved_on_device)
            }
            rememberProfileBaseline()
            ProcessWorker.enqueue(this)
            // Also revives a nonlinear-display OCR marker left by missing
            // credentials or an exhausted bounded retry run.
            CloudDisplayReocrWorker.enqueueAllPending(this)
        }

        binding.test.setOnClickListener {
            if (!Auth.signedIn(this)) {
                binding.msg.text = getString(R.string.connection_requires_sign_in)
                return@setOnClickListener
            }
            binding.msg.text = getString(R.string.testing)
            lifecycleScope.launch {
                val err = withContext(Dispatchers.IO) {
                    SupabaseClient(this@SettingsActivity).testConnection()
                }
                binding.msg.text = err ?: getString(R.string.connection_ok)
                if (err == null) UploadWorker.restartExplicitSyncAfterSettingsChange(
                    this@SettingsActivity,
                )
            }
        }

        binding.signOut.setOnClickListener {
            if (!Auth.signedIn(this)) {
                startActivity(Intent(this, LoginActivity::class.java))
            } else {
                binding.signOut.isEnabled = false
                lifecycleScope.launch {
                    val error = withContext(Dispatchers.IO) {
                        Auth.signOut(this@SettingsActivity)
                    }
                    binding.signOut.isEnabled = true
                    binding.accountEmail.text = getString(R.string.set_local_mode)
                    binding.signOut.text = getString(R.string.set_sign_in)
                    binding.displayName.setText(Prefs.displayName(this@SettingsActivity))
                    binding.mistralKey.setText(Prefs.mistralKey(this@SettingsActivity))
                    binding.deepseekKey.setText(Prefs.deepseekKey(this@SettingsActivity))
                    rememberProfileBaseline()
                    binding.msg.text = error?.let {
                        getString(R.string.signed_out_revoke_warning, it)
                    } ?: getString(R.string.signed_out_local)
                }
            }
        }
    }

    private fun renderCameraSettings() {
        binding.cameraProfileGroup.check(when (Prefs.cameraProfile(this)) {
            Prefs.CAMERA_PROFILE_LOW -> R.id.cameraProfileLow
            Prefs.CAMERA_PROFILE_DETAIL -> R.id.cameraProfileDetail
            else -> R.id.cameraProfileFast
        })
        binding.continuousLight.isChecked = Prefs.torchEnabled(this)
        binding.sharpenPreview.isEnabled =
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
        binding.sharpenPreview.isChecked = Prefs.sharpenPreview(this)
    }

    private fun renderOcrOverlaySettings() {
        val show = Prefs.showOcrRegions(this)
        binding.showOcrRegions.isChecked = show
        binding.ocrRegionOptions.alpha = if (show) 1f else .5f
        binding.ocrRegionOpacity.isEnabled = show
        binding.showOcrRegionLabels.isEnabled = show
        binding.ocrRegionOpacity.progress = Prefs.ocrRegionOpacityPercent(this)
        binding.ocrRegionOpacityLabel.text = getString(
            R.string.set_ocr_region_opacity_value,
            Prefs.ocrRegionOpacityPercent(this),
        )
        binding.showOcrRegionLabels.isChecked = Prefs.showOcrRegionLabels(this)
    }

    // --- captures on this phone ----------------------------------------------

    private fun chooseExportFolder() {
        val existing = Prefs.exportTreeUri(this).takeIf { it.isNotBlank() }
        exportFolderPicker.launch(existing?.let { runCatching { Uri.parse(it) }.getOrNull() })
    }

    private fun exportCaptures() {
        val tree = Prefs.exportTreeUri(this)
        if (!Prefs.exportGrantHeld(this, tree)) {
            binding.exportStatus.text = getString(
                if (tree.isBlank()) R.string.set_export_no_folder
                else R.string.set_export_permission_lost,
            )
            return
        }
        lifecycleScope.launch {
            val pending = withContext(Dispatchers.IO) {
                CaptureExport.plan(
                    CaptureExport.collect(this@SettingsActivity),
                    CaptureExport.ExportLedger.exported(this@SettingsActivity, tree),
                ).pending
            }
            if (pending == 0) {
                binding.exportStatus.text = getString(R.string.set_export_nothing)
                return@launch
            }
            CaptureExportWorker.enqueue(this@SettingsActivity, tree, fullReexport = false)
            binding.exportStatus.text = getString(R.string.set_export_started, pending)
        }
    }

    private fun renderCaptureStorage() {
        val tree = Prefs.exportTreeUri(this)
        binding.exportFolder.text = when {
            tree.isBlank() -> getString(R.string.set_export_no_folder)
            !Prefs.exportGrantHeld(this, tree) ->
                getString(R.string.set_export_permission_lost)
            // The tree Uri's last path segment is the provider's document id,
            // which is the closest thing to a human-readable folder name that
            // is available without a query against the provider.
            else -> getString(
                R.string.set_export_folder,
                Uri.decode(tree.substringAfterLast('/')),
            )
        }
        binding.exportNow.isEnabled = Prefs.exportGrantHeld(this, tree)
        Prefs.lastExportSummary(this)?.let {
            binding.exportStatus.text = getString(
                R.string.set_export_last, it.exported, it.unchanged, it.failed,
            )
        }
        lifecycleScope.launch {
            val summary = withContext(Dispatchers.IO) {
                Triple(
                    Entries.recent(this@SettingsActivity).size,
                    CaptureArchive.archivedIds(this@SettingsActivity).size,
                    CaptureArchive.archiveBytes(this@SettingsActivity),
                )
            }
            binding.archiveSummary.text = getString(
                R.string.set_archive_summary,
                summary.first,
                summary.second,
                formatByteSize(summary.third),
            )
        }
    }

    private fun renderPostProcessingSettings() {
        val preset = Prefs.postProcessingPreset(this)
        binding.postProcessingPresetGroup.check(when (preset) {
            PostProcessingPreset.AUTOMATIC_BY_DATE -> R.id.postProcessingPresetAutomatic
            PostProcessingPreset.MODERN_1950_AND_LATER -> R.id.postProcessingPresetModern
            PostProcessingPreset.OLDER_1850_TO_1949 -> R.id.postProcessingPresetOlder
            PostProcessingPreset.EARLY_BEFORE_1850 -> R.id.postProcessingPresetEarly
        })
        val features = Prefs.postProcessingFeatures(this)
        binding.postProcessingDewarp.isChecked =
            features.dewarpPerspectiveAndPageCurvature
        binding.postProcessingMarginCrop.isChecked =
            features.cropToDetectedPageMargins
        binding.postProcessingContrast.isChecked =
            features.normalizePageAndTextContrast
        binding.postProcessingSpineCrop.isChecked = features.detectAndCropSpine
    }

    private fun rememberProfileBaseline() {
        baselineDisplayName = binding.displayName.text.toString()
        baselineMistral = binding.mistralKey.text.toString()
        baselineDeepseek = binding.deepseekKey.text.toString()
    }
}
