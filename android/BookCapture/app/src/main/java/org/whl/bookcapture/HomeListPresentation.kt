package org.whl.bookcapture

/** A collapsible Home-list section. [items] retain their newest-first order. */
internal data class ScanCollectionGroup<T>(
    val key: String,
    val label: String,
    val items: List<T>,
)

internal const val UNFILED_SCAN_GROUP = "__unfiled__"

/**
 * Group scans by their frozen collection provenance. The current collection is
 * placed first without disturbing the newest-first ordering of any other group.
 */
internal fun <T> groupScansByCollection(
    items: List<T>,
    currentCollectionId: String?,
    collectionId: (T) -> String?,
    collectionLabel: (T) -> String,
    unfiledLabel: String,
): List<ScanCollectionGroup<T>> {
    val grouped = linkedMapOf<String, MutableList<T>>()
    val labels = linkedMapOf<String, String>()
    for (item in items) {
        val id = collectionId(item).orEmpty().trim()
        val key = id.ifEmpty { UNFILED_SCAN_GROUP }
        grouped.getOrPut(key) { mutableListOf() }.add(item)
        val label = collectionLabel(item).trim()
        if (labels[key].isNullOrEmpty() && label.isNotEmpty()) labels[key] = label
    }
    val groups = grouped.map { (key, groupedItems) ->
        ScanCollectionGroup(
            key = key,
            label = labels[key].orEmpty().ifEmpty { unfiledLabel },
            items = groupedItems,
        )
    }
    val current = currentCollectionId.orEmpty().trim()
    return if (current.isEmpty()) groups
    else groups.sortedByDescending { it.key == current }
}

/** Expand the current collection on first render, or the newest group if the
 * current collection has no scans yet. */
internal fun <T> initiallyExpandedScanGroup(
    groups: List<ScanCollectionGroup<T>>,
    currentCollectionId: String?,
): String? {
    val current = currentCollectionId.orEmpty().trim()
    return groups.firstOrNull { current.isNotEmpty() && it.key == current }?.key
        ?: groups.firstOrNull()?.key
}

internal data class ScanListLayoutMetrics(
    val thumbnailWidthDp: Int,
    val thumbnailHeightDp: Int,
    val rowVerticalPaddingDp: Int,
    val thumbnailEndMarginDp: Int,
)

private val STANDARD_SCAN_LIST_METRICS = ScanListLayoutMetrics(46, 60, 9, 11)

// Roughly 15% smaller thumbnails and 20% tighter row spacing. Integer dp sizes
// deliberately round to stable values across densities.
private val COMPACT_SCAN_LIST_METRICS = ScanListLayoutMetrics(39, 51, 7, 9)

internal fun scanListLayoutMetrics(compact: Boolean): ScanListLayoutMetrics =
    if (compact) COMPACT_SCAN_LIST_METRICS else STANDARD_SCAN_LIST_METRICS
