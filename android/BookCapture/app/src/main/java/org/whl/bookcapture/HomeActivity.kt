package org.whl.bookcapture

import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.lifecycle.lifecycleScope
import androidx.work.WorkManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityHomeBinding
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

    private lateinit var binding: ActivityHomeBinding
    private var thumbJob: Job? = null
    private var selectionMode = false
    private val selectedIds = linkedSetOf<String>()
    private var showingCollections = false
    private val expandedScanGroups = linkedSetOf<String>()
    private var scanGroupsInitialized = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityHomeBinding.inflate(layoutInflater)
        setContentView(binding.root)
        showingCollections = savedInstanceState?.getBoolean(STATE_TAB_COLLECTIONS) ?: false
        selectionMode = savedInstanceState?.getBoolean(STATE_SELECTION_MODE) ?: false
        savedInstanceState?.getStringArrayList(STATE_SELECTED_IDS)?.let(selectedIds::addAll)
        scanGroupsInitialized =
            savedInstanceState?.getBoolean(STATE_SCAN_GROUPS_INITIALIZED) ?: false
        savedInstanceState?.getStringArrayList(STATE_EXPANDED_SCAN_GROUPS)
            ?.let(expandedScanGroups::addAll)

        binding.tabScans.setOnClickListener { showTab(collections = false) }
        binding.tabCollections.setOnClickListener { showTab(collections = true) }
        binding.collectionBar.setOnClickListener { showTab(collections = true) }
        binding.newCollection.setOnClickListener { editCollection(null) }
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
                showTab(collections = true)
                return@setOnClickListener
            }
            startActivity(Intent(this, MainActivity::class.java))
        }
        binding.appMenu.setOnClickListener { showAppMenu() }
        binding.configWarning.setOnClickListener {
            startActivity(Intent(this, LoginActivity::class.java))
        }
        binding.btnSelect.setOnClickListener {
            selectionMode = true
            updateSelectionUi()
            refreshHome()
        }
        binding.cancelSelection.setOnClickListener { leaveSelectionMode() }
        binding.deleteSelected.setOnClickListener { confirmDeleteSelected() }
        // when background OCR / upload lands, the list re-renders itself
        for (name in listOf(
            ProcessWorker.UNIQUE_WORK_NAME,
            ProcessWorker.BACKLOG_WORK_NAME,
            "capture-upload",
        ))
            WorkManager.getInstance(this)
                .getWorkInfosForUniqueWorkLiveData(name)
                .observe(this) { refreshHome() }
        WorkManager.getInstance(this)
            .getWorkInfosForUniqueWorkLiveData(CollectionSyncWorker.WORK_NAME)
            .observe(this) {
                if (showingCollections) refreshCollections() else refreshCollectionBar()
            }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean(STATE_TAB_COLLECTIONS, showingCollections)
        outState.putBoolean(STATE_SELECTION_MODE, selectionMode)
        outState.putStringArrayList(STATE_SELECTED_IDS, ArrayList(selectedIds))
        outState.putBoolean(STATE_SCAN_GROUPS_INITIALIZED, scanGroupsInitialized)
        outState.putStringArrayList(
            STATE_EXPANDED_SCAN_GROUPS,
            ArrayList(expandedScanGroups),
        )
    }

    override fun onResume() {
        super.onResume()
        val signedIn = Auth.signedIn(this)
        binding.configWarning.visibility = if (signedIn) View.GONE else View.VISIBLE
        // returning to Home is a good moment to drain the queue and process
        // anything a previous run left un-OCR'd
        if (CaptureSession(this).pendingUploads().isNotEmpty() &&
            (signedIn || Prefs.transport(this) != "cloud")) {
            UploadWorker.kick(this)
        }
        ProcessWorker.enqueue(this)
        CollectionSyncWorker.enqueueCoalesced(this)
        showTab(showingCollections)
    }

    // --- the app menu, hung off the mark in the toolbar ----------------------

    private fun showAppMenu() {
        val menu = androidx.appcompat.widget.PopupMenu(this, binding.appMenu)
        menu.menuInflater.inflate(R.menu.home_app_menu, menu.menu)
        menu.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.menuSettings -> {
                    startActivity(Intent(this, SettingsActivity::class.java)); true
                }
                R.id.menuAbout -> { showAbout(); true }
                R.id.menuCheckUpdates -> { checkForUpdates(); true }
                else -> false
            }
        }
        menu.show()
    }

    private fun showAbout() {
        AlertDialog.Builder(this)
            .setTitle(R.string.about_title)
            .setMessage(getString(R.string.about_body, BuildConfig.VERSION_NAME))
            .setPositiveButton(R.string.about_close, null)
            .show()
    }

    /**
     * Reads the published releases and offers the newest build this one should
     * see. Runs off the main thread; the result is discarded if the Activity has
     * gone away, so a slow network can't resurrect a dialog on a dead window.
     */
    private fun checkForUpdates() {
        Toast.makeText(this, R.string.update_checking, Toast.LENGTH_SHORT).show()
        lifecycleScope.launch {
            val outcome = withContext(Dispatchers.IO) {
                runCatching { Updates.check(this@HomeActivity) }
            }
            fun say(message: String) =
                Toast.makeText(this@HomeActivity, message, Toast.LENGTH_LONG).show()
            when (val result = outcome.getOrNull()) {
                null -> say(getString(R.string.update_failed))
                Updates.Result.NotConfigured -> say(getString(R.string.update_not_configured))
                Updates.Result.UpToDate ->
                    say(getString(R.string.update_current, BuildConfig.VERSION_NAME))
                is Updates.Result.Available -> AlertDialog.Builder(this@HomeActivity)
                    .setTitle(R.string.update_available_title)
                    .setMessage(getString(
                        R.string.update_available_body,
                        result.release.version, BuildConfig.VERSION_NAME))
                    .setNegativeButton(android.R.string.cancel, null)
                    .setPositiveButton(R.string.update_download) { _, _ ->
                        openDownload(result.release)
                    }
                    .show()
            }
        }
    }

    /**
     * Hand the APK URL to the browser rather than installing in-app.
     *
     * Not a stopgap — don't "upgrade" this later. These builds are not Play
     * Store certified, so a self-install would land the user in the unknown-
     * sources flow regardless, and the app would additionally have to hold
     * REQUEST_INSTALL_PACKAGES for the privilege. The browser already handles
     * that path, and handles it with the warnings the user should be seeing.
     */
    private fun openDownload(update: Release) {
        val intent = Intent(Intent.ACTION_VIEW, android.net.Uri.parse(update.url))
        try {
            startActivity(intent)
        } catch (_: android.content.ActivityNotFoundException) {
            Toast.makeText(this, R.string.update_no_browser, Toast.LENGTH_LONG).show()
        }
    }

    // --- tabs ----------------------------------------------------------------

    private fun showTab(collections: Boolean) {
        showingCollections = collections
        // Selecting scans to delete is a Scans-tab activity; leaving it behind
        // on the Collections tab would strand the selection bar over a list it
        // cannot act on.
        if (collections && selectionMode) leaveSelectionMode()
        binding.homeList.visibility = if (collections) View.GONE else View.VISIBLE
        binding.collectionsList.visibility = if (collections) View.VISIBLE else View.GONE
        binding.newScan.visibility = if (collections) View.GONE else View.VISIBLE
        binding.newCollection.visibility = if (collections) View.VISIBLE else View.GONE
        binding.collectionBar.visibility = if (collections) View.GONE else View.VISIBLE
        binding.btnSelect.visibility =
            if (collections || selectionMode) View.GONE else View.VISIBLE
        emphasizeTab(binding.tabScans, !collections)
        emphasizeTab(binding.tabCollections, collections)
        if (collections) refreshCollections() else refreshHome()
    }

    private fun emphasizeTab(button: com.google.android.material.button.MaterialButton, on: Boolean) {
        button.alpha = if (on) 1f else .5f
        button.setTypeface(null, if (on) Typeface.BOLD else Typeface.NORMAL)
    }

    // --- collections ---------------------------------------------------------

    private fun refreshCollectionBar() {
        val current = Collections.current(this)
        binding.collectionBar.text = when {
            current == null -> getString(R.string.collections_none_selected)
            current.from.isEmpty() -> getString(R.string.collections_current, current.name)
            else -> getString(R.string.collections_current_from, current.name, current.from)
        }
        binding.collectionBar.setTextColor(
            getColor(if (current == null) R.color.whl_amber else R.color.whl_ink_dim))
    }

    private fun refreshCollections() {
        val list = binding.collectionsList
        list.removeAllViews()
        val collections = Collections.all(this)
        val current = Collections.current(this)
        refreshCollectionBar()
        if (collections.isEmpty()) {
            list.addView(emptyNotice(getString(R.string.collections_empty)))
            return
        }
        val counts = Entries.recent(this)
            .mapNotNull { it.provenance?.collectionId }
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
            name.text = c.name
            name.setTypeface(name.typeface, if (isCurrent) Typeface.BOLD else Typeface.NORMAL)
            row.setBackgroundResource(
                if (isCurrent) R.drawable.whl_collection_current else R.drawable.whl_row)
            row.findViewById<TextView>(R.id.sub).text = listOf(
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
                    this, getString(R.string.collections_current, c.name), Toast.LENGTH_SHORT
                ).show()
                refreshCollections()
            }
            list.addView(row)
        }
    }

    private fun emptyNotice(text: String): TextView = TextView(this).apply {
        typeface = Typeface.MONOSPACE
        textSize = 13f
        setTextColor(getColor(R.color.whl_ink_dim))
        setPadding(28, 40, 28, 28)
        this.text = text
    }

    /** Add ([existing] null) or edit one collection. */
    private fun editCollection(existing: BookCollection?) {
        // Keep the identity chosen by this edit. Sync can append cloud rows as
        // soon as mutate() returns, so selecting the list's last row would race
        // that pull and could choose a different collection.
        val collectionId = existing?.id ?: UUID.randomUUID().toString()
        val view = layoutInflater.inflate(R.layout.dialog_collection, null)
        val nameField = view.findViewById<android.widget.EditText>(R.id.collectionName)
        val fromField = view.findViewById<android.widget.EditText>(R.id.collectionFrom)
        // Existing values are editable content. The layout's examples remain
        // generic hints and never duplicate a saved collection as placeholder text.
        nameField.setText(existing?.name.orEmpty())
        fromField.setText(existing?.from.orEmpty())
        if (existing != null) {
            // Edit mode already contains the saved values; showing the new-item
            // examples again after a field is cleared reads like prefilled data.
            nameField.hint = null
            fromField.hint = null
        }
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
            val from = fromField.text.toString()
            val error = Collections.mutate(this) { current ->
                if (existing == null) addCollection(current, name, from, id = collectionId)
                else updateCollection(current, existing.id, name, from)
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
    }

    private fun confirmDeleteCollection(collection: BookCollection) {
        AlertDialog.Builder(this)
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
    }

    override fun onDestroy() {
        super.onDestroy()
        thumbJob?.cancel()
    }

    private fun refreshHome() {
        val list = binding.homeList
        list.removeAllViews()
        thumbJob?.cancel()
        val entries = Entries.recent(this)
        selectedIds.retainAll(entries.map { it.id }.toSet())
        updateSelectionUi()
        refreshCollectionBar()
        if (entries.isEmpty()) {
            list.addView(emptyNotice(getString(R.string.home_empty)))
            return
        }
        val inflater = LayoutInflater.from(this)
        val thumbs = ArrayList<Pair<ImageView, java.io.File>>()
        val knownCollectionNames = Collections.all(this).associate { it.id to it.name }
        val currentCollectionId = Collections.current(this)?.id
        val groups = groupScansByCollection(
            items = entries,
            currentCollectionId = currentCollectionId,
            collectionId = { it.provenance?.collectionId },
            collectionLabel = { entry ->
                entry.provenance?.collectionId?.let(knownCollectionNames::get)
                    .orEmpty().ifEmpty { entry.collectionName }
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
            list.addView(header)
            if (!expanded) continue

            for (e in group.items) {
                val row = inflater.inflate(R.layout.item_home, list, false)
                row.findViewById<TextView>(R.id.title).text = Entries.titleLabel(this, e)
                row.findViewById<TextView>(R.id.sub).text =
                    listOf(
                        e.author,
                        e.year,
                        resources.getQuantityString(
                            R.plurals.capture_count, e.photoCount, e.photoCount),
                        if (e.from.isEmpty()) ""
                        else getString(R.string.collections_row_from, e.from),
                    ).filter { it.isNotEmpty() }.joinToString(" · ")
                val state = Entries.statusLabel(this, e)
                row.findViewById<TextView>(R.id.state).text = state
                row.findViewById<View>(R.id.marker)
                    .setBackgroundColor(getColor(markerColor(state)))
                val thumb = row.findViewById<ImageView>(R.id.thumb)
                applyScanListLayout(row, thumb, compact)
                val selected = row.findViewById<CheckBox>(R.id.selected)
                selected.visibility = if (selectionMode) View.VISIBLE else View.GONE
                selected.isChecked = e.id in selectedIds
                selected.setOnClickListener { toggleSelection(e.id) }
                e.thumbnailPhoto()?.let { thumbs.add(thumb to it) }
                row.setOnClickListener {
                    if (selectionMode) toggleSelection(e.id)
                    else startActivity(Intent(this, EntryDetailActivity::class.java)
                        .putExtra(EntryDetailActivity.EXTRA_ID, e.id))
                }
                row.setOnLongClickListener {
                    if (!selectionMode) selectionMode = true
                    toggleSelection(e.id)
                    true
                }
                list.addView(row)
            }
        }
        // decode the page thumbnails off the UI thread, in list order
        thumbJob = lifecycleScope.launch {
            for ((iv, file) in thumbs) {
                val bmp = withContext(Dispatchers.IO) {
                    decodeSampledOriented(file, maxWidth = 512, maxHeight = 512)
                } ?: continue
                iv.setImageBitmap(bmp)
            }
        }
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

    private fun toggleSelection(id: String) {
        if (id in selectedIds) selectedIds.remove(id) else selectedIds.add(id)
        updateSelectionUi()
        refreshHome()
    }

    private fun updateSelectionUi() {
        binding.selectionBar.visibility = if (selectionMode) View.VISIBLE else View.GONE
        binding.btnSelect.visibility =
            if (selectionMode || showingCollections) View.GONE else View.VISIBLE
        binding.newScan.isEnabled = !selectionMode
        binding.selectionCount.text = resources.getQuantityString(
            R.plurals.home_selected_count, selectedIds.size, selectedIds.size)
        binding.deleteSelected.isEnabled = selectedIds.isNotEmpty()
        binding.deleteSelected.alpha = if (selectedIds.isNotEmpty()) 1f else .45f
    }

    private fun leaveSelectionMode() {
        selectionMode = false
        selectedIds.clear()
        updateSelectionUi()
        refreshHome()
    }

    private fun confirmDeleteSelected() {
        if (selectedIds.isEmpty()) return
        val count = selectedIds.size
        AlertDialog.Builder(this)
            .setTitle(R.string.home_delete_title)
            .setMessage(resources.getQuantityString(
                R.plurals.home_delete_message, count, count))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.home_delete_selected) { _, _ ->
                val ids = selectedIds.toList()
                lifecycleScope.launch {
                    val results = withContext(Dispatchers.IO) {
                        ids.map {
                            Entries.deleteLocalSafely(
                                this@HomeActivity,
                                it,
                                allowUploaded = true,
                            )
                        }
                    }
                    if (Entries.DeleteResult.ACTIVE_CAPTURE in results) {
                        Toast.makeText(
                            this@HomeActivity,
                            R.string.home_delete_active_skipped,
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                    if (Entries.DeleteResult.DELETE_FAILED in results) {
                        Toast.makeText(
                            this@HomeActivity,
                            R.string.home_delete_failed,
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                    selectionMode = false
                    selectedIds.clear()
                    refreshHome()
                }
            }
            .show()
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
        const val STATE_TAB_COLLECTIONS = "tab_collections"
        const val STATE_SELECTION_MODE = "selection_mode"
        const val STATE_SELECTED_IDS = "selected_ids"
        const val STATE_SCAN_GROUPS_INITIALIZED = "scan_groups_initialized"
        const val STATE_EXPANDED_SCAN_GROUPS = "expanded_scan_groups"
    }
}
