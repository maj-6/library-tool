package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class PostProcessingProfileTest {

    @Test
    fun storedPresetIsValidatedAndDefaultsToAutomatic() {
        assertEquals(
            PostProcessingPreset.AUTOMATIC_BY_DATE,
            PostProcessingPreset.fromStoredValue(null),
        )
        assertEquals(
            PostProcessingPreset.AUTOMATIC_BY_DATE,
            PostProcessingPreset.fromStoredValue("not-a-preset"),
        )
        assertEquals(
            PostProcessingPreset.OLDER_1850_TO_1949,
            PostProcessingPreset.fromStoredValue("older_1850_to_1949"),
        )
    }

    @Test
    fun automaticPresetHasDeterministicDateBoundariesAndSafeUnknownFallback() {
        fun treatment(year: Int?) = resolvePostProcessingProfile(
            PostProcessingPreset.AUTOMATIC_BY_DATE,
            year,
        ).resolvedTreatment

        assertEquals(PostProcessingTreatment.UNKNOWN_DATE, treatment(null))
        assertEquals(PostProcessingTreatment.UNKNOWN_DATE, treatment(0))
        assertEquals(PostProcessingTreatment.UNKNOWN_DATE, treatment(10_000))
        assertEquals(PostProcessingTreatment.EARLY, treatment(1849))
        assertEquals(PostProcessingTreatment.OLDER, treatment(1850))
        assertEquals(PostProcessingTreatment.OLDER, treatment(1949))
        assertEquals(PostProcessingTreatment.MODERN, treatment(1950))
    }

    @Test
    fun explicitDateRangePresetOverridesCatalogYear() {
        assertEquals(
            PostProcessingTreatment.EARLY,
            resolvePostProcessingProfile(
                PostProcessingPreset.EARLY_BEFORE_1850,
                publicationYear = 2024,
            ).resolvedTreatment,
        )
        assertEquals(
            PostProcessingTreatment.MODERN,
            resolvePostProcessingProfile(
                PostProcessingPreset.MODERN_1950_AND_LATER,
                publicationYear = 1700,
            ).resolvedTreatment,
        )
    }

    @Test
    fun olderProfilesPreserveMorePageContextAndUseGentlerContrast() {
        val modern = resolvePostProcessingProfile(
            PostProcessingPreset.MODERN_1950_AND_LATER,
            null,
        )
        val older = resolvePostProcessingProfile(
            PostProcessingPreset.OLDER_1850_TO_1949,
            null,
        )
        val early = resolvePostProcessingProfile(
            PostProcessingPreset.EARLY_BEFORE_1850,
            null,
        )

        assertTrue(older.pageDewarpStrengthPercent > modern.pageDewarpStrengthPercent)
        assertTrue(early.pageDewarpStrengthPercent > older.pageDewarpStrengthPercent)
        assertTrue(older.detectedMarginPaddingPercent > modern.detectedMarginPaddingPercent)
        assertTrue(early.detectedMarginPaddingPercent > older.detectedMarginPaddingPercent)
        assertTrue(older.contrastStrengthPercent < modern.contrastStrengthPercent)
        assertTrue(early.contrastStrengthPercent < older.contrastStrengthPercent)
        assertTrue(older.paperToneRetentionPercent > modern.paperToneRetentionPercent)
        assertTrue(early.paperToneRetentionPercent > older.paperToneRetentionPercent)
    }

    @Test
    fun featureGatesRemainIndependentOfDateTuning() {
        val features = PostProcessingFeatures(
            dewarpPerspectiveAndPageCurvature = false,
            cropToDetectedPageMargins = true,
            normalizePageAndTextContrast = false,
            detectAndCropSpine = true,
        )
        val profile = resolvePostProcessingProfile(
            PostProcessingPreset.AUTOMATIC_BY_DATE,
            publicationYear = 1790,
            features = features,
        )

        assertEquals(features, profile.features)
        assertEquals(1, profile.contractVersion)
        assertFalse(profile.features.dewarpPerspectiveAndPageCurvature)
        assertTrue(profile.features.cropToDetectedPageMargins)
        assertFalse(profile.features.normalizePageAndTextContrast)
        assertTrue(profile.features.detectAndCropSpine)
    }

    @Test
    fun settingsExposeAccessibleRegularCaseControlsAndPersistEveryFeature() {
        val layout = File("src/main/res/layout/activity_settings.xml").readText()
        for (id in listOf(
            "postProcessingSection",
            "postProcessingPresetGroup",
            "postProcessingPresetAutomatic",
            "postProcessingPresetModern",
            "postProcessingPresetOlder",
            "postProcessingPresetEarly",
            "postProcessingDewarp",
            "postProcessingMarginCrop",
            "postProcessingContrast",
            "postProcessingSpineCrop",
        )) {
            assertTrue("Missing settings control $id", layout.contains("android:id=\"@+id/$id\""))
        }
        assertTrue(layout.contains("android:fontFamily=\"@font/roboto_slab\""))
        assertTrue(layout.contains("android:textAllCaps=\"false\""))

        val strings = File("src/main/res/values/strings.xml").readText()
        assertTrue(strings.contains(">Post-processing</string>"))
        assertTrue(strings.contains(">Automatic by publication date</string>"))
        assertTrue(strings.contains(">Modern books (1950 and later)</string>"))
        assertTrue(strings.contains(">Older books (1850-1949)</string>"))
        assertTrue(strings.contains(">Early books (before 1850)</string>"))
        assertFalse(layout.contains("@string/set_post_processing_note"))
        assertFalse(layout.contains("postProcessingPresetSummary"))

        val prefs = File("src/main/java/org/whl/bookcapture/Prefs.kt").readText()
        for (key in listOf(
            "post_processing_dewarp",
            "post_processing_margin_crop",
            "post_processing_contrast",
            "post_processing_spine_crop",
        )) {
            assertTrue("Missing persisted preference $key", prefs.contains("\"$key\""))
            assertTrue("$key should default on", prefs.contains("getBoolean(\"$key\", true)"))
        }
    }
}
