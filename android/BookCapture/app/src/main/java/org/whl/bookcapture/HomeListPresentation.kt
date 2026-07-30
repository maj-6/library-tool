package org.whl.bookcapture

/** A collapsible Home-list section. [items] retain their newest-first order. */
internal data class ScanCollectionGroup<T>(
    val key: String,
    val label: String,
    val items: List<T>,
)

internal const val UNFILED_SCAN_GROUP = "__unfiled__"
internal const val HOME_SCAN_PAGE_SIZE = 24

private const val COLLECTION_PATH_SEPARATOR = " > "

/**
 * Render legacy frozen provenance that has no durable parent id. The physical
 * source is only a root prefix; it is never resolved as collection identity.
 */
internal fun collectionDisplayLabel(parentLocation: String, collectionName: String): String {
    val name = collectionName.trim()
    if (name.isEmpty()) return ""
    val parent = parentLocation.trim()
    return if (parent.isEmpty() || parent.equals(name, ignoreCase = true)) name
    else "$parent$COLLECTION_PATH_SEPARATOR$name"
}

/**
 * Build root-to-leaf labels from durable [BookCollection.parentId] edges. A
 * root collection may prefix its physical [BookCollection.from] location, but
 * a nested collection's physical provenance is not part of its identity path.
 *
 * Missing or ordinarily deleted parents stop the path. A merged parent follows
 * its durable survivor marker. Self-references and cycles stop at the first
 * repeated id, so malformed synced data can never hang the Home screen.
 */
internal fun collectionDisplayPaths(
    collections: List<BookCollection>,
): Map<String, String> {
    val byId = collections.associateBy { it.id }
    val displayable = collections.filter { it.mergedInto == null }
    return displayable.associate { collection ->
        val ancestors = mutableListOf(collection.name.trim())
        val visited = mutableSetOf(collection.id)
        var cursor = collection
        var reachedRoot = false
        while (true) {
            var nextId = cursor.parentId?.trim()?.ifEmpty { null }
            if (nextId == null) {
                reachedRoot = true
                break
            }
            var parent: BookCollection? = null
            while (nextId != null) {
                if (!visited.add(nextId)) break
                val candidate = byId[nextId] ?: break
                if (!candidate.deleted && candidate.mergedInto == null) {
                    parent = candidate
                    break
                }
                nextId = candidate.mergedInto?.trim()?.ifEmpty { null }
            }
            if (parent == null) break
            ancestors += parent.name.trim()
            cursor = parent
        }
        if (reachedRoot) {
            val rootLocation = cursor.from.trim()
            if (rootLocation.isNotEmpty() &&
                !rootLocation.equals(ancestors.last(), ignoreCase = true)
            ) {
                ancestors += rootLocation
            }
        }
        collection.id to ancestors.asReversed().joinToString(COLLECTION_PATH_SEPARATOR)
    }
}

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

/** Keep Home accordion-style so scan-row inflation has one bounded owner. */
internal fun <T> retainedExpandedScanGroup(
    groups: List<ScanCollectionGroup<T>>,
    expandedKeys: Set<String>,
): String? = groups.firstOrNull { it.key in expandedKeys }?.key

/**
 * A fixed scan-list window. Unlike an additive "show more" limit, paging can
 * never rebuild an arbitrarily large view hierarchy after enough taps.
 */
internal data class ScanGroupPage<T>(
    val items: List<T>,
    val startIndex: Int,
    val previousOffset: Int?,
    val previousCount: Int,
    val nextOffset: Int?,
    val nextCount: Int,
)

internal fun <T> scanGroupPage(
    items: List<T>,
    requestedOffset: Int,
    pageSize: Int,
): ScanGroupPage<T> {
    require(pageSize > 0)
    val lastPageStart = if (items.isEmpty()) 0 else (items.lastIndex / pageSize) * pageSize
    val start = requestedOffset.coerceAtLeast(0).coerceAtMost(lastPageStart)
    val end = minOf(items.size, start + pageSize)
    val previousOffset = (start - pageSize).coerceAtLeast(0).takeIf { start > 0 }
    val nextOffset = end.takeIf { end < items.size }
    return ScanGroupPage(
        items = items.subList(start, end),
        startIndex = start,
        previousOffset = previousOffset,
        previousCount = minOf(pageSize, start),
        nextOffset = nextOffset,
        nextCount = minOf(pageSize, items.size - end),
    )
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

internal enum class HomeStatusAdornment { NONE, WAITING, UPLOADED }

/**
 * The colored marker carries the successful/complete state. Keep row chrome
 * quiet by reserving text for actionable exceptions, a spinner for work that
 * has not settled, and a cloud-check glyph for delivered books.
 */
internal data class HomeStatusPresentation(
    val text: String = "",
    val adornment: HomeStatusAdornment = HomeStatusAdornment.NONE,
    val accessibilityLabel: String = "",
)

internal data class CaptureLibMarkerPresentation(
    val visible: Boolean,
    val accessibilityLabel: String = "",
)

/** The archive check is independent of upload/cloud status. Only the frozen
 * #239 `current` state confirms a usable sealed snapshot; stale, null, and
 * corrupt sidecars intentionally render nothing. */
internal fun captureLibMarkerPresentation(
    confirmation: CaptureLibConfirmation?,
    accessibilityLabel: String,
): CaptureLibMarkerPresentation =
    if (confirmation?.confirmed == true) {
        CaptureLibMarkerPresentation(true, accessibilityLabel)
    } else {
        CaptureLibMarkerPresentation(false)
    }

internal fun homeStatusPresentation(rawStatus: String): HomeStatusPresentation {
    val status = rawStatus.trim().lowercase()
    val withoutComplete = status.removePrefix("complete · ").trim()
    // A delivered capture may carry a pipeline qualifier ("uploaded · no
    // details read"). The delivery mark is driven by the head so the row still
    // reads as delivered, while the qualifier becomes the visible text.
    val head = withoutComplete.substringBefore(" · ").trim()
    val tail = withoutComplete.substringAfter(" · ", "").trim()
    return when {
        head == "uploaded" || head == "imported" ->
            HomeStatusPresentation(
                text = tail,
                adornment = HomeStatusAdornment.UPLOADED,
                accessibilityLabel = withoutComplete,
            )
        withoutComplete == "waiting" || withoutComplete == "processing" ||
            withoutComplete.startsWith("pending ") ||
            withoutComplete == "claim for cloud" ->
            HomeStatusPresentation(
                adornment = HomeStatusAdornment.WAITING,
                accessibilityLabel = "waiting",
            )
        withoutComplete == "pending" ->
            HomeStatusPresentation(
                adornment = HomeStatusAdornment.WAITING,
                accessibilityLabel = "waiting",
            )
        withoutComplete.startsWith("capturing · waiting") ||
            withoutComplete.startsWith("capturing · processing") ->
            HomeStatusPresentation(
                text = "capturing",
                adornment = HomeStatusAdornment.WAITING,
                accessibilityLabel = withoutComplete,
            )
        status == "complete" -> HomeStatusPresentation()
        else -> HomeStatusPresentation(
            text = withoutComplete,
            accessibilityLabel = withoutComplete,
        )
    }
}

/**
 * The line under an archived capture's title.
 *
 * "record only" is stated rather than implied by a missing thumbnail: a
 * photo-free directory is a deliberate outcome of the photo budget, and it
 * must not read as a capture that lost its pages to a bug.
 */
internal fun archiveRowSubtitle(
    archivedOn: String,
    photoCount: Int,
    recordOnly: Boolean,
    statusLabel: String,
): String = buildList {
    add(if (archivedOn.isBlank()) "archived" else "archived $archivedOn")
    add(
        when {
            recordOnly -> "record only"
            photoCount == 1 -> "1 page"
            else -> "$photoCount pages"
        },
    )
    statusLabel.trim().takeIf { it.isNotEmpty() }?.let(::add)
}.joinToString(" · ")

/** Byte counts for a storage readout. Binary units, one decimal place past
 * KB, and no locale formatting: this sits next to monospace capture counts. */
internal fun formatByteSize(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val units = listOf("KB", "MB", "GB", "TB")
    var value = bytes.toDouble() / 1024
    var unit = 0
    while (value >= 1024 && unit < units.lastIndex) {
        value /= 1024
        unit++
    }
    return if (unit == 0) "${value.toInt()} ${units[unit]}"
    else String.format("%.1f %s", value, units[unit])
}

/** A small, dependency-free Markdown projection for the About dialog. */
internal fun formatChangelogForAbout(markdown: String): String = markdown
    .lineSequence()
    .map { line ->
        when {
            line.startsWith("### ") -> line.removePrefix("### ")
            line.startsWith("## ") -> line.removePrefix("## ")
            line.startsWith("# ") -> line.removePrefix("# ")
            line.startsWith("- ") -> "\u2022 ${line.removePrefix("- ")}"
            else -> line
        }
    }
    .joinToString("\n")
    .replace(Regex("\n{3,}"), "\n\n")
    .trim()
