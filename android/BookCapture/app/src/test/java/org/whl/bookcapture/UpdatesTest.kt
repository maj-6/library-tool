package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/** Dormant APK precedence plus the active remote-interface refresh contract. */
class UpdatesTest {

    private fun release(version: String, channel: String = "stable") =
        Release(version, channel, "https://example.test/$version.apk", "")

    // --- version precedence --------------------------------------------------

    @Test
    fun numericPartsCompareAsNumbersNotText() {
        assertTrue(compareVersions("0.10.0", "0.9.0") > 0)
        assertTrue(compareVersions("0.5.10", "0.5.9") > 0)
        assertEquals(0, compareVersions("0.5.1", "0.5.1"))
    }

    @Test
    fun missingTrailingPartsCountAsZero() {
        assertEquals(0, compareVersions("1.2", "1.2.0"))
        assertTrue(compareVersions("1.2.1", "1.2") > 0)
    }

    @Test
    fun aReleaseOutranksItsOwnPrereleases() {
        assertTrue(compareVersions("0.5.1", "0.5.1-alpha.5") > 0)
        assertTrue(compareVersions("0.5.1-rc.1", "0.5.1") < 0)
    }

    @Test
    fun prereleaseIdentifiersComparePartwise() {
        assertTrue(compareVersions("0.5.1-alpha.10", "0.5.1-alpha.9") > 0)
        assertTrue(compareVersions("0.5.1-beta.1", "0.5.1-alpha.9") > 0)
        assertTrue(compareVersions("0.5.1-alpha", "0.5.1-alpha.1") < 0)
    }

    @Test
    fun aLeadingVIsIgnored() {
        assertEquals(0, compareVersions("v0.5.1", "0.5.1"))
        assertTrue(compareVersions("v0.6.0", "0.5.1") > 0)
    }

    // --- which channels a build may see --------------------------------------

    @Test
    fun stableBuildsAreNeverOfferedPrereleases() {
        assertEquals(setOf("stable"), channelsVisibleTo("0.5.1"))
    }

    @Test
    fun aPrereleaseBuildSeesItsOwnChannelAndStable() {
        assertEquals(setOf("stable", "alpha"), channelsVisibleTo("0.5.1-alpha.5"))
        assertEquals(setOf("stable", "beta"), channelsVisibleTo("0.6.0-beta.2"))
    }

    // --- picking the offer ---------------------------------------------------

    @Test
    fun nothingIsOfferedWhenAlreadyCurrent() {
        assertNull(pickUpdate("0.5.1", listOf(release("0.5.1"), release("0.5.0"))))
        assertNull(pickUpdate("0.6.0", listOf(release("0.5.1"))))
    }

    @Test
    fun theHighestVisibleVersionWinsRegardlessOfPublishOrder() {
        val offered = pickUpdate("0.5.0", listOf(
            release("0.5.1"), release("0.7.0"), release("0.6.2"),
        ))
        assertEquals("0.7.0", offered?.version)
    }

    @Test
    fun aStableUserIsNotOfferedANewerAlpha() {
        val offered = pickUpdate("0.5.1", listOf(
            release("0.9.0-alpha.1", channel = "alpha"),
            release("0.6.0"),
        ))
        assertEquals("0.6.0", offered?.version)
    }

    @Test
    fun anAlphaUserIsOfferedBothTheirChannelAndStable() {
        assertEquals(
            "0.9.0-alpha.2",
            pickUpdate("0.5.1-alpha.5", listOf(
                release("0.9.0-alpha.2", channel = "alpha"),
                release("0.6.0"),
            ))?.version,
        )
        // ...and a stable release still wins when it is genuinely higher
        assertEquals(
            "1.0.0",
            pickUpdate("0.5.1-alpha.5", listOf(
                release("0.9.0-alpha.2", channel = "alpha"),
                release("1.0.0"),
            ))?.version,
        )
    }

    @Test
    fun anAlphaUserIsOfferedTheStableOfTheirOwnVersion() {
        // 0.5.1 final is genuinely newer than 0.5.1-alpha.5 -- the common case
        // at the end of an alpha line, and the one a naive string compare misses.
        assertEquals(
            "0.5.1",
            pickUpdate("0.5.1-alpha.5", listOf(release("0.5.1")))?.version,
        )
    }

    @Test
    fun rowsWithNoDownloadUrlAreNotOffered() {
        assertNull(pickUpdate("0.5.0", listOf(Release("0.6.0", "stable", "  ", ""))))
    }

    @Test
    fun checkForUpdatesReportsOnlyRemoteInterfaceRefreshState() {
        assertSame(
            Updates.Result.UiUpdated,
            Updates.resultFor(RemoteUiCatalog.Refresh.CHANGED),
        )
        assertSame(
            Updates.Result.UiCurrent,
            Updates.resultFor(RemoteUiCatalog.Refresh.UNCHANGED),
        )
        assertThrows(IOException::class.java) {
            Updates.resultFor(RemoteUiCatalog.Refresh.EMPTY)
        }
    }
}
