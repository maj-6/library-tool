package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanPriorityIndicatorTest {
    private fun presentation(
        candidate: Boolean?,
        rank: Int?,
        assessment: ScanPriorityAssessment? = null,
    ) = scanPriorityPresentation(
        candidate = candidate,
        rank = rank,
        assessment = assessment,
        candidateLabel = "Scan candidate",
        rankLabel = { "Scan candidate, rank $it of 5; 1 is highest" },
        rankUnsetLabel = "Rank not assigned",
        assessmentLabel = {
            when (it) {
                ScanPriorityAssessment.NO_SCAN -> "No scan"
                ScanPriorityAssessment.LOW -> "Scan priority low"
                ScanPriorityAssessment.MEDIUM -> "Scan priority medium"
                ScanPriorityAssessment.HIGH -> "Scan priority high"
            }
        },
    )

    @Test
    fun nonCandidatesStayUnhighlightedEvenWithAnOrphanedPriority() {
        for (candidate in listOf<Boolean?>(null, false)) {
            val result = presentation(candidate, 2)
            assertFalse(result.visible)
            assertFalse(result.candidateGlyphVisible)
            assertEquals("", result.badge)
            assertEquals("", result.accessibilityLabel)
            assertEquals(ScanPriorityTone.NONE, result.tone)
        }
    }

    @Test
    fun explicitCandidateRendersItsValidNumberAndAccessibleLabel() {
        val result = presentation(true, 3)
        assertTrue(result.visible)
        assertTrue(result.candidateGlyphVisible)
        assertEquals("3", result.badge)
        assertEquals(
            "Scan candidate, rank 3 of 5; 1 is highest",
            result.accessibilityLabel,
        )
        assertEquals(ScanPriorityTone.CANDIDATE, result.tone)
    }

    @Test
    fun legacyOrInvalidCandidatePriorityUsesAnHonestUnknownBadge() {
        for (priority in listOf<Int?>(null, 0, 6)) {
            val result = presentation(true, priority)
            assertTrue(result.visible)
            assertTrue(result.candidateGlyphVisible)
            assertEquals("?", result.badge)
            assertEquals("Scan candidate. Rank not assigned", result.accessibilityLabel)
            assertEquals(ScanPriorityTone.CANDIDATE, result.tone)
        }
    }

    @Test
    fun parserAcceptsOnlyCanonicalAssignedValues() {
        assertEquals(
            ScanPriorityAssessment.NO_SCAN,
            ScanPriorityAssessment.parse("n/s (no scan)"),
        )
        assertEquals(ScanPriorityAssessment.LOW, ScanPriorityAssessment.parse("Low"))
        assertEquals(ScanPriorityAssessment.MEDIUM, ScanPriorityAssessment.parse("Medium"))
        assertEquals(ScanPriorityAssessment.HIGH, ScanPriorityAssessment.parse("High"))

        for (invalid in listOf<String?>(
            null,
            "",
            "high",
            " High",
            "No scan",
            "1",
        )) {
            assertEquals(null, ScanPriorityAssessment.parse(invalid))
        }
    }

    @Test
    fun assignedAssessmentStylesBookWithoutInventingCandidateState() {
        val expected = listOf(
            Triple(ScanPriorityAssessment.HIGH, "HI", ScanPriorityTone.HIGH),
            Triple(ScanPriorityAssessment.MEDIUM, "MED", ScanPriorityTone.MEDIUM),
            Triple(ScanPriorityAssessment.LOW, "LOW", ScanPriorityTone.LOW),
            Triple(ScanPriorityAssessment.NO_SCAN, "N/S", ScanPriorityTone.NO_SCAN),
        )

        for ((assessment, badge, tone) in expected) {
            val result = presentation(
                candidate = false,
                rank = 1,
                assessment = assessment,
            )
            assertTrue(result.visible)
            assertFalse(result.candidateGlyphVisible)
            assertEquals(badge, result.badge)
            assertEquals(tone, result.tone)
        }
        assertEquals(
            "Scan priority high",
            presentation(false, 1, ScanPriorityAssessment.HIGH).accessibilityLabel,
        )
        assertEquals(
            "No scan",
            presentation(false, 1, ScanPriorityAssessment.NO_SCAN).accessibilityLabel,
        )
    }

    @Test
    fun candidateGlyphAndAssignedAssessmentRemainIndependent() {
        val result = presentation(
            candidate = true,
            rank = 1,
            assessment = ScanPriorityAssessment.NO_SCAN,
        )

        assertTrue(result.visible)
        assertTrue(result.candidateGlyphVisible)
        assertEquals("N/S", result.badge)
        assertEquals(ScanPriorityTone.NO_SCAN, result.tone)
        assertEquals("Scan candidate. No scan", result.accessibilityLabel)
    }

    @Test
    fun textualAssessmentOwnsBadgeInsteadOfUnrelatedNumericRank() {
        val result = presentation(
            candidate = true,
            rank = 1,
            assessment = ScanPriorityAssessment.LOW,
        )

        assertEquals("LOW", result.badge)
        assertEquals(ScanPriorityTone.LOW, result.tone)
        assertEquals("Scan candidate. Scan priority low", result.accessibilityLabel)
    }
}
