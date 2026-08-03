package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CaptureOwnershipTest {

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()

    @Test
    fun signedInCaptureIsFrozenToThatAccount() {
        assertEquals(
            CaptureCreator(Prefs.CREATOR_ACCOUNT, "account-a"),
            captureCreatorFor("  account-a  ", ""),
        )
    }

    @Test
    fun signedOutCaptureUsesTheStableLocalIdentity() {
        assertEquals(
            CaptureCreator(Prefs.CREATOR_LOCAL, "device-local-id"),
            captureCreatorFor("", "  device-local-id  "),
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun signedOutCaptureCannotStartWithoutADurableIdentity() {
        captureCreatorFor("", "")
    }

    @Test
    fun captureSidecarPrecedesActiveStateAndFeedsEveryManifestPath() {
        val session = source("CaptureSession")
        val writeCreator = session.indexOf(
            "writeCreator(dir, captureCreator, Prefs.cameraProfile(ctx))",
        )
        val publishActive = session.indexOf("Prefs.setCurrentEntryId(ctx, id)", writeCreator)

        assertTrue(writeCreator >= 0)
        assertTrue(publishActive > writeCreator)
        assertTrue(session.contains("creator = creatorFor(dir)"))
        assertTrue(
            session.contains("writeManifest(dir, photos, newest, creatorFor(dir), readProvenance(dir))"),
        )
        assertTrue(session.contains(".put(\"creator\", JSONObject()"))
        assertTrue(session.contains("Prefs.CREATOR_LOCAL"))
    }

    @Test
    fun localIdentitySurvivesSessionClearing() {
        val prefs = source("Prefs")
        val clearSession = prefs.substringAfter("fun clearSession").substringBefore("// --- API keys")

        assertTrue(prefs.contains("anonymous_creator_id"))
        assertTrue(prefs.contains(".commit()"))
        assertFalse(clearSession.contains("anonymous_creator_id"))
    }

    @Test
    fun cloudDeliveryRequiresTheFrozenAccountOrExplicitLocalClaim() {
        assertEquals(
            CloudUploadOwnership.ALLOWED,
            cloudUploadOwnership(CaptureCreator(Prefs.CREATOR_ACCOUNT, "account-a"), "account-a"),
        )
        assertEquals(
            CloudUploadOwnership.DIFFERENT_ACCOUNT,
            cloudUploadOwnership(CaptureCreator(Prefs.CREATOR_ACCOUNT, "account-a"), "account-b"),
        )
        assertEquals(
            CloudUploadOwnership.NEEDS_CLAIM,
            cloudUploadOwnership(CaptureCreator(Prefs.CREATOR_LOCAL, "install-id"), "account-a"),
        )
    }

    @Test
    fun bulkClaimIncludesOnlyLocalCapturesAndNeedsConfirmation() {
        val candidates = listOf(
            "local-b" to CaptureCreator(Prefs.CREATOR_LOCAL, "install-id"),
            "owned" to CaptureCreator(Prefs.CREATOR_ACCOUNT, "account-a"),
            "other-account" to CaptureCreator(Prefs.CREATOR_ACCOUNT, "account-b"),
            "local-a" to CaptureCreator(Prefs.CREATOR_LOCAL, "install-id"),
        )

        assertEquals(
            listOf("local-a", "local-b"),
            captureIdsNeedingCloudClaim(candidates, "account-a"),
        )
        val home = source("HomeActivity")
        assertTrue(home.contains("showCloudClaimConfirmation(claimIds, owner)"))
        assertTrue(home.contains("R.string.home_sync_claim_confirm"))
        assertTrue(home.contains("expectedAccountId = owner"))
        assertTrue(home.contains("transport != \"lan\" && Auth.signedIn(this)"))
        assertTrue(home.contains("readClaimableCaptureCreator(this@HomeActivity, dir)"))
        assertTrue(home.contains("withContext(Dispatchers.IO)"))
        assertTrue(home.contains("session.recoverOrphans(snapshot.map { it.name }.toSet())"))
        assertTrue(home.contains("finally {"))
        assertTrue(home.contains("if (syncActionInFlight) return"))
        assertTrue(home.contains("syncActionInFlight || state.active && !canRetryActive"))

        val upload = source("UploadWorker")
        assertTrue(upload.contains("UploadEntryProblemReason.NEEDS_CLOUD_CLAIM"))
        assertTrue(upload.contains("retrying-claimed-capture"))
        assertTrue(upload.contains("ownershipBlockHandled"))
    }

    @Test
    fun uploadAndClaimPathsEnforceTheOwnershipContract() {
        val upload = source("UploadWorker")
        val ownership = source("CaptureOwnership")
        val client = source("SupabaseClient")

        assertTrue(upload.contains("cloudUploadOwnership(prepared.creator, uploadOwner)"))
        assertTrue(upload.contains("CloudUploadOwnership.NEEDS_CLAIM"))
        assertTrue(ownership.contains("EntryOperationLocks.withLock(entryId)"))
        assertTrue(ownership.contains("manifest.put(\"creator\", creator)"))
        assertTrue(client.contains("Prefs.userId(ctx) != ownerId"))
        assertTrue(client.contains(".put(\"created_by\", ownerId)"))
    }
}
