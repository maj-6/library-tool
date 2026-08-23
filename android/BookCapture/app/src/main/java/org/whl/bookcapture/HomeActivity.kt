package org.whl.bookcapture

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.text.method.LinkMovementMethod
import android.view.LayoutInflater
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.ArrayAdapter
import android.widget.Filter
import android.widget.Spinner
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.AppCompatAutoCompleteTextView
import androidx.appcompat.view.ActionMode
import androidx.core.text.HtmlCompat
import androidx.core.view.MenuCompat
import androidx.core.view.ViewCompat
import androidx.core.view.accessibility.AccessibilityNodeInfoCompat
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import com.google.android.material.button.MaterialButton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityHomeBinding
import org.json.JSONObject
import java.io.File
import java.util.UUID
import kotlin.coroutines.CoroutineContext
import kotlin.math.roundToInt

internal const val INSPECT_MEMBERSHIP_ISOLATION_MAX_ATTEMPTS = 32
internal const val HOME_EXTRA_OPEN_SCAN_QUEUE = "open_scan_search_queue"

private class CollectionNameSuggestionAdapter(
    context: Context,
    private val collections: List<BookCollection>,
) : ArrayAdapter<String>(context, android.R.layout.simple_dropdown_item_1line) {
    private val suggestionFilter = object : Filter() {
        override fun performFiltering(constraint: CharSequence?): FilterResults {
            val names = matchingCollectionNames(collections, constraint?.toString().orEmpty())
            return FilterResults().apply {
                values = names
                count = names.size
            }
        }

        override fun publishResults(constraint: CharSequence?, results: FilterResults) {
            @Suppress("UNCHECKED_CAST")
            val names = results.values as? List<String> ?: emptyList()
            setNotifyOnChange(false)
            clear()
            addAll(names)
            if (names.isEmpty()) notifyDataSetInvalidated() else notifyDataSetChanged()
        }
    }

    override fun getFilter(): Filter = suggestionFilter
}

internal data class InspectMembershipIsolationResult(
    val acceptedIds: Set<String>,
    val failedIds: Set<String>,
)

/**
 * Isolate a bad capture in an otherwise atomic membership batch.
 *
 * The server deliberately rejects the complete request when any id is missing
 * or belongs to another account. Successful halves are committed immediately;
 * failed singleton (or budget-exhausted) halves stay in the durable outbox for
 * a later retry. The attempt budget bounds a broken or universally stale batch.
 */
internal fun isolateInspectMembershipMutation(
    captureIds: List<String>,
    maximumAttempts: Int = INSPECT_MEMBERSHIP_ISOLATION_MAX_ATTEMPTS,
    shouldBisect: (Exception) -> Boolean,
    mutate: (List<String>) -> Set<String>,
    onAccepted: (Set<String>) -> Unit,
): InspectMembershipIsolationResult {
    require(captureIds.isNotEmpty()) { "capture ids are required" }
    require(captureIds.size <= CAPTURE_COLLECTION_MUTATION_MAX_IDS) {
        "capture batch is too large"
    }
    require(captureIds.all(String::isNotBlank) && captureIds.distinct().size == captureIds.size) {
        "capture ids must be non-blank and unique"
    }
    require(maximumAttempts in 1..INSPECT_MEMBERSHIP_ISOLATION_MAX_ATTEMPTS) {
        "invalid membership isolation attempt budget"
    }

    val accepted = linkedSetOf<String>()
    val failed = linkedSetOf<String>()
    var attempts = 0

    fun attempt(batch: List<String>) {
        if (attempts >= maximumAttempts) {
            failed += batch
            return
        }
        attempts += 1
        val response = try {
            mutate(batch)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (!shouldBisect(e)) {
                failed += batch
                return
            }
            null
        }

        if (response == batch.toSet()) {
            // Cache/outbox failures are not server batch failures. Propagate them
            // so an accepted mutation remains safely retryable and idempotent.
            onAccepted(response)
            accepted += batch
            return
        }
        if (batch.size == 1) {
            failed += batch.single()
            return
        }
        val midpoint = batch.size / 2
        attempt(batch.subList(0, midpoint))
        attempt(batch.subList(midpoint, batch.size))
    }

    attempt(captureIds)
    return InspectMembershipIsolationResult(accepted, failed)
}

internal fun shouldBisectInspectMembershipFailure(error: Exception): Boolean = when (error) {
    is SupabaseClient.InvalidResponse,
    is IllegalArgumentException -> true
    is SupabaseClient.HttpException ->
        error.code in 400..499 && error.code !in setOf(401, 408, 429)
    else -> false
}

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

    private data class ScanListItem(
        val entry: Entries.Entry,
        val titleLabel: String,
        val authorLabel: String,
        val yearLabel: String,
        val statusLabel: String,
    )

    private data class ScanListSnapshot(
        val items: List<ScanListItem>,
        val collectionPaths: Map<String, String>,
        val currentCollection: BookCollection?,
        val currentScanCollections: Map<ScanCollectionSlot, BookCollection>,
    )

    private data class InspectBookSnapshot(
        val item: CollectionInventoryItem,
        val titleLabel: String,
        val statusLabel: String?,
        val cloudBacked: Boolean = false,
        /** Trustworthy owner evidence, never inferred from a later session. */
        val cloudOwnerId: String = "",
    )

    private data class ThumbnailRequest(
        val entry: Entries.Entry,
        val maxWidth: Int,
        val maxHeight: Int,
        val image: ImageView? = null,
        val swatch: View? = null,
    )

    private data class InspectSnapshot(
        val collections: List<BookCollection>,
        val collectionPaths: Map<String, String>,
        val currentCollectionId: String?,
        val itemsByCollection: Map<String, List<InspectBookSnapshot>>,
        /**
         * Boxes the cloud has actually answered for, cached listing included when
         * that answer was "nothing". Absence means no answer yet — never "empty" —
         * so an empty box is never described as empty in the cloud on the strength
         * of a listing that failed or never ran.
         */
        val cloudListedCollections: Set<String>,
    )

    private data class InspectLookupRenderItem(
        val book: InspectLookupBook,
        val snapshot: InspectBookSnapshot,
        val collectionName: String,
        val collectionTagId: String,
    )

    private data class InspectMutationOutcome(
        val appliedCount: Int,
        val localCleanupFailures: Int,
        val cloudPending: Boolean,
    )

    private data class CollectionListSnapshot(
        val collections: List<BookCollection>,
        val currentCaptureCollection: BookCollection?,
        val currentScanCollections: Map<ScanCollectionSlot, BookCollection>,
        val collectionPaths: Map<String, String>,
        val bookCounts: Map<String, Int>,
    )

    private data class CollectionBarSnapshot(
        val currentCaptureCollection: BookCollection?,
        val currentScanCollections: Map<ScanCollectionSlot, BookCollection>,
        val collectionPaths: Map<String, String>,
    )

    private data class ScanQueueSummarySnapshot(
        val activeCollections: Map<ScanCollectionSlot, BookCollection>,
        val collectionPaths: Map<String, String>,
        val queueItems: List<ScanSearchQueueItem>,
        val presentations: List<ScanQueueSessionPresentation>,
        val valid: Boolean,
    )

    private enum class HomeTab { SCANS, SCAN_QUEUE, COLLECTIONS, INSPECT }

    private enum class ScanProposalDecisionOutcome { APPLIED, STALE, FAILED }

    private enum class InspectViewMode(val wireValue: String, val columns: Int, val layout: Int) {
        TILES("tiles", 2, R.layout.item_inspect_tile),
        CONTENT("content", 1, R.layout.item_inspect_content),
        ICONS("icons", 3, R.layout.item_inspect_icon);

        companion object {
            fun fromWire(value: String?): InspectViewMode =
                entries.firstOrNull { it.wireValue == value } ?: TILES
        }
    }

    private lateinit var binding: ActivityHomeBinding
    private var thumbJob: Job? = null
    private var scanListJob: Job? = null
    private var collectionBarJob: Job? = null
    private var collectionListJob: Job? = null
    private var inspectJob: Job? = null
    /** Owner-scoped boxes already listed from the cloud this freshness
     * generation; failures are re-armed for the next visit. */
    private val remoteBoxFetches: RemoteCollectionFetchTracker
        get() = REMOTE_BOX_FETCHES
    private var workerRefreshJob: Job? = null
    private var pendingWorkerContentRefresh = false
    private var pendingCollectionRefresh = false
    private val dynamicThumbnailViews = linkedSetOf<ImageView>()
    private val dynamicThumbnailBitmaps = linkedMapOf<ImageView, Bitmap>()
    private var activeTab = HomeTab.SCANS
    private var inspectedCollectionId: String? = null
    private var inspectVisibleBookLimit = INSPECT_BOOK_PAGE_SIZE
    private var inspectViewMode = InspectViewMode.TILES
    private var inspectSelection = InspectSelectionState()
    private val inspectBookViews = linkedMapOf<String, View>()
    private var inspectRenderedItems = emptyMap<String, InspectBookSnapshot>()
    private var inspectRenderedCollections = emptyList<BookCollection>()
    private var inspectRenderedCollectionPaths = emptyMap<String, String>()
    private var inspectActionMode: ActionMode? = null
    private var inspectMutationInFlight = false
    private var inspectRenderedSnapshot: InspectSnapshot? = null
    private var inspectLookupQuery = ""
    private var inspectCoverOcrText = ""
    private var suppressInspectLookupWatcher = false
    private var inspectLookupCloudBooks = emptyList<RemoteCollectionBook>()
    private var inspectLookupCloudOwner = ""
    private var inspectLookupCloudLoading = false
    private var inspectLookupCloudLoaded = false
    private var inspectLookupCloudFailed = false
    private var inspectLookupCloudGeneration = 0L
    private var scanQueueSummaryJob: Job? = null
    private var activeScanSearchQueueId: String? = null
    private var activeScanSearchProposal: ScanSearchQueueItem? = null
    private var scanQueueMutationInFlight = false
    private var reportedTagConflict = ""
    private val expandedScanGroups = linkedSetOf<String>()
    private var scanGroupsInitialized = false
    private var scanPageGroupKey: String? = null
    private var scanPageOffset = 0
    private var syncFeedbackRequestId: String? = null
    private var syncFeedbackPhase: CaptureSyncPhase? = null
    private var syncActionInFlight = false

    private val qrScanner = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val raw = result.data?.getStringExtra(QrScannerActivity.EXTRA_TAG_ID)
        lifecycleScope.launch {
            val collection = withContext(Dispatchers.IO) {
                findCollectionByTagId(
                    Collections.allRecords(this@HomeActivity),
                    raw.orEmpty()
                )
            }
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
            if (collection == null) {
                val normalized = normalizeCollectionTagId(raw.orEmpty())
                Toast.makeText(
                    this@HomeActivity,
                    if (normalized.isEmpty()) getString(R.string.inspect_scan_invalid)
                    else getString(R.string.inspect_scan_unknown, normalized),
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }
            inspectedCollectionId = collection.id
            activeTab = HomeTab.INSPECT
            Toast.makeText(
                this@HomeActivity,
                getString(R.string.inspect_scan_matched, collection.name, collection.tagId),
                Toast.LENGTH_SHORT,
            ).show()
            showTab(HomeTab.INSPECT)
            binding.homeScroll.post {
                if (activeTab == HomeTab.INSPECT) {
                    binding.homeScroll.fullScroll(View.FOCUS_UP)
                }
            }
        }
    }

    private val coverScanner = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val recognized = result.data
            ?.getStringExtra(CoverScannerActivity.EXTRA_RECOGNIZED_TEXT)
            .orEmpty()
            .take(INSPECT_COVER_TEXT_MAX)
        if (recognized.isBlank()) {
            Toast.makeText(this, R.string.inspect_cover_no_text, Toast.LENGTH_LONG).show()
            return@registerForActivityResult
        }
        activeScanSearchQueueId = null
        activeScanSearchProposal = null
        rearmFailedRemoteInspectLookup()
        inspectCoverOcrText = recognized
        val displayText = recognized.lineSequence()
            .map(String::trim)
            .firstOrNull { it.length >= 3 }
            .orEmpty()
            .take(INSPECT_LOOKUP_DISPLAY_MAX)
        suppressInspectLookupWatcher = true
        binding.inspectBookSearch.setText(displayText)
        binding.inspectBookSearch.setSelection(binding.inspectBookSearch.text?.length ?: 0)
        suppressInspectLookupWatcher = false
        inspectLookupQuery = displayText
        renderInspectLookup()
        ensureRemoteInspectLookup()
    }

    private val scanSearchCamera = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val count = result.data?.getIntExtra(CoverScannerActivity.EXTRA_CAPTURE_COUNT, 0) ?: 0
        val slot = result.data?.getStringExtra(CoverScannerActivity.EXTRA_SCAN_SLOT)
            ?.let(ScanCollectionSlot::fromWire)
            ?: return@registerForActivityResult
        Toast.makeText(
            this,
            getString(R.string.scan_queue_session_saved, count, slot.name),
            Toast.LENGTH_LONG,
        ).show()
        refreshScanSearchQueueSummary()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityHomeBinding.inflate(layoutInflater)
        setContentView(binding.root)
        val openScanQueue = intent.getBooleanExtra(HOME_EXTRA_OPEN_SCAN_QUEUE, false)
        activeTab = if (openScanQueue) {
            HomeTab.SCAN_QUEUE
        } else {
            savedInstanceState?.getString(STATE_ACTIVE_TAB)
                ?.let { runCatching { HomeTab.valueOf(it) }.getOrNull() }
                ?: if (savedInstanceState?.getBoolean(STATE_TAB_COLLECTIONS) == true) {
                    HomeTab.COLLECTIONS
                } else {
                    HomeTab.SCANS
                }
        }
        if (openScanQueue) intent.removeExtra(HOME_EXTRA_OPEN_SCAN_QUEUE)
        inspectedCollectionId = savedInstanceState?.getString(STATE_INSPECTED_COLLECTION)
        inspectViewMode = InspectViewMode.fromWire(
            savedInstanceState?.getString(STATE_INSPECT_VIEW_MODE) ?: Prefs.inspectViewMode(this),
        )
        inspectLookupQuery = savedInstanceState
            ?.getString(STATE_INSPECT_LOOKUP_QUERY)
            .orEmpty()
            .take(INSPECT_LOOKUP_DISPLAY_MAX)
        inspectCoverOcrText = savedInstanceState
            ?.getString(STATE_INSPECT_COVER_OCR)
            .orEmpty()
            .take(INSPECT_COVER_TEXT_MAX)
        activeScanSearchQueueId = savedInstanceState
            ?.getString(STATE_ACTIVE_SCAN_SEARCH_QUEUE)
            ?.takeIf(SAFE_CAPTURE_SYNC_ID::matches)
        scanGroupsInitialized =
            savedInstanceState?.getBoolean(STATE_SCAN_GROUPS_INITIALIZED) ?: false
        syncFeedbackRequestId = savedInstanceState?.getString(STATE_SYNC_FEEDBACK_REQUEST)
        syncFeedbackPhase = savedInstanceState?.getString(STATE_SYNC_FEEDBACK_PHASE)
            ?.let(CaptureSyncPhase::fromStoredValue)
        savedInstanceState?.getStringArrayList(STATE_EXPANDED_SCAN_GROUPS)
            ?.let(expandedScanGroups::addAll)
        savedInstanceState?.getStringArrayList(STATE_INSPECT_SELECTED_IDS)?.let { restored ->
            inspectSelection = InspectSelectionState(
                restored.asSequence()
                    .filter { it.isNotBlank() }
                    .take(CAPTURE_COLLECTION_MUTATION_MAX_IDS)
                    .toCollection(linkedSetOf()),
            )
        }

        binding.tabScans.setOnClickListener { showTab(HomeTab.SCANS) }
        binding.tabScanQueue.setOnClickListener { showTab(HomeTab.SCAN_QUEUE) }
        binding.tabCollections.setOnClickListener { showTab(HomeTab.COLLECTIONS) }
        binding.tabInspect.setOnClickListener { showTab(HomeTab.INSPECT) }
        binding.collectionBar.setOnClickListener { showTab(HomeTab.COLLECTIONS) }
        binding.newCollection.setOnClickListener { editCollection(null) }
        binding.scanBox.setOnClickListener {
            qrScanner.launch(Intent(this, QrScannerActivity::class.java))
        }
        binding.queueScanBook.setOnClickListener { startScanSearchSession() }
        suppressInspectLookupWatcher = true
        binding.inspectBookSearch.setText(inspectLookupQuery)
        binding.inspectBookSearch.setSelection(binding.inspectBookSearch.text?.length ?: 0)
        suppressInspectLookupWatcher = false
        binding.inspectBookSearch.doAfterTextChanged { editable ->
            if (suppressInspectLookupWatcher) return@doAfterTextChanged
            activeScanSearchQueueId = null
            activeScanSearchProposal = null
            rearmFailedRemoteInspectLookup()
            inspectLookupQuery = editable?.toString().orEmpty()
                .take(INSPECT_LOOKUP_DISPLAY_MAX)
            inspectCoverOcrText = ""
            renderInspectLookup()
            ensureRemoteInspectLookup()
        }
        binding.inspectBookSearch.setOnEditorActionListener { _, actionId, _ ->
            if (actionId != android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH) {
                return@setOnEditorActionListener false
            }
            rearmFailedRemoteInspectLookup()
            binding.inspectBookSearch.clearFocus()
            renderInspectLookup()
            ensureRemoteInspectLookup()
            true
        }
        binding.inspectScanCover.setOnClickListener {
            if (Prefs.mistralKey(this).isBlank()) {
                Toast.makeText(
                    this,
                    R.string.cover_scanner_requires_mistral_key,
                    Toast.LENGTH_LONG,
                ).show()
                return@setOnClickListener
            }
            coverScanner.launch(Intent(this, CoverScannerActivity::class.java))
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
                ProcessWorker.RETRY_WORK_NAME,
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
        workManager
            .getWorkInfosLiveData(activeUniqueWorkQuery(ScanSearchQueueSyncWorker.WORK_NAME))
            .observe(this) {
                refreshScanSearchQueueSummary()
            }
        workManager.getWorkInfosByTagLiveData(ScanSearchOcrWorker.WORK_TAG)
            .observe(this) {
                refreshScanSearchQueueSummary()
            }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(HOME_EXTRA_OPEN_SCAN_QUEUE, false)) {
            showTab(HomeTab.SCAN_QUEUE)
            intent.removeExtra(HOME_EXTRA_OPEN_SCAN_QUEUE)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_ACTIVE_TAB, activeTab.name)
        outState.putBoolean(STATE_TAB_COLLECTIONS, activeTab == HomeTab.COLLECTIONS)
        outState.putString(STATE_INSPECTED_COLLECTION, inspectedCollectionId)
        outState.putString(STATE_INSPECT_VIEW_MODE, inspectViewMode.wireValue)
        outState.putString(STATE_INSPECT_LOOKUP_QUERY, inspectLookupQuery)
        outState.putString(STATE_INSPECT_COVER_OCR, inspectCoverOcrText)
        outState.putString(STATE_ACTIVE_SCAN_SEARCH_QUEUE, activeScanSearchQueueId)
        outState.putStringArrayList(
            STATE_INSPECT_SELECTED_IDS,
            ArrayList(inspectSelection.selectedIds),
        )
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
        if (signedIn) {
            val owner = Prefs.userId(this)
            remoteBoxFetches.rearm(owner)
            if (inspectLookupCloudOwner != owner) resetRemoteInspectLookup(owner)
        } else {
            resetRemoteInspectLookup("")
        }
        binding.configWarning.visibility = if (signedIn) View.GONE else View.VISIBLE
        // A previously authorized batch may resume after process death, but a
        // new upload batch is created only by the Sync captures button.
        UploadWorker.kick(this)
        ProcessWorker.enqueue(this)
        // A delete tombstone is committed before its media cleanup begins. If
        // the prior Activity/process stopped in that gap, finish it now.
        lifecycleScope.launch(Dispatchers.IO) {
            retryPendingInspectBookCleanup(this@HomeActivity)
        }
        // alpha.5 could cancel the tail of a bulk reprocess chain while
        // leaving its per-capture hold files behind. Rebuild that chain off
        // the UI thread so an app update can release an already-stalled sync.
        lifecycleScope.launch(Dispatchers.IO) {
            ProcessWorker.resumePendingForcedRetries(this@HomeActivity)
        }
        lifecycleScope.launch(Dispatchers.IO) {
            ScanSearchOcrWorker.resumePending(this@HomeActivity)
        }
        CollectionSyncWorker.enqueueCoalesced(this)
        ScanSearchQueueSyncWorker.enqueue(this, guaranteed = false)
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
        refreshScanSearchQueueSummary()
        showTab(activeTab)
    }

    private fun startScanSearchSession(preferredSessionId: String? = null) {
        if (!Auth.signedIn(this) || !SAFE_CAPTURE_SYNC_ID.matches(Prefs.userId(this))) {
            Toast.makeText(
                this,
                R.string.scan_queue_requires_sign_in,
                Toast.LENGTH_LONG,
            ).show()
            return
        }
        lifecycleScope.launch {
            val launch = withContext(Dispatchers.IO) {
                val destinations = Collections.currentScans(this@HomeActivity)
                val store = ScanSearchQueue.read(this@HomeActivity)
                val requestedSession = preferredSessionId
                    ?.trim()
                    ?.lowercase()
                    ?.takeIf(SAFE_CAPTURE_SYNC_ID::matches)
                    ?.takeIf { sessionId ->
                        store.items.any {
                            it.sessionId == sessionId &&
                                it.status == ScanSearchStatus.PENDING &&
                                it.scanCollectionId.isEmpty()
                        }
                    }
                Triple(
                    destinations,
                    store.valid,
                    requestedSession ?: store.items.firstOrNull {
                        it.status == ScanSearchStatus.PENDING &&
                            it.scanCollectionId.isEmpty()
                    }?.sessionId ?: UUID.randomUUID().toString(),
                )
            }
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
            if (!launch.second) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.scan_queue_save_failed,
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }
            if (launch.first.isEmpty()) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.scan_queue_needs_collection,
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }
            scanSearchCamera.launch(
                Intent(this@HomeActivity, CoverScannerActivity::class.java)
                    .putExtra(CoverScannerActivity.EXTRA_QUEUE_SESSION, true)
                    .putExtra(CoverScannerActivity.EXTRA_SESSION_ID, launch.third),
            )
        }
    }

    private fun showScanSearchQueueItem(item: ScanSearchQueueItem) {
        activeScanSearchQueueId = item.id
        activeScanSearchProposal = item
        rearmFailedRemoteInspectLookup()
        inspectCoverOcrText = item.ocrText.take(INSPECT_COVER_TEXT_MAX)
        val displayText = inspectCoverOcrText.lineSequence()
            .map(String::trim)
            .firstOrNull { it.length >= 3 }
            .orEmpty()
            .take(INSPECT_LOOKUP_DISPLAY_MAX)
        suppressInspectLookupWatcher = true
        binding.inspectBookSearch.setText(displayText)
        binding.inspectBookSearch.setSelection(binding.inspectBookSearch.text?.length ?: 0)
        suppressInspectLookupWatcher = false
        inspectLookupQuery = displayText
        if (activeTab != HomeTab.INSPECT) {
            showTab(HomeTab.INSPECT)
        } else {
            renderInspectLookup()
            ensureRemoteInspectLookup()
        }
    }

    private fun refreshScanSearchQueueSummary() {
        scanQueueSummaryJob?.cancel()
        scanQueueSummaryJob = lifecycleScope.launch {
            val snapshot = withContext(Dispatchers.IO) {
                val owner = Prefs.userId(this@HomeActivity).trim().lowercase()
                val store = ScanSearchQueue.read(this@HomeActivity)
                val queueItems = if (!store.valid) emptyList() else store.items.filter {
                    it.ownerId.isEmpty() ||
                        (owner.isNotEmpty() && it.ownerId == owner)
                }
                val records = Collections.allRecords(this@HomeActivity)
                ScanQueueSummarySnapshot(
                    activeCollections = Collections.currentScans(this@HomeActivity),
                    collectionPaths = collectionDisplayPaths(records),
                    queueItems = queueItems,
                    presentations = scanQueueSessionPresentations(queueItems),
                    valid = store.valid,
                )
            }
            scanQueueSummaryJob = null
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
            val activeQueueId = activeScanSearchQueueId
            if (snapshot.valid && activeQueueId != null) {
                val refreshedProposal = snapshot.queueItems.firstOrNull {
                    it.id == activeQueueId && it.status in setOf(
                        ScanSearchStatus.PENDING,
                        ScanSearchStatus.PROPOSED,
                    )
                }
                activeScanSearchProposal = refreshedProposal
                if (refreshedProposal == null) activeScanSearchQueueId = null
                if (activeTab == HomeTab.INSPECT) renderInspectLookup()
            }
            val destinations = snapshot.activeCollections
            val destinationLabel = ScanCollectionSlot.entries.mapNotNull { slot ->
                destinations[slot]?.let { "${slot.name}: ${it.name}" }
            }.joinToString(" \u00b7 ")
            val sessionCount = snapshot.presentations.size
            binding.scanQueueSummary.visibility = View.VISIBLE
            binding.scanQueueSummary.text = when {
                !snapshot.valid -> getString(R.string.scan_queue_save_failed)
                destinations.isEmpty() -> getString(R.string.scan_queue_needs_collection)
                snapshot.presentations.isEmpty() -> getString(
                    R.string.scan_queue_summary_empty,
                    destinationLabel,
                )
                else -> getString(
                    R.string.scan_queue_summary,
                    destinationLabel,
                    sessionCount,
                )
            }
            binding.scanQueueSummary.contentDescription = binding.scanQueueSummary.text
            renderScanQueueInspector(snapshot)
        }
    }

    private fun renderScanQueueInspector(snapshot: ScanQueueSummarySnapshot) {
        val list = binding.scanQueueList
        list.removeAllViews()
        binding.scanQueueEmpty.visibility = if (!snapshot.valid ||
            snapshot.presentations.isEmpty()
        ) View.VISIBLE else View.GONE
        binding.scanQueueEmpty.setText(
            if (snapshot.valid) R.string.scan_queue_inspector_empty
            else R.string.scan_queue_inspector_invalid,
        )
        if (!snapshot.valid) return

        val inflater = LayoutInflater.from(this)
        snapshot.presentations.forEach { presentation ->
            val row = inflater.inflate(R.layout.item_scan_queue, list, false)
            val title = row.findViewById<TextView>(R.id.scanQueueRowTitle)
            val status = row.findViewById<TextView>(R.id.scanQueueRowStatus)
            val detail = row.findViewById<TextView>(R.id.scanQueueRowDetail)
            val action = row.findViewById<MaterialButton>(R.id.scanQueueRowAction)

            title.text = resources.getQuantityString(
                R.plurals.scan_queue_inspector_title,
                presentation.captureCount,
                presentation.captureCount,
            )
            val stateText = getString(scanQueueInspectorStateLabel(presentation.state))
            status.text = presentation.confidencePercent?.let { confidence ->
                listOf(
                    stateText,
                    getString(R.string.scan_queue_match_confidence, confidence),
                ).joinToString(" \u00b7 ")
            } ?: stateText
            status.setTextColor(getColor(when (presentation.state) {
                ScanQueueInspectorState.READY,
                ScanQueueInspectorState.APPROVED -> R.color.whl_green
                ScanQueueInspectorState.FAILED -> R.color.whl_red
                ScanQueueInspectorState.DRAFT,
                ScanQueueInspectorState.QUEUED,
                ScanQueueInspectorState.SAVING_APPROVAL,
                ScanQueueInspectorState.SAVING_REJECTION -> R.color.whl_amber
                ScanQueueInspectorState.MATCHING -> R.color.whl_cyan
                ScanQueueInspectorState.REJECTED -> R.color.whl_ink_dim
            }))

            val destination = presentation.destinationCollectionId
                .takeIf(String::isNotEmpty)
                ?.let { id -> snapshot.collectionPaths[id] ?: id.take(8) }
                ?.let { getString(R.string.scan_queue_inspector_destination, it) }
                ?: getString(R.string.scan_queue_inspector_no_destination)
            val recognized = presentation.items.asSequence()
                .map(ScanSearchQueueItem::ocrText)
                .map { it.replace(Regex("\\s+"), " ").trim() }
                .firstOrNull(String::isNotEmpty)
                ?.take(SCAN_QUEUE_INSPECTOR_OCR_EXCERPT_MAX)
            val evidence = when {
                presentation.errorMessage.isNotEmpty() -> getString(
                    R.string.scan_queue_error_detail,
                    presentation.errorMessage,
                )
                recognized != null -> getString(R.string.scan_queue_ocr_detail, recognized)
                presentation.items.any { it.visualSignature.isNotEmpty() } ->
                    getString(R.string.scan_queue_visual_only_detail)
                else -> stateText
            }
            detail.text = "$destination\n$evidence"

            when {
                presentation.reviewable -> {
                    action.visibility = View.VISIBLE
                    action.contentDescription = getString(R.string.scan_queue_review_action)
                    action.setOnClickListener {
                        showScanSearchQueueItem(presentation.representative)
                    }
                }
                presentation.state == ScanQueueInspectorState.DRAFT -> {
                    action.visibility = View.VISIBLE
                    action.contentDescription = getString(R.string.scan_queue_resume_action)
                    action.setOnClickListener {
                        startScanSearchSession(presentation.sessionId)
                    }
                }
                presentation.state == ScanQueueInspectorState.FAILED &&
                    presentation.errorMessage.isNotEmpty() &&
                    presentation.items.none {
                        it.errorMessage.isNotEmpty() && it.dirty
                    } -> {
                    action.visibility = View.VISIBLE
                    action.setIconResource(R.drawable.ic_delete)
                    action.contentDescription = getString(
                        R.string.scan_queue_dismiss_failure_action,
                    )
                    action.setOnClickListener {
                        dismissScanQueueFailures(presentation.sessionId)
                    }
                }
                else -> action.visibility = View.GONE
            }
            row.contentDescription = listOf(title.text, status.text, destination, evidence)
                .joinToString(". ")
            RemoteUiCatalog.apply(row)
            list.addView(row)
        }
    }

    private fun scanQueueInspectorStateLabel(state: ScanQueueInspectorState): Int = when (state) {
        ScanQueueInspectorState.DRAFT -> R.string.scan_queue_state_draft
        ScanQueueInspectorState.QUEUED -> R.string.scan_queue_state_queued
        ScanQueueInspectorState.MATCHING -> R.string.scan_queue_state_matching
        ScanQueueInspectorState.READY -> R.string.scan_queue_state_ready
        ScanQueueInspectorState.FAILED -> R.string.scan_queue_state_failed
        ScanQueueInspectorState.SAVING_APPROVAL -> R.string.scan_queue_state_saving_approval
        ScanQueueInspectorState.SAVING_REJECTION -> R.string.scan_queue_state_saving_rejection
        ScanQueueInspectorState.APPROVED -> R.string.scan_queue_state_approved
        ScanQueueInspectorState.REJECTED -> R.string.scan_queue_state_rejected
    }

    private fun dismissScanQueueFailures(sessionId: String) {
        lifecycleScope.launch {
            val removed = withContext(Dispatchers.IO) {
                ScanSearchQueue.dismissLocalFailures(this@HomeActivity, sessionId)
            }
            if (removed == null) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.scan_queue_failure_dismiss_failed,
                    Toast.LENGTH_LONG,
                ).show()
            } else if (removed > 0) {
                Toast.makeText(
                    this@HomeActivity,
                    resources.getQuantityString(
                        R.plurals.scan_queue_failure_dismissed,
                        removed,
                        removed,
                    ),
                    Toast.LENGTH_SHORT,
                ).show()
            }
            refreshScanSearchQueueSummary()
        }
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
        menu.menu.findItem(R.id.menuRetryProcessing).isVisible = retryableProcessingIds().isNotEmpty()
        MenuCompat.setGroupDividerEnabled(menu.menu, true)
        menu.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.menuSettings -> {
                    startActivity(Intent(this, SettingsActivity::class.java)); true
                }
                R.id.menuRetryProcessing -> { retryFailedProcessing(); true }
                R.id.menuAbout -> { showAbout(); true }
                R.id.menuCheckUpdates -> { checkForUpdates(); true }
                R.id.menuSignOut -> { signOut(); true }
                else -> false
            }
        }
        menu.show()
    }

    /** Captures whose pipeline outcome is worth another attempt. Reads the
     * browsing list only: an archived capture is deliberately out of scope for
     * a bulk action, and the archive is not scanned by any worker. */
    private fun retryableProcessingIds(): List<String> = runCatching {
        Entries.recent(this)
            .filter {
                it.processing.status == Entries.ProcessingStatus.FAILED ||
                    it.processing.status == Entries.ProcessingStatus.PARTIAL
            }
            .filterNot { it.reprocessPending() }
            .map { it.id }
    }.getOrDefault(emptyList())

    /**
     * Re-run OCR and extraction over everything that failed or came back
     * partial. The common cause is one shared condition — a rejected or
     * missing API key, an outage — so the useful unit of recovery is "all of
     * them", not one capture at a time.
     */
    private fun retryFailedProcessing() {
        lifecycleScope.launch {
            val requested = withContext(Dispatchers.IO) {
                retryableProcessingIds().filter { entryId ->
                    EntryOperationLocks.withLock(entryId) {
                        Entries.find(this@HomeActivity, entryId)?.requestReprocess() == true
                    }
                }
            }
            if (requested.isEmpty()) {
                Toast.makeText(
                    this@HomeActivity,
                    RemoteUiCatalog.text(this@HomeActivity, R.string.home_retry_none),
                    Toast.LENGTH_SHORT,
                ).show()
                return@launch
            }
            ProcessWorker.enqueueForcedRetry(this@HomeActivity, requested)
            Toast.makeText(
                this@HomeActivity,
                RemoteUiCatalog.text(
                    this@HomeActivity,
                    R.string.home_retry_queued,
                    requested.size,
                ),
                Toast.LENGTH_LONG,
            ).show()
            refreshContentTab()
        }
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
            refreshContentTab()
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
        if (tab != HomeTab.INSPECT && activeTab == HomeTab.INSPECT) {
            clearInspectSelection()
        }
        activeTab = tab
        if (tab != HomeTab.SCANS) {
            cancelScanListLoading()
            cancelCollectionBarLoading()
        }
        if (tab != HomeTab.COLLECTIONS) cancelCollectionListLoading()
        if (tab != HomeTab.INSPECT) cancelInspectLoading()
        binding.homeList.visibility = if (tab == HomeTab.SCANS) View.VISIBLE else View.GONE
        binding.collectionsList.visibility =
            if (tab == HomeTab.COLLECTIONS) View.VISIBLE else View.GONE
        binding.scanQueuePane.visibility =
            if (tab == HomeTab.SCAN_QUEUE) View.VISIBLE else View.GONE
        binding.inspectPane.visibility = if (tab == HomeTab.INSPECT) View.VISIBLE else View.GONE
        binding.scanActions.visibility = if (tab == HomeTab.SCANS) View.VISIBLE else View.GONE
        binding.newCollection.visibility =
            if (tab == HomeTab.COLLECTIONS) View.VISIBLE else View.GONE
        binding.inspectActions.visibility = if (tab == HomeTab.INSPECT) View.VISIBLE else View.GONE
        binding.scanQueueActions.visibility =
            if (tab == HomeTab.SCAN_QUEUE) View.VISIBLE else View.GONE
        binding.collectionBar.visibility = if (tab == HomeTab.SCANS) View.VISIBLE else View.GONE
        emphasizeTab(binding.tabScans, tab == HomeTab.SCANS)
        emphasizeTab(binding.tabScanQueue, tab == HomeTab.SCAN_QUEUE)
        emphasizeTab(binding.tabCollections, tab == HomeTab.COLLECTIONS)
        emphasizeTab(binding.tabInspect, tab == HomeTab.INSPECT)
        when (tab) {
            HomeTab.SCANS -> refreshHome()
            HomeTab.SCAN_QUEUE -> refreshScanSearchQueueSummary()
            HomeTab.COLLECTIONS -> refreshCollections()
            HomeTab.INSPECT -> {
                rearmFailedRemoteInspectLookup()
                refreshScanSearchQueueSummary()
                refreshInspect()
            }
        }
    }

    private fun refreshContentTab() {
        invalidateRemoteInspectLookup()
        when (activeTab) {
            HomeTab.SCANS -> refreshHome()
            HomeTab.SCAN_QUEUE -> refreshScanSearchQueueSummary()
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
            if (refreshContent) {
                refreshSyncButton()
                refreshContentTab()
            }
            if (collectionChanged) {
                showCollectionTagConflictIfNeeded()
                if (!refreshContent) {
                    invalidateRemoteInspectLookup()
                    when (activeTab) {
                        HomeTab.COLLECTIONS -> refreshCollections()
                        HomeTab.INSPECT -> refreshInspect()
                        HomeTab.SCAN_QUEUE -> refreshScanSearchQueueSummary()
                        HomeTab.SCANS -> refreshCollectionBar()
                    }
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
        if (syncActionInFlight) return
        val transport = Prefs.transport(this)
        val canSync = Prefs.transport(this) != "cloud" || Auth.signedIn(this)
        if (!canSync) {
            Toast.makeText(
                this,
                RemoteUiCatalog.text(this, R.string.home_sync_sign_in),
                Toast.LENGTH_LONG,
            ).show()
            return
        }

        // Auto is cloud-capable whenever its LAN destination is unavailable.
        // Ask before adopting local captures even if this particular run may
        // select LAN; the user remains in control of the permanent association.
        if (transport != "lan" && Auth.signedIn(this)) {
            val owner = Prefs.userId(this)
            syncActionInFlight = true
            binding.syncCaptures.isEnabled = false
            lifecycleScope.launch {
                try {
                    val claimIds = withContext(Dispatchers.IO) {
                        val session = CaptureSession(this@HomeActivity)
                        val snapshot = session.manualSyncCandidates()
                        session.recoverOrphans(snapshot.map { it.name }.toSet())
                        captureIdsNeedingCloudClaim(
                            session.manualSyncCandidates().mapNotNull { dir ->
                                readClaimableCaptureCreator(this@HomeActivity, dir)
                                    ?.let { dir.name to it }
                            },
                            owner,
                        )
                    }
                    if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
                    if (!Auth.signedIn(this@HomeActivity) ||
                        Prefs.userId(this@HomeActivity) != owner) {
                        Toast.makeText(
                            this@HomeActivity,
                            RemoteUiCatalog.text(this@HomeActivity, R.string.home_sync_sign_in),
                            Toast.LENGTH_LONG,
                        ).show()
                        return@launch
                    }
                    if (claimIds.isNotEmpty()) {
                        showCloudClaimConfirmation(claimIds, owner)
                    } else {
                        startCaptureSync()
                    }
                } finally {
                    syncActionInFlight = false
                    if (lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
                        refreshSyncButton()
                    }
                }
            }
            return
        }
        startCaptureSync()
    }

    private fun showCloudClaimConfirmation(entryIds: List<String>, owner: String) {
        val accountLabel = Prefs.email(this).ifBlank { owner }
        val dialog = AlertDialog.Builder(this)
            .setTitle(getString(R.string.home_sync_claim_title, entryIds.size))
            .setMessage(getString(
                R.string.home_sync_claim_message,
                entryIds.size,
                accountLabel,
            ))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.home_sync_claim_confirm) { _, _ ->
                lifecycleScope.launch {
                    if (syncActionInFlight) return@launch
                    syncActionInFlight = true
                    binding.syncCaptures.isEnabled = false
                    try {
                        val results = withContext(Dispatchers.IO) {
                            val claimed = mutableListOf<Pair<String, ClaimCaptureResult>>()
                            for (entryId in entryIds) {
                                val result = claimCaptureForCloud(
                                    this@HomeActivity,
                                    entryId,
                                    expectedAccountId = owner,
                                )
                                claimed += entryId to result
                                if (result == ClaimCaptureResult.SIGNED_OUT ||
                                    result == ClaimCaptureResult.DIFFERENT_ACCOUNT) break
                            }
                            claimed
                        }
                        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
                        if (!Auth.signedIn(this@HomeActivity) ||
                            Prefs.userId(this@HomeActivity) != owner) {
                            Toast.makeText(
                                this@HomeActivity,
                                RemoteUiCatalog.text(
                                    this@HomeActivity,
                                    R.string.home_sync_sign_in,
                                ),
                                Toast.LENGTH_LONG,
                            ).show()
                            return@launch
                        }
                        val successfulIds = results.mapNotNull { (entryId, result) ->
                            entryId.takeIf {
                                result == ClaimCaptureResult.CLAIMED ||
                                    result == ClaimCaptureResult.ALREADY_OWNED
                            }
                        }
                        Prefs.reopenCaptureSyncAfterCloudClaim(
                            this@HomeActivity,
                            owner,
                            successfulIds,
                        )
                        if (successfulIds.size < entryIds.size) {
                            Toast.makeText(
                                this@HomeActivity,
                                RemoteUiCatalog.text(
                                    this@HomeActivity,
                                    R.string.home_sync_claim_partial,
                                    successfulIds.size,
                                    entryIds.size,
                                ),
                                Toast.LENGTH_LONG,
                            ).show()
                        }
                        startCaptureSync()
                    } finally {
                        syncActionInFlight = false
                        if (lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
                            refreshSyncButton()
                        }
                    }
                }
            }
            .show()
        RemoteUiCatalog.apply(dialog)
    }

    private fun startCaptureSync() {
        // Retry the alpha.5 marker repair on an explicit recovery press too;
        // the worker-side synchronized/live-work check makes this a no-op
        // while a valid processing chain is already running.
        lifecycleScope.launch(Dispatchers.IO) {
            ProcessWorker.resumePendingForcedRetries(this@HomeActivity)
        }
        val known = Entries.recent(this)
        val pendingReviewChanges = known.any {
            CaptureMetadataStore.hasPendingReviewSync(it.dir)
        }
        val delivered = known.filter { it.uploaded }
        val deliveredNeedingAttention = delivered.count {
            it.processing.status == Entries.ProcessingStatus.FAILED ||
                it.processing.status == Entries.ProcessingStatus.PARTIAL
        }
        val owner = Prefs.userId(this)
        if (owner.isNotEmpty()) {
            remoteBoxFetches.rearm(owner)
            if (activeTab == HomeTab.INSPECT) {
                inspectedCollectionId?.let(::ensureRemoteBoxListing)
            }
        }
        val state = UploadWorker.enqueueExplicitSync(this)
        CaptureMetadataSyncWorker.enqueueExplicitSync(this)
        Prefs.captureSyncRecord(this)?.takeIf { state.active }?.let { record ->
            syncFeedbackRequestId = record.requestId
            syncFeedbackPhase = state.phase
        }
        refreshSyncButton(state)
        if (state.requestedCount == 0) {
            val message = when (
                captureSyncEmptyReason(
                    requestedCount = state.requestedCount,
                    pendingReviewChanges = pendingReviewChanges,
                    liveCaptureOpen = Prefs.currentEntryId(this) != null,
                    deliveredCount = delivered.size,
                    deliveredNeedingAttention = deliveredNeedingAttention,
                )
            ) {
                CaptureSyncEmptyReason.REVIEW_QUEUED ->
                    RemoteUiCatalog.text(this, R.string.home_sync_review_queued)
                CaptureSyncEmptyReason.LIVE_CAPTURE_ONLY ->
                    RemoteUiCatalog.text(this, R.string.home_sync_live_capture_only)
                CaptureSyncEmptyReason.DELIVERED_WITH_PROCESSING_ISSUES ->
                    RemoteUiCatalog.text(
                        this,
                        R.string.home_sync_delivered_needs_processing,
                        deliveredNeedingAttention,
                    )
                CaptureSyncEmptyReason.ALL_DELIVERED ->
                    RemoteUiCatalog.text(
                        this,
                        R.string.home_sync_all_delivered,
                        delivered.size,
                    )
                CaptureSyncEmptyReason.NOTHING ->
                    RemoteUiCatalog.text(this, R.string.home_sync_none)
            }
            Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        }
    }

    private fun refreshSyncButton(
        state: CaptureSyncState = UploadWorker.captureSyncState(this),
    ) {
        val record = Prefs.captureSyncRecord(this)
        if (record?.requestId == syncFeedbackRequestId) {
            val previous = syncFeedbackPhase
            syncFeedbackPhase = state.phase
            if (previous?.active == true && !state.active) {
                val summary = when (state.phase) {
                    CaptureSyncPhase.COMPLETE -> RemoteUiCatalog.text(
                        this, R.string.home_sync_complete, state.syncedCount,
                    )
                    CaptureSyncPhase.COMPLETE_WITH_ERRORS -> RemoteUiCatalog.text(
                        this,
                        R.string.home_sync_partial,
                        state.syncedCount,
                        state.blockedCount + state.skippedCount,
                    )
                    else -> RemoteUiCatalog.text(this, R.string.home_sync_failed)
                }
                val message = if (state.phase == CaptureSyncPhase.COMPLETE) summary else {
                    Prefs.lastUploadError(this)?.takeIf { it.isNotBlank() }
                        ?.let { "$summary\n$it" } ?: summary
                }
                Toast.makeText(this, message, Toast.LENGTH_LONG).show()
                binding.syncCaptures.announceForAccessibility(message)
                syncFeedbackRequestId = null
                syncFeedbackPhase = null
            }
        }
        // A process death can leave durable accounting at RUNNING after its
        // WorkSpec is terminal. A press is safe: unchanged RUNNING work uses
        // KEEP, while a changed queue snapshot is reconciled under the same
        // request identity before that work resumes.
        val canRetryActive = state.active && state.phase != CaptureSyncPhase.QUEUED
        val showRetryLabel = state.phase == CaptureSyncPhase.WAITING_FOR_PROCESSING ||
            state.phase == CaptureSyncPhase.RETRYING ||
            state.phase == CaptureSyncPhase.COMPLETE_WITH_ERRORS ||
            state.phase == CaptureSyncPhase.FAILED
        val syncControlDisabled = syncActionInFlight || state.active && !canRetryActive
        binding.syncCaptures.isEnabled = !syncControlDisabled
        binding.syncCaptures.alpha = if (syncControlDisabled) .72f else 1f
        binding.syncCaptures.text = when {
            state.phase == CaptureSyncPhase.QUEUED ->
                RemoteUiCatalog.text(this, R.string.home_sync_queued)
            showRetryLabel && state.requestedCount > 0 -> RemoteUiCatalog.text(
                this,
                R.string.home_sync_retry,
                state.syncedCount,
                state.requestedCount,
            )
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

    /**
     * Local snapshot reads are blocking filesystem work. Cancellation cannot
     * interrupt a directory walk already in progress, so serialize all tab
     * snapshots: canceled waiters leave the mutex queue and only the newest
     * requested tab can run after the current read reaches a cancellation
     * checkpoint.
     */
    private suspend fun <T> loadHomeSnapshot(
        block: (CoroutineContext) -> T,
    ): T = withContext(Dispatchers.IO) {
        val loadContext = currentCoroutineContext()
        SNAPSHOT_LOAD_MUTEX.withLock {
            loadContext.ensureActive()
            block(loadContext)
        }
    }

    private fun refreshCollectionBar() {
        cancelCollectionBarLoading()
        collectionBarJob = lifecycleScope.launch {
            val snapshot = loadHomeSnapshot { loadContext ->
                val records = Collections.allRecords(this@HomeActivity)
                loadContext.ensureActive()
                CollectionBarSnapshot(
                    currentCaptureCollection = resolveCurrentCollection(
                        records,
                        Prefs.currentCollectionId(this@HomeActivity),
                        CollectionType.CAPTURE,
                    ),
                    currentScanCollections = resolveScanCollectionSlots(
                        records,
                        ScanCollectionSlot.entries.associateWith {
                            Prefs.currentScanCollectionId(this@HomeActivity, it)
                        },
                    ),
                    collectionPaths = collectionDisplayPaths(records),
                )
            }
            collectionBarJob = null
            if (activeTab != HomeTab.SCANS ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            renderCollectionBar(
                snapshot.currentCaptureCollection,
                snapshot.currentScanCollections,
                snapshot.collectionPaths,
            )
        }
    }

    private fun renderCollectionBar(
        currentCapture: BookCollection?,
        currentScans: Map<ScanCollectionSlot, BookCollection>,
        paths: Map<String, String>,
    ) {
        val captureLabel = currentCapture?.let { paths[it.id] ?: it.name }
            ?: getString(R.string.collections_none_selected)
        val scanLabel = ScanCollectionSlot.entries.joinToString("  \u00b7  ") { slot ->
            val collection = currentScans[slot]
            "${slot.name}: ${collection?.let { paths[it.id] ?: it.name } ?: "\u2014"}"
        }
        binding.collectionBar.text = getString(
            R.string.collections_active_pair,
            captureLabel,
            scanLabel,
        )
        binding.collectionBar.setTextColor(
            getColor(
                if (currentCapture == null || currentScans.isEmpty()) R.color.whl_amber
                else R.color.whl_ink_dim,
            ),
        )
    }

    private fun refreshCollections() {
        resetThumbnailLoading()
        cancelCollectionListLoading()
        collectionListJob = lifecycleScope.launch {
            val snapshot = loadHomeSnapshot { loadContext ->
                val records = Collections.allRecords(this@HomeActivity)
                loadContext.ensureActive()
                val live = records.filter { !it.deleted && it.mergedInto == null }
                val conflictTagId = Prefs.collectionTagConflict(this@HomeActivity)
                val retiredConflict = records.filter {
                    it.deleted &&
                        it.mergedInto == null &&
                        conflictTagId.isNotEmpty() &&
                        canonicalCollectionTagId(it) == conflictTagId
                }
                val currentCapture = resolveCurrentCollection(
                    records,
                    Prefs.currentCollectionId(this@HomeActivity),
                    CollectionType.CAPTURE,
                )
                val currentScans = resolveScanCollectionSlots(
                    records,
                    ScanCollectionSlot.entries.associateWith {
                        Prefs.currentScanCollectionId(this@HomeActivity, it)
                    },
                )
                val counts = CollectionInventory.items(this@HomeActivity)
                    .also { loadContext.ensureActive() }
                    .mapNotNull {
                        loadContext.ensureActive()
                        resolvedLiveCollectionId(it.summary.collectionId, records)
                    }
                    .groupingBy { it }
                    .eachCount()
                CollectionListSnapshot(
                    collections = live + retiredConflict,
                    currentCaptureCollection = currentCapture,
                    currentScanCollections = currentScans,
                    collectionPaths = collectionDisplayPaths(records),
                    bookCounts = counts,
                )
            }
            collectionListJob = null
            if (activeTab != HomeTab.COLLECTIONS ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            renderCollections(snapshot)
        }
    }

    private fun renderCollections(snapshot: CollectionListSnapshot) {
        val list = binding.collectionsList
        list.removeAllViews()
        val collections = snapshot.collections
        val currentCapture = snapshot.currentCaptureCollection
        val currentScans = snapshot.currentScanCollections
        val collectionPaths = snapshot.collectionPaths
        val counts = snapshot.bookCounts
        renderCollectionBar(currentCapture, currentScans, collectionPaths)
        if (collections.isEmpty()) {
            list.addView(emptyNotice(getString(R.string.collections_empty)))
            return
        }
        val inflater = LayoutInflater.from(this)
        for (c in collections) {
            val row = inflater.inflate(R.layout.item_collection, list, false)
            val isRetiredConflict = c.deleted && c.mergedInto == null
            val assignedScanSlots = currentScans.entries
                .filter { it.value.id == c.id }
                .map { it.key }
            val isCurrent = !isRetiredConflict && when (c.collectionType) {
                CollectionType.CAPTURE -> c.id == currentCapture?.id
                CollectionType.SCAN -> assignedScanSlots.isNotEmpty()
            }
            row.isSelected = isCurrent
            ViewCompat.setStateDescription(
                row,
                when {
                    isCurrent && c.collectionType == CollectionType.SCAN ->
                        getString(
                            R.string.collections_current_scan_slots_state,
                            assignedScanSlots.joinToString { it.name },
                        )
                    isCurrent -> getString(R.string.collections_current_state)
                    isRetiredConflict -> getString(R.string.collections_retired_conflict_state)
                    else -> null
                },
            )
            val name = row.findViewById<TextView>(R.id.name)
            val displayName = collectionPaths[c.id] ?: c.name
            name.text = if (isRetiredConflict) {
                getString(R.string.collections_retired_tag_conflict, displayName)
            } else {
                displayName
            }
            name.setTypeface(name.typeface, if (isCurrent) Typeface.BOLD else Typeface.NORMAL)
            row.setBackgroundResource(
                if (isCurrent) R.drawable.whl_collection_current else R.drawable.whl_row)
            row.findViewById<TextView>(R.id.sub).text = listOf(
                if (assignedScanSlots.isEmpty()) "" else getString(
                    R.string.collections_scan_slot_badge,
                    assignedScanSlots.joinToString("/") { it.name },
                ),
                getString(
                    if (c.collectionType == CollectionType.SCAN) {
                        R.string.collections_type_scan_short
                    } else {
                        R.string.collections_type_capture_short
                    },
                ),
                c.tagId,
                if (c.from.isEmpty()) getString(R.string.collections_row_no_from)
                else getString(R.string.collections_row_from, c.from),
                resources.getQuantityString(
                    R.plurals.collections_row_books, counts[c.id] ?: 0, counts[c.id] ?: 0),
            ).filter { it.isNotEmpty() }.joinToString(" \u00b7 ")
            val edit = row.findViewById<View>(R.id.editCollection)
            edit.contentDescription = getString(
                if (isRetiredConflict) R.string.collections_retag_retired_description
                else R.string.collections_edit_description,
                c.name,
            )
            edit.setOnClickListener { editCollection(c) }
            val delete = row.findViewById<View>(R.id.deleteCollection)
            if (isRetiredConflict) {
                delete.visibility = View.GONE
                row.setOnClickListener { editCollection(c) }
            } else {
                delete.contentDescription =
                    getString(R.string.collections_delete_description, c.name)
                delete.setOnClickListener {
                    confirmDeleteCollection(c)
                }
                row.setOnClickListener {
                    if (c.collectionType == CollectionType.SCAN) {
                        chooseScanCollectionSlot(c, currentScans)
                    } else {
                        Prefs.setCurrentCollectionId(this, c.id)
                        expandedScanGroups.add(c.id)
                    }
                    if (c.collectionType != CollectionType.SCAN) Toast.makeText(
                        this,
                        getString(
                            R.string.collections_current,
                            displayName,
                        ),
                        Toast.LENGTH_SHORT,
                    ).show()
                    if (c.collectionType != CollectionType.SCAN) refreshCollections()
                }
            }
            RemoteUiCatalog.apply(row)
            list.addView(row)
        }
    }

    private fun chooseScanCollectionSlot(
        collection: BookCollection,
        current: Map<ScanCollectionSlot, BookCollection>,
    ) {
        val slots = ScanCollectionSlot.entries
        val currentlyAssigned = current.entries.firstOrNull {
            it.value.id == collection.id
        }?.key
        val choices = buildList {
            slots.forEach { slot ->
                val occupant = current[slot]
                add(if (occupant == null || occupant.id == collection.id) {
                    slot.name
                } else {
                    getString(R.string.collections_scan_slot_replace, slot.name, occupant.name)
                })
            }
            if (currentlyAssigned != null) add(getString(R.string.collections_scan_slot_clear))
        }.toTypedArray()
        val dialog = AlertDialog.Builder(this)
            .setTitle(getString(R.string.collections_scan_slot_title, collection.name))
            .setItems(choices) { _, index ->
                val existing = ScanCollectionSlot.entries.associateWith {
                    current[it]?.id
                }
                val selections = if (index < slots.size) {
                    assignScanCollectionSlot(existing, slots[index], collection.id)
                } else {
                    existing.mapValues { (_, id) ->
                        id.takeUnless { it == collection.id }
                    }
                }
                if (!Prefs.setCurrentScanCollectionIds(this, selections)) {
                    Toast.makeText(
                        this,
                        R.string.collections_scan_slot_save_failed,
                        Toast.LENGTH_LONG,
                    ).show()
                } else {
                    Toast.makeText(
                        this,
                        if (index < slots.size) {
                            getString(
                                R.string.collections_current_scan_slot,
                                slots[index].name,
                                collection.name,
                            )
                        } else {
                            getString(R.string.collections_scan_slot_cleared, collection.name)
                        },
                        Toast.LENGTH_SHORT,
                    ).show()
                }
                refreshCollections()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .create()
        dialog.show()
        RemoteUiCatalog.apply(dialog)
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
        val isRetiredConflict = existing?.deleted == true && existing.mergedInto == null
        val collectionRecords = Collections.allRecords(this)
        val collections = collectionRecords.filter { !it.deleted && it.mergedInto == null }
        val collectionPaths = collectionDisplayPaths(collectionRecords)
        val view = layoutInflater.inflate(R.layout.dialog_collection, null)
        val nameField = view.findViewById<AppCompatAutoCompleteTextView>(R.id.collectionName)
        val tagIdField = view.findViewById<android.widget.EditText>(R.id.collectionTagId)
        val typeField = view.findViewById<Spinner>(R.id.collectionType)
        val parentField = view.findViewById<Spinner>(R.id.collectionParent)
        val fromField = view.findViewById<android.widget.EditText>(R.id.collectionFrom)
        val parentIds = mutableListOf<String?>(null)
        val collectionTypes = listOf(CollectionType.CAPTURE, CollectionType.SCAN)
        typeField.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            listOf(
                getString(R.string.collections_type_capture),
                getString(R.string.collections_type_scan),
            ),
        ).apply {
            setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        }
        typeField.setSelection(collectionTypes.indexOf(existing?.collectionType).coerceAtLeast(0))
        typeField.isEnabled = existing == null
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
        if (existing == null) {
            nameField.setAdapter(CollectionNameSuggestionAdapter(this, collections))
        }
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
        if (isRetiredConflict) {
            nameField.isEnabled = false
            typeField.isEnabled = false
            parentField.isEnabled = false
            fromField.isEnabled = false
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle(
                when {
                    existing == null -> R.string.collections_add_title
                    isRetiredConflict -> R.string.collections_retag_retired_title
                    else -> R.string.collections_edit_title
                })
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
            val collectionType = collectionTypes.getOrElse(typeField.selectedItemPosition) {
                CollectionType.CAPTURE
            }
            val parentId = parentIds.getOrNull(parentField.selectedItemPosition)
            val error = if (isRetiredConflict) {
                Collections.retagRetired(this, checkNotNull(existing).id, enteredTagId)
            } else {
                Collections.mutate(this) { current ->
                    if (existing == null) {
                        addCollection(
                            current,
                            name,
                            from,
                            id = collectionId,
                            parentId = parentId,
                            tagId = tagId,
                            collectionType = collectionType,
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
            }
            if (error != null) {
                Toast.makeText(this, error, Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            // A collection the user just created is almost certainly the one
            // they are about to scan into; select it so the next tap works.
            if (existing == null) {
                if (collectionType == CollectionType.SCAN) {
                    val active = Collections.currentScans(this)
                    ScanCollectionSlot.entries.firstOrNull { it !in active }?.let { slot ->
                        Prefs.setCurrentScanCollectionId(this, collectionId, slot)
                    }
                } else {
                    Prefs.setCurrentCollectionId(this, collectionId)
                    expandedScanGroups.add(collectionId)
                }
            }
            invalidateRemoteInspectLookup()
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
                } else {
                    invalidateRemoteInspectLookup()
                }
                refreshCollections()
            }
            .show()
        RemoteUiCatalog.apply(dialog)
    }

    // --- inspect -------------------------------------------------------------

    private fun refreshInspect() {
        resetThumbnailLoading()
        cancelInspectLoading()
        inspectVisibleBookLimit = INSPECT_BOOK_PAGE_SIZE
        inspectJob = lifecycleScope.launch {
            val snapshot = loadHomeSnapshot { loadContext ->
                val records = Collections.allRecords(this@HomeActivity)
                loadContext.ensureActive()
                val paths = collectionDisplayPaths(records)
                val collections = records
                    .filter { !it.deleted && it.mergedInto == null }
                    .sortedBy { (paths[it.id] ?: it.name).lowercase() }
                val cachedOwner = Prefs.userId(this@HomeActivity)
                val cachedBoxes = RemoteCollectionBooks
                    .read(this@HomeActivity, cachedOwner)
                    .byCollection
                loadContext.ensureActive()
                // The cache is keyed by the box that was queried, but a later
                // move deliberately leaves the capture's immutable provenance
                // behind. Resolve each capture once by its newest membership
                // revision and group by the effective collection inside the row,
                // never by that stale outer cache key.
                val remoteBooksById = linkedMapOf<String, RemoteCollectionBook>()
                cachedBoxes.values.flatten().forEach { book ->
                    loadContext.ensureActive()
                    val existing = remoteBooksById[book.captureId]
                    val replace = existing == null ||
                        book.membershipRevision > existing.membershipRevision ||
                        (book.membershipRevision == existing.membershipRevision &&
                            book.removed && !existing.removed)
                    if (replace) remoteBooksById[book.captureId] = book
                }
                val storedMemberships = InspectBookMemberships.read(this@HomeActivity)
                val localMemberships = storedMemberships.memberships.takeIf {
                    storedMemberships.valid
                }.orEmpty()

                fun effectiveMembership(
                    captureId: String,
                    originalCollectionId: String,
                ): Pair<String, Boolean> {
                    val local = localMemberships[captureId]
                    if (local != null) return local.collectionId to local.removed
                    val remote = remoteBooksById[captureId]
                    if (remote != null) return remote.collectionId to remote.removed
                    return originalCollectionId to false
                }

                // Local rows are routed through mutable membership before their
                // frozen scan provenance. They are still added first so the merge
                // below keeps live photos/status over a cloud summary twin.
                val localRows = CollectionInventory.items(this@HomeActivity)
                    .also { loadContext.ensureActive() }
                    .mapNotNull { item ->
                        loadContext.ensureActive()
                        val (effectiveId, removed) = effectiveMembership(
                            item.summary.entryId,
                            item.summary.collectionId,
                        )
                        if (removed) return@mapNotNull null
                        val collectionId = resolvedLiveCollectionId(
                            effectiveId,
                            records,
                        ) ?: return@mapNotNull null
                        val override = localMemberships[item.summary.entryId]
                        val effectiveCollection = records.firstOrNull { it.id == collectionId }
                        val enriched = if (override == null || effectiveCollection == null) {
                            item
                        } else {
                            val marked = effectiveCollection.collectionType == CollectionType.SCAN
                            item.copy(summary = item.summary.copy(
                                collectionType = effectiveCollection.collectionType,
                                scanMarked = marked,
                                scanSourceCollectionId = if (marked) {
                                    item.summary.scanSourceCollectionId.ifEmpty {
                                        item.current?.provenance?.collectionId
                                            ?: item.summary.collectionId
                                    }
                                } else {
                                    item.summary.scanSourceCollectionId
                                },
                                scanDestinationCollectionId = if (marked) {
                                    collectionId
                                } else {
                                    item.summary.scanDestinationCollectionId
                                },
                            ))
                        }
                        collectionId to enriched
                    }
                // "We asked the cloud about THIS box" is only true for a key that
                // is still live. A key that has since merged away was a query for
                // the LOSER's captures; the survivor has its own that were never
                // fetched, so crediting it would let the empty state claim the
                // cloud is empty for a box it never queried.
                val cloudListed = cachedBoxes.keys.filterTo(mutableSetOf()) {
                    resolvedLiveCollectionId(it, records) == it
                }
                val remoteRows = remoteBooksById.values.mapNotNull { book ->
                    loadContext.ensureActive()
                    val (effectiveId, removed) = effectiveMembership(
                        book.captureId,
                        book.collectionId,
                    )
                    if (removed) return@mapNotNull null
                    val collectionId = resolvedLiveCollectionId(
                        effectiveId,
                        records,
                    ) ?: return@mapNotNull null
                    collectionId to book.toInventoryItem()
                }
                val itemsByCollection = (localRows + remoteRows)
                    .groupBy({ it.first }, { it.second })
                    .mapValues { (_, items) ->
                        loadContext.ensureActive()
                        mergeCollectionBookItems(items).map { item ->
                            InspectBookSnapshot(
                                item = item,
                                titleLabel = item.summary.title.ifEmpty {
                                    getString(R.string.inspect_book_untitled)
                                },
                                statusLabel = item.current?.let {
                                    Entries.statusLabel(this@HomeActivity, it)
                                },
                                cloudBacked = item.summary.entryId in remoteBooksById ||
                                    item.summary.deliveryTransport == "cloud" ||
                                    item.current?.deliveryTransport == "cloud",
                                cloudOwnerId = when {
                                    item.summary.entryId in remoteBooksById -> cachedOwner
                                    item.current?.cloudOwnerId?.isNotEmpty() == true ->
                                        item.current.cloudOwnerId
                                    else -> item.summary.cloudOwnerId
                                },
                            )
                        }.sortedWith(
                            compareBy<InspectBookSnapshot> {
                                it.titleLabel.lowercase()
                            }.thenByDescending { it.item.summary.createdAt },
                        )
                    }
                InspectSnapshot(
                    collections = collections,
                    collectionPaths = paths,
                    currentCollectionId = resolveCurrentCollection(
                        records,
                        Prefs.currentCollectionId(this@HomeActivity),
                    )?.id,
                    itemsByCollection = itemsByCollection,
                    cloudListedCollections = cloudListed,
                )
            }
            inspectJob = null
            if (activeTab != HomeTab.INSPECT ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            renderInspect(snapshot)
        }
    }

    private fun renderInspect(snapshot: InspectSnapshot) {
        // Show-more can race an authoritative tab refresh just like scan-page
        // navigation. The new render owns the only thumbnail job.
        resetThumbnailLoading()
        val collections = snapshot.collections
        val paths = snapshot.collectionPaths
        inspectRenderedSnapshot = snapshot
        val counts = snapshot.itemsByCollection.mapValues { it.value.size }
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
        inspectBookViews.clear()
        inspectRenderedCollections = collections
        inspectRenderedCollectionPaths = paths

        if (inspectLookupActive()) {
            clearInspectSelection()
            renderInspectLookup(snapshot)
            return
        }
        binding.inspectLookupStatus.visibility = View.GONE
        binding.inspectLookupResults.visibility = View.GONE
        binding.inspectCollectionScroll.visibility = View.VISIBLE
        binding.inspectBooks.visibility = View.VISIBLE

        if (collections.isEmpty()) {
            clearInspectSelection()
            inspectRenderedItems = emptyMap()
            inspectedCollectionId = null
            inspectVisibleBookLimit = INSPECT_BOOK_PAGE_SIZE
            binding.inspectSelectionHeader.visibility = View.GONE
            binding.inspectEmpty.visibility = View.VISIBLE
            binding.inspectEmpty.text = getString(R.string.inspect_empty_collections)
            return
        }

        val selected = collections.firstOrNull { it.id == inspectedCollectionId }
            ?: collections.firstOrNull { it.id == snapshot.currentCollectionId }
            ?: collections.first()
        inspectedCollectionId = selected.id
        // The local rows are already rendered below; this only adds the books the
        // cloud knows about and this phone does not. Idempotent per session, so
        // the show-more path can re-render without re-fetching.
        ensureRemoteBoxListing(selected.id)

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
                clearInspectSelection()
                inspectedCollectionId = collection.id
                refreshInspect()
            }
            RemoteUiCatalog.apply(chip)
            binding.inspectCollectionChips.addView(chip)
            if (isSelected) selectedChip = chip
        }
        selectedChip?.let { chip ->
            binding.inspectCollectionScroll.post {
                if (activeTab == HomeTab.INSPECT && chip.isAttachedToWindow) {
                    binding.inspectCollectionScroll.smoothScrollTo(
                        (chip.left - dp(10)).coerceAtLeast(0),
                        0,
                    )
                }
            }
        }

        val selectedName = paths[selected.id] ?: selected.name
        val selectedItems = snapshot.itemsByCollection[selected.id].orEmpty()
        inspectRenderedItems = selectedItems.associateBy { it.item.summary.entryId }
        inspectSelection = inspectSelection.reconcile(inspectRenderedItems.keys)
        val furthestSelected = selectedItems.indexOfLast {
            inspectSelection.isSelected(it.item.summary.entryId)
        }
        if (furthestSelected >= inspectVisibleBookLimit) {
            inspectVisibleBookLimit = minOf(
                selectedItems.size,
                ((furthestSelected / INSPECT_BOOK_PAGE_SIZE) + 1) * INSPECT_BOOK_PAGE_SIZE,
            )
        }
        if (!inspectSelection.active) {
            inspectActionMode?.finish()
        } else {
            ensureInspectActionMode()
        }
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
            clearInspectSelection()
            binding.inspectEmpty.visibility = View.VISIBLE
            // Three distinct truths, and only the last one may say the cloud is
            // empty: signed out (cannot ask), asked-but-no-answer (offline or the
            // listing failed), and answered-with-nothing.
            val canAsk = Prefs.configured(this) && Auth.signedIn(this) &&
                Prefs.userId(this).isNotEmpty()
            binding.inspectEmpty.text = getString(
                when {
                    !canAsk -> R.string.inspect_empty_books
                    selected.id in snapshot.cloudListedCollections ->
                        R.string.inspect_empty_books_signed_in
                    else -> R.string.inspect_empty_books_pending
                },
            )
            return
        }
        binding.inspectEmpty.visibility = View.GONE
        renderInspectBooks(selectedItems.take(inspectVisibleBookLimit))
        val remaining = selectedItems.size - inspectVisibleBookLimit
        if (remaining > 0) {
            val nextPageSize = minOf(remaining, INSPECT_BOOK_PAGE_SIZE)
            val showMore = MaterialButton(
                this,
                null,
                com.google.android.material.R.attr.materialButtonOutlinedStyle,
            ).apply {
                text = getString(R.string.inspect_show_more, nextPageSize)
                isAllCaps = false
                minHeight = dp(48)
                setOnClickListener {
                    inspectVisibleBookLimit += nextPageSize
                    renderInspect(snapshot)
                }
            }
            RemoteUiCatalog.apply(showMore)
            binding.inspectBooks.addView(
                showMore,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply {
                    setMargins(dp(8), dp(8), dp(8), dp(12))
                },
            )
        }
    }

    private fun renderInspectBooks(items: List<InspectBookSnapshot>) {
        val inflater = LayoutInflater.from(this)
        val thumbnails = mutableListOf<ThumbnailRequest>()
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
                val inflatedParams = view.layoutParams
                val requestedHeight = inflatedParams?.height
                    ?: ViewGroup.LayoutParams.WRAP_CONTENT
                view.layoutParams = LinearLayout.LayoutParams(
                    0,
                    requestedHeight,
                    1f,
                ).apply {
                    (inflatedParams as? ViewGroup.MarginLayoutParams)?.let { margins ->
                        setMargins(
                            margins.leftMargin,
                            margins.topMargin,
                            margins.rightMargin,
                            margins.bottomMargin,
                        )
                    }
                }
                bindInspectBook(view, item, thumbnails)
                RemoteUiCatalog.apply(view)
                row.addView(view)
                inspectBookViews[item.item.summary.entryId] = view
            }
            repeat(inspectViewMode.columns - chunk.size) {
                row.addView(Space(this), LinearLayout.LayoutParams(0, 1, 1f))
            }
            binding.inspectBooks.addView(row)
        }
        updateInspectSelectionUi()
        startThumbnailLoading(thumbnails, HomeTab.INSPECT)
    }

    private fun bindInspectBook(
        view: View,
        snapshot: InspectBookSnapshot,
        thumbnails: MutableList<ThumbnailRequest>,
    ) {
        val item = snapshot.item
        val summary = item.summary
        view.findViewById<TextView>(R.id.inspectTitle).text = snapshot.titleLabel
        view.findViewById<TextView>(R.id.inspectAuthor)?.text =
            summary.author.ifEmpty { getString(R.string.inspect_content_missing) }
        view.findViewById<TextView>(R.id.inspectYear)?.text =
            summary.year.ifEmpty { getString(R.string.inspect_content_missing) }
        bindScanPriorityIndicator(
            view,
            summary.digitizationCandidate,
            summary.scanPriority,
        )
        val details = mutableListOf<String>()
        summary.author.takeIf { it.isNotEmpty() }?.let(details::add)
        summary.year.takeIf { it.isNotEmpty() }?.let(details::add)
        if (inspectViewMode != InspectViewMode.ICONS) {
            details += resources.getQuantityString(
                R.plurals.inspect_book_pages,
                summary.photoCount,
                summary.photoCount,
            )
            details += when {
                item.current != null -> snapshot.statusLabel.orEmpty()
                // Both cases are photo-less, but "media was cleared" would be a
                // lie about a book this phone never captured.
                item.remote -> getString(R.string.inspect_book_remote)
                else -> getString(R.string.inspect_book_archived)
            }
        }
        view.findViewById<TextView>(R.id.inspectSubtitle)?.text =
            details.joinToString(" \u00b7 ")
        item.current?.let { entry ->
            val image = view.findViewById<ImageView>(R.id.inspectThumb)
            val swatch = view.findViewById<View>(R.id.inspectCoverSwatch)
            if (image != null || swatch != null) {
                thumbnails += ThumbnailRequest(
                    entry = entry,
                    maxWidth = if (swatch != null) 48 else 384,
                    maxHeight = if (swatch != null) 64 else 512,
                    image = image,
                    swatch = swatch,
                )
            }
        }
        val open = {
            if (item.current != null) openEntryDetails(summary.entryId)
            else Toast.makeText(
                this,
                if (item.remote) R.string.inspect_book_remote_unavailable
                else R.string.inspect_book_unavailable,
                Toast.LENGTH_LONG,
            ).show()
        }
        val entryId = summary.entryId
        val priorityDescription = scanPriorityPresentation(
            candidate = summary.digitizationCandidate,
            priority = summary.scanPriority,
            candidateLabel = getString(R.string.home_digitization_candidate),
            priorityLabel = { getString(R.string.scan_priority_description, it) },
            priorityUnsetLabel = getString(R.string.scan_priority_unset),
        ).accessibilityLabel
        view.contentDescription = listOf(
            snapshot.titleLabel,
            details.joinToString(" \u00b7 "),
            priorityDescription,
        ).filter(String::isNotEmpty).joinToString(", ")
        view.setOnClickListener {
            if (inspectActionMode != null) toggleInspectSelection(entryId) else open()
        }
        view.setOnLongClickListener {
            selectInspectBook(entryId)
            true
        }
        ViewCompat.replaceAccessibilityAction(
            view,
            AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_LONG_CLICK,
            getString(R.string.inspect_select_book),
        ) { _, _ ->
            selectInspectBook(entryId)
            true
        }
    }

    private fun inspectLookupActive(): Boolean =
        activeScanSearchProposal?.candidateCaptureId?.isNotBlank() == true ||
            inspectCoverOcrText.isNotBlank() ||
            normalizeInspectBookLookupText(inspectLookupQuery).length >= 2

    private fun renderInspectLookup(snapshot: InspectSnapshot? = inspectRenderedSnapshot) {
        snapshot ?: return
        if (!inspectLookupActive()) {
            if (binding.inspectLookupResults.visibility == View.VISIBLE ||
                binding.inspectCollectionScroll.visibility != View.VISIBLE
            ) {
                renderInspect(snapshot)
            }
            return
        }

        resetThumbnailLoading()
        binding.inspectCollectionScroll.visibility = View.GONE
        binding.inspectSelectionHeader.visibility = View.GONE
        binding.inspectEmpty.visibility = View.GONE
        binding.inspectBooks.visibility = View.GONE
        binding.inspectLookupStatus.visibility = View.VISIBLE
        binding.inspectLookupResults.visibility = View.VISIBLE
        binding.inspectLookupResults.removeAllViews()

        val renderItems = inspectLookupRenderItems(snapshot)
        val records = renderItems.map(InspectLookupRenderItem::book)
        val rawQuery = inspectCoverOcrText.ifBlank { inspectLookupQuery }
        val mode = if (inspectCoverOcrText.isNotBlank()) {
            InspectLookupMode.COVER_OCR
        } else {
            InspectLookupMode.TYPED
        }
        val proposalId = activeScanSearchProposal?.candidateCaptureId.orEmpty()
        val matches = if (proposalId.isNotEmpty()) {
            records.firstOrNull { it.entryId == proposalId }?.let {
                listOf(InspectLookupMatch(it, Int.MAX_VALUE, InspectBookMatchKind.EXACT))
            }.orEmpty()
        } else {
            findInspectBookMatches(records, rawQuery, mode, INSPECT_LOOKUP_LIMIT)
        }
        val topScore = matches.firstOrNull()?.score
        val ambiguous = topScore != null && matches.asSequence()
            .takeWhile { it.score == topScore }
            .map { it.book.collectionId }
            .distinct()
            .take(2)
            .count() > 1
        binding.inspectLookupStatus.text = when (inspectLookupStatusKind(
            matchCount = matches.size,
            ambiguous = ambiguous,
            cloudLoading = inspectLookupCloudLoading,
            cloudFailed = inspectLookupCloudFailed,
        )) {
            InspectLookupStatusKind.SEARCHING_CLOUD ->
                getString(R.string.inspect_lookup_searching_cloud)
            InspectLookupStatusKind.NO_MATCHES_CLOUD_UNAVAILABLE ->
                getString(R.string.inspect_lookup_no_matches_cloud_unavailable)
            InspectLookupStatusKind.NO_MATCHES -> getString(R.string.inspect_lookup_no_matches)
            InspectLookupStatusKind.MATCHES_CLOUD_UNAVAILABLE -> resources.getQuantityString(
                R.plurals.inspect_lookup_matches_cloud_unavailable,
                matches.size,
                matches.size,
            )
            InspectLookupStatusKind.AMBIGUOUS_MATCHES -> resources.getQuantityString(
                R.plurals.inspect_lookup_matches_ambiguous,
                matches.size,
                matches.size,
            )
            InspectLookupStatusKind.MATCHES_SEARCHING_CLOUD -> resources.getQuantityString(
                R.plurals.inspect_lookup_matches_searching,
                matches.size,
                matches.size,
            )
            InspectLookupStatusKind.MATCHES -> resources.getQuantityString(
                R.plurals.inspect_lookup_matches,
                matches.size,
                matches.size,
            )
        }

        val byId = renderItems.associateBy { it.book.entryId }
        val thumbnails = mutableListOf<ThumbnailRequest>()
        val inflater = LayoutInflater.from(this)
        matches.forEach { match ->
            val item = byId[match.book.entryId] ?: return@forEach
            val row = inflater.inflate(
                R.layout.item_inspect_lookup,
                binding.inspectLookupResults,
                false,
            )
            row.findViewById<TextView>(R.id.inspectTitle).text = item.book.title
            val proposal = activeScanSearchProposal?.takeIf {
                it.status == ScanSearchStatus.PROPOSED &&
                    it.candidateCaptureId == item.book.entryId
            }
            val confidenceLabel = proposal?.matchConfidence?.let {
                getString(R.string.scan_queue_match_confidence, (it * 100).roundToInt())
            }.orEmpty()
            row.findViewById<TextView>(R.id.inspectSubtitle).text = listOf(
                item.book.author,
                item.book.year,
                confidenceLabel,
            ).filter(String::isNotBlank).joinToString(" \u00b7 ")
            val collectionDescription = getString(
                R.string.inspect_lookup_collection,
                item.collectionName,
                item.collectionTagId,
            )
            row.findViewById<TextView>(R.id.inspectLookupCollection).text =
                collectionDescription
            val summary = item.snapshot.item.summary
            bindScanPriorityIndicator(
                row,
                summary.digitizationCandidate,
                summary.scanPriority,
            )
            item.snapshot.item.current?.let { entry ->
                thumbnails += ThumbnailRequest(
                    entry = entry,
                    maxWidth = 256,
                    maxHeight = 320,
                    image = row.findViewById(R.id.inspectThumb),
                )
            }
            val priorityDescription = scanPriorityPresentation(
                candidate = summary.digitizationCandidate,
                priority = summary.scanPriority,
                candidateLabel = getString(R.string.home_digitization_candidate),
                priorityLabel = { getString(R.string.scan_priority_description, it) },
                priorityUnsetLabel = getString(R.string.scan_priority_unset),
            ).accessibilityLabel
            row.contentDescription = listOf(
                item.book.title,
                item.book.author,
                item.book.year,
                collectionDescription,
                priorityDescription,
            ).filter(String::isNotBlank).joinToString(", ")
            row.setOnClickListener {
                val queueId = activeScanSearchQueueId
                if (queueId == null) {
                    showInspectLookupCollection(item)
                } else if (proposal == null) {
                    Toast.makeText(
                        this,
                        R.string.scan_queue_waiting_for_proposal,
                        Toast.LENGTH_SHORT,
                    ).show()
                } else {
                    showScanSearchProposalReview(queueId, item)
                }
            }
            RemoteUiCatalog.apply(row)
            binding.inspectLookupResults.addView(row)
        }
        startThumbnailLoading(thumbnails, HomeTab.INSPECT)
        ensureRemoteInspectLookup()
    }

    private fun inspectLookupRenderItems(
        snapshot: InspectSnapshot,
    ): List<InspectLookupRenderItem> {
        val collections = snapshot.collections.associateBy(BookCollection::id)
        val items = linkedMapOf<String, Pair<String, InspectBookSnapshot>>()
        snapshot.itemsByCollection.forEach { (collectionId, books) ->
            books.forEach { book ->
                items[book.item.summary.entryId] = collectionId to book
            }
        }
        val freshCloudById = linkedMapOf<String, RemoteCollectionBook>()
        inspectLookupCloudBooks.asSequence()
            .filterNot(RemoteCollectionBook::removed)
            .forEach { remote ->
                freshCloudById[remote.captureId] = remote
                val existing = items[remote.captureId]
                val remoteItem = remote.toInventoryItem()
                if (existing == null) {
                    items[remote.captureId] = remote.collectionId to InspectBookSnapshot(
                        item = remoteItem,
                        titleLabel = remote.title.ifBlank {
                            getString(R.string.inspect_book_untitled)
                        },
                        statusLabel = null,
                        cloudBacked = true,
                        cloudOwnerId = inspectLookupCloudOwner,
                    )
                } else {
                    val merged = mergeCollectionBookItems(
                        listOf(existing.second.item, remoteItem),
                    ).single()
                    // The all-box response has already applied current local
                    // membership and merge aliases, so it owns the location even
                    // when local provenance or a selected-box cache is older.
                    items[remote.captureId] =
                        remote.collectionId to existing.second.copy(item = merged)
                }
            }

        return items.mapNotNull { (entryId, located) ->
            val (existingCollectionId, rawSnapshot) = located
            val currentDesktop = rawSnapshot.item.current?.desktopBook
            val rawSummary = rawSnapshot.item.summary
            val freshCloud = freshCloudById[entryId]
            val book = mergeInspectLookupBookSources(
                entryId = entryId,
                existingCollectionId = existingCollectionId,
                existing = InspectLookupBibliography(
                    title = rawSummary.title,
                    author = rawSummary.author,
                    year = rawSummary.year,
                ),
                currentDesktop = InspectLookupBibliography(
                    title = currentDesktop?.bibliography?.title.orEmpty(),
                    author = currentDesktop?.bibliography?.author.orEmpty(),
                    year = currentDesktop?.bibliography?.year.orEmpty(),
                ),
                freshCloudCollectionId = freshCloud?.collectionId.orEmpty(),
                freshCloud = InspectLookupBibliography(
                    title = freshCloud?.title.orEmpty(),
                    author = freshCloud?.author.orEmpty(),
                    year = freshCloud?.year.orEmpty(),
                ),
                createdAt = rawSummary.createdAt,
            )
            val collection = collections[book.collectionId] ?: return@mapNotNull null
            val candidate = when {
                currentDesktop?.digitizationCandidateClassification == true -> true
                rawSummary.digitizationCandidate -> true
                currentDesktop?.digitizationCandidateClassification == false -> false
                else -> rawSummary.digitizationCandidateClassification
            }
            val priority = if (candidate == true) {
                currentDesktop?.scanPriority ?: rawSummary.scanPriority
            } else {
                null
            }
            val summary = rawSummary.copy(
                title = book.title,
                author = book.author,
                year = book.year,
                digitizationCandidateClassification = candidate,
                scanPriority = priority,
            )
            val enrichedSnapshot = rawSnapshot.copy(
                item = rawSnapshot.item.copy(summary = summary),
                titleLabel = book.title.ifBlank { getString(R.string.inspect_book_untitled) },
            )
            InspectLookupRenderItem(
                book = book,
                snapshot = enrichedSnapshot,
                collectionName = snapshot.collectionPaths[book.collectionId] ?: collection.name,
                collectionTagId = collection.tagId,
            )
        }
    }

    private fun showInspectLookupCollection(item: InspectLookupRenderItem) {
        inspectedCollectionId = item.book.collectionId
        inspectCoverOcrText = ""
        inspectLookupQuery = ""
        suppressInspectLookupWatcher = true
        binding.inspectBookSearch.text?.clear()
        suppressInspectLookupWatcher = false
        Toast.makeText(
            this,
            getString(R.string.inspect_lookup_found, item.collectionName),
            Toast.LENGTH_SHORT,
        ).show()
        refreshInspect()
        binding.homeScroll.post {
            if (activeTab == HomeTab.INSPECT) binding.homeScroll.fullScroll(View.FOCUS_UP)
        }
    }

    private fun showScanSearchProposalReview(
        queueId: String,
        item: InspectLookupRenderItem,
    ) {
        if (scanQueueMutationInFlight) return
        lifecycleScope.launch {
            val details = withContext(Dispatchers.IO) {
                val queue = ScanSearchQueue.read(this@HomeActivity)
                    .takeIf { it.valid }
                    ?.items
                    ?.firstOrNull {
                        it.id == queueId && it.status == ScanSearchStatus.PROPOSED &&
                            it.candidateCaptureId == item.book.entryId
                    }
                    ?: return@withContext null
                val records = Collections.allRecords(this@HomeActivity)
                val destination = records.firstOrNull {
                    it.id == queue.scanCollectionId && !it.deleted && it.mergedInto == null &&
                        it.collectionType == CollectionType.SCAN
                } ?: return@withContext null
                Triple(queue, destination, records)
            }
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return@launch
            if (details == null) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.scan_queue_match_failed,
                    Toast.LENGTH_LONG,
                ).show()
                refreshScanSearchQueueSummary()
                return@launch
            }
            val (queue, destination, records) = details
            val sourceId = item.snapshot.item.summary.scanSourceCollectionId
                .takeIf(SAFE_COLLECTION_FILTER_ID::matches)
                ?: item.book.collectionId
            val sourceName = records.firstOrNull { it.id == sourceId }?.let {
                inspectRenderedCollectionPaths[it.id] ?: it.name
            } ?: item.collectionName
            val dialog = AlertDialog.Builder(this@HomeActivity)
                .setTitle(R.string.scan_queue_review_title)
                .setMessage(
                    getString(
                        R.string.scan_queue_review_message,
                        item.book.title,
                        queue.matchConfidence?.let { (it * 100).roundToInt() }
                            ?.toString() ?: "?",
                        sourceName,
                        inspectRenderedCollectionPaths[destination.id] ?: destination.name,
                    ),
                )
                .setNeutralButton(android.R.string.cancel, null)
                .setNegativeButton(R.string.scan_queue_reject_action) { _, _ ->
                    rejectScanSearchProposal(queue)
                }
                .setPositiveButton(R.string.scan_queue_approve_action) { _, _ ->
                    approveScanSearchProposal(queue, destination, item)
                }
                .create()
            dialog.show()
            RemoteUiCatalog.apply(dialog)
        }
    }

    private fun approveScanSearchProposal(
        queue: ScanSearchQueueItem,
        destination: BookCollection,
        item: InspectLookupRenderItem,
    ) {
        if (scanQueueMutationInFlight) return
        scanQueueMutationInFlight = true
        lifecycleScope.launch {
            val outcome = withContext(Dispatchers.IO) {
                INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock {
                    try {
                        val captureId = item.book.entryId.trim().lowercase()
                        require(SAFE_CAPTURE_SYNC_ID.matches(captureId))
                        val owner = queue.ownerId.trim().lowercase()
                        require(SAFE_CAPTURE_SYNC_ID.matches(owner))
                        require(Prefs.userId(this@HomeActivity).trim().lowercase() == owner)
                        require(queue.candidateCaptureId == captureId)
                        require(queue.scanCollectionId == destination.id)
                        if (Prefs.currentEntryId(this@HomeActivity) == captureId) {
                            return@withLock ScanProposalDecisionOutcome.FAILED
                        }

                        val records = Collections.allRecords(this@HomeActivity)
                        val liveDestination = records.firstOrNull {
                            it.id == destination.id && !it.deleted && it.mergedInto == null &&
                                it.collectionType == CollectionType.SCAN
                        }
                        val storedEntry = Entries.findIncludingArchive(
                            this@HomeActivity,
                            captureId,
                        )
                        val summary = item.snapshot.item.summary
                        val sourceId = sequenceOf(
                            summary.scanSourceCollectionId,
                            storedEntry?.provenance?.collectionId.orEmpty(),
                            item.book.collectionId,
                        ).map(String::trim)
                            .firstOrNull { candidate ->
                                SAFE_COLLECTION_FILTER_ID.matches(candidate) &&
                                    records.any {
                                        it.id == candidate &&
                                            it.collectionType == CollectionType.CAPTURE
                                    }
                            }
                        val client = ScanWorkflowClient(this@HomeActivity, owner)
                        val accepted = try {
                            client.approve(queue.id, captureId)
                        } catch (error: SupabaseClient.HttpException) {
                            if (isStaleScanProposalError(error)) {
                                refreshLiveScanSearchQueue(client, owner)
                                return@withLock ScanProposalDecisionOutcome.STALE
                            }
                            throw error
                        }
                        check(accepted.status == ScanSearchStatus.MATCHED &&
                            accepted.candidateCaptureId == captureId &&
                            accepted.matchedCaptureId == captureId)

                        // The approval RPC has already moved cloud membership and
                        // recorded provenance atomically. These local projections
                        // are best-effort cache/sidecar updates, never an outbox.
                        if (storedEntry != null && sourceId != null && liveDestination != null) {
                            runCatching {
                                CaptureScanMarkStore.write(
                                    storedEntry.dir,
                                    sourceId,
                                    liveDestination.id,
                                )
                            }
                        }
                        if (liveDestination != null) {
                            runCatching {
                                RemoteCollectionBooks.applyMembershipMutation(
                                    this@HomeActivity,
                                    setOf(captureId),
                                    liveDestination.id,
                                    removed = false,
                                    owner = owner,
                                )
                            }
                        }
                        ScanSearchQueue.acknowledge(this@HomeActivity, queue, accepted)
                        refreshLiveScanSearchQueue(client, owner)
                        ScanProposalDecisionOutcome.APPLIED
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                        ScanProposalDecisionOutcome.FAILED
                    }
                }
            }
            scanQueueMutationInFlight = false
            if (outcome == ScanProposalDecisionOutcome.FAILED) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.scan_queue_match_failed,
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }
            activeScanSearchQueueId = null
            activeScanSearchProposal = null
            inspectCoverOcrText = ""
            inspectLookupQuery = ""
            suppressInspectLookupWatcher = true
            binding.inspectBookSearch.text?.clear()
            suppressInspectLookupWatcher = false
            ScanSearchQueueSyncWorker.enqueue(this@HomeActivity)
            invalidateRemoteInspectLookup()
            refreshScanSearchQueueSummary()
            refreshInspect()
            Toast.makeText(
                this@HomeActivity,
                if (outcome == ScanProposalDecisionOutcome.STALE) {
                    R.string.scan_queue_proposal_stale
                } else {
                    R.string.scan_queue_match_saved
                },
                Toast.LENGTH_LONG,
            ).show()
        }
    }

    private fun rejectScanSearchProposal(queue: ScanSearchQueueItem) {
        if (scanQueueMutationInFlight) return
        scanQueueMutationInFlight = true
        lifecycleScope.launch {
            val outcome = withContext(Dispatchers.IO) {
                INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock {
                    try {
                        val owner = queue.ownerId.trim().lowercase()
                        val captureId = queue.candidateCaptureId.trim().lowercase()
                        require(SAFE_CAPTURE_SYNC_ID.matches(owner))
                        require(SAFE_CAPTURE_SYNC_ID.matches(captureId))
                        require(Prefs.userId(this@HomeActivity).trim().lowercase() == owner)
                        val client = ScanWorkflowClient(this@HomeActivity, owner)
                        val accepted = try {
                            client.reject(queue.id, captureId)
                        } catch (error: SupabaseClient.HttpException) {
                            if (isStaleScanProposalError(error)) {
                                refreshLiveScanSearchQueue(client, owner)
                                return@withLock ScanProposalDecisionOutcome.STALE
                            }
                            throw error
                        }
                        check(accepted.status == ScanSearchStatus.REJECTED &&
                            accepted.candidateCaptureId == captureId)
                        ScanSearchQueue.acknowledge(this@HomeActivity, queue, accepted)
                        refreshLiveScanSearchQueue(client, owner)
                        ScanProposalDecisionOutcome.APPLIED
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                        ScanProposalDecisionOutcome.FAILED
                    }
                }
            }
            scanQueueMutationInFlight = false
            if (outcome == ScanProposalDecisionOutcome.FAILED) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.scan_queue_match_failed,
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }
            activeScanSearchQueueId = null
            activeScanSearchProposal = null
            inspectCoverOcrText = ""
            inspectLookupQuery = ""
            suppressInspectLookupWatcher = true
            binding.inspectBookSearch.text?.clear()
            suppressInspectLookupWatcher = false
            ScanSearchQueueSyncWorker.enqueue(this@HomeActivity)
            refreshScanSearchQueueSummary()
            refreshInspect()
            Toast.makeText(
                this@HomeActivity,
                if (outcome == ScanProposalDecisionOutcome.STALE) {
                    R.string.scan_queue_proposal_stale
                } else {
                    R.string.scan_queue_rejected
                },
                Toast.LENGTH_LONG,
            ).show()
        }
    }

    /** Pull the complete live snapshot so accepted/rejected session siblings vanish locally. */
    private fun refreshLiveScanSearchQueue(
        client: ScanWorkflowClient,
        owner: String,
    ): Boolean = try {
        ScanSearchQueue.mergeCloud(this, owner, client.queue())
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        false
    }

    private fun resetRemoteInspectLookup(owner: String) {
        inspectLookupCloudGeneration += 1
        inspectLookupCloudBooks = emptyList()
        inspectLookupCloudOwner = owner
        inspectLookupCloudLoading = false
        inspectLookupCloudLoaded = false
        inspectLookupCloudFailed = false
    }

    private fun currentInspectLookupOwner(): String =
        if (Auth.signedIn(this)) Prefs.userId(this) else ""

    /** Retire every in-flight request before rebuilding from changed source data. */
    private fun invalidateRemoteInspectLookup() {
        resetRemoteInspectLookup(currentInspectLookupOwner())
    }

    /** A failure remains visible until the operator retries or revisits Inspect. */
    private fun rearmFailedRemoteInspectLookup() {
        if (inspectLookupCloudFailed) invalidateRemoteInspectLookup()
    }

    /** Fetch every owner box only when lookup needs a complete cross-box answer. */
    private fun ensureRemoteInspectLookup() {
        if (!inspectLookupActive() || inspectLookupCloudLoading || inspectLookupCloudLoaded ||
            inspectLookupCloudFailed || !Prefs.configured(this) || !Auth.signedIn(this)
        ) return
        val owner = Prefs.userId(this)
        if (owner.isEmpty()) return
        if (inspectLookupCloudOwner != owner) resetRemoteInspectLookup(owner)
        inspectLookupCloudLoading = true
        val generation = inspectLookupCloudGeneration
        renderInspectLookup()
        lifecycleScope.launch {
            val loaded = try {
                withContext(Dispatchers.IO) {
                    val records = Collections.allRecords(this@HomeActivity)
                    val collectionIds = records.asSequence()
                        .map { it.id.trim().lowercase() }
                        .filter(SAFE_COLLECTION_FILTER_ID::matches)
                        .distinct()
                        .toList()
                    if (collectionIds.isEmpty()) return@withContext emptyList()
                    val client = SupabaseClient(this@HomeActivity, owner)
                    val byId = linkedMapOf<String, RemoteCollectionBook>()
                    collectionIds.chunked(INSPECT_LOOKUP_COLLECTION_BATCH).forEach { batch ->
                        currentCoroutineContext().ensureActive()
                        client.capturesForCollections(batch).forEach { book ->
                            val existing = byId[book.captureId]
                            val replace = existing == null ||
                                book.membershipRevision > existing.membershipRevision ||
                                (book.membershipRevision == existing.membershipRevision &&
                                    book.removed && !existing.removed)
                            if (replace) byId[book.captureId] = book
                        }
                    }
                    val books = byId.values.toList()
                    val enriched = enrichRemoteCollectionBooks(
                        books,
                        client.desktopBookMetadata(books.map { it.captureId }),
                    )
                    val memberships = InspectBookMemberships.read(this@HomeActivity)
                        .takeIf { it.valid }
                        ?.memberships
                        .orEmpty()
                    enriched.mapNotNull { book ->
                        currentCoroutineContext().ensureActive()
                        val local = memberships[book.captureId]
                        val removed = local?.removed ?: book.removed
                        if (removed) return@mapNotNull null
                        val requestedId = local?.collectionId?.ifEmpty { book.collectionId }
                            ?: book.collectionId
                        val collectionId = resolvedLiveCollectionId(requestedId, records)
                            ?: return@mapNotNull null
                        book.copy(collectionId = collectionId, removed = false)
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                null
            }
            val signedIn = Auth.signedIn(this@HomeActivity)
            val currentOwner = Prefs.userId(this@HomeActivity).takeIf { signedIn }.orEmpty()
            when (inspectLookupCloudCompletion(
                requestGeneration = generation,
                currentGeneration = inspectLookupCloudGeneration,
                requestOwner = owner,
                currentOwner = currentOwner,
                signedIn = signedIn,
            )) {
                InspectLookupCloudCompletion.IGNORE_RETIRED_GENERATION -> return@launch
                InspectLookupCloudCompletion.RESET_STALE_OWNER -> {
                    // This request still owns the loading flag, but its account no
                    // longer does. Clear it without touching a newer generation.
                    resetRemoteInspectLookup(currentOwner)
                    if (activeTab == HomeTab.INSPECT &&
                        lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
                    ) renderInspectLookup()
                    return@launch
                }
                InspectLookupCloudCompletion.ACCEPT -> Unit
            }
            inspectLookupCloudLoading = false
            inspectLookupCloudLoaded = loaded != null
            inspectLookupCloudFailed = loaded == null
            inspectLookupCloudBooks = loaded.orEmpty()
            if (activeTab == HomeTab.INSPECT &&
                lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) renderInspectLookup()
        }
    }

    private val inspectActionModeCallback = object : ActionMode.Callback {
        override fun onCreateActionMode(mode: ActionMode, menu: Menu): Boolean {
            menu.add(Menu.NONE, MENU_INSPECT_MARK_SCAN, 0, R.string.inspect_selection_mark_scan)
                .setIcon(R.drawable.ic_scan_priority)
                .setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
            menu.add(Menu.NONE, MENU_INSPECT_MOVE, 1, R.string.inspect_selection_move)
                .setIcon(R.drawable.ic_collections)
                .setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
            menu.add(Menu.NONE, MENU_INSPECT_DELETE, 2, R.string.inspect_selection_delete)
                .setIcon(R.drawable.ic_delete)
                .setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
            return true
        }

        override fun onPrepareActionMode(mode: ActionMode, menu: Menu): Boolean {
            menu.findItem(MENU_INSPECT_MARK_SCAN)?.isEnabled = !inspectMutationInFlight
            menu.findItem(MENU_INSPECT_MOVE)?.isEnabled = !inspectMutationInFlight
            menu.findItem(MENU_INSPECT_DELETE)?.isEnabled = !inspectMutationInFlight
            return true
        }

        override fun onActionItemClicked(mode: ActionMode, item: MenuItem): Boolean = when (item.itemId) {
            MENU_INSPECT_MARK_SCAN -> {
                val destination = Collections.currentScan(this@HomeActivity)
                if (destination == null) {
                    Toast.makeText(
                        this@HomeActivity,
                        R.string.scan_queue_needs_collection,
                        Toast.LENGTH_LONG,
                    ).show()
                } else {
                    mutateInspectSelection(destination, removed = false)
                }
                true
            }
            MENU_INSPECT_MOVE -> {
                showInspectMoveDialog()
                true
            }
            MENU_INSPECT_DELETE -> {
                showInspectDeleteConfirmation()
                true
            }
            else -> false
        }

        override fun onDestroyActionMode(mode: ActionMode) {
            if (inspectActionMode === mode) inspectActionMode = null
            inspectSelection = inspectSelection.clear()
            updateInspectSelectionUi()
        }
    }

    private fun ensureInspectActionMode() {
        if (!inspectSelection.active || inspectActionMode != null || isFinishing) return
        inspectActionMode = startSupportActionMode(inspectActionModeCallback)
        updateInspectSelectionUi()
    }

    private fun selectInspectBook(entryId: String) {
        if (inspectMutationInFlight) return
        if (entryId !in inspectRenderedItems) return
        if (!inspectSelection.isSelected(entryId) &&
            inspectSelection.size >= CAPTURE_COLLECTION_MUTATION_MAX_IDS
        ) {
            Toast.makeText(this, R.string.inspect_selection_limit, Toast.LENGTH_LONG).show()
            return
        }
        val updated = inspectSelection.addFromLongPress(entryId)
        val changed = updated != inspectSelection
        inspectSelection = updated
        ensureInspectActionMode()
        updateInspectSelectionUi()
        if (changed) {
            binding.inspectBooks.announceForAccessibility(
                resources.getQuantityString(
                    R.plurals.inspect_selection_count,
                    inspectSelection.size,
                    inspectSelection.size,
                ),
            )
        }
    }

    private fun toggleInspectSelection(entryId: String) {
        if (inspectMutationInFlight) return
        if (entryId !in inspectRenderedItems) return
        if (!inspectSelection.isSelected(entryId) &&
            inspectSelection.size >= CAPTURE_COLLECTION_MUTATION_MAX_IDS
        ) {
            Toast.makeText(this, R.string.inspect_selection_limit, Toast.LENGTH_LONG).show()
            return
        }
        inspectSelection = inspectSelection.toggleFromTap(entryId)
        if (!inspectSelection.active) {
            inspectActionMode?.finish()
        } else {
            ensureInspectActionMode()
            updateInspectSelectionUi()
        }
    }

    private fun clearInspectSelection() {
        val mode = inspectActionMode
        if (mode != null) {
            mode.finish()
        } else if (inspectSelection.active) {
            inspectSelection = inspectSelection.clear()
            updateInspectSelectionUi()
        }
    }

    private fun updateInspectSelectionUi() {
        inspectBookViews.forEach { (entryId, view) ->
            val selected = inspectSelection.isSelected(entryId)
            view.isActivated = selected
            ViewCompat.setStateDescription(
                view,
                getString(R.string.selection_selected_state).takeIf { selected },
            )
        }
        inspectActionMode?.apply {
            title = resources.getQuantityString(
                R.plurals.inspect_selection_count,
                inspectSelection.size,
                inspectSelection.size,
            )
            invalidate()
        }
    }

    private fun showInspectMoveDialog() {
        if (inspectMutationInFlight || !inspectSelection.active) return
        val destinations = inspectRenderedCollections.filter { it.id != inspectedCollectionId }
        if (destinations.isEmpty()) {
            Toast.makeText(this, R.string.inspect_selection_no_destination, Toast.LENGTH_LONG).show()
            return
        }
        val labels = destinations.map {
            inspectRenderedCollectionPaths[it.id] ?: it.name
        }.toTypedArray()
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.inspect_selection_move_title)
            .setItems(labels) { _, index ->
                mutateInspectSelection(destinations[index], removed = false)
            }
            .setNegativeButton(android.R.string.cancel, null)
            .create()
        dialog.show()
        RemoteUiCatalog.apply(dialog)
    }

    private fun showInspectDeleteConfirmation() {
        if (inspectMutationInFlight || !inspectSelection.active) return
        val count = inspectSelection.size
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.inspect_selection_delete_title)
            .setMessage(
                resources.getQuantityString(
                    R.plurals.inspect_selection_delete_message,
                    count,
                    count,
                ),
            )
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.inspect_selection_delete) { _, _ ->
                mutateInspectSelection(destination = null, removed = true)
            }
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(getColor(R.color.whl_red))
        }
        dialog.show()
        RemoteUiCatalog.apply(dialog)
    }

    private fun mutateInspectSelection(
        destination: BookCollection?,
        removed: Boolean,
    ) {
        if (inspectMutationInFlight || !inspectSelection.active) return
        val selected = inspectSelection.selectedIds.mapNotNull(inspectRenderedItems::get)
        if (selected.isEmpty()) return
        val activeCaptureId = Prefs.currentEntryId(this)
        if (selected.any { it.item.summary.entryId == activeCaptureId }) {
            Toast.makeText(this, R.string.inspect_selection_active_capture, Toast.LENGTH_LONG).show()
            return
        }
        val targetCollectionId = destination?.id ?: inspectedCollectionId.orEmpty()
        if (targetCollectionId.isEmpty()) return

        inspectMutationInFlight = true
        inspectActionMode?.invalidate()
        // No listing that started before this action may acknowledge or cache an
        // older membership after the mutation lands.
        remoteBoxFetches.rearm(Prefs.userId(this))
        lifecycleScope.launch {
            val outcome = withContext(Dispatchers.IO) {
                INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock {
                    try {
                        val collectionRecords = Collections.allRecords(this@HomeActivity)
                        val liveTargetCollectionId = resolvedLiveCollectionId(
                            targetCollectionId,
                            collectionRecords,
                        ) ?: throw IllegalStateException("destination collection is no longer live")
                        val liveDestination = collectionRecords.firstOrNull {
                            it.id == liveTargetCollectionId && !it.deleted && it.mergedInto == null
                        } ?: throw IllegalStateException("destination collection is no longer live")
                        val selectedIds = selected.map { it.item.summary.entryId }
                        val existingMemberships = InspectBookMemberships
                            .read(this@HomeActivity)
                        check(existingMemberships.valid) {
                            "membership intents could not be read"
                        }
                        val activeCloudSyncTargets = Prefs
                            .activeCaptureSyncRecord(this@HomeActivity)
                            ?.takeIf { record ->
                                if (record.resolvedTransport.isNotEmpty()) {
                                    record.resolvedTransport == "cloud"
                                } else {
                                    record.transportMode != "lan"
                                }
                            }
                            ?.targetIds
                            .orEmpty()

                        val cloudCandidates = mutableListOf<InspectCloudMutationCandidate>()
                        val localEntriesById = linkedMapOf<String, List<Entries.Entry>>()
                        selected.forEach { snapshot ->
                            val id = snapshot.item.summary.entryId
                            EntryOperationLocks.withLock(id) {
                                val storedEntry = Entries.findIncludingArchive(
                                    this@HomeActivity,
                                    id,
                                )
                                val currentEntry = snapshot.item.current
                                val cloudBacked = snapshot.cloudBacked || snapshot.item.remote ||
                                    currentEntry?.let {
                                        isCloudMetadataEntry(this@HomeActivity, it)
                                    } == true || storedEntry?.let {
                                        isCloudMetadataEntry(this@HomeActivity, it)
                                    } == true
                                val transports = listOf(
                                    snapshot.item.summary.deliveryTransport,
                                    currentEntry?.deliveryTransport.orEmpty(),
                                    storedEntry?.deliveryTransport.orEmpty(),
                                ).filter(String::isNotEmpty).toSet()
                                val localEntries = listOfNotNull(currentEntry, storedEntry)
                                    .distinctBy { it.dir.absolutePath }
                                localEntriesById[id] = localEntries
                                val knownLan = "lan" in transports && "cloud" !in transports
                                val couldBeInterruptedCloudInsert = id in activeCloudSyncTargets
                                cloudCandidates += InspectCloudMutationCandidate(
                                    captureId = id,
                                    cloudBacked = cloudBacked,
                                    ownerProbeEligible = !knownLan && (
                                        cloudBacked ||
                                            localEntries.isEmpty() ||
                                            localEntries.any { it.uploaded } ||
                                            couldBeInterruptedCloudInsert
                                        ),
                                    ownerEvidence = listOf(
                                        snapshot.cloudOwnerId,
                                        snapshot.item.summary.cloudOwnerId,
                                        currentEntry?.cloudOwnerId.orEmpty(),
                                        storedEntry?.cloudOwnerId.orEmpty(),
                                        existingMemberships.memberships[id]
                                            ?.cloudOwnerId
                                            .orEmpty(),
                                    ),
                                )
                            }
                        }

                        val cloudPlan = planInspectCloudMutation(cloudCandidates)
                        // Validate every durable owner before making the local
                        // action visible. The complete move/tombstone then lands
                        // in one write, so failures never leave a half-delete.
                        check(InspectBookMemberships.setMembership(
                            this@HomeActivity,
                            selectedIds,
                            liveTargetCollectionId,
                            removed,
                            cleanupPending = removed,
                        )) { "membership intent could not be saved" }

                        // Keep the entry-local marker aligned with active
                        // membership. When moving into a scan collection, retain
                        // the first capture-type source; scan-to-scan moves only
                        // update the destination. Moving out clears the active
                        // sidecar while provenance/cloud retain the audit trail.
                        localEntriesById.forEach { (captureId, localEntries) ->
                            localEntries.forEach { entry ->
                                if (removed || liveDestination.collectionType != CollectionType.SCAN) {
                                    CaptureScanMarkStore.clear(entry.dir)
                                } else {
                                    val snapshot = selected.first {
                                        it.item.summary.entryId == captureId
                                    }
                                    val previousMark = CaptureScanMarkStore.read(entry.dir)
                                    val pendingSource = existingMemberships.memberships[captureId]
                                        ?.takeIf { !it.removed }
                                        ?.collectionId
                                        ?.takeIf { sourceId ->
                                            collectionRecords.firstOrNull { it.id == sourceId }
                                                ?.collectionType == CollectionType.CAPTURE
                                        }
                                    val summarySource = snapshot.item.summary
                                        .scanSourceCollectionId
                                        .takeIf(SAFE_CAPTURE_SYNC_ID::matches)
                                    val effectiveSource = pendingSource
                                        ?: previousMark?.sourceCollectionId
                                        ?: summarySource
                                        ?: inspectedCollectionId?.takeIf { sourceId ->
                                            collectionRecords.firstOrNull { it.id == sourceId }
                                                ?.collectionType == CollectionType.CAPTURE
                                        }
                                        ?: entry.provenance?.collectionId?.takeIf { sourceId ->
                                            collectionRecords.firstOrNull { it.id == sourceId }
                                                ?.collectionType == CollectionType.CAPTURE
                                        }
                                    if (effectiveSource != null &&
                                        effectiveSource != liveTargetCollectionId
                                    ) {
                                        CaptureScanMarkStore.write(
                                            entry.dir,
                                            effectiveSource,
                                            liveTargetCollectionId,
                                        )
                                    }
                                }
                            }
                        }
                        var cleanupFailures = 0
                        if (removed) {
                            selectedIds.forEach { captureId ->
                                when (Entries.deleteLocalSafely(
                                    this@HomeActivity,
                                    captureId,
                                    allowUploaded = true,
                                )) {
                                    Entries.DeleteResult.DELETED,
                                    Entries.DeleteResult.MISSING ->
                                        InspectBookMemberships.markCleanupComplete(
                                            this@HomeActivity,
                                            setOf(captureId),
                                        )
                                    Entries.DeleteResult.ACTIVE_CAPTURE,
                                    Entries.DeleteResult.ALREADY_UPLOADED,
                                    Entries.DeleteResult.DELETE_FAILED -> cleanupFailures += 1
                                }
                            }
                        }
                        var cloudPersistencePending = false
                        cloudPlan.captureIdsByOwner.forEach { (owner, captureIds) ->
                            if (!InspectBookMemberships.markCloud(
                                    this@HomeActivity,
                                    captureIds,
                                    owner,
                                )
                            ) {
                                cloudPersistencePending = true
                            }
                            remoteBoxFetches.rearm(owner)
                        }

                        val mutationOwner = Prefs.userId(this@HomeActivity)
                            .trim()
                            .lowercase()
                        val canMutateCloud = Prefs.configured(this@HomeActivity) &&
                            Auth.signedIn(this@HomeActivity) &&
                            SAFE_CAPTURE_SYNC_ID.matches(mutationOwner)
                        val client = if (canMutateCloud) {
                            SupabaseClient(this@HomeActivity, mutationOwner)
                        } else {
                            null
                        }
                        val resolvedCloudIds = cloudPlan.captureIdsByOwner[mutationOwner]
                            .orEmpty()
                            .toMutableSet()
                        val unresolvedCloudIds = cloudPlan.unresolvedCloudCaptureIds
                            .toMutableSet()

                        // Legacy inventory rows and an upload interrupted after
                        // remote insert have no durable owner stamp. An RLS-scoped
                        // existence lookup may prove the current owner; absence
                        // never licenses adopting that account.
                        if (client != null && cloudPlan.probeCaptureIds.isNotEmpty()) {
                            try {
                                val discovered = client.captureImportStates(
                                    cloudPlan.probeCaptureIds.toList(),
                                ).keys
                                if (discovered.isNotEmpty()) {
                                    if (!InspectBookMemberships.markCloud(
                                            this@HomeActivity,
                                            discovered,
                                            mutationOwner,
                                        )
                                    ) {
                                        cloudPersistencePending = true
                                    }
                                    resolvedCloudIds += discovered
                                    unresolvedCloudIds -= discovered
                                }
                            } catch (e: CancellationException) {
                                throw e
                            } catch (_: Exception) {
                                // The staged intent remains durable. A later box
                                // listing or UploadWorker will prove its owner.
                            }
                        }

                        var cloudPending = cloudPersistencePending ||
                            unresolvedCloudIds.isNotEmpty() ||
                            cloudPlan.captureIdsByOwner.any { (owner) ->
                                owner != mutationOwner || !canMutateCloud
                            }
                        if (resolvedCloudIds.isNotEmpty()) {
                            if (client == null) {
                                cloudPending = true
                            } else {
                                val isolated = isolateInspectMembershipMutation(
                                    captureIds = resolvedCloudIds.toList(),
                                    shouldBisect = ::shouldBisectInspectMembershipFailure,
                                    mutate = { batch ->
                                        client.mutateCaptureCollection(
                                            captureIds = batch,
                                            collectionId = liveTargetCollectionId,
                                            removed = removed,
                                        )
                                    },
                                    onAccepted = { accepted ->
                                        if (!RemoteCollectionBooks.applyMembershipMutation(
                                                this@HomeActivity,
                                                accepted,
                                                liveTargetCollectionId,
                                                removed,
                                                mutationOwner,
                                            )
                                        ) {
                                            cloudPending = true
                                        }
                                    },
                                )
                                if (isolated.failedIds.isNotEmpty()) cloudPending = true
                            }
                        }

                        Result.success(
                            InspectMutationOutcome(
                                appliedCount = selected.size,
                                localCleanupFailures = cleanupFailures,
                                cloudPending = cloudPending,
                            ),
                        )
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: Exception) {
                        Result.failure(e)
                    }
                }
            }
            inspectMutationInFlight = false
            inspectActionMode?.invalidate()
            val mutation = outcome.getOrNull()
            if (mutation == null) {
                Toast.makeText(
                    this@HomeActivity,
                    R.string.inspect_selection_failed,
                    Toast.LENGTH_LONG,
                ).show()
                return@launch
            }

            val warnings = buildList {
                if (mutation.localCleanupFailures > 0) add(resources.getQuantityString(
                    R.plurals.inspect_selection_cleanup_failed,
                    mutation.localCleanupFailures,
                    mutation.localCleanupFailures,
                ))
                if (mutation.cloudPending) add(
                    getString(R.string.inspect_selection_cloud_pending),
                )
            }
            val message = warnings.takeIf { it.isNotEmpty() }?.joinToString("\n\n")
                ?: resources.getQuantityString(
                    if (removed) R.plurals.inspect_selection_deleted
                    else R.plurals.inspect_selection_moved,
                    mutation.appliedCount,
                    mutation.appliedCount,
                )
            Toast.makeText(
                this@HomeActivity,
                message,
                if (warnings.isNotEmpty()) {
                    Toast.LENGTH_LONG
                } else {
                    Toast.LENGTH_SHORT
                },
            ).show()
            clearInspectSelection()
            invalidateRemoteInspectLookup()
            refreshInspect()
        }
    }

    override fun onDestroy() {
        stopHomeLoading()
        super.onDestroy()
    }

    private fun refreshHome() {
        resetThumbnailLoading()
        cancelScanListLoading()
        cancelCollectionBarLoading()
        scanListJob = lifecycleScope.launch {
            val snapshot = loadHomeSnapshot { loadContext ->
                val entries = Entries.recent(this@HomeActivity)
                loadContext.ensureActive()
                val records = Collections.allRecords(this@HomeActivity)
                loadContext.ensureActive()
                ScanListSnapshot(
                    items = entries.map { entry ->
                        loadContext.ensureActive()
                        val bibliography = Entries.captureListBibliography(entry)
                        ScanListItem(
                            entry = entry,
                            titleLabel = Entries.titleLabel(
                                this@HomeActivity,
                                entry,
                                bibliography,
                            ),
                            authorLabel = bibliography.author,
                            yearLabel = bibliography.year,
                            statusLabel = Entries.statusLabel(this@HomeActivity, entry),
                        )
                    },
                    collectionPaths = collectionDisplayPaths(records),
                    currentCollection = resolveCurrentCollection(
                        records,
                        Prefs.currentCollectionId(this@HomeActivity),
                        CollectionType.CAPTURE,
                    ),
                    currentScanCollections = resolveScanCollectionSlots(
                        records,
                        ScanCollectionSlot.entries.associateWith {
                            Prefs.currentScanCollectionId(this@HomeActivity, it)
                        },
                    ),
                )
            }
            scanListJob = null
            if (activeTab != HomeTab.SCANS ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            // Every authoritative disk/worker refresh returns to the newest
            // bounded page. Reset here, immediately before rendering, so an
            // older in-flight refresh cannot retain a page chosen meanwhile.
            resetScanPagination()
            renderHome(snapshot)
        }
    }

    private fun resetScanPagination() {
        scanPageGroupKey = null
        scanPageOffset = 0
    }

    private fun renderHome(
        snapshot: ScanListSnapshot,
        updateCollectionBar: Boolean = true,
        focusPageGroupKey: String? = null,
    ) {
        // Page navigation can race an authoritative IO refresh. Every render
        // owns exactly one decoder job and retires the previous page's views
        // and bitmaps before replacing the hierarchy.
        resetThumbnailLoading()
        val list = binding.homeList
        list.removeAllViews()
        val entries = snapshot.items.map(ScanListItem::entry)
        val itemById = snapshot.items.associateBy { it.entry.id }
        if (updateCollectionBar) {
            renderCollectionBar(
                snapshot.currentCollection,
                snapshot.currentScanCollections,
                snapshot.collectionPaths,
            )
        }
        if (entries.isEmpty()) {
            list.addView(emptyNotice(getString(R.string.home_empty)))
            return
        }
        val inflater = LayoutInflater.from(this)
        val thumbs = ArrayList<ThumbnailRequest>()
        var pageFocusTarget: View? = null
        var pageAnnouncement: String? = null
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
        val expandedGroupKey = retainedExpandedScanGroup(groups, expandedScanGroups)
        expandedScanGroups.clear()
        expandedGroupKey?.let(expandedScanGroups::add)
        val compact = Prefs.compactScanList(this)
        for (group in groups) {
            val expanded = group.key == expandedGroupKey
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
                resetScanPagination()
                expandedScanGroups.clear()
                if (!expanded) expandedScanGroups.add(group.key)
                refreshHome()
            }
            RemoteUiCatalog.apply(header)
            list.addView(header)
            if (!expanded) continue

            val page = scanGroupPage(
                group.items,
                if (scanPageGroupKey == group.key) scanPageOffset else 0,
                HOME_SCAN_PAGE_SIZE,
            )
            if (group.key == focusPageGroupKey) {
                pageAnnouncement = getString(
                    R.string.home_group_page_status,
                    group.label,
                    page.startIndex + 1,
                    page.startIndex + page.items.size,
                    group.items.size,
                )
            }
            for (e in page.items) {
                val item = checkNotNull(itemById[e.id])
                val row = inflater.inflate(R.layout.item_home, list, false)
                val libMarker = captureLibMarkerPresentation(
                    e.captureLibConfirmation,
                    getString(R.string.capture_lib_confirmed),
                )
                row.findViewById<ImageView>(R.id.captureLibConfirmed).apply {
                    visibility = if (libMarker.visible) View.VISIBLE else View.GONE
                    contentDescription = libMarker.accessibilityLabel
                }
                row.findViewById<TextView>(R.id.title).text = item.titleLabel
                row.findViewById<TextView>(R.id.sub).text =
                    listOf(
                        item.authorLabel,
                        item.yearLabel,
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
                val thumbFrame = row.findViewById<View>(R.id.thumbFrame)
                applyScanListLayout(row, thumbFrame, compact)
                thumbs += ThumbnailRequest(
                    image = thumb,
                    entry = e,
                    maxWidth = 512,
                    maxHeight = 512,
                )
                bindDesktopMetadata(row, e)
                val openBook = {
                    openEntryDetails(e.id)
                }
                val showActions = {
                    showEntryActionDialog(this, e.id) { refreshHome() }
                }
                row.setOnClickListener { openBook() }
                row.setOnLongClickListener {
                    showActions()
                    true
                }
                configureScanRowAccessibility(row, openBook, showActions)
                RemoteUiCatalog.apply(row)
                list.addView(row)
                if (group.key == focusPageGroupKey && pageFocusTarget == null) {
                    pageFocusTarget = row
                }
            }
            if (page.previousOffset != null || page.nextOffset != null) {
                val controls = LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = android.view.Gravity.CENTER
                }
                fun addPageButton(
                    textValue: String,
                    offset: Int,
                ) {
                    val targetEnd = minOf(group.items.size, offset + HOME_SCAN_PAGE_SIZE)
                    val button = MaterialButton(
                        this,
                        null,
                        com.google.android.material.R.attr.materialButtonOutlinedStyle,
                    ).apply {
                        text = textValue
                        contentDescription = getString(
                            R.string.home_group_page_description,
                            textValue,
                            group.label,
                            offset + 1,
                            targetEnd,
                            group.items.size,
                        )
                        isAllCaps = false
                        minHeight = dp(48)
                        setOnClickListener {
                            scanPageGroupKey = group.key
                            scanPageOffset = offset
                            renderHome(
                                snapshot,
                                updateCollectionBar = false,
                                focusPageGroupKey = group.key,
                            )
                        }
                    }
                    RemoteUiCatalog.apply(button)
                    controls.addView(
                        button,
                        LinearLayout.LayoutParams(
                            0,
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                            1f,
                        ).apply {
                            marginStart = dp(4)
                            marginEnd = dp(4)
                        },
                    )
                }
                page.previousOffset?.let { offset ->
                    val label = resources.getQuantityString(
                        R.plurals.home_group_show_newer,
                        page.previousCount,
                        page.previousCount,
                    )
                    addPageButton(label, offset)
                }
                page.nextOffset?.let { offset ->
                    val label = resources.getQuantityString(
                        R.plurals.home_group_show_older,
                        page.nextCount,
                        page.nextCount,
                    )
                    addPageButton(label, offset)
                }
                list.addView(
                    controls,
                    LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                    ).apply {
                        setMargins(dp(8), dp(8), dp(8), dp(12))
                    },
                )
            }
        }
        startThumbnailLoading(thumbs, HomeTab.SCANS)
        val focusTarget = pageFocusTarget
        val announcement = pageAnnouncement
        if (focusTarget != null && announcement != null) {
            binding.homeScroll.post {
                if (activeTab != HomeTab.SCANS || !focusTarget.isAttachedToWindow) {
                    return@post
                }
                binding.homeScroll.smoothScrollTo(
                    0,
                    (binding.homeList.top + focusTarget.top).coerceAtLeast(0),
                )
                focusTarget.requestFocus()
                focusTarget.performAccessibilityAction(
                    AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS,
                    null,
                )
                binding.homeList.announceForAccessibility(announcement)
            }
        }
    }

    /** Resolve descriptors and decode only rows that survived the visible page. */
    private fun startThumbnailLoading(
        requests: List<ThumbnailRequest>,
        requiredTab: HomeTab,
    ) {
        thumbJob = lifecycleScope.launch(Dispatchers.IO) {
            for (request in requests) {
                val loadContext = currentCoroutineContext()
                var decodedBitmap: Bitmap? = null
                var cleanupPending = false
                try {
                    THUMBNAIL_LOAD_MUTEX.withLock {
                        loadContext.ensureActive()
                        val descriptor = request.entry.thumbnailDescriptor()
                            ?: return@withLock
                        loadContext.ensureActive()
                        val decoded = decodeSampledOriented(
                            descriptor.displayFile,
                            maxWidth = request.maxWidth,
                            maxHeight = request.maxHeight,
                        ) ?: return@withLock
                        decodedBitmap = decoded
                        cleanupPending = descriptor.postProcessingPending
                        if (cleanupPending) {
                            decodedBitmap = softenPendingThumbnail(decoded)
                        }
                        loadContext.ensureActive()
                    }
                    val readyBitmap = decodedBitmap ?: continue
                    withContext(Dispatchers.Main) {
                        val target = request.image ?: request.swatch ?: return@withContext
                        if (activeTab != requiredTab ||
                            !target.isAttachedToWindow ||
                            !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
                        ) return@withContext
                        request.image?.let { image ->
                            image.alpha = if (cleanupPending) .82f else 1f
                            setDynamicThumbnail(image, readyBitmap)
                            decodedBitmap = null
                        } ?: request.swatch?.setBackgroundColor(averageCoverColor(readyBitmap))
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

    /** A small, evenly sampled mean is sufficient for the Content row's cover
     * hint. Transparent pixels and nearly white scanner margins are ignored so
     * the swatch follows the actual binding more often than the page edge. */
    private fun averageCoverColor(bitmap: Bitmap): Int {
        var red = 0L
        var green = 0L
        var blue = 0L
        var samples = 0L
        val stepX = (bitmap.width / 8).coerceAtLeast(1)
        val stepY = (bitmap.height / 10).coerceAtLeast(1)
        var y = stepY / 2
        while (y < bitmap.height) {
            var x = stepX / 2
            while (x < bitmap.width) {
                val pixel = bitmap.getPixel(x, y)
                val r = Color.red(pixel)
                val g = Color.green(pixel)
                val b = Color.blue(pixel)
                if (Color.alpha(pixel) >= 128 && !(r > 242 && g > 242 && b > 242)) {
                    red += r
                    green += g
                    blue += b
                    samples += 1
                }
                x += stepX
            }
            y += stepY
        }
        if (samples == 0L) return getColor(R.color.whl_face_sh2)
        return Color.rgb(
            (red / samples).toInt(),
            (green / samples).toInt(),
            (blue / samples).toInt(),
        )
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

    private fun cancelCollectionBarLoading() {
        collectionBarJob?.cancel()
        collectionBarJob = null
    }

    private fun cancelCollectionListLoading() {
        collectionListJob?.cancel()
        collectionListJob = null
    }

    private fun cancelInspectLoading() {
        inspectJob?.cancel()
        inspectJob = null
    }

    /**
     * List the selected box from the cloud once per owner/freshness generation.
     *
     * The local rows are already on screen by the time this runs, so this only
     * ever ADDS books this handset does not hold. A failure is silent by design:
     * an offline phone must keep showing the box it already knows, and a cached
     * listing from an earlier visit stays valid.
     *
     * Deliberately NOT cancelled when the user picks another chip. Each box writes
     * its own cache key, and cancelling would leave the abandoned box marked as
     * already-fetched with nothing to show. `lifecycleScope` still tears every
     * in-flight listing down with the activity.
     */
    private fun ensureRemoteBoxListing(collectionId: String) {
        if (collectionId.isEmpty()) return
        if (inspectMutationInFlight) return
        if (!Prefs.configured(this) || !Auth.signedIn(this)) return
        val owner = Prefs.userId(this)
        if (owner.isEmpty()) return
        val ticket = remoteBoxFetches.begin(owner, collectionId) ?: return
        lifecycleScope.launch {
            // `record` reports whether the cache now reflects the cloud, so an
            // unwritable or corrupt store re-arms the retry exactly like a network
            // failure does.
            var landed = false
            try {
                landed = withContext(Dispatchers.IO) {
                    try {
                        if (!remoteBoxFetches.isCurrent(ticket)) {
                            return@withContext false
                        }
                        val records = Collections.allRecords(this@HomeActivity)
                        // captures.meta keeps a merge loser's uuid forever, so ask
                        // for the whole closure or the old label's books vanish.
                        val closure = collectionMergeClosure(records, collectionId)
                        val client = SupabaseClient(this@HomeActivity, owner)
                        var books = client.capturesForCollections(closure)

                        // A local membership overlay is also the durable outbox
                        // for a move/delete made offline or while an upload was
                        // between remote insert and local commit. Reconcile any
                        // returned cloud row before acknowledging the snapshot.
                        val isolatedRetryFailures = linkedSetOf<String>()
                        val reconciledPending = INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock {
                            if (!remoteBoxFetches.isCurrent(ticket)) {
                                throw CancellationException("stale collection listing")
                            }
                            // Re-read only after winning the same process-wide
                            // mutation order as the UI. A newer move can therefore
                            // never be overwritten by an older listing's retry.
                            val pendingMemberships = InspectBookMemberships
                                .read(this@HomeActivity)
                                .takeIf { it.valid }
                                ?.memberships
                                .orEmpty()
                            val fetchedById = books.associateBy { it.captureId }
                            val cachedById = RemoteCollectionBooks
                                .read(this@HomeActivity, owner)
                                .byCollection.values
                                .flatten()
                                .groupBy { it.captureId }
                                .mapValues { (_, copies) ->
                                    copies.maxBy { it.membershipRevision }
                                }
                            val pendingGroups = linkedMapOf<
                                Pair<String, Boolean>,
                                MutableList<String>,
                            >()
                            pendingMemberships.forEach { (captureId, pending) ->
                                val authoritative = fetchedById[captureId]
                                val reference = authoritative ?: cachedById[captureId]
                                if (pending.cloudOwnerId.isNotEmpty() &&
                                    pending.cloudOwnerId != owner
                                ) {
                                    return@forEach
                                }
                                if (reference == null && pending.cloudOwnerId != owner) {
                                    return@forEach
                                }
                                val requestedId = pending.collectionId.ifEmpty {
                                    reference?.collectionId ?: return@forEach
                                }
                                val targetId = resolvedLiveCollectionId(requestedId, records)
                                if (targetId == null) {
                                    // A tombstone remains authoritative even if
                                    // its former box was deleted. Keep it until a
                                    // live destination can be resolved for the RPC.
                                    if (pending.removed) return@forEach
                                    check(InspectBookMemberships.clear(
                                        this@HomeActivity,
                                        setOf(captureId),
                                    ))
                                    return@forEach
                                }
                                if (authoritative != null &&
                                    targetId == authoritative.collectionId &&
                                    pending.removed == authoritative.removed
                                ) {
                                    return@forEach
                                }
                                if (targetId != pending.collectionId) {
                                    when (InspectBookMemberships.compareAndSet(
                                        this@HomeActivity,
                                        captureId,
                                        pending,
                                        pending.copy(collectionId = targetId),
                                    )) {
                                        InspectMembershipCompareResult.UPDATED -> Unit
                                        InspectMembershipCompareResult.CHANGED -> return@forEach
                                        InspectMembershipCompareResult.FAILED ->
                                            error("merged membership intent could not be saved")
                                    }
                                }
                                pendingGroups
                                    .getOrPut(targetId to pending.removed) { mutableListOf() }
                                    .add(captureId)
                            }
                            var acceptedPendingMutation = false
                            pendingGroups.forEach { (state, captureIds) ->
                                captureIds.chunked(CAPTURE_COLLECTION_MUTATION_MAX_IDS)
                                    .forEach { batch ->
                                        val isolated = isolateInspectMembershipMutation(
                                            captureIds = batch,
                                            shouldBisect = ::shouldBisectInspectMembershipFailure,
                                            mutate = { isolatedBatch ->
                                                if (!remoteBoxFetches.isCurrent(ticket)) {
                                                    throw CancellationException(
                                                        "stale membership retry",
                                                    )
                                                }
                                                client.mutateCaptureCollection(
                                                    captureIds = isolatedBatch,
                                                    collectionId = state.first,
                                                    removed = state.second,
                                                )
                                            },
                                            onAccepted = { accepted ->
                                                check(RemoteCollectionBooks.applyMembershipMutation(
                                                    this@HomeActivity,
                                                    accepted,
                                                    state.first,
                                                    state.second,
                                                    owner,
                                                )) { "accepted membership cache could not be saved" }
                                            },
                                        )
                                        if (isolated.acceptedIds.isNotEmpty()) {
                                            acceptedPendingMutation = true
                                        }
                                        isolatedRetryFailures += isolated.failedIds
                                    }
                            }
                            acceptedPendingMutation
                        }
                        if (reconciledPending) {
                            // Read once more from the committed server state so
                            // cache revisions and overlay acknowledgements are
                            // authoritative, not locally manufactured guesses.
                            if (!remoteBoxFetches.isCurrent(ticket)) {
                                return@withContext false
                            }
                            books = client.capturesForCollections(closure)
                        }
                        // Most captures carry no title of their own (extraction
                        // needs an API key), so fill the blanks from the desktop's
                        // curated projection. Failing that lookup must not lose the
                        // listing — an untitled row still tells you the box holds
                        // a book.
                        val enriched = try {
                            enrichRemoteCollectionBooks(
                                books,
                                client.desktopBookMetadata(books.map { it.captureId }),
                            )
                        } catch (e: CancellationException) {
                            throw e
                        } catch (_: Exception) {
                            books
                        }
                        // HttpURLConnection is blocking and may return after the
                        // Activity was rotated/stopped. Cancellation must win
                        // before the shared cache or membership overlay changes.
                        currentCoroutineContext().ensureActive()
                        val recordResult = RemoteCollectionBooks.recordAuthoritative(
                            this@HomeActivity,
                            collectionId,
                            enriched,
                            queriedCollectionIds = closure,
                            owner = owner,
                            discardCollectionIds = records.asSequence()
                                .filter { it.deleted || it.mergedInto != null }
                                .mapTo(mutableSetOf()) { it.id },
                            commitIf = {
                                remoteBoxFetches.isCurrent(ticket) &&
                                    Auth.signedIn(this@HomeActivity) &&
                                    Prefs.userId(this@HomeActivity) == owner
                            },
                        )
                        val overlayAcknowledged = if (recordResult == null) {
                            false
                        } else if ((recordResult.acknowledgedCaptureIds -
                                isolatedRetryFailures).isEmpty()
                        ) {
                            true
                        } else {
                            // This owner-scoped response is authoritative. Local
                            // overlays exist only to bridge the RPC/cache window;
                            // once a fresh listing includes a capture, its server
                            // membership wins (including changes from a different
                            // signed-in device).
                            InspectBookMemberships.clear(
                                this@HomeActivity,
                                recordResult.acknowledgedCaptureIds - isolatedRetryFailures,
                            )
                        }
                        recordResult != null && overlayAcknowledged
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                        false
                    }
                }
            } finally {
                // Re-arm the retry for anything that did not land, cancellation
                // and an invalidated generation included.
                remoteBoxFetches.finish(ticket, landed)
            }
            if (!landed) return@launch
            invalidateRemoteInspectLookup()
            if (activeTab != HomeTab.INSPECT ||
                !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            ) return@launch
            refreshInspect()
        }
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
        scanQueueSummaryJob?.cancel()
        scanQueueSummaryJob = null
        cancelScanListLoading()
        cancelCollectionBarLoading()
        cancelCollectionListLoading()
        cancelInspectLoading()
        resetThumbnailLoading()
    }

    private fun configureScanRowAccessibility(
        row: View,
        openBook: () -> Unit,
        showActions: () -> Unit,
    ) {
        val summaryIds = listOf(
            R.id.captureLibConfirmed,
            R.id.title,
            R.id.sub,
            R.id.state,
            R.id.waitingIndicator,
            R.id.stateIcon,
            // chAvailability is deliberately absent: it is an interactive
            // control, and everything in this list is made
            // IMPORTANT_FOR_ACCESSIBILITY_NO so it folds into the row summary.
            // Folding it in would hide approve/reject from TalkBack entirely.
            // The interactive copyrightStatus button is excluded for the same
            // reason.
            R.id.whlAvailability,
            R.id.internetArchiveAvailability,
            R.id.scanPriorityIndicator,
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
            getString(R.string.home_book_actions),
        ) { _, _ ->
            showActions()
            true
        }
    }

    /**
     * The compact CH / WHL / IA row.
     *
     * One glyph per source, tinted by tone, so the three slots stay the same
     * size and position whatever they are saying — a row that reflows as
     * answers arrive makes the whole list jump under your thumb.
     *
     * CH is the only interactive slot, and only while a match is unreviewed:
     * tap approves, long press rejects. Both return true so the gesture does
     * not fall through to the row's own long-press, which opens its action
     * chooser.
     */
    private fun bindCatalogIndicators(row: View, entry: Entries.Entry) {
        val state = ChMatchStore.read(entry.dir)
        val presentation = CatalogIndicatorPresenter.from(entry, state)

        fun paint(view: ImageView, indicator: CatalogIndicator?) {
            val tone = indicator?.tone ?: CatalogTone.HIDDEN
            // PENDING is transient — the bundled index answers in milliseconds —
            // so it shows nothing rather than flashing a placeholder per row.
            val shown = tone == CatalogTone.CONFIRMED ||
                tone == CatalogTone.PROPOSED ||
                tone == CatalogTone.ABSENT
            view.visibility = if (shown) View.VISIBLE else View.GONE
            if (!shown) return
            view.setColorFilter(
                getColor(
                    when (tone) {
                        CatalogTone.PROPOSED -> R.color.whl_amber
                        CatalogTone.ABSENT -> R.color.whl_blue2
                        else -> R.color.whl_green
                    },
                ),
            )
            // A settled negative is real information but not news; muting it
            // keeps a list of mostly-absent books readable.
            view.alpha = if (tone == CatalogTone.ABSENT) 0.45f else 1f
            view.contentDescription = indicator?.description
        }

        paint(row.findViewById(R.id.whlAvailability), presentation.of(CatalogSource.WHL))
        paint(row.findViewById(R.id.internetArchiveAvailability), presentation.of(CatalogSource.IA))

        val chView = row.findViewById<androidx.appcompat.widget.AppCompatImageButton>(
            R.id.chAvailability,
        )
        val ch = presentation.of(CatalogSource.CH)
        paint(chView, ch)

        when {
            ch?.actionable == true -> {
                chView.isClickable = true
                chView.isLongClickable = true
                chView.setOnClickListener { decideChMatch(row, entry, ChDecision.APPROVED, state) }
                chView.setOnLongClickListener {
                    decideChMatch(row, entry, ChDecision.REJECTED, state)
                    true
                }
            }
            // An approved match stays inspectable: what a merge did to your
            // record should not be a one-time notification you can miss.
            ch?.tone == CatalogTone.CONFIRMED -> {
                chView.isClickable = true
                chView.isLongClickable = false
                chView.setOnClickListener {
                    ChMergeDialog.show(this, chMergePreview(entry, state))
                }
                chView.setOnLongClickListener(null)
            }
            else -> {
                chView.setOnClickListener(null)
                chView.setOnLongClickListener(null)
                chView.isClickable = false
                chView.isLongClickable = false
            }
        }
    }

    private fun chMergePreview(entry: Entries.Entry, state: ChMatchState): ChMergePreview {
        val meta = runCatching { entry.bookJsonText()?.let(::JSONObject) }.getOrNull()
        return ChMergePresenter.preview(ChMergePresenter.scanFields(meta), state.candidate)
    }

    /**
     * Record an approve/reject and offer to undo it.
     *
     * The decision is written before the snackbar is shown, so closing the app
     * mid-undo keeps the decision rather than losing it; Undo restores the
     * exact prior state rather than reconstructing "unreviewed", which would
     * discard a previous decision if one was being changed.
     */
    private fun decideChMatch(
        row: View,
        entry: Entries.Entry,
        decision: ChDecision,
        previous: ChMatchState,
    ) {
        // Snapshot the exact persisted bytes before touching them. Undo restores
        // this verbatim rather than re-deriving the record, so unknown keys the
        // extraction contract may have grown survive an undo untouched.
        val metaFile = File(entry.dir, "meta.json")
        val priorMeta: String? = runCatching {
            metaFile.takeIf { it.isFile }?.readText()
        }.getOrNull()

        var preview: ChMergePreview? = null
        if (decision == ChDecision.APPROVED) {
            val meta = runCatching { priorMeta?.let(::JSONObject) }.getOrNull()
            preview = ChMergePresenter.preview(ChMergePresenter.scanFields(meta), previous.candidate)
            runCatching {
                Entries.atomicWrite(metaFile, ChMergePresenter.apply(meta, preview).toString())
            }.onFailure {
                Toast.makeText(this, R.string.ch_merge_failed, Toast.LENGTH_LONG).show()
                return
            }
        }

        ChMatchStore.write(entry.dir, previous.copy(decision = decision))

        fun undo() {
            ChMatchStore.write(entry.dir, previous)
            if (decision == ChDecision.APPROVED) {
                runCatching {
                    if (priorMeta == null) metaFile.delete()
                    else Entries.atomicWrite(metaFile, priorMeta)
                }
            }
            refreshHome()
        }

        if (decision == ChDecision.APPROVED && preview != null) {
            ChMergeDialog.show(this, preview, ::undo)
        } else {
            com.google.android.material.snackbar.Snackbar
                .make(
                    row,
                    getString(R.string.catalog_ch_rejected_toast),
                    com.google.android.material.snackbar.Snackbar.LENGTH_LONG,
                )
                .setAction(R.string.catalog_ch_undo) { undo() }
                .show()
        }
        bindCatalogIndicators(row, entry)
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
            val showActions = {
                showEntryActionDialog(this, entry.id) { refreshHome() }
            }
            copyrightView.setOnLongClickListener {
                showActions()
                true
            }
            ViewCompat.replaceAccessibilityAction(
                copyrightView,
                AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_LONG_CLICK,
                getString(R.string.home_book_actions),
            ) { _, _ ->
                showActions()
                true
            }
        } else {
            copyrightView.setOnClickListener(null)
            copyrightView.setOnLongClickListener(null)
            ViewCompat.removeAccessibilityAction(
                copyrightView,
                AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_LONG_CLICK.id,
            )
        }

        bindCatalogIndicators(row, entry)
        bindScanPriorityIndicator(
            row,
            entryScanCandidate(this, entry),
            desktop?.scanPriority,
        )
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

    private fun applyScanListLayout(row: View, thumbFrame: View, compact: Boolean) {
        val metrics = scanListLayoutMetrics(compact)
        row.setPaddingRelative(
            row.paddingStart,
            dp(metrics.rowVerticalPaddingDp),
            row.paddingEnd,
            dp(metrics.rowVerticalPaddingDp),
        )
        val params = thumbFrame.layoutParams as ViewGroup.MarginLayoutParams
        params.width = dp(metrics.thumbnailWidthDp)
        params.height = dp(metrics.thumbnailHeightDp)
        params.marginEnd = dp(metrics.thumbnailEndMarginDp)
        thumbFrame.layoutParams = params
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
        const val STATE_INSPECT_SELECTED_IDS = "inspect_selected_ids"
        const val STATE_INSPECT_LOOKUP_QUERY = "inspect_lookup_query"
        const val STATE_INSPECT_COVER_OCR = "inspect_cover_ocr"
        const val STATE_ACTIVE_SCAN_SEARCH_QUEUE = "active_scan_search_queue"
        const val STATE_SCAN_GROUPS_INITIALIZED = "scan_groups_initialized"
        const val STATE_EXPANDED_SCAN_GROUPS = "expanded_scan_groups"
        const val STATE_SYNC_FEEDBACK_REQUEST = "sync_feedback_request"
        const val STATE_SYNC_FEEDBACK_PHASE = "sync_feedback_phase"
        const val WORK_REFRESH_COALESCE_MS = 200L
        const val INSPECT_BOOK_PAGE_SIZE = 48
        const val INSPECT_LOOKUP_LIMIT = 20
        const val INSPECT_LOOKUP_COLLECTION_BATCH = 40
        const val INSPECT_LOOKUP_DISPLAY_MAX = 240
        const val INSPECT_COVER_TEXT_MAX = 8_000
        const val SCAN_QUEUE_INSPECTOR_OCR_EXCERPT_MAX = 240
        const val MENU_INSPECT_MARK_SCAN = 10_200
        const val MENU_INSPECT_MOVE = 10_201
        const val MENU_INSPECT_DELETE = 10_202
        val REMOTE_BOX_FETCHES = RemoteCollectionFetchTracker()
        val SNAPSHOT_LOAD_MUTEX = Mutex()
        val THUMBNAIL_LOAD_MUTEX = Mutex()
    }
}
