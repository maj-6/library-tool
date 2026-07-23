package org.whl.bookcapture

import android.app.Dialog
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Bundle
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.tabs.TabLayout
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityEntryDetailBinding
import java.io.File

/** Catalog-style presentation for one historical or recently captured book. */
class EntryDetailActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_ID = "entry_id"
        private const val STATE_OCR_EXPANDED = "ocr_expanded"
        private const val STATE_DIAGNOSTICS_EXPANDED = "diagnostics_expanded"
        private const val STATE_DIAGNOSTICS_TAB = "diagnostics_tab"
        private const val STATE_DIAGNOSTICS_SCROLL = "diagnostics_scroll"
        private const val DIAGNOSTICS_TAB_JSON = 0
        private const val DIAGNOSTICS_TAB_MISTRAL = 1
    }

    private lateinit var binding: ActivityEntryDetailBinding
    private var photoJob: Job? = null
    private var viewerJob: Job? = null
    private var viewerDialog: Dialog? = null
    private var ocrExpanded = false
    private var diagnosticsExpanded = false
    private var diagnosticsTab = DIAGNOSTICS_TAB_JSON
    private var diagnosticsContent: BookDiagnosticsContent? = null
    private var diagnosticsEntry: Entries.Entry? = null
    private var diagnosticsLoadedEntryId: String? = null
    private var diagnosticsRequestedEntryId: String? = null
    private var diagnosticsJob: Job? = null
    private var diagnosticsLoadGeneration = 0
    private var diagnosticsPendingScrollY: Int? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityEntryDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)
        ocrExpanded = savedInstanceState?.getBoolean(STATE_OCR_EXPANDED, false) ?: false
        diagnosticsExpanded = savedInstanceState
            ?.getBoolean(STATE_DIAGNOSTICS_EXPANDED, false) ?: false
        diagnosticsTab = savedInstanceState
            ?.getInt(STATE_DIAGNOSTICS_TAB, DIAGNOSTICS_TAB_JSON)
            ?.coerceIn(DIAGNOSTICS_TAB_JSON, DIAGNOSTICS_TAB_MISTRAL)
            ?: DIAGNOSTICS_TAB_JSON
        diagnosticsPendingScrollY = savedInstanceState
            ?.getInt(STATE_DIAGNOSTICS_SCROLL, 0)
        binding.toolbar.setNavigationOnClickListener { finish() }
        binding.ocrToggle.setOnClickListener {
            ocrExpanded = !ocrExpanded
            renderOcrExpansion()
        }
        configureDiagnosticsPanel()
        val entryId = intent.getStringExtra(EXTRA_ID).orEmpty()
        for (workName in listOf(
            ProcessWorker.UNIQUE_WORK_NAME,
            ProcessWorker.BACKLOG_WORK_NAME,
            ProcessWorker.workNameForEntry(entryId),
            CaptureMetadataSyncWorker.WORK_NAME,
            CaptureMetadataSyncWorker.PULL_WORK_NAME,
        )) {
            WorkManager.getInstance(this)
                .getWorkInfosForUniqueWorkLiveData(workName)
                .observe(this) { render() }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putBoolean(STATE_OCR_EXPANDED, ocrExpanded)
        outState.putBoolean(STATE_DIAGNOSTICS_EXPANDED, diagnosticsExpanded)
        outState.putInt(STATE_DIAGNOSTICS_TAB, diagnosticsTab)
        outState.putInt(STATE_DIAGNOSTICS_SCROLL, binding.diagnosticsScroll.scrollY)
        super.onSaveInstanceState(outState)
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onDestroy() {
        photoJob?.cancel()
        diagnosticsJob?.cancel()
        viewerJob?.cancel()
        viewerDialog?.dismiss()
        viewerDialog = null
        super.onDestroy()
    }

    private fun render() {
        val entry = Entries.find(this, intent.getStringExtra(EXTRA_ID).orEmpty())
            ?: return finish()
        diagnosticsEntry = entry
        val details = BookDetailPresenter.from(entry.meta)

        binding.title.text = details.title.ifEmpty { getString(R.string.detail_untitled) }
        binding.author.text = details.author
        binding.author.visibility = details.author.visibleOrGone()
        binding.year.text = details.year
        binding.year.visibility = details.year.visibleOrGone()
        binding.volumeTag.text = details.volumeTag
        binding.volumeTag.contentDescription = details.volumeTag
        binding.volumeTag.visibility = details.volumeTag.visibleOrGone()
        renderFields(binding.secondaryDetails, details.secondary, compact = false)

        binding.stateLine.text = listOf(
            Entries.statusLabel(this, entry),
            resources.getQuantityString(R.plurals.capture_count, entry.photoCount, entry.photoCount),
            android.text.format.DateFormat.format("yyyy-MM-dd HH:mm", entry.createdAt),
        ).joinToString("  \u00b7  ")

        binding.overviewText.text = details.overview
        binding.overviewSection.visibility = details.overview.visibleOrGone()
        val otherFields = details.other + desktopDetailFields(entry)
        renderFields(binding.otherFields, otherFields, compact = true)
        binding.otherSection.visibility = if (otherFields.isEmpty()) View.GONE else View.VISIBLE
        renderOwnership(entry)

        binding.ocrText.text = entry.ocrText().ifEmpty { getString(R.string.detail_no_ocr) }
        renderOcrExpansion()
        if (diagnosticsExpanded) loadDiagnostics(entry, force = true)
        renderPhotos(entry)
    }

    private fun renderOwnership(entry: Entries.Entry) {
        val ownership = cloudUploadOwnership(
            readCaptureCreator(this, entry.dir),
            Prefs.userId(this),
        )
        binding.ownershipNotice.visibility =
            if (!entry.uploaded && ownership != CloudUploadOwnership.ALLOWED) View.VISIBLE else View.GONE
        binding.ownershipNotice.text = when (ownership) {
            CloudUploadOwnership.ALLOWED -> ""
            CloudUploadOwnership.NEEDS_CLAIM -> getString(
                if (Auth.signedIn(this)) R.string.detail_local_claim_available
                else R.string.detail_local_sign_in_to_claim,
            )
            CloudUploadOwnership.DIFFERENT_ACCOUNT -> getString(R.string.detail_different_account)
        }
        binding.claimCloud.visibility = if (
            !entry.uploaded && ownership == CloudUploadOwnership.NEEDS_CLAIM && Auth.signedIn(this)
        ) View.VISIBLE else View.GONE
        binding.claimCloud.setOnClickListener { showClaimConfirmation(entry.id) }
    }

    private fun desktopDetailFields(entry: Entries.Entry): List<BookDetailField> {
        val desktop = entry.desktopBook ?: return emptyList()
        val fields = mutableListOf<BookDetailField>()
        val copyright = desktop.copyright
        if (desktop.registered || copyright.status.isNotBlank()) {
            val evidence = buildList {
                if (copyright.registrationRecords.isNotEmpty()) add(resources.getQuantityString(
                    R.plurals.detail_registration_count,
                    copyright.registrationRecords.size,
                    copyright.registrationRecords.size,
                ))
                if (copyright.renewalRecords.isNotEmpty()) add(resources.getQuantityString(
                    R.plurals.detail_renewal_count,
                    copyright.renewalRecords.size,
                    copyright.renewalRecords.size,
                ))
            }
            fields += BookDetailField(
                getString(R.string.detail_copyright),
                (listOf(copyright.status.ifBlank { getString(R.string.detail_unknown) }) + evidence)
                    .joinToString(" \u00b7 "),
            )
        }
        fields += BookDetailField(
            getString(R.string.detail_whl_availability),
            desktopAvailabilityText(desktop.whl),
        )
        fields += BookDetailField(
            getString(R.string.detail_ia_availability),
            desktopAvailabilityText(desktop.internetArchive),
        )
        desktop.scanStatus.takeIf(String::isNotBlank)?.let {
            fields += BookDetailField(getString(R.string.detail_scan_status), it)
        }
        if (desktop.remarks.isNotEmpty()) {
            fields += BookDetailField(
                getString(R.string.detail_remarks),
                desktop.remarks.joinToString("\n"),
            )
        }
        val localReview = entry.captureReview
        val needsReview = localReview?.needsReview == true
        val needsAttention = localReview?.needsAttention == true || needsReview
        if (needsAttention) {
            val reason = localReview?.attentionReason.orEmpty()
            fields += BookDetailField(
                getString(if (needsReview) R.string.home_needs_review else R.string.home_needs_attention),
                reason.ifBlank { getString(R.string.detail_marked) },
            )
        }
        return fields
    }

    private fun desktopAvailabilityText(availability: DesktopAvailability): String {
        val state = getString(when (availability.state) {
            DesktopAvailabilityState.AVAILABLE -> R.string.detail_available
            DesktopAvailabilityState.UNAVAILABLE -> R.string.detail_unavailable
            DesktopAvailabilityState.UNKNOWN -> R.string.detail_unknown
        })
        val detail = availability.detail.ifBlank { availability.identifier }
        return if (detail.isBlank()) state else "$state \u00b7 $detail"
    }

    private fun renderFields(
        container: LinearLayout,
        fields: List<BookDetailField>,
        compact: Boolean,
    ) {
        container.removeAllViews()
        fields.forEach { field ->
            val row = fieldRow(field.label, field.value, compact)
            RemoteUiCatalog.apply(row)
            container.addView(row)
        }
    }

    private fun renderOcrExpansion() {
        binding.ocrText.visibility = if (ocrExpanded) View.VISIBLE else View.GONE
        binding.ocrToggle.setIconResource(
            if (ocrExpanded) R.drawable.ic_expand_less else R.drawable.ic_expand_more,
        )
        binding.ocrToggle.contentDescription = getString(
            if (ocrExpanded) R.string.detail_collapse_ocr else R.string.detail_expand_ocr,
        )
        binding.ocrToggle.isSelected = ocrExpanded
        RemoteUiCatalog.apply(binding.ocrToggle)
    }

    private fun configureDiagnosticsPanel() {
        binding.diagnosticsTabs.apply {
            addTab(newTab()
                .setText(R.string.detail_diagnostics_json_tab)
                .setContentDescription(R.string.detail_diagnostics_json_tab_description))
            addTab(newTab()
                .setText(R.string.detail_diagnostics_mistral_tab)
                .setContentDescription(R.string.detail_diagnostics_mistral_tab_description))
            getTabAt(diagnosticsTab)?.select()
            addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
                override fun onTabSelected(tab: TabLayout.Tab) {
                    diagnosticsTab = tab.position
                    diagnosticsPendingScrollY = null
                    renderDiagnosticsText()
                    if (diagnosticsExpanded && diagnosticsContent == null) {
                        diagnosticsEntry?.let { loadDiagnostics(it, force = false) }
                    }
                    binding.diagnosticsScroll.post { binding.diagnosticsScroll.scrollTo(0, 0) }
                }

                override fun onTabUnselected(tab: TabLayout.Tab) = Unit
                override fun onTabReselected(tab: TabLayout.Tab) = Unit
            })
        }
        binding.diagnosticsToggle.setOnClickListener {
            diagnosticsExpanded = !diagnosticsExpanded
            if (diagnosticsExpanded) {
                diagnosticsEntry?.let { loadDiagnostics(it, force = false) }
            } else {
                diagnosticsPendingScrollY = binding.diagnosticsScroll.scrollY
                cancelDiagnosticsLoad()
            }
            renderDiagnosticsExpansion()
        }
        renderDiagnosticsExpansion()
    }

    private fun loadDiagnostics(entry: Entries.Entry, force: Boolean) {
        if (!diagnosticsExpanded) return
        if (diagnosticsJob?.isActive == true && diagnosticsRequestedEntryId == entry.id) return
        if (!force && diagnosticsLoadedEntryId == entry.id && diagnosticsContent != null) return

        diagnosticsJob?.cancel()
        diagnosticsRequestedEntryId = entry.id
        if (diagnosticsLoadedEntryId != entry.id) {
            diagnosticsContent = null
            diagnosticsLoadedEntryId = null
            renderDiagnosticsText()
        }
        val generation = ++diagnosticsLoadGeneration
        diagnosticsJob = lifecycleScope.launch {
            val content = withContext(Dispatchers.IO) { BookDiagnosticsPresenter.from(entry) }
            if (generation != diagnosticsLoadGeneration || !diagnosticsExpanded) return@launch
            diagnosticsContent = content
            diagnosticsLoadedEntryId = entry.id
            diagnosticsRequestedEntryId = null
            diagnosticsJob = null
            renderDiagnosticsText()
        }
    }

    private fun cancelDiagnosticsLoad() {
        diagnosticsLoadGeneration++
        diagnosticsJob?.cancel()
        diagnosticsJob = null
        diagnosticsRequestedEntryId = null
        diagnosticsContent = null
        diagnosticsLoadedEntryId = null
    }

    private fun renderDiagnosticsExpansion() {
        binding.diagnosticsContent.visibility = if (diagnosticsExpanded) View.VISIBLE else View.GONE
        binding.diagnosticsToggle.setIconResource(
            if (diagnosticsExpanded) R.drawable.ic_expand_less else R.drawable.ic_expand_more,
        )
        binding.diagnosticsToggle.contentDescription = getString(
            if (diagnosticsExpanded) {
                R.string.detail_collapse_diagnostics
            } else {
                R.string.detail_expand_diagnostics
            },
        )
        binding.diagnosticsToggle.isSelected = diagnosticsExpanded
        ViewCompat.setStateDescription(
            binding.diagnosticsToggle,
            getString(if (diagnosticsExpanded) R.string.expanded else R.string.collapsed),
        )
        RemoteUiCatalog.apply(binding.diagnosticsToggle)
    }

    private fun renderDiagnosticsText() {
        val content = diagnosticsContent
        if (content == null) {
            binding.diagnosticsText.text = getString(R.string.detail_loading_book_data)
            return
        }
        binding.diagnosticsText.text = when (diagnosticsTab) {
            DIAGNOSTICS_TAB_MISTRAL -> {
                if (content.mistralSections.isEmpty()) {
                    getString(R.string.detail_no_persisted_mistral)
                } else {
                    content.mistralSections.joinToString("\n\n\u2014\u2014\u2014\n\n") { section ->
                        val heading = when (section.kind) {
                            Entries.MistralResponseKind.OCR -> getString(
                                R.string.detail_mistral_capture_heading,
                                section.captureOrder ?: 1,
                            )
                            Entries.MistralResponseKind.EXTRACTION -> getString(
                                R.string.detail_mistral_extraction_heading,
                            )
                        }
                        if (section.validJson) {
                            "$heading\n\n${section.humanReadableBody}"
                        } else {
                            "$heading\n\n${getString(R.string.detail_invalid_mistral_json)}" +
                                "\n\n${section.humanReadableBody}"
                        }
                    }
                }
            }
            else -> content.bookJson?.let(::syntaxHighlightedJson)
                ?: getString(R.string.detail_no_book_json)
        }
        diagnosticsPendingScrollY?.let { scrollY ->
            diagnosticsPendingScrollY = null
            binding.diagnosticsScroll.post { binding.diagnosticsScroll.scrollTo(0, scrollY) }
        }
    }

    private fun syntaxHighlightedJson(json: String): CharSequence {
        val styled = SpannableString(json)
        JsonSyntaxTokenizer.tokenize(json).forEach { token ->
            val color = when (token.kind) {
                JsonSyntaxKind.KEY -> R.color.whl_cyan
                JsonSyntaxKind.STRING -> R.color.whl_green
                JsonSyntaxKind.NUMBER -> R.color.whl_amber
                JsonSyntaxKind.BOOLEAN, JsonSyntaxKind.NULL -> R.color.whl_red
                JsonSyntaxKind.PUNCTUATION -> R.color.whl_ink_dim
            }
            styled.setSpan(
                ForegroundColorSpan(getColor(color)),
                token.start,
                token.end,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
            )
        }
        return styled
    }

    private fun renderPhotos(entry: Entries.Entry) {
        photoJob?.cancel()
        binding.photos.removeAllViews()
        binding.heroPhoto.setPhotoBitmap(null)
        val descriptors = entry.photoDescriptors()
        val heroFile = entry.detailHeroPhoto()
        val hero = heroFile?.let(entry::photoDescriptor)
        binding.photoSection.visibility = if (hero == null) View.GONE else View.VISIBLE
        if (hero == null) return

        val others = descriptors.filterNot { it.assetId == hero.assetId }
        binding.otherPhotosLabel.visibility = if (others.isEmpty()) View.GONE else View.VISIBLE
        binding.photosScroll.visibility = if (others.isEmpty()) View.GONE else View.VISIBLE
        binding.heroState.text = photoStatusLabel(hero, hero.order)
        binding.heroState.visibility = View.VISIBLE
        binding.heroPhoto.setOnClickListener {
            showPhotoViewer(hero, photoStatusLabel(hero, hero.order))
        }

        photoJob = lifecycleScope.launch {
            val (heroBitmap, heroOriginal) = withContext(Dispatchers.IO) {
                val display = decodeSampledOriented(
                    hero.displayFile,
                    maxWidth = 1800,
                    maxHeight = 1800,
                )
                val original = if (hero.originalPreserved && hero.rawFile.isFile) {
                    decodeSampledOriented(hero.rawFile, maxWidth = 1800, maxHeight = 1800)
                } else null
                display to original
            }
            if (heroBitmap != null) {
                binding.heroPhoto.setPhotoBitmap(heroBitmap)
                installOriginalHold(binding.heroPhoto, hero, heroBitmap, heroOriginal)
            }
            applyOverlay(binding.heroPhoto, hero)

            others.forEach { descriptor ->
                val (bitmap, original) = withContext(Dispatchers.IO) {
                    val decoded = decodeSampledOriented(
                        descriptor.displayFile,
                        maxWidth = 420,
                        maxHeight = 420,
                    ) ?: return@withContext null to null
                    val display = if (descriptor.postProcessingPending) {
                        softenedThumbnail(decoded)
                    } else decoded
                    val raw = if (descriptor.originalPreserved && descriptor.rawFile.isFile) {
                        decodeSampledOriented(
                            descriptor.rawFile,
                            maxWidth = 420,
                            maxHeight = 420,
                        )
                    } else null
                    display to raw
                }
                bitmap ?: return@forEach
                addThumbnail(descriptor, bitmap, original)
            }
        }
    }

    private fun addThumbnail(
        descriptor: EntryPhotoDescriptor,
        bitmap: Bitmap,
        original: Bitmap?,
    ) {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(0, 0, resources.getDimensionPixelSize(R.dimen.detail_thumbnail_gap), 0)
        }
        val height = resources.getDimensionPixelSize(R.dimen.detail_thumbnail_height)
        val width = (height * bitmap.width / bitmap.height.coerceAtLeast(1)).coerceAtLeast(height / 2)
        val image = ZoomablePhotoView(this).apply {
            layoutParams = LinearLayout.LayoutParams(width, height)
            setBackgroundResource(R.drawable.whl_photo_frame)
            setPadding(1, 1, 1, 1)
            setPhotoBitmap(bitmap)
            alpha = if (descriptor.postProcessingPending) .82f else 1f
            contentDescription = getString(
                if (descriptor.postProcessingPending) {
                    R.string.detail_photo_pending_description
                } else {
                    R.string.detail_photo_description
                },
                descriptor.order + 1,
            )
            setOnClickListener {
                showPhotoViewer(descriptor, photoStatusLabel(descriptor, descriptor.order))
            }
        }
        applyOverlay(image, descriptor)
        installOriginalHold(image, descriptor, bitmap, original)
        column.addView(image)
        column.addView(TextView(this).apply {
            text = photoStatusLabel(descriptor, descriptor.order)
            setTextColor(getColor(R.color.whl_ink_dim))
            textSize = 11f
            gravity = Gravity.CENTER
            maxLines = 1
            setPadding(3, 4, 3, 0)
        }, LinearLayout.LayoutParams(width, ViewGroup.LayoutParams.WRAP_CONTENT))
        RemoteUiCatalog.apply(column)
        binding.photos.addView(column)
    }

    /** Press-and-hold compares the retained camera original in place. Releasing
     * restores the corrected display revision and its revision-bound OCR boxes. */
    private fun installOriginalHold(
        view: ZoomablePhotoView,
        descriptor: EntryPhotoDescriptor,
        display: Bitmap,
        original: Bitmap?,
    ) {
        view.onOriginalHoldChanged = original?.let { raw ->
            { showingOriginal ->
                view.setPhotoBitmap(if (showingOriginal) raw else display)
                if (showingOriginal) {
                    view.setOverlayRegions(emptyList())
                } else {
                    applyOverlay(view, descriptor)
                }
            }
        }
    }

    /** A pending derivative remains visibly distinct without altering its
     * immutable source. The verified cloud result replaces this preview. */
    private fun softenedThumbnail(source: Bitmap): Bitmap {
        if (source.width < 4 || source.height < 4) return source
        val small = Bitmap.createScaledBitmap(
            source,
            (source.width / 12).coerceAtLeast(2),
            (source.height / 12).coerceAtLeast(2),
            true,
        )
        if (small === source) return source
        val softened = Bitmap.createScaledBitmap(small, source.width, source.height, true)
        small.recycle()
        if (softened !== source) source.recycle()
        return softened
    }

    private fun showPhotoViewer(descriptor: EntryPhotoDescriptor, label: String) {
        viewerJob?.cancel()
        viewerDialog?.dismiss()
        val dialog = Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen)
        val root = android.widget.FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        val photo = ZoomablePhotoView(this).apply {
            zoomEnabled = true
            contentDescription = label
        }
        root.addView(photo, android.widget.FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT,
        ))

        val topBar = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            orientation = LinearLayout.HORIZONTAL
            setPadding(8, 8, 8, 8)
            setBackgroundColor(0xB522211F.toInt())
        }
        val close = ImageButton(this).apply {
            setImageResource(R.drawable.ic_close_detail)
            imageTintList = android.content.res.ColorStateList.valueOf(Color.WHITE)
            setBackgroundColor(Color.TRANSPARENT)
            contentDescription = getString(R.string.close)
            minimumWidth = resources.getDimensionPixelSize(R.dimen.touch_target)
            minimumHeight = resources.getDimensionPixelSize(R.dimen.touch_target)
            setOnClickListener { dialog.dismiss() }
        }
        topBar.addView(close)
        topBar.addView(TextView(this).apply {
            text = label
            setTextColor(Color.WHITE)
            textSize = 15f
            maxLines = 2
            setPadding(8, 0, 8, 0)
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))

        val canCompare = descriptor.rawFile.canonicalPath != descriptor.displayFile.canonicalPath &&
            descriptor.rawFile.isFile
        val compare = MaterialButton(this).apply {
            text = getString(R.string.detail_original)
            setIconResource(R.drawable.ic_compare_original)
            iconTint = android.content.res.ColorStateList.valueOf(Color.WHITE)
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.TRANSPARENT)
            isAllCaps = false
            contentDescription = getString(R.string.detail_compare_description)
            visibility = if (canCompare) View.VISIBLE else View.GONE
        }
        topBar.addView(compare)
        root.addView(topBar, android.widget.FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.TOP,
        ))

        var showingOriginal = false
        fun show(original: Boolean) {
            if (!canCompare && original) return
            showingOriginal = original
            compare.text = getString(
                if (original) R.string.detail_processed else R.string.detail_original,
            )
            RemoteUiCatalog.apply(compare)
            val target = if (original) descriptor.rawFile else descriptor.displayFile
            viewerJob?.cancel()
            viewerJob = lifecycleScope.launch {
                val bitmap = withContext(Dispatchers.IO) {
                    decodeSampledOriented(target, maxWidth = 3000, maxHeight = 3000)
                } ?: return@launch
                if (dialog.isShowing) {
                    photo.setPhotoBitmap(bitmap)
                    if (original) photo.setOverlayRegions(emptyList())
                    else applyOverlay(photo, descriptor)
                }
            }
        }
        compare.setOnClickListener { show(!showingOriginal) }
        photo.onOriginalHoldChanged = if (canCompare) { original -> show(original) } else null
        dialog.setContentView(root)
        dialog.setOnDismissListener {
            viewerJob?.cancel()
            if (viewerDialog === dialog) viewerDialog = null
        }
        viewerDialog = dialog
        dialog.show()
        show(false)
        RemoteUiCatalog.apply(dialog)
    }

    /** Geometry is revision-bound by EntryPhotoDescriptor; unrecognized or
     * geometry-less provider responses intentionally render no decorative box. */
    private fun overlayRegions(descriptor: EntryPhotoDescriptor): List<PhotoOverlayRegion> =
        descriptor.geometry.mapNotNull { region ->
            val points = region.normalizedPolygon.map { point ->
                android.graphics.PointF(point.x.toFloat(), point.y.toFloat())
            }
            points.takeIf { it.size >= 3 }?.let {
                PhotoOverlayRegion(it, region.label)
            }
        }

    private fun applyOverlay(view: ZoomablePhotoView, descriptor: EntryPhotoDescriptor) {
        val regions = if (Prefs.showOcrRegions(this)) overlayRegions(descriptor) else emptyList()
        view.setOverlayRegions(
            regions,
            opacity = Prefs.ocrRegionOpacityPercent(this) / 100f,
            labels = Prefs.showOcrRegionLabels(this),
        )
    }

    private fun roleLabel(descriptor: EntryPhotoDescriptor, order: Int): String {
        val role = descriptor.role.toString()
            .lowercase()
            .replace('_', ' ')
            .replaceFirstChar { it.titlecase() }
        if (role == "Other" || role == "Unknown") {
            return getString(R.string.detail_page_number, order + 1)
        }
        return if (descriptor.roleSuggested) getString(R.string.detail_suggested_role, role) else role
    }

    private fun photoStatusLabel(descriptor: EntryPhotoDescriptor, order: Int): String {
        val label = roleLabel(descriptor, order)
        return if (descriptor.postProcessingPending) {
            getString(R.string.detail_cleanup_pending, label)
        } else label
    }

    private fun showClaimConfirmation(entryId: String) {
        val dialog = MaterialAlertDialogBuilder(this)
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
        RemoteUiCatalog.apply(dialog)
    }

    private fun fieldRow(label: String, value: String, compact: Boolean): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.TOP
            setPadding(0, if (compact) 5 else 4, 0, if (compact) 5 else 4)
        }
        val labelView = TextView(this).apply {
            textSize = if (compact) 11f else 12f
            setTextColor(getColor(R.color.whl_ink_dim))
            text = label
        }
        val valueView = TextView(this).apply {
            if (compact) typeface = android.graphics.Typeface.MONOSPACE
            textSize = if (compact) 11f else 13f
            setTextColor(getColor(R.color.whl_ink))
            text = value
            setPadding(10, 0, 0, 0)
            setTextIsSelectable(true)
        }
        row.addView(labelView, LinearLayout.LayoutParams(
            resources.getDimensionPixelSize(
                if (compact) R.dimen.detail_field_label_width else R.dimen.detail_secondary_label_width,
            ),
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ))
        row.addView(valueView, LinearLayout.LayoutParams(
            0,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            1f,
        ))
        return row
    }

    private fun String.visibleOrGone(): Int = if (isBlank()) View.GONE else View.VISIBLE
}
