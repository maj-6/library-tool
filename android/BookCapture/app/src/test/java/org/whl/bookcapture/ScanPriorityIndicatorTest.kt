package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanPriorityIndicatorTest {
    private fun presentation(candidate: Boolean?, priority: Int?) = scanPriorityPresentation(
        candidate = candidate,
        priority = priority,
        candidateLabel = "Scan candidate",
        priorityLabel = { "Scan candidate, priority $it" },
        priorityUnsetLabel = "Priority not assigned",
    )

    @Test
    fun nonCandidatesStayUnhighlightedEvenWithAnOrphanedPriority() {
        for (candidate in listOf<Boolean?>(null, false)) {
            val result = presentation(candidate, 2)
            assertFalse(result.visible)
            assertEquals("", result.badge)
            assertEquals("", result.accessibilityLabel)
        }
    }

    @Test
    fun explicitCandidateRendersItsValidNumberAndAccessibleLabel() {
        val result = presentation(true, 3)
        assertTrue(result.visible)
        assertEquals("3", result.badge)
        assertEquals("Scan candidate, priority 3", result.accessibilityLabel)
    }

    @Test
    fun legacyOrInvalidCandidatePriorityUsesAnHonestUnknownBadge() {
        for (priority in listOf<Int?>(null, 0, 6)) {
            val result = presentation(true, priority)
            assertTrue(result.visible)
            assertEquals("?", result.badge)
            assertEquals("Scan candidate. Priority not assigned", result.accessibilityLabel)
        }
    }
}
