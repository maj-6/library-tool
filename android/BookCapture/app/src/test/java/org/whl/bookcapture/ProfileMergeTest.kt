package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ProfileMergeTest {

    @Test
    fun deepseekOnlyEditPreservesNewerCloudMistralKey() {
        val merged = mergeProfileApiKeys(
            latest = ProfileApiKeys(mistral = "new-cloud-mistral", deepseek = "old-deepseek"),
            mistralEdit = null,
            deepseekEdit = "new-phone-deepseek",
        )

        assertEquals("new-cloud-mistral", merged.mistral)
        assertEquals("new-phone-deepseek", merged.deepseek)
    }

    @Test
    fun mistralOnlyEditPreservesNewerCloudDeepseekKey() {
        val merged = mergeProfileApiKeys(
            latest = ProfileApiKeys(mistral = "old-mistral", deepseek = "new-cloud-deepseek"),
            mistralEdit = "  new-phone-mistral  ",
            deepseekEdit = null,
        )

        assertEquals("new-phone-mistral", merged.mistral)
        assertEquals("new-cloud-deepseek", merged.deepseek)
    }

    @Test
    fun updatedAtOptimisticLockFilterEncodesTimestamp() {
        assertEquals(
            "updated_at=eq.2026-07-17T12%3A34%3A56.123456%2B00%3A00",
            profileUpdatedAtFilter("2026-07-17T12:34:56.123456+00:00"),
        )
    }

    @Test
    fun nullUpdatedAtOptimisticLockFilterMatchesNull() {
        assertEquals("updated_at=is.null", profileUpdatedAtFilter(null))
    }

    @Test
    fun localProcessingKeysAreSeparateFromAccountCacheAndSurviveSignOut() {
        val prefs = File("src/main/java/org/whl/bookcapture/Prefs.kt").readText()
        val clearSession = prefs.substringAfter("fun clearSession").substringBefore("// --- processing keys")

        assertTrue(prefs.contains("local_mistral_key"))
        assertTrue(prefs.contains("local_deepseek_key"))
        assertTrue(prefs.contains("if (owner.isEmpty())"))
        assertFalse(clearSession.contains("remove(\"local_mistral_key\")"))
        assertFalse(clearSession.contains("remove(\"local_deepseek_key\")"))
    }
}
