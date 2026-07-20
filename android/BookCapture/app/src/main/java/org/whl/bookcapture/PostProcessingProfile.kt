package org.whl.bookcapture

/**
 * Stable, device-independent description of the requested display-derivative
 * cleanup. The camera original is never a target of this profile.
 *
 * [PostProcessingPreset.AUTOMATIC_BY_DATE] resolves from catalog metadata.
 * Explicit presets ignore the supplied year, which makes a user choice
 * reproducible even when a later metadata pass changes the publication date.
 */
internal enum class PostProcessingPreset(val storedValue: String) {
    AUTOMATIC_BY_DATE("automatic_by_date"),
    MODERN_1950_AND_LATER("modern_1950_and_later"),
    OLDER_1850_TO_1949("older_1850_to_1949"),
    EARLY_BEFORE_1850("early_before_1850"),
    ;

    companion object {
        fun fromStoredValue(value: String?): PostProcessingPreset =
            entries.firstOrNull { it.storedValue == value?.trim() } ?: AUTOMATIC_BY_DATE
    }
}

/** The concrete tuning family sent to a future post-processing service. */
internal enum class PostProcessingTreatment(val contractValue: String) {
    MODERN("modern"),
    OLDER("older"),
    EARLY("early"),
    UNKNOWN_DATE("unknown_date"),
}

/** Feature gates are independent of the selected date-range tuning. */
internal data class PostProcessingFeatures(
    val dewarpPerspectiveAndPageCurvature: Boolean = true,
    val cropToDetectedPageMargins: Boolean = true,
    val normalizePageAndTextContrast: Boolean = true,
    val detectAndCropSpine: Boolean = true,
)

/**
 * Fully resolved request. Percentage values are deliberately integral so the
 * same catalog year and preferences always produce identical request values
 * on Android, desktop, and a future cloud worker.
 */
internal data class PostProcessingProfile(
    val contractVersion: Int = CONTRACT_VERSION,
    val selectedPreset: PostProcessingPreset,
    val resolvedTreatment: PostProcessingTreatment,
    val publicationYear: Int?,
    val features: PostProcessingFeatures,
    val pageDewarpStrengthPercent: Int,
    val detectedMarginPaddingPercent: Int,
    val contrastStrengthPercent: Int,
    val paperToneRetentionPercent: Int,
) {
    companion object {
        const val CONTRACT_VERSION = 1
    }
}

private data class PostProcessingTuning(
    val dewarp: Int,
    val marginPadding: Int,
    val contrast: Int,
    val paperToneRetention: Int,
)

private val tuningByTreatment = mapOf(
    // Newer, comparatively flat and uniform pages can be cropped closely and
    // normalized more strongly for consistent display.
    PostProcessingTreatment.MODERN to PostProcessingTuning(
        dewarp = 55,
        marginPadding = 2,
        contrast = 70,
        paperToneRetention = 25,
    ),
    // Older bindings often have more gutter curvature and variable paper.
    // Leave more margin and retain more of the paper tone.
    PostProcessingTreatment.OLDER to PostProcessingTuning(
        dewarp = 70,
        marginPadding = 4,
        contrast = 50,
        paperToneRetention = 75,
    ),
    // Early books need the strongest curvature model but the least aggressive
    // crop/contrast treatment so irregular edges, foxing, and annotations are
    // not mistaken for disposable background.
    PostProcessingTreatment.EARLY to PostProcessingTuning(
        dewarp = 85,
        marginPadding = 8,
        contrast = 35,
        paperToneRetention = 90,
    ),
    // A missing or unparseable year must not silently receive the modern,
    // destructive-looking treatment. Use a conservative balanced fallback.
    PostProcessingTreatment.UNKNOWN_DATE to PostProcessingTuning(
        dewarp = 65,
        marginPadding = 5,
        contrast = 45,
        paperToneRetention = 75,
    ),
)

/**
 * Resolves an automatic or explicit preset. Valid years use the proleptic
 * catalog range 1..9999; other values are treated as unknown metadata.
 */
internal fun resolvePostProcessingProfile(
    selectedPreset: PostProcessingPreset,
    publicationYear: Int?,
    features: PostProcessingFeatures = PostProcessingFeatures(),
): PostProcessingProfile {
    val validYear = publicationYear?.takeIf { it in 1..9999 }
    val treatment = when (selectedPreset) {
        PostProcessingPreset.AUTOMATIC_BY_DATE -> when {
            validYear == null -> PostProcessingTreatment.UNKNOWN_DATE
            validYear < 1850 -> PostProcessingTreatment.EARLY
            validYear < 1950 -> PostProcessingTreatment.OLDER
            else -> PostProcessingTreatment.MODERN
        }
        PostProcessingPreset.MODERN_1950_AND_LATER -> PostProcessingTreatment.MODERN
        PostProcessingPreset.OLDER_1850_TO_1949 -> PostProcessingTreatment.OLDER
        PostProcessingPreset.EARLY_BEFORE_1850 -> PostProcessingTreatment.EARLY
    }
    val tuning = checkNotNull(tuningByTreatment[treatment])
    return PostProcessingProfile(
        selectedPreset = selectedPreset,
        resolvedTreatment = treatment,
        publicationYear = validYear,
        features = features,
        pageDewarpStrengthPercent = tuning.dewarp,
        detectedMarginPaddingPercent = tuning.marginPadding,
        contrastStrengthPercent = tuning.contrast,
        paperToneRetentionPercent = tuning.paperToneRetention,
    )
}
