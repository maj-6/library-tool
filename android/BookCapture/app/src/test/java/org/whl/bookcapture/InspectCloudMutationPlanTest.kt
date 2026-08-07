package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class InspectCloudMutationPlanTest {

    private val ownerA = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    private val captureA = "11111111-2222-4333-8444-555555555555"
    private val captureB = "66666666-7777-4888-8999-000000000000"

    @Test
    fun durableOwnerIsPreservedInsteadOfAdoptingTheCurrentSession() {
        val plan = planInspectCloudMutation(
            listOf(
                InspectCloudMutationCandidate(
                    captureId = captureA,
                    cloudBacked = true,
                    ownerProbeEligible = true,
                    ownerEvidence = listOf(ownerA.uppercase(), ownerA),
                ),
            ),
        )

        assertEquals(mapOf(ownerA to setOf(captureA)), plan.captureIdsByOwner)
        assertEquals(emptySet<String>(), plan.probeCaptureIds)
        assertEquals(emptySet<String>(), plan.unresolvedCloudCaptureIds)
    }

    @Test
    fun eligibleUnknownRowsAreProbedButDefiniteLocalRowsAreNot() {
        val plan = planInspectCloudMutation(
            listOf(
                InspectCloudMutationCandidate(
                    captureA,
                    cloudBacked = true,
                    ownerProbeEligible = true,
                ),
                InspectCloudMutationCandidate(
                    captureB,
                    cloudBacked = false,
                    ownerProbeEligible = true,
                ),
                InspectCloudMutationCandidate(
                    "99999999-aaaa-4bbb-8ccc-dddddddddddd",
                    cloudBacked = false,
                    ownerProbeEligible = false,
                ),
            ),
        )

        assertEquals(setOf(captureA, captureB), plan.probeCaptureIds)
        assertEquals(setOf(captureA), plan.unresolvedCloudCaptureIds)
    }

    @Test
    fun conflictingOwnerEvidenceFailsClosed() {
        assertThrows(IllegalArgumentException::class.java) {
            planInspectCloudMutation(
                listOf(
                    InspectCloudMutationCandidate(
                        captureA,
                        cloudBacked = true,
                        ownerProbeEligible = true,
                        ownerEvidence = listOf(
                            ownerA,
                            "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                        ),
                    ),
                ),
            )
        }
    }
}
