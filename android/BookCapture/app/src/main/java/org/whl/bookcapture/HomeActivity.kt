package org.whl.bookcapture

import android.app.Activity
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.Typeface
import android.os.Bundle
import android.text.method.LinkMovementMethod
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.ArrayAdapter
import android.widget.Spinner
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.text.HtmlCompat
import androidx.core.view.MenuCompat
import androidx.core.view.ViewCompat
import androidx.core.view.accessibility.AccessibilityNodeInfoCompat
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import com.google.android.material.button.MaterialButton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityHomeBinding
import org.json.JSONObject
import java.util.UUID
import kotlin.math.roundToInt

/**
 * The landing screen. Launching the app opens HERE, not the camera: a list of
 * recent scans — each a page thumbnail, the extracted title / author / year (or
 * "Processing…" until the pipeline catches up), and its status (pending upload,
 * uploaded, imported). Tapping a scan opens the full detail (all photos, OCR
 * text, every field). "New scan" is the way into capture.
 *
 * This screen is the local-first entry point and nudges whichever configured
 * delivery path is available; cloud-only actions remain account-gated.
 */
class HomeActivity : AppCompatActivity() {

    private enum class HomeTab { SCANS, COLLECTIONS, INSPECT }

    private enum class InspectViewMode(val wireValue: String, val columns: Int, val layout: Int) {
        TILES("tiles", 2, R.layout.item_inspect_tile),
        CONTENT("content", 1, R.layout.item_inspect_content),
        ICONS("icons", 3, R.layout.item_inspect_icon);

        companion object {
            fun fromWire(value: String?): InspectViewMode =
                entries.firstOrNull { it.wireValue == value } ?: TILES
        }
    }

    private data class ScanListItem(
        val entry: Entries.Entry,
        val titleLabel: String,
        val statusLabel: String,
        val thumbnail: EntryPhotoDescriptor?,
    )

    private data class ScanListSnapshot(
        val items: List<ScanListItem>,
        val collectionPaths: Map<String, String>,
        val currentCollection: BookCollection?,
    )

    private data class InspectListItem(
        val collectionId: String,
        val summary: CollectionInventorySummary,
        val current: Entries.Entry?,
        val statusLabel: String?,
        val thumbnail: EntryPhotoDescriptor?,
    )

    private data class InspectSnapshot(
        val collections: List<BookCollection>,
        val collectionPaths: Map<String, String>,
        val items: List<InspectListItem>,
        val currentCollection: BookCollection?,
    )

    private lateinit var binding: ActivityHomeBinding
    private var thumbJob: Job? = null
    private var scanListJob: Job? = null
    private var inspectListJob: Job? = null
    private var workerRefreshJob: Job? = null
    private var pendingWorkerContentRefresh = false
    private var pendingCollectionRefresh = false
    private val dynamicThumbnailViews = linkedSetOf<ImageView>()
    private val dynamicThumbnailBitmaps = linkedMapOf<ImageView, Bitmap>()
    private var activeTab = HomeTab.SCANS
    private var inspectedCollectionId: String? = null
    private var inspectViewMode = InspectViewMode.TILES
    private var reportedTagConflict = ""
    private val expandedScanGroups = linkedSetOf<String>()
    private var scanGroupsInitialized = false
    private var syncFeedbackRequestId: String? = null
    private var syncFeedbackPhase: CaptureSyncPhase? = null

    private val qrScanner = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val raw = result.data?.getStringExtra(QrScannerActivity.EXTRA_TAG_ID)
        val collection = findCollectionByTagId(Collections.allRecords(this), raw.orEmpty())
        if (collection == null) {
            val normalized = normalizeCollectionTagId(raw.orEmpty())
            Toast.makeText(
                this,
                if (normalized.isEmpty()) getString(R.string.inspect_scan_invalid)
                else getString(R.string.inspect_scan_unknown, normalized),
                Toast.LENGTH_LONG,
            ).show()
            return@registerForActivityResult
        }
        inspectedCollectionId = collection.id
        activeTab = HomeTab.INSPECT
        Toast.makeText(
            this,
            getString(R.string.inspect_scan_matched, collection.name, collection.tagId),
            Toast.LENGTH_SHORT,
        ).show()
        showTab(HomeTab.INSPECT)
        binding.homeScroll.post { binding.homeScroll.fullScroll(View.FOCUS_UP) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityHomeBinding.inflate(layoutInflater)
        setContentView(binding.root)
        activeTab = savedInstanceState?.getString(STATE_ACTIVE_TAB)
            ?.let { runCatching { HomeTab.valueOf(it) }.getOrNull() }
            ?: if (savedInstanceState?.getBoolean(STATE_TAB_COLLECTIONS) == true) {
                HomeTab.COLLECTIONS
            } else {
                HomeTab.SCANS
            }
        inspectedCollectionId = savedInstanceState?.getString(STATE_INSPECTED_COLLECTION)
        inspectViewMode = InspectViewMode.fromWire(
            savedInstanceState?.getString(STATE_INSPECT_VIEW_MODE) ?: Prefs.inspectViewMode(this),
        )
        scanGroupsInitialized =
            savedInstanceState?.getBoolean(STATE_SCAN_GROUPS_INITIALIZED) ?: false
        syncFeedbackRequestId = savedInstanceState?.getString(STATE_SYNC_FEEDBACK_REQUEST)
        syncFeedbackPhase = savedInstanceState?.getString(STATE_SYNC_FEEDBACK_PHASE)
            ?.let(CaptureSyncPhase::fromStoredValue)
        savedInstanceState?.getStringArrayList(STATE_EXPANDED_SCAN_GROUPS)
            ?.let(expandedScanGroups::addAll)

        binding.tabScans.setOnClickListener { showTab(HomeTab.SCANS) }
        binding.tabCollections.setOnClickListener { showTab(HomeTab.COLLECTIONS) }
        binding.tabInspect.setOnClickListener { showTab(HomeTab.INSPECT) }
        binding.collectionBar.setOnClickListener { showTab(HomeTab.COLLECTIONS) }
        binding.newCollection.setOnClickListener { editCollection(null) }
        binding.scanBox.setOnClickListener {
            qrScanner.launch(Intent(this, QrScannerActivity::class.java))
        }
        binding.inspectViewModes.addOnButtonCheckedListener { _, checkedId, isChecked ->
            if (!isChecked) return@addOnButtonCheckedListener
            inspectViewMode = when (checkedId) {
                R.id.inspectModeContent -> InspectViewMode.CONTENT
                R.id.inspectModeIcons -> InspectViewMode.ICONS
                else -> InspectViewMode.TILES
            }
            Prefs.setInspectViewMode(this, inspectViewMode.wireValue)
            if (activeTab == HomeTab.INSPECT) refreshInspect()
        }
        binding.inspectViewModes.check(
            when (inspectViewMode) {
                InspectViewMode.TILES -> R.id.inspectModeTiles
                InspectViewMode.CONTENT -> R.id.inspectModeContent
                InspectViewMode.ICONS -> R.id.inspectModeIcons
            },
        )
        binding.newScan.setOnClickListener {
            // A book has to belong to a batch, so the origin is never guessed
            // later. With nothing chosen, send the user to pick rather than
            // starting a capture that would have no provenance.
            //
            // An already-open capture is exempt: it chose its collection when it
            // started, and this screen is the app's only route back to the
            // camera — gating it would strand a half-photographed book with no
            // way to seal or discard it.
            val resuming = Prefs.currentEntryId(this) != null
            if (!resuming && Collections.current(this) == null) {
                Toast.makeText(this, R.string.collections_choose_first, Toast.LENGTH_LONG).show()
                showTab(HomeTab.COLLECTIONS)
                return@setOnClickListener
            }
            startActivity(Intent(this, MainActivity::class.java))
        }
        binding.appMenu.setOnClickListener { showAppMenu() }
        binding.configWarning.setOnClickListener {
            startActivity(Intent(this, LoginActivity::class.java))
        }
        binding.syncCaptures.setOnClickListener { syncCaptures() }
        // when background OCR / upload lands, the list re-renders itself
        val workManager = WorkManager.getInstance(this)
        workManager
            .getWorkInfosLiveData(activeUniqueWorkQuery(
                ProcessWorker.UNIQUE_WORK_NAME,
                ProcessWorker.BACKLOG_WORK_NAME,
                UploadWorker.EXPLICIT_SYNC_WORK_NAME,
                CaptureMetadataSyncWorker.WORK_NAME,
                CaptureMetadataSyncWorker.PULL_WORK_NAME,
            ))
            .observe(this) {
                scheduleWorkerRefresh(contentChanged = true)
            }
        workManager
            .getWorkInfosLiveData(activeUniqueWorkQuery(CollectionSyncWorker.WORK_NAME))
            .observe(this) {
                scheduleWorkerRefresh(contentChanged = false)
            }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_ACTIVE_TAB, activeTab.name)
        outState.putBoolean(STATE_TAB_COLLECTIONS, activeTab == HomeTab.COLLECTIONS)
        outState.putString(STATE_INSPECTED_COLLECTION, inspectedCollectionId)
        outState.putString(STATE_INSPECT_VIEW_MODE, inspectViewMode.wireValue)
        outState.putBoolean(STATE_SCAN_GROUPS_INITIALIZED, scanGroupsInitialized)
        outState.putString(STATE_SYNC_FEEDBACK_REQUEST, syncFeedbackRequestId)
        outState.putString(STATE_SYNC_FEEDBACK_PHASE, syncFeedbackPhase?.storedValue)
        outState.putStringArrayList(
            STATE_EXPANDED_SCAN_GROUPS,
            ArrayList(expandedScanGroups),
        )
    }

    override fun onResume() {
        super.onResume()
        val signedIn = Auth.signedIn(this)
        binding.configWarning.visibility = if (signedIn) View.GONE else View.VISIBLE
        // A previously authorized batch may resume after process death, but a
        // new upload batch is created only by the Sync captures button.
        UploadWorker.kick(this)
        ProcessWorker.enqueue(this)
        CollectionSyncWorker.enqueueCoalesced(this)
        CaptureMetadataSyncWorker.enqueuePull(this)
        Prefs.activeCaptureSyncRecord(this)?.let { active ->
            if (syncFeedbackRequestId == null) {
                syncFeedbackRequestId = active.requestId
                syncFeedbackPhase = active.phase
            }
        }
        cancelScheduledWorkerRefresh()
        refreshSyncButton()
        showCollectionTagConflictIfNeeded()
        showTab(activeTab)
    }

    private fun showCollectionTagConflictIfNeeded() {
        val tagId = Prefs.collectionTagConflict(this)
        if (tagId.isEmpty()) {
            reportedTagConflict = ""
            return
        }
        if (tagId == reportedTagConflict) return
        reportedTagConflict = tagId
        Toast.makeText(
            this,
            getString(R.string.collections_sync_tag_conflict, tagId),
            Toast.LENGTH_LONG,
        ).show()
    }

    override fun onStop() {
        stopHomeLoading()
        super.onStop()
    }

    // --- the app menu, hung off the mark in the toolbar ----------------------

    private fun showAppMenu() {
        val menu = androidx.appcompat.widget.PopupMenu(this, binding.appMenu)
        menu.menuInflater.inflate(R.menu.home_app_menu, menu.menu)
        RemoteUiCatalog.apply(this, menu.menu)
        menu.menu.findItem(R.id.menuSignOut).isVisible = Auth.signedIn(this)
        MenuCompat.setGroupDividerEnabled(menu.menu, true)
        menu.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.menuSettings -> {
                    startActivity(Intent(this, SettingsActivity::class.java)); true
                }
                R.id.menuAbout -> { showAbout(); true }
                R.id.menuCheckUpdates -> { checkForUpdates(); true }
                R.id.menuSignOut -> { signOut(); true }
                else -> false
            }
        }
        menu.show()
    }

    private fun showAbout() {
        val view = layoutInflater.inflate(R.layout.dialog_about, null)
        view.findViewById<TextView>(R.id.aboutTitle).text = getString(R.string.about_title)
        view.findViewById<TextView>(R.id.aboutVersion).text =
            getString(R.string.about_version, BuildConfig.VERSION_NAME)
        view.findViewById<TextView>(R.id.aboutDescription).apply {
            text = HtmlCompat.fromHtml(
                getString(R.string.about_description_html),
                HtmlCompat.FROM_HTML_MODE_COMPACT,
            )
            movementMethod = LinkMovementMethod.getInstance()
        }
        view.findViewById<TextView>(R.id.aboutChangelog).text = aboutChangelog()
        RemoteUiCatalog.apply(view)
        val dialog = AlertDialog.Builder(this)
            .setView(view)
            .setPositiveButton(R.string.about_close, null)
            .show()
        RemoteUiCatalog.apply(dialog)
    }

    private fun aboutChangelog(): String {
        if (BuildConfig.DEBUG) return getString(R.string.about_changelog_not_included)
        val resourceId = resources.getIdentifier("android_changelog", "raw", packageName)
        if (resourceId == 0) return getString(R.string.about_changelog_not_included)
        val markdown = resources.openRawResource(resourceId).bufferedReader().use { it.readText() }
        return formatChangelogForAbout(markdown)
    }

    private fun signOut() {
        lifecycleScope.launch {
            val error = withContext(Dispatchers.IO) { Auth.signOut(this@HomeActivity) }
            binding.configWarning.visibility = View.VISIBLE
            refreshCollectionBar()
            Toast.makeText(
                this@HomeActivity,
                error?.let { getString(R.string.signed_out_revoke_warning, it) }
                    ?: getString(R.string.signed_out_local),
                Toast.LENGTH_LONG,
            ).show()
        }
    }

    /** Refresh remote in-app icons and strings without offering an uncertified APK. */
    private fun checkForUpdates() {
        Toast.makeText(this, R.string.update_checking, Toast.LENGTH_SHORT).show()
        lifecycleScope.launch {
            val outcome = withContext(Dispatchers.IO) {
                runCatching { Updates.check(this@HomeActivity) }
            }
            fun say(message: String) =
                Toast.makeText(this@HomeActivity, message, Toast.LENGTH_LONG).show()
            when (outcome.getOrNull()) {
                null -> say(getString(R.string.update_failed))
                Updates.Result.NotConfigured -> say(getString(R.string.update_not_configured))
                Updates.Result.UiCurrent -> say(getString(R.string.update_resources_current))
                Updates.Result.UiUpdated -> say(getString(R.string.update_resources_updated))
            }
        }
    }

    // --- tabs ----------------------------------------------------------------

    private fun showTab(tab: HomeTab) {
        activeTab = tab
        if (tab != HomeTab.SCANS) cancelScanListLoading()
        if (tab != HomeTab.INSPECT) cancelInspectListLoading()
        binding.homeList.visibility = if (tab == HomeTab.SCANS) View.VISIBLE else View.GONE
        binding.collectionsList.visibility =
            if (tab == HomeTab.COLLECTIONS) View.VISIBLE else View.GONE
        binding.inspectPane.visibility = if (tab == HomeTab.INSPECT) View.VISIBLE else View.GONE
        binding.scanActions.visibility = if (tab == HomeTab.SCANS) View.VISIBLE else View.GONE
        binding.newCollection.visibility =
            if (tab == HomeTab.COLLECTIONS) View.VISIBLE else View.GONE
        binding.scanBox.visibility = if (tab == HomeTab.INSPECT) View.VISIBLE else View.GONE
        binding.collectionBar.visibility = if (tab == HomeTab.SCANS) View.VISIBLE else View.GONE
        emphasizeTab(binding.tabScans, tab == HomeTab.SCANS)
        emphasizeTab(binding.tabCollections, tab == HomeTab.COLLECTIONS)
        emphasizeTab(binding.tabInspect, tab == HomeTab.INSPECT)
        when (tab) {
            HomeTab.SCANS -> refreshHome()
            HomeTab.COLLECTIONS -> refreshCollections()
            HomeTab.INSPECT -> refreshInspect()
        }
    }

    private fun refreshContentTab() {
        when (activeTab) {
            HomeTab.SCANS -> refreshHome()
            HomeTab.COLLECTIONS -> refreshCollections()
            HomeTab.INSPECT -> refreshInspect()
        }
    }

    /**
     * A worker transition often reaches several unique-work observers at once.
     * Treat those callbacks as invalidations and rebuild once after the burst.
     */
    private fun scheduleWorkerRefresh(contentChanged: Boolean) {
        if (contentChanged) pendingWorkerContentRefresh = true
        else pendingCollectionRefresh = true
        if (workerRefreshJob?.isActive == true) return
        workerRefreshJob = lifecycleScope.launch {
            delay(WORK_REFRESH_COALESCE_MS)
            val refreshContent = pendingWorkerContentRefresh
            val collectionChanged = pendingCollectionRefresh
            pendingWorkerContentRefresh = false
            pendingCollectionRefresh = false
            workerRefreshJob = null
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
            if (collectionChanged) showCollectionTagConflictIfNeeded()
            if (refreshContent) {
                refreshSyncButton()
                refreshContentTab()
            } else if (collectionChanged) {
                when (activeTab) {
                    HomeTab.SCANS -> refreshCollectionBar()
                    HomeTab.COLLECTIONS -> refreshCollections()
                    HomeTab.INSPECT -> refreshInspect()
                }
            }
        }
    }

    private fun cancelScheduledWorkerRefresh() {
        workerRefreshJob?.cancel()
        workerRefreshJob = null
        pendingWorkerContentRefresh = false
        pendingCollectionRefresh = false
    }

    private fun syncCaptures() {
        val canSync = Prefs.transport(this) != "cloud" || Auth.signedIn(this)
        if (!canSync) {
            Toast.makeText(
                this,
                RemoteUiCatalog.text(this, R.string.home_sync_sign_in),
                Toast.LENGTH_LONG,
            ).show()
            return
        }
        val pendingReviewChanges = Entries.recent(this).any {
            CaptureMetadataStore.hasPendingReviewSync(it.dir)
        }
        val state = UploadWorker.enqueueExplicitSync(this)
        CaptureMetadataSyncWorker.enqueueExplicitSync(this)
        Prefs.captureSyncRecord(this)?.takeIf { state.active }?.let { record ->
            syncFeedbackRequestId = record.requestId
            syncFeedbackPhase = state.phase
        }
        refreshSyncButton(state)
        if (state.requestedCount == 0)
            Toast.makeText(
                this,
                RemoteUiCatalog.text(
                    this,
                    if (pendingReviewChanges) R.string.home_sync_review_queued
                    else R.string.home_sync_none,
                ),
                Toast.LENGTH_SHORT,
            ).show()
    }

    private fun refreshSyncButton(
        state: CaptureSyncState = UploadWorker.captureSyncState(this),
    ) {
        val record = Prefs.captureSyncRecord(this)
        if (record?.requestId == syncFeedbackRequestId) {
            val previous = syncFeedbackPhase
            syncFeedbackPhase = state.phase
            if (previous?.active == true && !state.active) {
                val message = when (state.phase) {
                    CaptureSyncPhase.COMPLETE -> RemoteUiCatalog.text(
                        this, R.string.home_sync_complete, state.syncedCount,
                    )
                    CaptureSyncPhase.COMPLETE_WITH_ERRORS -> RemoteUiCatalog.text(
                        this,
                        R.string.home_sync_partial,
                        state.syncedCount,
                        state.blockedCount,
                    )
                    else -> RemoteUiCatalog.text(this, R.string.home_sync_failed)
                }
                Toast.makeText(this, message, Toast.LENGTH_LONG).show()
                binding.syncCaptures.announceForAccessibility(message)
                syncFeedbackRequestId = null
                syncFeedbackPhase = null
            }
        }
        binding.syncCaptures.isEnabled = !state.active
        binding.syncCaptures.alpha = if (state.active) .72f else 1f
        binding.syncCaptures.text = when {
            state.phase == CaptureSyncPhase.QUEUED ->
                RemoteUiCatalog.text(this, R.string.home_sync_queued)
            state.active && state.requestedCount > 0 -> RemoteUiCatalog.text(
                this,
                R.string.home_sync_running,
                state.syncedCount,
                state.requestedCount,
            )
            else -> RemoteUiCatalog.text(this, R.string.home_sync_captures)
        }
    }

    private fun emphasizeTab(button: MaterialButton, on: Boolean) {
        button.isSelected = on
        button.alpha = if (on) 1f else .5f
        button.setTypeface(null, if (on) Typeface.BOLD else Typeface.NORMAL)
        ViewCompat.setStateDescription(
            button,
            getString(R.string.selection_selected_state).takeIf { on },
        )
    }

    // --- collections ---------------------------------------------------------

    private fun refreshCollectionBar() {
        val current = Collections.current(this)
        val paths = collectionDisplayPaths(Collections.allRecords(this))
        renderCollectionBar(current, paths)
    }

    private fun renderCollectionBar(
        current: BookCollection?,
        paths: Map<String, String>,
    ) {
        binding.collectionBar.text = if (current == null) {
            getString(R.string.collections_none_selected)
        } else {
            getString(R.string.collections_current, paths[current.id] ?: current.name)
        }
        binding.collectionBar.setTextColor(
            getColor(if (current == null) R.color.whl_amber else R.color.whl_ink_dim))
    }

    private fun refreshCollections() {
        resetThumbnailLoading()
        val list = binding.collectionsList
        list.removeAllViews()
        val collections = Collections.all(this)
        val current = Collections.current(this)
        val collectionRecords = Collections.allRecords(this)
        val collectionPaths = collectionDisplayPaths(collectionRecords)
        refreshCollectionBar()
        if (collections.isEmpty()) {
            list.addView(emptyNotice(getString(R.string.collections_empty)))
            return
        }
        val counts = CollectionInventory.items(this)
            .mapNotNull { resolvedLiveCollectionId(it.summary.collectionId, collectionRecords) }
            .groupingBy { it }.eachCount()
        val inflater = LayoutInflater.from(this)
        for (c in collections) {
            val row = inflater.inflate(R.layout.item_collection, list, false)
            val isCurrent = c.id == current?.id
            row.isSelected = isCurrent
            ViewCompat.setStateDescription(
                row,
                getString(R.string.collections_current_state).takeIf { isCurrent },
            )
            val name = row.findViewById<TextView>(R.id.name)
            val displayName = collectionPaths[c.id] ?: c.name
            name.text = displayName
            name.setTypeface(name.typeface, if (isCurrent) Typeface.BOLD else Typeface.NORMAL)
            row.setBackgroundResource(
                if (isCurrent) R.drawable.whl_collection_current else R.drawable.whl_row)
            row.findViewById<TextView>(R.id.sub).text = listOf(
                c.tagId,
                if (c.from.isEmpty()) getString(R.string.collections_row_no_from)
                else getString(R.string.collections_row_from, c.from),
                resources.getQuantityString(
                    R.plurals.collections_row_books, counts[c.id] ?: 0, counts[c.id] ?: 0),
            ).filter { it.isNotEmpty() }.joinToString(" · ")
            val edit = row.findViewById<View>(R.id.editCollection)
            edit.contentDescription = getString(R.string.collections_edit_description, c.name)
            edit.setOnClickListener { editCollection(c) }
            val delete = row.findViewById<View>(R.id.deleteCollection)
            delete.contentDescription = getString(R.string.collections_delete_description, c.name)
            delete.setOnClickListener {
                confirmDeleteCollection(c)
            }
            row.setOnClickListener {
                Prefs.setCurrentCollectionId(this, c.id)
                expandedScanGroups.add(c.id)
                Toast.makeText(
                    this,
                    getString(R.string.collections_current, displayName),
                    Toast.LENGTH_SHORT,
                ).show()
                refreshCollections()
            }
            RemoteUiCatalog.apply(row)
            list.addView(row)
        }
    }

    private fun emptyNotice(text: String): TextView = TextView(this).apply {
        typeface = Typeface.MONOSPACE
        textSize = 13f
        setTextColor(getColor(R.color.whl_ink_dim))
        setPadding(28, 40, 28, 28)
        this.text = text
    }.also { RemoteUiCatalog.apply(it) }

    /** Add ([existing] null) or edit one collection. */
    private fun editCollection(existing: BookCollection?) {
        // Keep the identity chosen by this edit. Sync can append cloud rows as
        // soon as mutate() returns, so selecting the list's last row would race
        // that pull and could choose a different collection.
        val collectionId = existing?.id ?: UUID.randomUUID().toString()
        val collectionRecords = Collections.allRecords(this)
        val collections = collectionRecords.filter { !it.deleted && it.mergedInto == null }
        val collectionPaths = collectionDisplayPaths(collectionRecords)
        val view = layoutInflater.inflate(R.layout.dialog_collection, null)
        val nameField = view.findViewById<android.widget.EditText>(R.id.collectionName)
        val tagIdField = view.findViewById<android.widget.EditText>(R.id.collectionTagId)
        val parentField = view.findViewById<Spinner>(R.id.collectionParent)
        val fromField = view.findViewById<android.widget.EditText>(R.id.collectionFrom)
        val parentIds = mutableListOf<String?>(null)
        val parentLabels = mutableListOf(getString(R.string.collections_parent_none))
        collectionParentCandidates(collections, collectionId)
            .sortedBy { (collectionPaths[it.id] ?: it.name).lowercase() }
            .forEach { parent ->
                parentIds += parent.id
                parentLabels += collectionPaths[parent.id] ?: parent.name
            }
        existing?.parentId?.takeIf { it !in parentIds }?.let { missingParentId ->
            parentIds += missingParentId
            parentLabels += getString(R.string.collections_parent_unavailable)
        }
        parentField.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            parentLabels,
        ).apply {
            setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        }
        parentField.setSelection(parentIds.indexOf(existing?.parentId).coerceAtLeast(0))
        nameField.setText(existing?.name.orEmpty())
        tagIdField.setText(existing?.tagId.orEmpty())
        var tagIdEdited = false
        tagIdField.doAfterTextChanged { tagIdEdited = true }
        if (existing == null) {
            nameField.doAfterTextChanged { value ->
                if (tagIdField.text.isNullOrBlank() && !value.isNullOrBlank()) {
                    tagIdField.hint = suggestCollectionTagId(value.toString(), collectionRecords)
                }
            }
        }
        fromField.setText(existing?.from.orEmpty())
        val dialog = AlertDialog.Builder(this)
            .setTitle(
                if (existing == null) R.string.collections_add_title
                else R.string.collections_edit_title)
            .setView(view)
            .create()
        view.findViewById<View>(R.id.cancelCollectionEdit).setOnClickListener {
            dialog.dismiss()
        }
        view.findViewById<View>(R.id.saveCollectionEdit).setOnClickListener {
            val name = nameField.text.toString()
            val enteredTagId = tagIdField.text.toString()
            // A cleared/new tag means "suggest one". Include tombstones so a
            // printed label from a retired box is never offered again.
            val tagId = if (existing != null && !tagIdEdited) null else {
                enteredTagId.ifBlank {
                    suggestCollectionTagId(
                        name,
                        Collections.allRecords(this),
                        exceptId = existing?.id,
                    )
                }
            }
            val from = fromField.text.toString()
            val parentId = parentIds.getOrNull(parentField.selectedItemPosition)
            val error = Collections.mutate(this) { current ->
                if (existing == null) {
                    addCollection(
                        current,
                        name,
                        from,
                        id = collectionId,
                        parentId = parentId,
                        tagId = tagId,
                    )
                } else {
                    updateCollection(
                        current,
                        existing.id,
                        name,
                        from,
                        parentId,
                        tagId = tagId,
                    )
                }
            }
            if (error != null) {
                Toast.makeText(this, error, Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            // A collection the user just created is almost certainly the one
            // they are about to scan into; select it so the next tap works.
            if (existing == null) {
                Prefs.setCurrentCollectionId(this, collectionId)
                expandedScanGroups.add(collectionId)
            }
            dialog.dismiss()
            refreshCollections()
        }
        dialog.show()
        RemoteUiCatalog.apply(dialog)
    }

    private fun confirmDeleteCollection(collection: BookCollection) {
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.collections_delete_title)
            .setMessage(getString(R.string.collections_delete_message, collection.name))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.collections_delete) { _, _ ->
                if (!Collections.delete(this, collection.id)) {
                    Toast.makeText(this, R.string.collections_delete_failed, Toast.LENGTH_LONG)
                        .show()
                }
                refreshCollections()
            }
            .show()
        RemoteUiCatalog.apply(dialog)
    }

    // --- inspect -------------------------------------------------------------

    /** Follow only explicit merge aliases. A normal deleted collection remains
     * historical and does not silently become a different physical box. */
    private fun resolvedLiveCollectionId(
        collectionId: String,
        records: List<BookCollection>,
    ): String? {
        if (collectionId.isEmpty()) return null
        val byId = records.associateBy { it.id }
        val visited = mutableSetOf<String>()
        var cursor = collectionId
        while (visited.add(cursor)) {
            val row = byId[cursor] ?: return null
            val merged = row.mergedInto
            if (merged != null) {
                cursor = merged
                continue
            }
            return row.id.takeIf { !row.deleted }
        }
        return null
    }

    private fun refreshInspect() {
        resetThumbnailLoading()
        inspectListJob?.cancel()
        inspectListJob = lifecycleScope.launch {
            val snapshot = withContext(Dispatchers.IO) {
                loadInspectSnapshot()
            }
            inspectListJob = null
            if (activeTab != HomeTab.INSPECT ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            renderInspect(snapshot)
        }
    }

    private fun loadInspectSnapshot(): InspectSnapshot {
        val records = Collections.allRecords(this)
        val live = records.filter { !it.deleted && it.mergedInto == null }
        val paths = collectionDisplayPaths(records)
        val collections = live.sortedBy { (paths[it.id] ?: it.name).lowercase() }
        val items = CollectionInventory.items(this).mapNotNull { item ->
            val collectionId =
                resolvedLiveCollectionId(item.summary.collectionId, records) ?: return@mapNotNull null
            InspectListItem(
                collectionId = collectionId,
                summary = item.summary,
                current = item.current,
                statusLabel = item.current?.let { Entries.statusLabel(this, it) },
                thumbnail = item.current?.thumbnailDescriptor(),
            )
        }
        return InspectSnapshot(
            collections = collections,
            collectionPaths = paths,
            items = items,
            currentCollection = Collections.current(this),
        )
    }

    private fun renderInspect(snapshot: InspectSnapshot) {
        val collections = snapshot.collections
        val paths = snapshot.collectionPaths
        val counts = snapshot.items.groupingBy(InspectListItem::collectionId).eachCount()
        val totalBooks = counts.values.sum()

        val collectionCount = resources.getQuantityString(
            R.plurals.inspect_collection_count,
            collections.size,
            collections.size,
        )
        val totalBookCount = resources.getQuantityString(
            R.plurals.inspect_book_count,
            totalBooks,
            totalBooks,
        )
        binding.inspectSummary.text = getString(
            R.string.inspect_summary,
            collectionCount,
            totalBookCount,
        )
        binding.inspectCollectionChips.removeAllViews()
        binding.inspectBooks.removeAllViews()

        if (collections.isEmpty()) {
            inspectedCollectionId = null
            binding.inspectSelectionHeader.visibility = View.GONE
            binding.inspectEmpty.visibility = View.VISIBLE
            binding.inspectEmpty.text = getString(R.string.inspect_empty_collections)
            return
        }

        val selected = collections.firstOrNull { it.id == inspectedCollectionId }
            ?: snapshot.currentCollection?.let { current ->
                collections.firstOrNull { it.id == current.id }
            }
            ?: collections.first()
        inspectedCollectionId = selected.id

        var selectedChip: View? = null
        val inflater = LayoutInflater.from(this)
        collections.forEach { collection ->
            val chip = inflater.inflate(
                R.layout.item_inspect_collection,
                binding.inspectCollectionChips,
                false,
            ) as MaterialButton
            val count = counts[collection.id] ?: 0
            val bookCount = resources.getQuantityString(
                R.plurals.inspect_book_count,
                count,
                count,
            )
            val displayName = paths[collection.id] ?: collection.name
            chip.text = getString(
                R.string.inspect_collection_chip,
                displayName,
                collection.tagId,
                bookCount,
            )
            chip.contentDescription = getString(
                R.string.inspect_collection_description,
                displayName,
                collection.tagId,
                bookCount,
            )
            val isSelected = collection.id == selected.id
            chip.isSelected = isSelected
            ViewCompat.setStateDescription(
                chip,
                getString(R.string.selection_selected_state).takeIf { isSelected },
            )
            chip.strokeWidth = dp(if (isSelected) 2 else 1)
            chip.strokeColor = ColorStateList.valueOf(
                getColor(if (isSelected) R.color.whl_cyan else R.color.whl_face_sh2),
            )
            chip.backgroundTintList = ColorStateList.valueOf(
                getColor(if (isSelected) R.color.whl_row_checked else R.color.whl_face_hi),
            )
            chip.setOnClickListener {
                inspectedCollectionId = collection.id
                refreshInspect()
            }
            RemoteUiCatalog.apply(chip)
            binding.inspectCollectionChips.addView(chip)
            if (isSelected) selectedChip = chip
        }
        selectedChip?.let { chip ->
            binding.inspectCollectionScroll.post {
                binding.inspectCollectionScroll.smoothScrollTo(
                    (chip.left - dp(10)).coerceAtLeast(0),
                    0,
                )
            }
        }

        val selectedName = paths[selected.id] ?: selected.name
        val selectedItems = snapshot.items.asSequence()
            .filter { it.collectionId == selected.id }
            .sortedWith(
                compareBy<InspectListItem> { item ->
                    item.summary.title.ifEmpty {
                        getString(R.string.inspect_book_untitled)
                    }.lowercase()
                }.thenByDescending { it.summary.createdAt },
            )
            .toList()
        binding.inspectSelectionHeader.visibility = View.VISIBLE
        binding.inspectCollectionName.text = selectedName
        val selectedBookCount = resources.getQuantityString(
            R.plurals.inspect_book_count,
            selectedItems.size,
            selectedItems.size,
        )
        val origin = if (selected.from.isEmpty()) {
            ""
        } else {
            " \u00b7 ${getString(R.string.collections_row_from, selected.from)}"
        }
        binding.inspectCollectionMeta.text = getString(
            R.string.inspect_collection_meta,
            selected.tagId,
            selectedBookCount,
            origin,
        )

        if (selectedItems.isEmpty()) {
            binding.inspectEmpty.visibility = View.VISIBLE
            binding.inspectEmpty.text = getString(R.string.inspect_empty_books)
            return
        }
        binding.inspectEmpty.visibility = View.GONE
        renderInspectBooks(selectedItems)
    }

    private fun renderInspectBooks(items: List<InspectListItem>) {
        val inflater = LayoutInflater.from(this)
        val thumbnails = mutableListOf<Triple<ImageView, java.io.File, Boolean>>()
        items.chunked(inspectViewMode.columns).forEach { chunk ->
            val row = LinearLayout(this).apply {
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
                orientation = LinearLayout.HORIZONTAL
                gravity = android.view.Gravity.TOP
            }
            chunk.forEach { item ->
                val view = inflater.inflate(inspectViewMode.layout, row, false)
                view.layoutParams = LinearLayout.LayoutParams(
                    0,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    1f,
                )
                bindInspectBook(view, item, thumbnails)
                RemoteUiCatalog.apply(view)
                row.addView(view)
            }
            repeat(inspectViewMode.columns - chunk.size) {
                row.addView(Space(this), LinearLayout.LayoutParams(0, 1, 1f))
            }
            binding.inspectBooks.addView(row)
        }
        thumbJob = lifecycleScope.launch(Dispatchers.IO) {
            for ((image, file, cleanupPending) in thumbnails) {
                var decodedBitmap: Bitmap? = null
                try {
                    val decoded = decodeSampledOriented(file, maxWidth = 384, maxHeight = 512)
                        ?: continue
                    decodedBitmap = decoded
                    if (cleanupPending) decodedBitmap = softenPendingThumbnail(decoded)
                    withContext(Dispatchers.Main) {
                        val bitmap = decodedBitmap ?: return@withContext
                        if (activeTab != HomeTab.INSPECT ||
                            image.parent == null ||
                            !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
                        ) return@withContext
                        setDynamicThumbnail(image, bitmap)
                        decodedBitmap = null
                    }
                } finally {
                    decodedBitmap?.takeIf { !it.isRecycled }?.recycle()
                }
            }
        }
    }

    private fun bindInspectBook(
        view: View,
        item: InspectListItem,
        thumbnails: MutableList<Triple<ImageView, java.io.File, Boolean>>,
    ) {
        val summary = item.summary
        view.findViewById<TextView>(R.id.inspectTitle).text =
            summary.title.ifEmpty { getString(R.string.inspect_book_untitled) }
        val details = mutableListOf<String>()
        summary.author.takeIf { it.isNotEmpty() }?.let(details::add)
        summary.year.takeIf { it.isNotEmpty() }?.let(details::add)
        if (inspectViewMode != InspectViewMode.ICONS) {
            details += resources.getQuantityString(
                R.plurals.inspect_book_pages,
                summary.photoCount,
                summary.photoCount,
            )
            details += item.statusLabel ?: getString(R.string.inspect_book_archived)
        }
        view.findViewById<TextView>(R.id.inspectSubtitle).text =
            details.joinToString(" \u00b7 ")
        item.thumbnail?.let { descriptor ->
            val image = view.findViewById<ImageView>(R.id.inspectThumb)
            image.alpha = if (descriptor.postProcessingPending) .82f else 1f
            thumbnails += Triple(
                image,
                descriptor.displayFile,
                descriptor.postProcessingPending,
            )
        }
        val open = {
            if (item.current != null) openEntryDetails(summary.entryId)
            else Toast.makeText(
                this,
                R.string.inspect_book_unavailable,
                Toast.LENGTH_LONG,
            ).show()
        }
        view.setOnClickListener { open() }
        view.findViewById<View>(R.id.inspectOpen)?.apply {
            visibility = if (item.current == null) View.INVISIBLE else View.VISIBLE
            setOnClickListener { open() }
        }
    }

    override fun onDestroy() {
        stopHomeLoading()
        super.onDestroy()
    }

    private fun refreshHome() {
        resetThumbnailLoading()
        scanListJob?.cancel()
        scanListJob = lifecycleScope.launch {
            val snapshot = withContext(Dispatchers.IO) {
                val entries = Entries.recent(this@HomeActivity)
                val records = Collections.allRecords(this@HomeActivity)
                ScanListSnapshot(
                    items = entries.map { entry ->
                        ScanListItem(
                            entry = entry,
                            titleLabel = Entries.titleLabel(this@HomeActivity, entry),
                            statusLabel = Entries.statusLabel(this@HomeActivity, entry),
                            thumbnail = entry.thumbnailDescriptor(),
                        )
                    },
                    collectionPaths = collectionDisplayPaths(records),
                    currentCollection = Collections.current(this@HomeActivity),
                )
            }
            scanListJob = null
            if (activeTab != HomeTab.SCANS ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            renderHome(snapshot)
        }
    }

    private fun renderHome(snapshot: ScanListSnapshot) {
        val list = binding.homeList
        list.removeAllViews()
        val entries = snapshot.items.map(ScanListItem::entry)
        val itemById = snapshot.items.associateBy { it.entry.id }
        renderCollectionBar(snapshot.currentCollection, snapshot.collectionPaths)
        if (entries.isEmpty()) {
            list.addView(emptyNotice(getString(R.string.home_empty)))
            return
        }
        val inflater = LayoutInflater.from(this)
        val thumbs = ArrayList<Triple<ImageView, java.io.File, Boolean>>()
        val knownCollectionPaths = snapshot.collectionPaths
        val currentCollectionId = snapshot.currentCollection?.id
        val groups = groupScansByCollection(
            items = entries,
            currentCollectionId = currentCollectionId,
            collectionId = { it.provenance?.collectionId },
            collectionLabel = { entry ->
                entry.provenance?.collectionId?.let(knownCollectionPaths::get)
                    .orEmpty().ifEmpty {
                        collectionDisplayLabel(entry.from, entry.collectionName)
                    }
            },
            unfiledLabel = getString(R.string.home_group_unfiled),
        )
        if (!scanGroupsInitialized) {
            initiallyExpandedScanGroup(groups, currentCollectionId)?.let(expandedScanGroups::add)
            scanGroupsInitialized = true
        }
        val compact = Prefs.compactScanList(this)
        for (group in groups) {
            val expanded = group.key in expandedScanGroups
            val header = inflater.inflate(R.layout.item_scan_group, list, false)
            val groupName = header.findViewById<TextView>(R.id.groupName)
            groupName.text = group.label
            groupName.setTypeface(
                groupName.typeface,
                if (group.key == currentCollectionId) Typeface.BOLD else Typeface.NORMAL,
            )
            val count = resources.getQuantityString(
                R.plurals.home_group_scan_count,
                group.items.size,
                group.items.size,
            )
            header.findViewById<TextView>(R.id.groupCount).text = count
            header.findViewById<ImageView>(R.id.groupChevron).setImageResource(
                if (expanded) R.drawable.ic_expand_more else R.drawable.ic_chevron_right)
            header.contentDescription = getString(
                if (expanded) R.string.home_group_collapse else R.string.home_group_expand,
                group.label,
                count,
            )
            header.setOnClickListener {
                if (!expandedScanGroups.add(group.key)) expandedScanGroups.remove(group.key)
                refreshHome()
            }
            RemoteUiCatalog.apply(header)
            list.addView(header)
            if (!expanded) continue

            for (e in group.items) {
                val item = checkNotNull(itemById[e.id])
                val row = inflater.inflate(R.layout.item_home, list, false)
                row.findViewById<TextView>(R.id.title).text = item.titleLabel
                row.findViewById<TextView>(R.id.sub).text =
                    listOf(
                        e.author,
                        e.year,
                        resources.getQuantityString(
                            R.plurals.capture_count, e.photoCount, e.photoCount),
                        if (e.from.isEmpty()) ""
                        else getString(R.string.collections_row_from, e.from),
                    ).filter { it.isNotEmpty() }.joinToString(" · ")
                val state = item.statusLabel
                val presentation = homeStatusPresentation(state)
                row.findViewById<TextView>(R.id.state).apply {
                    text = presentation.text
                    visibility = if (presentation.text.isEmpty()) View.GONE else View.VISIBLE
                }
                row.findViewById<ProgressBar>(R.id.waitingIndicator).apply {
                    visibility = if (presentation.adornment == HomeStatusAdornment.WAITING)
                        View.VISIBLE else View.GONE
                    contentDescription = getString(R.string.home_status_waiting)
                }
                row.findViewById<ImageView>(R.id.stateIcon).apply {
                    visibility = if (presentation.adornment == HomeStatusAdornment.UPLOADED)
                        View.VISIBLE else View.GONE
                    contentDescription = getString(
                        if (presentation.accessibilityLabel == "imported")
                            R.string.home_status_imported else R.string.home_status_uploaded,
                    )
                }
                row.findViewById<View>(R.id.marker)
                    .setBackgroundColor(getColor(markerColor(state)))
                val thumb = row.findViewById<ImageView>(R.id.thumb)
                applyScanListLayout(row, thumb, compact)
                item.thumbnail?.let { descriptor ->
                    val cleanupPending = descriptor.postProcessingPending
                    thumb.alpha = if (cleanupPending) .82f else 1f
                    thumbs.add(Triple(thumb, descriptor.displayFile, cleanupPending))
                }
                bindDesktopMetadata(row, e)
                val openBook = {
                    openEntryDetails(e.id)
                }
                val markAttention = {
                    showEntryAttentionDialog(this, e.id) { refreshHome() }
                }
                row.setOnClickListener { openBook() }
                row.setOnLongClickListener {
                    markAttention()
                    true
                }
                configureScanRowAccessibility(row, openBook, markAttention)
                RemoteUiCatalog.apply(row)
                list.addView(row)
            }
        }
        // decode the page thumbnails off the UI thread, in list order
        thumbJob = lifecycleScope.launch(Dispatchers.IO) {
            for ((iv, file, cleanupPending) in thumbs) {
                var decodedBitmap: Bitmap? = null
                try {
                    val decoded = decodeSampledOriented(file, maxWidth = 512, maxHeight = 512)
                        ?: continue
                    decodedBitmap = decoded
                    if (cleanupPending) decodedBitmap = softenPendingThumbnail(decoded)
                    withContext(Dispatchers.Main) {
                        val bitmap = decodedBitmap ?: return@withContext
                        if (activeTab != HomeTab.SCANS ||
                            iv.parent == null ||
                            !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
                        ) return@withContext
                        setDynamicThumbnail(iv, bitmap)
                        decodedBitmap = null
                    }
                } finally {
                    decodedBitmap?.takeIf { !it.isRecycled }?.recycle()
                }
            }
        }
    }

    private fun setDynamicThumbnail(image: ImageView, bitmap: Bitmap) {
        dynamicThumbnailViews.add(image)
        image.setImageBitmap(bitmap)
        dynamicThumbnailBitmaps.put(image, bitmap)
            ?.takeIf { previous -> previous !== bitmap && !previous.isRecycled }
            ?.recycle()
    }

    private fun resetThumbnailLoading() {
        thumbJob?.cancel()
        thumbJob = null
        releaseDynamicThumbnails()
    }

    private fun cancelScanListLoading() {
        scanListJob?.cancel()
        scanListJob = null
    }

    private fun cancelInspectListLoading() {
        inspectListJob?.cancel()
        inspectListJob = null
    }

    private fun releaseDynamicThumbnails() {
        dynamicThumbnailViews.forEach { it.setImageDrawable(null) }
        dynamicThumbnailViews.clear()
        dynamicThumbnailBitmaps.values.forEach { bitmap ->
            if (!bitmap.isRecycled) bitmap.recycle()
        }
        dynamicThumbnailBitmaps.clear()
    }

    private fun stopHomeLoading() {
        cancelScheduledWorkerRefresh()
        cancelScanListLoading()
        cancelInspectListLoading()
        resetThumbnailLoading()
    }

    private fun configureScanRowAccessibility(
        row: View,
        openBook: () -> Unit,
        markAttention: () -> Unit,
    ) {
        val summaryIds = listOf(
            R.id.title,
            R.id.sub,
            R.id.state,
            R.id.waitingIndicator,
            R.id.stateIcon,
            R.id.whlAvailability,
            R.id.internetArchiveAvailability,
            R.id.scanStatus,
            R.id.remarksStatus,
            R.id.attentionStatus,
        )
        row.contentDescription = summaryIds.mapNotNull { id ->
            row.findViewById<View>(id).takeIf { it.visibility == View.VISIBLE }?.let { child ->
                when (child) {
                    is TextView -> child.text?.toString()
                    else -> child.contentDescription?.toString()
                }?.trim()?.takeIf(String::isNotEmpty)
            }
        }.distinct().joinToString(". ")
        summaryIds.forEach { id ->
            row.findViewById<View>(id).importantForAccessibility =
                View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        row.findViewById<View>(R.id.thumb).importantForAccessibility =
            View.IMPORTANT_FOR_ACCESSIBILITY_NO
        ViewCompat.setScreenReaderFocusable(row, true)
        ViewCompat.replaceAccessibilityAction(
            row,
            AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_CLICK,
            getString(R.string.home_open_details),
        ) { _, _ ->
            openBook()
            true
        }
        ViewCompat.replaceAccessibilityAction(
            row,
            AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_LONG_CLICK,
            getString(R.string.home_mark_needs_attention),
        ) { _, _ ->
            markAttention()
            true
        }
    }

    private fun bindDesktopMetadata(row: View, entry: Entries.Entry) {
        val desktop = entry.desktopBook
        val copyrightView = row.findViewById<androidx.appcompat.widget.AppCompatImageButton>(
            R.id.copyrightStatus,
        )
        val copyright = desktop?.copyright
        val hasCopyright = copyright != null && (
            desktop.registered || copyright.status.isNotBlank() ||
                copyright.registrationRecords.isNotEmpty() || copyright.renewalRecords.isNotEmpty()
            )
        copyrightView.visibility = if (hasCopyright) View.VISIBLE else View.GONE
        if (copyright != null && hasCopyright) {
            copyrightView.setImageDrawable(copyrightStatusDrawable(this, copyright))
            copyrightView.contentDescription = listOf(
                getString(R.string.home_copyright_status),
                copyright.status,
                resources.getQuantityString(
                    R.plurals.detail_registration_count,
                    copyright.registrationRecords.size,
                    copyright.registrationRecords.size,
                ),
                resources.getQuantityString(
                    R.plurals.detail_renewal_count,
                    copyright.renewalRecords.size,
                    copyright.renewalRecords.size,
                ),
            ).filter(String::isNotBlank).joinToString(": ")
            copyrightView.setOnClickListener { showCopyrightRecords(copyright) }
            copyrightView.setOnLongClickListener {
                showEntryAttentionDialog(this, entry.id) { refreshHome() }
                true
            }
        } else {
            copyrightView.setOnClickListener(null)
            copyrightView.setOnLongClickListener(null)
        }

        row.findViewById<ImageView>(R.id.whlAvailability).apply {
            val availability = desktop?.whl
            visibility = if (availability?.available == true) View.VISIBLE else View.GONE
            contentDescription = availability?.detail?.takeIf(String::isNotBlank)?.let {
                getString(R.string.home_availability_detail, getString(R.string.home_whl_available), it)
            } ?: getString(R.string.home_whl_available)
        }
        row.findViewById<ImageView>(R.id.internetArchiveAvailability).apply {
            val availability = desktop?.internetArchive
            visibility = if (availability?.available == true) View.VISIBLE else View.GONE
            contentDescription = availability?.detail?.takeIf(String::isNotBlank)?.let {
                getString(R.string.home_availability_detail, getString(R.string.home_ia_available), it)
            } ?: getString(R.string.home_ia_available)
        }
        row.findViewById<ImageView>(R.id.scanStatus).apply {
            val status = desktop?.scanStatus.orEmpty().trim()
            val actionable = status.isNotEmpty() &&
                status.lowercase() !in setOf("none", "unknown")
            visibility = if (actionable) View.VISIBLE else View.GONE
            contentDescription = getString(R.string.home_scan_status_value, status)
        }
        row.findViewById<ImageView>(R.id.remarksStatus).apply {
            val remarks = desktop?.remarks.orEmpty()
            visibility = if (remarks.isNotEmpty()) View.VISIBLE else View.GONE
            contentDescription = resources.getQuantityString(
                R.plurals.home_remarks_count,
                remarks.size,
                remarks.size,
            )
        }

        val localReview = entry.captureReview
        val needsAttention = localReview?.needsAttention == true
        val needsReview = localReview?.needsReview == true
        val reason = localReview?.attentionReason.orEmpty()
        row.findViewById<ImageView>(R.id.attentionStatus).apply {
            visibility = if (needsAttention || needsReview) View.VISIBLE else View.GONE
            setColorFilter(getColor(if (needsReview) R.color.whl_red else R.color.whl_amber))
            contentDescription = buildString {
                append(getString(
                    if (needsReview) R.string.home_needs_review else R.string.home_needs_attention,
                ))
                if (reason.isNotBlank()) append(": ").append(reason)
            }
        }
    }

    private fun showCopyrightRecords(copyright: DesktopCopyrightMetadata) {
        val sections = mutableListOf<String>()
        if (copyright.status.isNotBlank()) {
            sections += getString(R.string.copyright_status_value, copyright.status)
        }
        fun appendRecords(heading: Int, records: List<JSONObject>) {
            if (records.isEmpty()) return
            val rendered = mutableListOf<String>()
            var omitted = 0
            for ((index, record) in records.withIndex()) {
                val next = copyrightRecordText(record)
                val currentSize = sections.sumOf(String::length) +
                    rendered.sumOf(String::length) + next.length
                if (currentSize > COPYRIGHT_POPUP_CONTENT_BUDGET) {
                    omitted = records.size - index
                    break
                }
                rendered += next
            }
            if (omitted > 0) {
                rendered += resources.getQuantityString(
                    R.plurals.copyright_records_omitted,
                    omitted,
                    omitted,
                )
            }
            sections += getString(heading) + "\n" + rendered.joinToString("\n\n")
        }
        appendRecords(R.string.copyright_registration_heading, copyright.registrationRecords)
        appendRecords(R.string.copyright_renewal_heading, copyright.renewalRecords)
        if (sections.isEmpty()) sections += getString(R.string.copyright_no_records)

        val message = TextView(this).apply {
            setPadding(dp(20), dp(8), dp(20), dp(8))
            setTextColor(getColor(R.color.whl_ink))
            textSize = 12f
            typeface = android.graphics.Typeface.MONOSPACE
            text = sections.joinToString("\n\n")
            setTextIsSelectable(true)
        }
        val scroll = ScrollView(this).apply { addView(message) }
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.copyright_records_title)
            .setView(scroll)
            .setPositiveButton(R.string.close, null)
            .show()
        RemoteUiCatalog.apply(dialog)
    }

    private fun copyrightRecordText(record: JSONObject): String {
        val preferred = listOf(
            "registration_number", "reg_number", "number", "registration_date", "date",
            "renewal_id", "renewal_number", "renewal_date", "renewal_year", "title", "author",
            "source", "via",
        )
        val ordered = preferred.filter(record::has) +
            record.keys().asSequence().filterNot(preferred::contains).sorted().toList()
        val distinct = ordered.distinct()
        val shown = distinct.take(COPYRIGHT_RECORD_FIELD_LIMIT)
        val lines = shown.mapNotNull { key ->
            val value = record.opt(key)?.takeUnless { it == JSONObject.NULL }?.toString()?.trim()
                .orEmpty()
            value.takeIf(String::isNotEmpty)?.let {
                val bounded = if (it.length <= COPYRIGHT_RECORD_VALUE_LIMIT) it
                else it.take(COPYRIGHT_RECORD_VALUE_LIMIT - 1) + "…"
                "${key.replace('_', ' ')}: $bounded"
            }
        }.toMutableList()
        val omitted = distinct.size - shown.size
        if (omitted > 0) lines += resources.getQuantityString(
            R.plurals.copyright_fields_omitted,
            omitted,
            omitted,
        )
        return lines.joinToString("\n").ifBlank { record.toString().take(COPYRIGHT_RECORD_VALUE_LIMIT) }
    }

    /** A deliberately cheap blur for small list thumbnails while the remote
     * cleanup derivative is pending. The retained source file is untouched. */
    private fun softenPendingThumbnail(source: Bitmap): Bitmap {
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

    private fun applyScanListLayout(row: View, thumb: ImageView, compact: Boolean) {
        val metrics = scanListLayoutMetrics(compact)
        row.setPaddingRelative(
            row.paddingStart,
            dp(metrics.rowVerticalPaddingDp),
            row.paddingEnd,
            dp(metrics.rowVerticalPaddingDp),
        )
        val params = thumb.layoutParams as ViewGroup.MarginLayoutParams
        params.width = dp(metrics.thumbnailWidthDp)
        params.height = dp(metrics.thumbnailHeightDp)
        params.marginEnd = dp(metrics.thumbnailEndMarginDp)
        thumb.layoutParams = params
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).roundToInt()

    private fun openEntryDetails(id: String) {
        startActivity(
            Intent(this, EntryDetailActivity::class.java)
                .putExtra(EntryDetailActivity.EXTRA_ID, id),
        )
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

    private companion object {
        const val COPYRIGHT_POPUP_CONTENT_BUDGET = 22_000
        const val COPYRIGHT_RECORD_FIELD_LIMIT = 20
        const val COPYRIGHT_RECORD_VALUE_LIMIT = 500
        const val STATE_ACTIVE_TAB = "active_tab"
        const val STATE_TAB_COLLECTIONS = "tab_collections"
        const val STATE_INSPECTED_COLLECTION = "inspected_collection"
        const val STATE_INSPECT_VIEW_MODE = "inspect_view_mode"
        const val STATE_SCAN_GROUPS_INITIALIZED = "scan_groups_initialized"
        const val STATE_EXPANDED_SCAN_GROUPS = "expanded_scan_groups"
        const val STATE_SYNC_FEEDBACK_REQUEST = "sync_feedback_request"
        const val STATE_SYNC_FEEDBACK_PHASE = "sync_feedback_phase"
        const val WORK_REFRESH_COALESCE_MS = 200L
    }
}
