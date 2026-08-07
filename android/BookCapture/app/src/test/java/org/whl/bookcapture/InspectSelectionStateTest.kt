package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class InspectSelectionStateTest {

    @Test
    fun longPressStartsSelectionAndAddsWithoutDuplicating() {
        val first = InspectSelectionState().addFromLongPress("book-a")
        val second = first.addFromLongPress("book-b")

        assertTrue(first.active)
        assertEquals(setOf("book-a", "book-b"), second.selectedIds)
        assertEquals(2, second.size)
        assertSame(second, second.addFromLongPress("book-b"))
        assertSame(second, second.addFromLongPress("  "))
    }

    @Test
    fun tapTogglesMembershipAndLastDeselectionEndsSelection() {
        val selected = InspectSelectionState(setOf("book-a"))
            .toggleFromTap("book-b")

        assertTrue(selected.isSelected("book-a"))
        assertTrue(selected.isSelected("book-b"))

        val one = selected.toggleFromTap("book-a")
        assertEquals(setOf("book-b"), one.selectedIds)
        val empty = one.toggleFromTap("book-b")
        assertFalse(empty.active)
        assertEquals(0, empty.size)
    }

    @Test
    fun reconcileDropsVanishedBooksAndPreservesSelectionOrder() {
        val original = InspectSelectionState(linkedSetOf("b", "a", "c"))

        val reconciled = original.reconcile(listOf("c", "b", "new"))

        assertEquals(listOf("b", "c"), reconciled.selectedIds.toList())
        assertSame(reconciled, reconciled.reconcile(listOf("b", "c", "other")))
    }

    @Test
    fun clearAndInvalidInputsAreSafeNoOps() {
        val empty = InspectSelectionState()
        assertSame(empty, empty.clear())
        assertSame(empty, empty.addFromLongPress(""))
        assertSame(empty, empty.toggleFromTap("  "))

        val selected = empty.addFromLongPress("book-a")
        assertEquals(InspectSelectionState(), selected.clear())
        assertEquals(InspectSelectionState(), selected.reconcile(emptyList()))
    }
}
