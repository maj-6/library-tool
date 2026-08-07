package org.whl.bookcapture

/** Immutable selection behavior shared by every Inspect presentation mode. */
internal data class InspectSelectionState(
    val selectedIds: Set<String> = emptySet(),
) {
    val active: Boolean get() = selectedIds.isNotEmpty()
    val size: Int get() = selectedIds.size

    fun isSelected(captureId: String): Boolean = captureId in selectedIds

    /** A long press starts selection or adds another book to the active selection. */
    fun addFromLongPress(captureId: String): InspectSelectionState {
        val id = captureId.trim()
        if (id.isEmpty() || id in selectedIds) return this
        return copy(selectedIds = linkedSetOf<String>().apply {
            addAll(selectedIds)
            add(id)
        })
    }

    /** Toggle one book. Callers use this for taps only while [active] is true. */
    fun toggleFromTap(captureId: String): InspectSelectionState {
        val id = captureId.trim()
        if (id.isEmpty()) return this
        val updated = linkedSetOf<String>().apply { addAll(selectedIds) }
        if (!updated.add(id)) updated.remove(id)
        return if (updated == selectedIds) this else copy(selectedIds = updated)
    }

    /** Keep selections that still exist after an authoritative Inspect refresh. */
    fun reconcile(availableIds: Collection<String>): InspectSelectionState {
        if (selectedIds.isEmpty()) return this
        val available = availableIds.asSequence()
            .map(String::trim)
            .filter(String::isNotEmpty)
            .toSet()
        val updated = selectedIds.filterTo(linkedSetOf()) { it in available }
        return if (updated == selectedIds) this else copy(selectedIds = updated)
    }

    fun clear(): InspectSelectionState = if (selectedIds.isEmpty()) this else InspectSelectionState()
}
