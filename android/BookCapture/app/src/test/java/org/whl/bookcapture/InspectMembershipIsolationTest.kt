package org.whl.bookcapture

import kotlinx.coroutines.CancellationException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.IOException

class InspectMembershipIsolationTest {

    @Test
    fun oneRejectedIdDoesNotBlockValidMembershipIntents() {
        val ids = (0 until 32).map { "capture-$it" }
        val stale = "capture-19"
        val requests = mutableListOf<List<String>>()
        val cached = linkedSetOf<String>()

        val result = isolateInspectMembershipMutation(
            captureIds = ids,
            shouldBisect = { true },
            mutate = { batch ->
                requests += batch
                if (stale in batch) {
                    throw SupabaseClient.HttpException(403, "missing or wrong owner")
                }
                batch.toSet()
            },
            onAccepted = { accepted ->
                assertFalse(stale in accepted)
                cached += accepted
            },
        )

        assertEquals(ids.toSet() - stale, result.acceptedIds)
        assertEquals(setOf(stale), result.failedIds)
        assertEquals(result.acceptedIds, cached)
        assertTrue(requests.size <= INSPECT_MEMBERSHIP_ISOLATION_MAX_ATTEMPTS)
        assertTrue(requests.all { it.size <= CAPTURE_COLLECTION_MUTATION_MAX_IDS })
    }

    @Test
    fun attemptBudgetLeavesEveryUnresolvedIntentPending() {
        val ids = (0 until CAPTURE_COLLECTION_MUTATION_MAX_IDS).map { "capture-$it" }
        var attempts = 0

        val result = isolateInspectMembershipMutation(
            captureIds = ids,
            maximumAttempts = 5,
            shouldBisect = { true },
            mutate = { _ ->
                attempts += 1
                throw SupabaseClient.HttpException(403, "rejected")
            },
            onAccepted = { error("no failed request may update the cache") },
        )

        assertEquals(5, attempts)
        assertTrue(result.acceptedIds.isEmpty())
        assertEquals(ids.toSet(), result.failedIds)
    }

    @Test
    fun cancellationPropagatesWithoutBisectionOrCacheWrites() {
        var attempts = 0
        var cacheWrites = 0

        assertThrows(CancellationException::class.java) {
            isolateInspectMembershipMutation(
                captureIds = listOf("capture-a", "capture-b"),
                shouldBisect = { true },
                mutate = {
                    attempts += 1
                    throw CancellationException("activity stopped")
                },
                onAccepted = { cacheWrites += 1 },
            )
        }

        assertEquals(1, attempts)
        assertEquals(0, cacheWrites)
    }

    @Test
    fun transientFailuresDoNotFanOutButOwnerRejectionsDo() {
        assertFalse(shouldBisectInspectMembershipFailure(IOException("offline")))
        assertFalse(
            shouldBisectInspectMembershipFailure(
                SupabaseClient.HttpException(401, "expired session"),
            ),
        )
        assertFalse(
            shouldBisectInspectMembershipFailure(
                SupabaseClient.HttpException(429, "rate limited"),
            ),
        )
        assertTrue(
            shouldBisectInspectMembershipFailure(
                SupabaseClient.HttpException(403, "wrong owner"),
            ),
        )
        assertTrue(
            shouldBisectInspectMembershipFailure(
                SupabaseClient.InvalidResponse("partial response"),
            ),
        )
    }

    @Test
    fun homeRetryClearsOnlyAuthoritativelyAcknowledgedIntents() {
        val source = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val retry = source.substringAfter("val isolatedRetryFailures")
            .substringBefore("// Most captures carry no title")
        assertTrue(retry.contains("isolateInspectMembershipMutation("))
        assertTrue(retry.contains("RemoteCollectionBooks.applyMembershipMutation("))
        val acceptedCallback = retry.substringAfter("onAccepted = { accepted ->")
            .substringBefore("},\n                                        )")
        assertFalse(acceptedCallback.contains("InspectBookMemberships.clear("))
        assertTrue(retry.contains("isolatedRetryFailures += isolated.failedIds"))

        val acknowledgement = source.substringAfter("val overlayAcknowledged")
            .substringBefore("recordResult != null && overlayAcknowledged")
        assertTrue(
            acknowledgement.contains(
                "recordResult.acknowledgedCaptureIds - isolatedRetryFailures",
            ),
        )
        assertTrue(acknowledgement.contains("InspectBookMemberships.clear("))
    }

    @Test
    fun inspectTalkBackDescriptionDoesNotRepeatAuthorAndYear() {
        val source = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val bind = source.substringAfter("private fun bindInspectBook(")
            .substringBefore("private val inspectActionModeCallback")
        val description = bind.substringAfter("view.contentDescription =")
            .substringBefore("view.setOnClickListener")

        assertTrue(description.contains("snapshot.titleLabel"))
        assertTrue(description.contains("details.joinToString"))
        assertFalse(description.contains("summary.author"))
        assertFalse(description.contains("summary.year"))
    }

    @Test
    fun deleteValidatesOwnersBeforeAtomicStageAndHasResumeCleanupRecovery() {
        val source = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val mutation = source.substringAfter("private fun mutateInspectSelection(")
            .substringBefore("override fun onDestroy()")
        assertTrue(
            mutation.indexOf("val cloudPlan = planInspectCloudMutation(cloudCandidates)") <
                mutation.indexOf("InspectBookMemberships.setMembership("),
        )
        assertTrue(mutation.contains("cleanupPending = removed"))
        assertTrue(mutation.contains("InspectBookMemberships.markCleanupComplete("))
        assertTrue(mutation.contains("val activeCloudSyncTargets = Prefs"))
        assertTrue(mutation.contains("couldBeInterruptedCloudInsert"))

        val resume = source.substringAfter("override fun onResume()")
            .substringBefore("override fun onStop()")
        assertTrue(resume.contains("retryPendingInspectBookCleanup(this@HomeActivity)"))
    }

    @Test
    fun cleanupRetryIsOrderedAndCollectionChangesCannotDiscardATombstone() {
        val home = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val memberships = File(
            "src/main/java/org/whl/bookcapture/InspectBookMemberships.kt",
        ).readText()
        val upload = File("src/main/java/org/whl/bookcapture/UploadWorker.kt").readText()

        val mutation = home.substringAfter("private fun mutateInspectSelection(")
            .substringBefore("override fun onDestroy()")
        val retry = home.substringAfter("val reconciledPending =")
            .substringBefore("if (reconciledPending)")
        val cleanup = memberships.substringAfter(
            "internal suspend fun retryPendingInspectBookCleanup",
        )
        assertTrue(mutation.contains("INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock"))
        assertTrue(retry.contains("INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock"))
        assertTrue(cleanup.contains("INSPECT_MEMBERSHIP_MUTATION_MUTEX.withLock"))
        assertTrue(cleanup.contains("if (latest?.removed != true || !latest.cleanupPending)"))

        assertTrue(retry.contains("if (pending.removed) return@forEach"))
        assertTrue(retry.contains("pending.copy(collectionId = targetId)"))
        assertTrue(upload.contains("if (membership.removed)"))
        assertTrue(upload.contains("deleted capture has no live collection"))
    }
}
