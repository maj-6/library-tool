package org.whl.bookcapture

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

internal data class CaptureCreator(val kind: String, val id: String)

/** Pure ownership selection used at the instant a capture starts. */
internal fun captureCreatorFor(accountId: String, anonymousId: String): CaptureCreator {
    val account = accountId.trim()
    if (account.isNotEmpty()) return CaptureCreator(Prefs.CREATOR_ACCOUNT, account)
    val local = anonymousId.trim()
    require(local.isNotEmpty()) { "Anonymous creator id is required" }
    return CaptureCreator(Prefs.CREATOR_LOCAL, local)
}

/**
 * Local state. Two kinds live here:
 *
 *  - device-local facts: the device label, the open entry's id, the last
 *    upload/processing error the main screen shows;
 *  - the signed-in session (access/refresh token) plus a cache of the
 *    account's cloud profile (display name, API keys) so the background
 *    pipeline works offline between logins.
 *
 * The Supabase project itself is baked in at build time (BuildConfig) — the
 * anon key is public by design, it is the login that authorizes anything.
 * A custom project can still be pointed at from Settings.
 */
object Prefs {
    private val profileLock = Any()
    private val deviceIdentityLock = Any()
    private val captureSyncLock = Any()

    private const val CAPTURE_SYNC_REQUEST_ID = "capture_sync_request_id"
    private const val CAPTURE_SYNC_PHASE = "capture_sync_phase"
    private const val CAPTURE_SYNC_TARGET_IDS = "capture_sync_target_ids"
    private const val CAPTURE_SYNC_SYNCED_IDS = "capture_sync_synced_ids"
    private const val CAPTURE_SYNC_BLOCKED_IDS = "capture_sync_blocked_ids"
    private const val CAPTURE_SYNC_TRANSPORT_MODE = "capture_sync_transport_mode"
    private const val CAPTURE_SYNC_LAN_HOST = "capture_sync_lan_host"
    private const val CAPTURE_SYNC_CLOUD_OWNER = "capture_sync_cloud_owner"
    private const val CAPTURE_SYNC_RESOLVED_TRANSPORT = "capture_sync_resolved_transport"

    private fun sp(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences("bookcapture", Context.MODE_PRIVATE)

    private fun str(ctx: Context, k: String): String = sp(ctx).getString(k, "")!!.trim()
    private fun put(ctx: Context, vararg kv: Pair<String, String?>) {
        val e = sp(ctx).edit()
        for ((k, v) in kv) e.putString(k, v ?: "")
        e.apply()
    }

    // --- project -------------------------------------------------------------

    fun supabaseUrl(ctx: Context): String =
        str(ctx, "supabase_url").ifEmpty { BuildConfig.SUPABASE_URL }.trimEnd('/')

    fun anonKey(ctx: Context): String =
        str(ctx, "anon_key").ifEmpty { BuildConfig.SUPABASE_ANON_KEY }

    fun setProject(ctx: Context, url: String, anon: String) =
        put(ctx, "supabase_url" to url.trim(), "anon_key" to anon.trim())

    // --- transport: how captures leave the phone -----------------------------
    // "cloud" (Supabase, the default), "lan" (a paired desktop over the local
    // network, offline), or "auto" (LAN when the desktop answers, else cloud).

    fun transport(ctx: Context): String = str(ctx, "transport").ifEmpty { "cloud" }
    fun setTransport(ctx: Context, v: String) = put(ctx, "transport" to v)
    fun lanHost(ctx: Context): String = str(ctx, "lan_host")      // "192.168.1.5:8899"
    fun lanToken(ctx: Context): String = str(ctx, "lan_token")
    fun setLan(ctx: Context, host: String, token: String) =
        put(ctx, "lan_host" to host.trim(), "lan_token" to token.trim())

    fun configured(ctx: Context): Boolean =
        supabaseUrl(ctx).isNotEmpty() && anonKey(ctx).isNotEmpty()

    // --- capture options -----------------------------------------------------

    /** Optional live-viewfinder sharpen (Android 13+); off by default. */
    fun sharpenPreview(ctx: Context): Boolean = sp(ctx).getBoolean("sharpen_preview", false)
    fun setSharpenPreview(ctx: Context, on: Boolean) =
        sp(ctx).edit().putBoolean("sharpen_preview", on).apply()

    const val CAMERA_PROFILE_LOW = "low"
    const val CAMERA_PROFILE_FAST = "fast"
    const val CAMERA_PROFILE_DETAIL = "detail"

    internal fun validatedCameraProfile(value: String?): String = when (value?.trim()) {
        CAMERA_PROFILE_LOW -> CAMERA_PROFILE_LOW
        CAMERA_PROFILE_DETAIL -> CAMERA_PROFILE_DETAIL
        else -> CAMERA_PROFILE_FAST
    }

    fun cameraProfile(ctx: Context): String =
        validatedCameraProfile(sp(ctx).getString("camera_profile", CAMERA_PROFILE_FAST))

    fun setCameraProfile(ctx: Context, profile: String) =
        put(ctx, "camera_profile" to validatedCameraProfile(profile))

    fun torchEnabled(ctx: Context): Boolean = sp(ctx).getBoolean("camera_torch", false)
    fun setTorchEnabled(ctx: Context, on: Boolean) =
        sp(ctx).edit().putBoolean("camera_torch", on).apply()

    /** Camera controls are stored as requests, then clamped against the active
     * lens's advertised range every time CameraX binds. */
    fun cameraZoomRatio(ctx: Context): Float =
        sp(ctx).getFloat("camera_zoom_ratio", 1f).takeIf { it.isFinite() && it > 0f } ?: 1f

    fun setCameraZoomRatio(ctx: Context, ratio: Float) =
        sp(ctx).edit().putFloat(
            "camera_zoom_ratio",
            ratio.takeIf { it.isFinite() && it > 0f } ?: 1f,
        ).apply()

    fun cameraExposureIndex(ctx: Context): Int =
        sp(ctx).getInt("camera_exposure_index", 0)

    fun setCameraExposureIndex(ctx: Context, index: Int) =
        sp(ctx).edit().putInt("camera_exposure_index", index).apply()

    fun cameraFocusLocked(ctx: Context): Boolean =
        sp(ctx).getBoolean("camera_focus_locked", false)

    fun setCameraFocusLocked(ctx: Context, locked: Boolean) =
        sp(ctx).edit().putBoolean("camera_focus_locked", locked).apply()

    fun cameraFocusPointX(ctx: Context): Float =
        sp(ctx).getFloat("camera_focus_x", 0.5f)
            .takeIf { it.isFinite() }?.coerceIn(0f, 1f) ?: 0.5f

    fun cameraFocusPointY(ctx: Context): Float =
        sp(ctx).getFloat("camera_focus_y", 0.5f)
            .takeIf { it.isFinite() }?.coerceIn(0f, 1f) ?: 0.5f

    fun setCameraFocusPoint(ctx: Context, x: Float, y: Float) =
        sp(ctx).edit()
            .putFloat("camera_focus_x", x.takeIf { it.isFinite() }?.coerceIn(0f, 1f) ?: 0.5f)
            .putFloat("camera_focus_y", y.takeIf { it.isFinite() }?.coerceIn(0f, 1f) ?: 0.5f)
            .apply()

    fun cameraCaptureOrientation(ctx: Context): String =
        cameraCaptureOrientation(
            sp(ctx).getString("camera_capture_orientation", null),
        ).storedValue

    internal fun setCameraCaptureOrientation(ctx: Context, orientation: CameraCaptureOrientation) =
        put(ctx, "camera_capture_orientation" to orientation.storedValue)

    fun cameraDiagnostics(ctx: Context): String = str(ctx, "camera_diagnostics")
    fun setCameraDiagnostics(ctx: Context, value: String) =
        put(ctx, "camera_diagnostics" to value.trim())

    fun resetCameraOptions(ctx: Context) {
        sp(ctx).edit()
            .remove("camera_profile")
            .remove("camera_torch")
            .remove("sharpen_preview")
            .remove("camera_zoom_ratio")
            .remove("camera_exposure_index")
            .remove("camera_focus_locked")
            .remove("camera_focus_x")
            .remove("camera_focus_y")
            .remove("camera_capture_orientation")
            .apply()
    }

    /** Hands-free voice control (Vosk). Opt-in and OFF by default: enabling it is
     *  what triggers the mic-permission request and the one-time model download,
     *  so the camera never depends on the microphone. `voice_enabled` was used
     *  by an unpublished build; migrate it into the upstream preference name. */
    fun voiceControl(ctx: Context): Boolean {
        val prefs = sp(ctx)
        if (prefs.contains("voice_control")) {
            return prefs.getBoolean("voice_control", false)
        }
        val legacy = prefs.getBoolean("voice_enabled", false)
        if (prefs.contains("voice_enabled")) {
            prefs.edit()
                .putBoolean("voice_control", legacy)
                .remove("voice_enabled")
                .apply()
        }
        return legacy
    }

    fun setVoiceControl(ctx: Context, on: Boolean) {
        sp(ctx).edit()
            .putBoolean("voice_control", on)
            .remove("voice_enabled")
            .apply()
    }

    /** Compatibility aliases for the local camera work merged into this branch. */
    fun voiceEnabled(ctx: Context): Boolean = voiceControl(ctx)
    fun setVoiceEnabled(ctx: Context, on: Boolean) = setVoiceControl(ctx, on)

    // --- device --------------------------------------------------------------

    fun deviceName(ctx: Context): String =
        str(ctx, "device_name").ifEmpty { android.os.Build.MODEL ?: "phone" }

    fun setDeviceName(ctx: Context, v: String) = put(ctx, "device_name" to v.trim())

    /** Home-list density is a device preference: it changes only presentation,
     * never capture metadata or the account profile. */
    fun compactScanList(ctx: Context): Boolean =
        sp(ctx).getBoolean("compact_scan_list", false)

    fun setCompactScanList(ctx: Context, compact: Boolean) =
        sp(ctx).edit().putBoolean("compact_scan_list", compact).apply()

    /** OCR geometry is a display preference. It never changes the persisted
     * provider evidence, so hiding boxes cannot erase a later-useful sidecar. */
    fun showOcrRegions(ctx: Context): Boolean =
        sp(ctx).getBoolean("show_ocr_regions", true)

    fun setShowOcrRegions(ctx: Context, show: Boolean) =
        sp(ctx).edit().putBoolean("show_ocr_regions", show).apply()

    fun ocrRegionOpacityPercent(ctx: Context): Int =
        sp(ctx).getInt("ocr_region_opacity", 55).coerceIn(10, 90)

    fun setOcrRegionOpacityPercent(ctx: Context, percent: Int) =
        sp(ctx).edit().putInt("ocr_region_opacity", percent.coerceIn(10, 90)).apply()

    fun showOcrRegionLabels(ctx: Context): Boolean =
        sp(ctx).getBoolean("show_ocr_region_labels", false)

    fun setShowOcrRegionLabels(ctx: Context, show: Boolean) =
        sp(ctx).edit().putBoolean("show_ocr_region_labels", show).apply()

    /** Shared extraction guidance replaces the removed per-book DeepSeek
     * action. Individual legacy instructions still take precedence. */
    fun extractionInstructions(ctx: Context): String = str(ctx, "extraction_instructions")

    fun setExtractionInstructions(ctx: Context, value: String) =
        put(ctx, "extraction_instructions" to value.trim().take(4_000))

    // --- display-derivative post-processing ---------------------------------

    /**
     * Post-processing is device-local until the derivative service exists.
     * These preferences describe a requested derivative only; capture
     * originals remain immutable regardless of the selected profile.
     */
    internal fun postProcessingPreset(ctx: Context): PostProcessingPreset =
        PostProcessingPreset.fromStoredValue(
            sp(ctx).getString("post_processing_preset", null),
        )

    internal fun setPostProcessingPreset(ctx: Context, preset: PostProcessingPreset) =
        put(ctx, "post_processing_preset" to preset.storedValue)

    internal fun postProcessingFeatures(ctx: Context): PostProcessingFeatures =
        PostProcessingFeatures(
            dewarpPerspectiveAndPageCurvature =
                sp(ctx).getBoolean("post_processing_dewarp", true),
            cropToDetectedPageMargins =
                sp(ctx).getBoolean("post_processing_margin_crop", true),
            normalizePageAndTextContrast =
                sp(ctx).getBoolean("post_processing_contrast", true),
            detectAndCropSpine =
                sp(ctx).getBoolean("post_processing_spine_crop", true),
        )

    internal fun setPostProcessingDewarp(ctx: Context, enabled: Boolean) =
        sp(ctx).edit().putBoolean("post_processing_dewarp", enabled).apply()

    internal fun setPostProcessingMarginCrop(ctx: Context, enabled: Boolean) =
        sp(ctx).edit().putBoolean("post_processing_margin_crop", enabled).apply()

    internal fun setPostProcessingContrast(ctx: Context, enabled: Boolean) =
        sp(ctx).edit().putBoolean("post_processing_contrast", enabled).apply()

    internal fun setPostProcessingSpineCrop(ctx: Context, enabled: Boolean) =
        sp(ctx).edit().putBoolean("post_processing_spine_crop", enabled).apply()

    /** A ready-to-serialize request for a catalog year (or an unknown year). */
    internal fun postProcessingProfile(
        ctx: Context,
        publicationYear: Int?,
    ): PostProcessingProfile =
        resolvePostProcessingProfile(
            selectedPreset = postProcessingPreset(ctx),
            publicationYear = publicationYear,
            features = postProcessingFeatures(ctx),
        )

    const val CREATOR_ACCOUNT = "account"
    const val CREATOR_LOCAL = "local"

    /** Stable for this installation and intentionally survives account sign-out.
     *  It identifies captures made without an account; it is not an auth token. */
    internal fun anonymousCreatorId(ctx: Context): String = synchronized(deviceIdentityLock) {
        str(ctx, "anonymous_creator_id").ifEmpty {
            val fresh = UUID.randomUUID().toString()
            check(sp(ctx).edit().putString("anonymous_creator_id", fresh).commit()) {
                "Could not persist anonymous creator identity"
            }
            fresh
        }
    }

    internal fun captureCreator(ctx: Context): CaptureCreator {
        val account = userId(ctx)
        return if (account.isNotEmpty()) {
            captureCreatorFor(account, "unused")
        } else {
            captureCreatorFor("", anonymousCreatorId(ctx))
        }
    }

    /** The entry currently being captured, so UploadWorker's orphan recovery
     *  can tell "live" from "left behind by a crash". */
    fun currentEntryId(ctx: Context): String? = str(ctx, "current_entry").ifEmpty { null }
    fun setCurrentEntryId(ctx: Context, id: String?) = put(ctx, "current_entry" to id)

    /** The collection new books are scanned into. Only a pointer — the list
     *  itself lives in [Collections], and a capture freezes its own copy of the
     *  name and "from" at start(), so changing the selection later never
     *  rewrites provenance on books already captured. */
    fun currentCollectionId(ctx: Context): String? =
        str(ctx, "current_collection").ifEmpty { null }
    fun setCurrentCollectionId(ctx: Context, id: String?) = put(ctx, "current_collection" to id)

    /** A terminal command accepted while CameraX owns a file must survive an
     * Activity replacement. Commit synchronously because this is a tiny state
     * transition whose durability is more important than avoiding a disk
     * flush on a button press. */
    fun setPendingCaptureCommand(
        ctx: Context,
        entryId: String?,
        command: String?,
        targetPage: Int? = null,
    ) {
        val valid = command?.takeIf { it in PENDING_CAPTURE_COMMANDS }
        val validTarget = targetPage?.takeIf { valid == "undo" && it > 0 } ?: -1
        sp(ctx).edit()
            .putString("pending_capture_entry", if (valid == null) "" else entryId.orEmpty())
            .putString("pending_capture_command", valid.orEmpty())
            .putInt("pending_capture_target_page", validTarget)
            .commit()
    }

    fun pendingCaptureCommand(ctx: Context, entryId: String?): String? {
        val storedEntry = str(ctx, "pending_capture_entry")
        val command = str(ctx, "pending_capture_command")
        if (storedEntry.isEmpty() || storedEntry != entryId ||
            command !in PENDING_CAPTURE_COMMANDS) {
            if (storedEntry.isNotEmpty() || command.isNotEmpty()) {
                setPendingCaptureCommand(ctx, null, null)
            }
            return null
        }
        return command
    }

    /** The exact accepted page owned by a deferred Undo. If that page never
     * commits (capture error/process death), Undo is already satisfied and
     * must not fall back to deleting an older photo or note. */
    fun pendingCaptureTargetPage(ctx: Context, entryId: String?): Int? {
        if (pendingCaptureCommand(ctx, entryId) != "undo") return null
        return sp(ctx).getInt("pending_capture_target_page", -1).takeIf { it > 0 }
    }

    fun clearPendingCaptureCommand(ctx: Context, entryId: String?) {
        if (str(ctx, "pending_capture_entry") == entryId.orEmpty()) {
            setPendingCaptureCommand(ctx, null, null)
        }
    }

    private val PENDING_CAPTURE_COMMANDS = setOf("done", "cancel", "restart", "undo")

    /** Last failure that retrying can't fix; the main screen shows these
     *  instead of counting work forever. */
    fun lastUploadError(ctx: Context): String? = str(ctx, "last_upload_error").ifEmpty { null }
    fun setLastUploadError(ctx: Context, m: String?) = put(ctx, "last_upload_error" to m)
    fun lastProcError(ctx: Context): String? = str(ctx, "last_proc_error").ifEmpty { null }
    fun setLastProcError(ctx: Context, m: String?) = put(ctx, "last_proc_error" to m)

    // --- explicit capture synchronization ----------------------------------

    /**
     * A capture upload is authorized only by this durable record. Keeping the
     * request id in both preferences and WorkManager input prevents lifecycle
     * nudges, old APK work, or a superseded batch from uploading anything.
     */
    internal fun captureSyncRecord(ctx: Context): CaptureSyncRecord? =
        synchronized(captureSyncLock) { captureSyncRecordLocked(ctx) }

    internal fun activeCaptureSyncRecord(ctx: Context): CaptureSyncRecord? =
        synchronized(captureSyncLock) {
            captureSyncRecordLocked(ctx)?.takeIf { it.phase.active }
        }

    /** Freeze the eligible ids at button-press time. A repeated press while
     * the batch is active reuses the same identity and target set. */
    internal fun beginCaptureSync(
        ctx: Context,
        targetIds: Collection<String>,
    ): CaptureSyncStart = synchronized(captureSyncLock) {
        val start = beginCaptureSyncRecord(
            existing = captureSyncRecordLocked(ctx),
            targetIds = targetIds,
            newRequestId = UUID.randomUUID().toString(),
            transportMode = transport(ctx),
            lanHost = lanHost(ctx),
            cloudOwner = userId(ctx),
        )
        if (!start.created) return@synchronized start
        check(writeCaptureSyncRecordLocked(ctx, start.record)) {
            "Could not persist capture sync request"
        }
        start
    }

    /** Settings may repair an undelivered batch. Once one capture has landed,
     * its transport/destination stays frozen so a single request cannot split
     * across desktops or cloud accounts. */
    internal fun refreshUndeliveredCaptureSyncDestination(ctx: Context): CaptureSyncRecord? =
        synchronized(captureSyncLock) {
            val current = captureSyncRecordLocked(ctx)?.takeIf { it.phase.active }
                ?: return@synchronized null
            if (current.syncedIds.isNotEmpty()) return@synchronized current
            val mode = transport(ctx).takeIf { it in setOf("cloud", "lan", "auto") }
                ?: "cloud"
            val updated = current.copy(
                transportMode = mode,
                lanHost = lanHost(ctx),
                cloudOwner = userId(ctx),
                resolvedTransport = if (mode == "auto") "" else mode,
            )
            if (!writeCaptureSyncRecordLocked(ctx, updated)) return@synchronized current
            updated
        }

    internal fun resolveCaptureSyncTransport(
        ctx: Context,
        requestId: String,
        transport: String,
    ): String? = synchronized(captureSyncLock) {
        if (transport !in setOf("cloud", "lan")) return@synchronized null
        val current = captureSyncRecordLocked(ctx)
            ?.takeIf { it.requestId == requestId && it.phase.active }
            ?: return@synchronized null
        if (current.resolvedTransport.isNotEmpty()) {
            return@synchronized current.resolvedTransport
        }
        if (current.transportMode != "auto") return@synchronized null
        val updated = current.copy(resolvedTransport = transport)
        if (!writeCaptureSyncRecordLocked(ctx, updated)) return@synchronized null
        transport
    }

    internal fun setCaptureSyncPhase(
        ctx: Context,
        requestId: String,
        phase: CaptureSyncPhase,
    ): Boolean = synchronized(captureSyncLock) {
        val current = captureSyncRecordLocked(ctx)
            ?.takeIf { it.requestId == requestId } ?: return@synchronized false
        writeCaptureSyncRecordLocked(ctx, current.copy(phase = phase))
    }

    internal fun markCaptureSynced(
        ctx: Context,
        requestId: String,
        entryId: String,
    ): Boolean = synchronized(captureSyncLock) {
        val current = captureSyncRecordLocked(ctx)
            ?.takeIf { it.requestId == requestId && entryId in it.targetIds }
            ?: return@synchronized false
        writeCaptureSyncRecordLocked(
            ctx,
            current.copy(
                syncedIds = current.syncedIds + entryId,
                blockedIds = current.blockedIds - entryId,
            ),
        )
    }

    internal fun markCaptureSyncBlocked(
        ctx: Context,
        requestId: String,
        entryId: String,
    ): Boolean = synchronized(captureSyncLock) {
        val current = captureSyncRecordLocked(ctx)
            ?.takeIf {
                it.requestId == requestId && entryId in it.targetIds &&
                    entryId !in it.syncedIds
            } ?: return@synchronized false
        writeCaptureSyncRecordLocked(
            ctx,
            current.copy(blockedIds = current.blockedIds + entryId),
        )
    }

    private fun captureSyncRecordLocked(ctx: Context): CaptureSyncRecord? {
        val prefs = sp(ctx)
        val requestId = prefs.getString(CAPTURE_SYNC_REQUEST_ID, "").orEmpty().trim()
        if (requestId.isEmpty()) return null
        val targets = normalizedCaptureSyncIds(
            prefs.getStringSet(CAPTURE_SYNC_TARGET_IDS, emptySet()).orEmpty(),
        )
        val synced = normalizedCaptureSyncIds(
            prefs.getStringSet(CAPTURE_SYNC_SYNCED_IDS, emptySet()).orEmpty(),
        ).intersect(targets)
        val blocked = normalizedCaptureSyncIds(
            prefs.getStringSet(CAPTURE_SYNC_BLOCKED_IDS, emptySet()).orEmpty(),
        ).intersect(targets - synced)
        val mode = prefs.getString(CAPTURE_SYNC_TRANSPORT_MODE, null)
            ?.takeIf { it in setOf("cloud", "lan", "auto") }
            ?: transport(ctx)
        val resolved = prefs.getString(CAPTURE_SYNC_RESOLVED_TRANSPORT, null)
            ?.takeIf { it in setOf("cloud", "lan") }
            ?: if (mode == "auto") "" else mode
        return CaptureSyncRecord(
            requestId = requestId,
            phase = CaptureSyncPhase.fromStoredValue(
                prefs.getString(CAPTURE_SYNC_PHASE, null),
            ),
            targetIds = targets,
            syncedIds = synced,
            blockedIds = blocked,
            transportMode = mode,
            lanHost = prefs.getString(CAPTURE_SYNC_LAN_HOST, null) ?: lanHost(ctx),
            cloudOwner = prefs.getString(CAPTURE_SYNC_CLOUD_OWNER, null) ?: userId(ctx),
            resolvedTransport = resolved,
        )
    }

    private fun writeCaptureSyncRecordLocked(
        ctx: Context,
        record: CaptureSyncRecord,
    ): Boolean = sp(ctx).edit()
        .putString(CAPTURE_SYNC_REQUEST_ID, record.requestId)
        .putString(CAPTURE_SYNC_PHASE, record.phase.storedValue)
        .putStringSet(CAPTURE_SYNC_TARGET_IDS, record.targetIds.toSet())
        .putStringSet(CAPTURE_SYNC_SYNCED_IDS, record.syncedIds.toSet())
        .putStringSet(CAPTURE_SYNC_BLOCKED_IDS, record.blockedIds.toSet())
        .putString(CAPTURE_SYNC_TRANSPORT_MODE, record.transportMode)
        .putString(CAPTURE_SYNC_LAN_HOST, record.lanHost)
        .putString(CAPTURE_SYNC_CLOUD_OWNER, record.cloudOwner)
        .putString(CAPTURE_SYNC_RESOLVED_TRANSPORT, record.resolvedTransport)
        .commit()

    // --- session ---------------------------------------------------------------

    fun accessToken(ctx: Context): String = str(ctx, "access_token")
    fun refreshToken(ctx: Context): String = str(ctx, "refresh_token")
    fun tokenExpiry(ctx: Context): Long = sp(ctx).getLong("token_expiry", 0L)
    fun userId(ctx: Context): String = str(ctx, "user_id")
    fun email(ctx: Context): String = str(ctx, "email")
    fun displayName(ctx: Context): String = if (userId(ctx).isNotEmpty()) {
        str(ctx, "display_name")
    } else {
        localProfileValue(ctx, "local_display_name", "display_name")
    }
    fun setDisplayName(ctx: Context, v: String) = put(
        ctx,
        (if (userId(ctx).isNotEmpty()) "display_name" else "local_display_name") to v.trim(),
    )

    fun setSession(ctx: Context, access: String, refresh: String,
                   expiresAtMs: Long, userId: String, email: String) {
        synchronized(profileLock) {
            val previousOwner = this.userId(ctx)
            val cachedProfileOwner = previousOwner.ifEmpty { pendingProfileOwner(ctx) }
            val edit = sp(ctx).edit()
                .putString("access_token", access)
                .putString("refresh_token", refresh)
                .putLong("token_expiry", expiresAtMs)
                .putString("user_id", userId)
                .putString("email", email)
            if (previousOwner.isEmpty()) {
                for ((legacy, local) in listOf(
                    "display_name" to "local_display_name",
                    "mistral_key" to "local_mistral_key",
                    "deepseek_key" to "local_deepseek_key",
                )) {
                    val value = str(ctx, legacy)
                    if (value.isNotEmpty() && str(ctx, local).isEmpty()) {
                        edit.putString(local, value)
                    }
                    edit.remove(legacy)
                }
            }
            if (cachedProfileOwner.isNotEmpty() && cachedProfileOwner != userId) {
                edit.remove("display_name")
                    .remove("mistral_key").remove("deepseek_key")
                    .remove("profile_pending_fields").remove("profile_pending_owner")
                    .remove("profile_sync_error")
            }
            edit.apply()
        }
    }

    fun clearSession(ctx: Context) {
        synchronized(profileLock) {
            sp(ctx).edit()
                .remove("access_token").remove("refresh_token").remove("token_expiry")
                .remove("user_id").remove("email").remove("display_name")
                .remove("mistral_key").remove("deepseek_key")
                .remove("pkce_verifier")
                .remove("profile_pending_fields").remove("profile_pending_owner")
                .remove("profile_sync_error")
                .apply()
        }
    }

    // --- processing keys ---------------------------------------------------------
    // Account cache and device-local mode are deliberately separate. Signing out
    // must not leak an account's secrets, and must not erase keys a LAN-only user
    // explicitly saved for this phone.

    fun mistralKey(ctx: Context): String = if (userId(ctx).isNotEmpty()) {
        str(ctx, "mistral_key")
    } else {
        localProfileValue(ctx, "local_mistral_key", "mistral_key")
    }
    fun deepseekKey(ctx: Context): String = if (userId(ctx).isNotEmpty()) {
        str(ctx, "deepseek_key")
    } else {
        localProfileValue(ctx, "local_deepseek_key", "deepseek_key")
    }
    fun setApiKeys(ctx: Context, mistral: String, deepseek: String) = if (userId(ctx).isNotEmpty()) {
        put(ctx, "mistral_key" to mistral.trim(), "deepseek_key" to deepseek.trim())
    } else {
        put(ctx, "local_mistral_key" to mistral.trim(), "local_deepseek_key" to deepseek.trim())
    }

    /** One-time migration for values saved while signed out by older builds.
     * Account logout removes the legacy cache first, so it is never adopted as
     * a device-local secret accidentally. */
    private fun localProfileValue(ctx: Context, localKey: String, legacyKey: String): String =
        str(ctx, localKey).ifEmpty {
            str(ctx, legacyKey).also { legacy ->
                if (legacy.isNotEmpty()) {
                    sp(ctx).edit().putString(localKey, legacy).remove(legacyKey).apply()
                }
            }
        }

    // --- deferred profile synchronization -----------------------------------

    const val PROFILE_DISPLAY_NAME = "display_name"
    const val PROFILE_MISTRAL = "mistral"
    const val PROFILE_DEEPSEEK = "deepseek"
    private val PROFILE_FIELDS = setOf(PROFILE_DISPLAY_NAME, PROFILE_MISTRAL, PROFILE_DEEPSEEK)

    fun pendingProfileFields(ctx: Context): Set<String> =
        sp(ctx).getStringSet("profile_pending_fields", emptySet())
            .orEmpty().filterTo(mutableSetOf()) { it in PROFILE_FIELDS }

    fun pendingProfileOwner(ctx: Context): String = str(ctx, "profile_pending_owner")

    fun markProfilePending(ctx: Context, fields: Set<String>) {
        synchronized(profileLock) {
            val valid = fields.filterTo(mutableSetOf()) { it in PROFILE_FIELDS }
            val owner = userId(ctx)
            if (valid.isEmpty() || owner.isEmpty()) return
            sp(ctx).edit()
                .putStringSet("profile_pending_fields", pendingProfileFields(ctx) + valid)
                .putString("profile_pending_owner", owner)
                .remove("profile_sync_error")
                .apply()
        }
    }

    fun clearProfilePending(ctx: Context, fields: Set<String>, expectedOwner: String? = null) {
        synchronized(profileLock) {
            if (expectedOwner != null &&
                (pendingProfileOwner(ctx) != expectedOwner || userId(ctx) != expectedOwner)) return
            val remaining = pendingProfileFields(ctx) - fields
            val edit = sp(ctx).edit().putStringSet("profile_pending_fields", remaining)
            if (remaining.isEmpty()) {
                edit.remove("profile_pending_owner").remove("profile_sync_error")
            }
            edit.apply()
        }
    }

    fun profileSyncError(ctx: Context): String? = str(ctx, "profile_sync_error").ifEmpty { null }
    fun setProfileSyncError(ctx: Context, message: String?, expectedOwner: String? = null) {
        synchronized(profileLock) {
            if (expectedOwner != null &&
                (pendingProfileOwner(ctx) != expectedOwner || userId(ctx) != expectedOwner)) return
            put(ctx, "profile_sync_error" to message)
        }
    }

    /** One atomic local commit: cloud hydration cannot land between values and
     *  their pending flags and silently replace the user's edit. */
    fun saveProfileLocally(
        ctx: Context,
        displayName: String,
        mistral: String,
        deepseek: String,
        pendingFields: Set<String>,
    ) {
        synchronized(profileLock) {
            val owner = userId(ctx)
            val valid = pendingFields.filterTo(mutableSetOf()) {
                owner.isNotEmpty() && it in PROFILE_FIELDS
            }
            val edit = sp(ctx).edit()
            if (owner.isEmpty()) {
                edit.putString("local_display_name", displayName.trim())
                    .putString("local_mistral_key", mistral.trim())
                    .putString("local_deepseek_key", deepseek.trim())
            } else {
                edit.putString("display_name", displayName.trim())
                    .putString("mistral_key", mistral.trim())
                    .putString("deepseek_key", deepseek.trim())
            }
            if (valid.isNotEmpty()) {
                edit.putStringSet("profile_pending_fields", pendingProfileFields(ctx) + valid)
                    .putString("profile_pending_owner", owner)
                    .remove("profile_sync_error")
            }
            edit.apply()
        }
    }

    /** Merge a cloud snapshot only into fields that are not locally pending.
     *  The account is checked again after the network request completes. */
    fun applyCloudProfile(
        ctx: Context,
        ownerId: String,
        displayName: String?,
        mistral: String?,
        deepseek: String?,
    ) {
        synchronized(profileLock) {
            if (userId(ctx) != ownerId) return
            val pending = pendingProfileFields(ctx)
                .takeIf { pendingProfileOwner(ctx) == ownerId }.orEmpty()
            val edit = sp(ctx).edit()
            if (displayName != null && PROFILE_DISPLAY_NAME !in pending)
                edit.putString("display_name", displayName.trim())
            if (mistral != null && PROFILE_MISTRAL !in pending)
                edit.putString("mistral_key", mistral.trim())
            if (deepseek != null && PROFILE_DEEPSEEK !in pending)
                edit.putString("deepseek_key", deepseek.trim())
            edit.apply()
        }
    }
}
