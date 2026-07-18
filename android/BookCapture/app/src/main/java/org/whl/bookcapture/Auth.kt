package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL
import java.time.Instant

internal data class ProfileApiKeys(val mistral: String, val deepseek: String)

internal fun mergeProfileApiKeys(
    latest: ProfileApiKeys,
    mistralEdit: String?,
    deepseekEdit: String?,
): ProfileApiKeys = ProfileApiKeys(
    mistral = mistralEdit?.trim() ?: latest.mistral,
    deepseek = deepseekEdit?.trim() ?: latest.deepseek,
)

internal fun profileUpdatedAtFilter(updatedAt: String?): String =
    if (updatedAt.isNullOrBlank()) {
        "updated_at=is.null"
    } else {
        "updated_at=eq.${URLEncoder.encode(updatedAt, Charsets.UTF_8.name())}"
    }

/**
 * Supabase Auth over plain REST — the same accounts the desktop signs in
 * with. The session is persistent: the refresh token lives in Prefs and
 * [accessToken] silently renews the (hour-lived) access token, so login
 * happens once per device, not per use.
 *
 * After sign-in the account's cloud profile is pulled: display name from
 * `profiles` (public to signed-in users) and API keys from `profile_secrets`
 * (readable by exactly the owner). Both are cached in Prefs so capture and
 * background processing work offline.
 */
object Auth {

    fun signedIn(ctx: Context): Boolean = Prefs.refreshToken(ctx).isNotEmpty()

    /** Sign in; returns null or a user-readable error. Blocking. */
    fun signIn(ctx: Context, email: String, password: String): String? =
        session(ctx, "token?grant_type=password",
                JSONObject().put("email", email).put("password", password))

    /** Revoke the server-side session when reachable, then always leave the
     * device in local mode. Blocking; callers run this off the main thread. */
    fun signOut(ctx: Context): String? {
        val token = accessToken(ctx)
        var error: String? = null
        if (token != null) {
            try {
                val c = conn(
                    "POST",
                    "${Prefs.supabaseUrl(ctx)}/auth/v1/logout?scope=local",
                    Prefs.anonKey(ctx),
                    token,
                )
                c.doOutput = true
                c.outputStream.use { it.write(byteArrayOf()) }
                finish(c)
            } catch (e: HttpError) {
                error = e.readable()
            } catch (e: Exception) {
                error = e.message ?: e.javaClass.simpleName
            }
        }
        Prefs.clearSession(ctx)
        return error
    }

    /** A live access token, silently refreshed when within a minute of
     *  expiry; null when signed out or the refresh token was revoked.
     *  Blocking; call off the main thread. */
    @Synchronized
    fun accessToken(ctx: Context): String? {
        if (!signedIn(ctx)) return null
        val access = Prefs.accessToken(ctx)
        if (access.isNotEmpty() && System.currentTimeMillis() < Prefs.tokenExpiry(ctx) - 60_000)
            return access
        val err = session(ctx, "token?grant_type=refresh_token",
                          JSONObject().put("refresh_token", Prefs.refreshToken(ctx)))
        return if (err == null) Prefs.accessToken(ctx).ifEmpty { null } else null
    }

    /** POST an auth endpoint; on success store the returned session and pull
     *  the profile. Returns null or a readable error. */
    private fun session(ctx: Context, path: String, body: JSONObject): String? {
        val data: JSONObject = try {
            post("${Prefs.supabaseUrl(ctx)}/auth/v1/$path", Prefs.anonKey(ctx), body)
        } catch (e: HttpError) {
            // 400/401 on refresh = the session is dead for good: force re-login
            // rather than failing every upload forever with a stale token
            if (path.startsWith("token?grant_type=refresh") && e.code in 400..401)
                Prefs.clearSession(ctx)
            return e.readable()
        } catch (e: Exception) {
            return e.message ?: e.javaClass.simpleName
        }
        val access = data.optString("access_token")
        if (access.isEmpty()) return null           // signup pending confirmation
        val user = data.optJSONObject("user") ?: JSONObject()
        Prefs.setSession(
            ctx, access,
            data.optString("refresh_token"),
            System.currentTimeMillis() + data.optLong("expires_in", 3600) * 1000,
            user.optString("id"), user.optString("email"))
        try { pullProfile(ctx) } catch (_: Exception) { /* cache fills next time */ }
        return null
    }

    // --- cloud profile ----------------------------------------------------------

    /** Refresh the local cache of display name + API keys. Blocking. */
    fun pullProfile(ctx: Context) {
        val uid = Prefs.userId(ctx).ifEmpty { return }
        val token = accessToken(ctx) ?: return
        // A sign-out/sign-in can complete while accessToken refreshes. Never
        // combine one account's bearer token with another account's row id.
        if (Prefs.userId(ctx) != uid) return
        val base = Prefs.supabaseUrl(ctx)
        val profile = rest(ctx, "GET",
            "$base/rest/v1/profiles?id=eq.$uid&select=display_name", token)
            .optJSONObject(0)
        val secrets = rest(ctx, "GET",
            "$base/rest/v1/profile_secrets?id=eq.$uid&select=api_keys", token)
            .optJSONObject(0)
        val keys = secrets?.optJSONObject("api_keys")
        Prefs.applyCloudProfile(
            ctx = ctx,
            ownerId = uid,
            displayName = profile?.optString("display_name"),
            // A missing row means this account has no keys; preserve the old
            // behavior of clearing a stale cache while pending edits remain
            // protected inside applyCloudProfile.
            mistral = keys?.optString("mistral") ?: "",
            deepseek = keys?.optString("deepseek") ?: "",
        )
    }

    /** Push only fields the user actually edited. API keys share one JSONB
     *  value, so fetch and merge its latest cloud value before writing; a
     *  stale phone editing DeepSeek must not erase a newer Mistral key.
     *  Local intent is persisted by the caller before this blocking sync. */
    fun pushProfile(ctx: Context, expectedOwner: String? = null,
                    displayName: String? = null, mistral: String? = null,
                    deepseek: String? = null): String? {
        if (displayName == null && mistral == null && deepseek == null) return null
        val token = accessToken(ctx) ?: return "signed out"
        val uid = Prefs.userId(ctx)
        if (uid.isEmpty() || expectedOwner != null && uid != expectedOwner)
            return "account changed"
        val base = Prefs.supabaseUrl(ctx)
        return try {
            displayName?.let {
                upsert(ctx, "$base/rest/v1/profiles?on_conflict=id", token,
                       JSONObject().put("id", uid).put("display_name", it.trim()))
            }
            if (mistral != null || deepseek != null) {
                updateProfileSecrets(ctx, base, token, uid, mistral, deepseek)
            }
            null
        } catch (e: HttpError) {
            e.readable()
        } catch (e: Exception) {
            e.message ?: e.javaClass.simpleName
        }
    }

    /** profile_secrets.api_keys is one JSONB value. A read/merge/upsert loses
     * edits when two devices race, so use updated_at as an optimistic lock.
     * Each conflict re-reads the winner, reapplies only this device's edited
     * fields, and retries a bounded number of times; WorkManager handles a
     * longer-lived contention or network failure later. */
    private fun updateProfileSecrets(
        ctx: Context,
        base: String,
        token: String,
        uid: String,
        mistralEdit: String?,
        deepseekEdit: String?,
    ) {
        repeat(4) {
            if (Prefs.userId(ctx) != uid) throw IOException("account changed")
            val current = rest(ctx, "GET",
                "$base/rest/v1/profile_secrets?id=eq.$uid&select=api_keys,updated_at", token)
                .optJSONObject(0)
            val latestKeys = current?.optJSONObject("api_keys") ?: JSONObject()
            val merged = mergeProfileApiKeys(
                latest = ProfileApiKeys(
                    mistral = latestKeys.optString("mistral"),
                    deepseek = latestKeys.optString("deepseek"),
                ),
                mistralEdit = mistralEdit,
                deepseekEdit = deepseekEdit,
            )
            // Preserve future/desktop-owned JSON keys instead of replacing the
            // object with the two fields Android currently understands.
            latestKeys.put("mistral", merged.mistral)
            latestKeys.put("deepseek", merged.deepseek)
            val writtenAt = Instant.now().toString()

            val wrote = if (current == null) {
                insertProfileSecretsIfAbsent(
                    ctx, base, token,
                    JSONObject()
                        .put("id", uid)
                        .put("api_keys", latestKeys)
                        .put("updated_at", writtenAt),
                )
            } else {
                patchProfileSecretsIfUnchanged(
                    ctx, base, token, uid, current.optString("updated_at").ifBlank { null },
                    JSONObject()
                        .put("api_keys", latestKeys)
                        .put("updated_at", writtenAt),
                )
            }
            if (wrote) return
        }
        throw IOException("profile changed on another device; retrying")
    }

    private fun patchProfileSecretsIfUnchanged(
        ctx: Context,
        base: String,
        token: String,
        uid: String,
        updatedAt: String?,
        body: JSONObject,
    ): Boolean {
        val rows = writeReturning(
            ctx = ctx,
            method = "PATCH",
            url = "$base/rest/v1/profile_secrets?id=eq.$uid&${profileUpdatedAtFilter(updatedAt)}",
            token = token,
            body = body.toString(),
            prefer = "return=representation",
        )
        return rows.length() > 0
    }

    private fun insertProfileSecretsIfAbsent(
        ctx: Context,
        base: String,
        token: String,
        row: JSONObject,
    ): Boolean {
        val rows = writeReturning(
            ctx = ctx,
            method = "POST",
            url = "$base/rest/v1/profile_secrets?on_conflict=id",
            token = token,
            body = JSONArray().put(row).toString(),
            prefer = "resolution=ignore-duplicates,return=representation",
        )
        return rows.length() > 0
    }

    // --- HTTP ---------------------------------------------------------------------

    class HttpError(val code: Int, val body: String) : IOException("HTTP $code") {
        /** Auth errors come as {"error_description": ...} or {"msg"/"message": ...}. */
        fun readable(): String = try {
            val o = JSONObject(body)
            listOf("error_description", "msg", "message", "error")
                .firstNotNullOfOrNull { k -> o.optString(k).ifEmpty { null } }
                ?: "HTTP $code"
        } catch (_: Exception) { "HTTP $code: ${body.take(120)}" }
    }

    private fun conn(method: String, url: String, anon: String,
                     bearer: String): HttpURLConnection {
        val c = URL(url).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 20_000
        c.readTimeout = 30_000
        c.setRequestProperty("apikey", anon)
        c.setRequestProperty("Authorization", "Bearer $bearer")
        c.setRequestProperty("Content-Type", "application/json")
        return c
    }

    private fun finish(c: HttpURLConnection): String {
        val code = c.responseCode
        val body = try {
            (if (code in 200..299) c.inputStream else c.errorStream)
                ?.use { it.readBytes().decodeToString() } ?: ""
        } catch (_: Exception) { "" }
        if (code !in 200..299) throw HttpError(code, body)
        return body
    }

    private fun post(url: String, anon: String, body: JSONObject): JSONObject {
        val c = conn("POST", url, anon, anon)
        c.doOutput = true
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        val out = finish(c)
        return if (out.isBlank()) JSONObject() else JSONObject(out)
    }

    private fun rest(ctx: Context, method: String, url: String, token: String): JSONArray {
        val c = conn(method, url, Prefs.anonKey(ctx), token)
        val out = finish(c)
        return if (out.isBlank()) JSONArray() else JSONArray(out)
    }

    private fun writeReturning(
        ctx: Context,
        method: String,
        url: String,
        token: String,
        body: String,
        prefer: String,
    ): JSONArray {
        val c = conn(method, url, Prefs.anonKey(ctx), token)
        c.setRequestProperty("Prefer", prefer)
        c.doOutput = true
        c.outputStream.use { it.write(body.toByteArray()) }
        val out = finish(c)
        return if (out.isBlank()) JSONArray() else JSONArray(out)
    }

    private fun upsert(ctx: Context, url: String, token: String, row: JSONObject) {
        val c = conn("POST", url, Prefs.anonKey(ctx), token)
        c.setRequestProperty("Prefer", "resolution=merge-duplicates,return=minimal")
        c.doOutput = true
        c.outputStream.use { it.write("[$row]".toByteArray()) }
        finish(c)
    }
}
