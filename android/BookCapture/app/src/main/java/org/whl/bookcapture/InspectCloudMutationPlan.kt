package org.whl.bookcapture

/** Owner and transport evidence gathered while a selected entry is locked. */
internal data class InspectCloudMutationCandidate(
    val captureId: String,
    val cloudBacked: Boolean,
    /** Safe to ask the current account whether this capture exists there. */
    val ownerProbeEligible: Boolean,
    val ownerEvidence: Collection<String> = emptyList(),
)

internal data class InspectCloudMutationPlan(
    val captureIdsByOwner: Map<String, Set<String>>,
    /** Owner-scoped existence lookup candidates, including an interrupted upload. */
    val probeCaptureIds: Set<String>,
    /** Known cloud rows for which no trustworthy owner is available yet. */
    val unresolvedCloudCaptureIds: Set<String>,
)

/**
 * Build a fail-closed cloud plan without adopting the account that happens to
 * be signed in. Conflicting durable owner evidence aborts the action instead of
 * retagging one account's capture for another account.
 */
internal fun planInspectCloudMutation(
    candidates: Collection<InspectCloudMutationCandidate>,
): InspectCloudMutationPlan {
    val byOwner = linkedMapOf<String, MutableSet<String>>()
    val probe = linkedSetOf<String>()
    val unresolved = linkedSetOf<String>()

    candidates.forEach { candidate ->
        val captureId = candidate.captureId.trim().lowercase()
        require(captureId.isNotEmpty()) { "capture id is required" }
        val owners = candidate.ownerEvidence.asSequence()
            .map { it.trim().lowercase() }
            .filter(String::isNotEmpty)
            .toSet()
        require(owners.all(SAFE_CAPTURE_SYNC_ID::matches)) { "invalid cloud owner" }
        require(owners.size <= 1) { "conflicting cloud owners" }

        val owner = owners.singleOrNull()
        if (owner != null) {
            byOwner.getOrPut(owner) { linkedSetOf() }.add(captureId)
        } else {
            if (candidate.ownerProbeEligible && SAFE_CAPTURE_SYNC_ID.matches(captureId)) {
                probe += captureId
            }
            if (candidate.cloudBacked) unresolved += captureId
        }
    }

    return InspectCloudMutationPlan(
        captureIdsByOwner = byOwner.mapValues { (_, ids) -> ids.toSet() },
        probeCaptureIds = probe,
        unresolvedCloudCaptureIds = unresolved,
    )
}
