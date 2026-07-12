package org.whl.bookcapture

import android.content.Context
import android.net.Uri
import android.util.Base64
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.SecureRandom

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

    /** Create an account; returns null or an error. With email confirmation
     *  off (this project) signup returns a session directly. Blocking. */
    fun register(ctx: Context, email: String, password: String): String? {
        val err = session(ctx, "signup",
                          JSONObject().put("email", email).put("password", password))
        if (err == null && !signedIn(ctx))
            return "account created — confirm the email, then sign in"
        return err
    }

    fun signOut(ctx: Context) = Prefs.clearSession(ctx)

    // --- OAuth (Google / GitHub via Supabase PKCE) ------------------------------
    // The provider brokering happens server-side in GoTrue; the phone only opens
    // a browser tab and later redeems a one-time code. The token response is
    // byte-for-byte the password grant's, so it flows through session() unchanged.

    const val OAUTH_REDIRECT = "org.whl.bookcapture://auth-callback"

    /** URL to open in a browser tab to start provider sign-in. Stashes the PKCE
     *  verifier for [completeOAuth] to redeem against the redirect's code. */
    fun oauthAuthorizeUrl(ctx: Context, provider: String): String {
        val verifier = randomVerifier()
        Prefs.setPkceVerifier(ctx, verifier)
        val challenge = s256(verifier)
        return "${Prefs.supabaseUrl(ctx)}/auth/v1/authorize" +
            "?provider=$provider" +
            "&redirect_to=${Uri.encode(OAUTH_REDIRECT)}" +
            "&code_challenge=$challenge&code_challenge_method=s256"
    }

    /** Redeem the redirect's auth code for a session. Blocking; returns null or a
     *  readable error. Same response shape as signIn, so the stored session +
     *  pullProfile happen exactly as the password path. */
    fun completeOAuth(ctx: Context, code: String): String? {
        val verifier = Prefs.pkceVerifier(ctx)
        if (verifier.isEmpty()) return "sign-in expired — try again"
        val err = session(ctx, "token?grant_type=pkce",
                          JSONObject().put("auth_code", code).put("code_verifier", verifier))
        Prefs.setPkceVerifier(ctx, "")            // one-shot, win or lose
        return err
    }

    private fun randomVerifier(): String {
        val bytes = ByteArray(64).also { SecureRandom().nextBytes(it) }
        return Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }

    private fun s256(verifier: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray())
        return Base64.encodeToString(digest, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
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
        val token = accessToken(ctx) ?: return
        val base = Prefs.supabaseUrl(ctx)
        val uid = Prefs.userId(ctx)
        rest(ctx, "GET", "$base/rest/v1/profiles?id=eq.$uid&select=display_name", token)
            .optJSONObject(0)?.let { Prefs.setDisplayName(ctx, it.optString("display_name")) }
        val keys = rest(ctx, "GET",
                        "$base/rest/v1/profile_secrets?id=eq.$uid&select=api_keys", token)
            .optJSONObject(0)?.optJSONObject("api_keys") ?: JSONObject()
        Prefs.setApiKeys(ctx, keys.optString("mistral"), keys.optString("deepseek"))
    }

    /** Push profile fields to the cloud (and the local cache). Blocking;
     *  returns null or an error. */
    fun pushProfile(ctx: Context, displayName: String,
                    mistral: String, deepseek: String): String? {
        val token = accessToken(ctx) ?: return "signed out"
        val base = Prefs.supabaseUrl(ctx)
        val uid = Prefs.userId(ctx)
        return try {
            upsert(ctx, "$base/rest/v1/profiles?on_conflict=id", token,
                   JSONObject().put("id", uid).put("display_name", displayName.trim()))
            upsert(ctx, "$base/rest/v1/profile_secrets?on_conflict=id", token,
                   JSONObject().put("id", uid).put("api_keys",
                       JSONObject().put("mistral", mistral.trim())
                                   .put("deepseek", deepseek.trim())))
            Prefs.setDisplayName(ctx, displayName)
            Prefs.setApiKeys(ctx, mistral, deepseek)
            null
        } catch (e: HttpError) {
            e.readable()
        } catch (e: Exception) {
            e.message ?: e.javaClass.simpleName
        }
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

    private fun rest(ctx: Context, method: String, url: String, token: String): org.json.JSONArray {
        val c = conn(method, url, Prefs.anonKey(ctx), token)
        val out = finish(c)
        return if (out.isBlank()) org.json.JSONArray() else org.json.JSONArray(out)
    }

    private fun upsert(ctx: Context, url: String, token: String, row: JSONObject) {
        val c = conn("POST", url, Prefs.anonKey(ctx), token)
        c.setRequestProperty("Prefer", "resolution=merge-duplicates,return=minimal")
        c.doOutput = true
        c.outputStream.use { it.write("[$row]".toByteArray()) }
        finish(c)
    }
}
