package org.whl.bookcapture

import android.content.Context
import android.content.SharedPreferences

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

    // --- device --------------------------------------------------------------

    fun deviceName(ctx: Context): String =
        str(ctx, "device_name").ifEmpty { android.os.Build.MODEL ?: "phone" }

    fun setDeviceName(ctx: Context, v: String) = put(ctx, "device_name" to v.trim())

    /** The entry currently being captured, so UploadWorker's orphan recovery
     *  can tell "live" from "left behind by a crash". */
    fun currentEntryId(ctx: Context): String? = str(ctx, "current_entry").ifEmpty { null }
    fun setCurrentEntryId(ctx: Context, id: String?) = put(ctx, "current_entry" to id)

    /** Last failure that retrying can't fix; the main screen shows these
     *  instead of counting work forever. */
    fun lastUploadError(ctx: Context): String? = str(ctx, "last_upload_error").ifEmpty { null }
    fun setLastUploadError(ctx: Context, m: String?) = put(ctx, "last_upload_error" to m)
    fun lastProcError(ctx: Context): String? = str(ctx, "last_proc_error").ifEmpty { null }
    fun setLastProcError(ctx: Context, m: String?) = put(ctx, "last_proc_error" to m)

    // --- session ---------------------------------------------------------------

    fun accessToken(ctx: Context): String = str(ctx, "access_token")
    fun refreshToken(ctx: Context): String = str(ctx, "refresh_token")
    fun tokenExpiry(ctx: Context): Long = sp(ctx).getLong("token_expiry", 0L)
    fun userId(ctx: Context): String = str(ctx, "user_id")
    fun email(ctx: Context): String = str(ctx, "email")
    fun displayName(ctx: Context): String = str(ctx, "display_name")
    fun setDisplayName(ctx: Context, v: String) = put(ctx, "display_name" to v.trim())

    fun setSession(ctx: Context, access: String, refresh: String,
                   expiresAtMs: Long, userId: String, email: String) {
        sp(ctx).edit()
            .putString("access_token", access)
            .putString("refresh_token", refresh)
            .putLong("token_expiry", expiresAtMs)
            .putString("user_id", userId)
            .putString("email", email)
            .apply()
    }

    fun clearSession(ctx: Context) {
        sp(ctx).edit()
            .remove("access_token").remove("refresh_token").remove("token_expiry")
            .remove("user_id").remove("email").remove("display_name")
            .remove("mistral_key").remove("deepseek_key")
            .remove("pkce_verifier")
            .apply()
    }

    // --- API keys (cache of the cloud profile_secrets row) -----------------------

    fun mistralKey(ctx: Context): String = str(ctx, "mistral_key")
    fun deepseekKey(ctx: Context): String = str(ctx, "deepseek_key")
    fun setApiKeys(ctx: Context, mistral: String, deepseek: String) =
        put(ctx, "mistral_key" to mistral.trim(), "deepseek_key" to deepseek.trim())
}
