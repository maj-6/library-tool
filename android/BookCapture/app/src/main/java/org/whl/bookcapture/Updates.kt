package org.whl.bookcapture

import android.content.Context
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URL

/** A published build of the app, as the cloud `releases` table records it. */
data class Release(val version: String, val channel: String, val url: String, val notes: String)

/**
 * Which prerelease channels a build should be offered.
 *
 * Stable users are never shown an alpha. A user already running a prerelease is
 * shown their own channel and stable, because the next thing published for them
 * could be either — an alpha line usually ends by becoming the stable release.
 */
internal fun channelsVisibleTo(currentVersion: String): Set<String> {
    val pre = currentVersion.substringAfter('-', "").substringBefore('.').lowercase()
    return if (pre.isEmpty()) setOf("stable") else setOf("stable", pre)
}

/**
 * Semantic-version precedence, enough of it for our tags.
 *
 * The parts that matter here and are easy to get wrong: numeric identifiers
 * compare as numbers, not text ("10" > "9"), and a prerelease sorts BEFORE the
 * release it leads to, so 0.5.1-alpha.5 < 0.5.1. Returns <0, 0 or >0.
 */
internal fun compareVersions(a: String, b: String): Int {
    fun core(v: String) = v.trim().removePrefix("v").substringBefore('-').substringBefore('+')
    fun pre(v: String) = v.trim().removePrefix("v").substringBefore('+')
        .substringAfter('-', "")

    val ac = core(a).split('.')
    val bc = core(b).split('.')
    for (i in 0 until maxOf(ac.size, bc.size)) {
        val x = ac.getOrNull(i)?.toIntOrNull() ?: 0
        val y = bc.getOrNull(i)?.toIntOrNull() ?: 0
        if (x != y) return x.compareTo(y)
    }

    val ap = pre(a)
    val bp = pre(b)
    if (ap.isEmpty() && bp.isEmpty()) return 0
    if (ap.isEmpty()) return 1          // a release outranks its own prereleases
    if (bp.isEmpty()) return -1

    val ai = ap.split('.')
    val bi = bp.split('.')
    for (i in 0 until maxOf(ai.size, bi.size)) {
        val x = ai.getOrNull(i) ?: return -1   // fewer identifiers sorts lower
        val y = bi.getOrNull(i) ?: return 1
        val xn = x.toIntOrNull()
        val yn = y.toIntOrNull()
        val cmp = when {
            xn != null && yn != null -> xn.compareTo(yn)
            xn != null -> -1                   // numeric sorts below alphanumeric
            yn != null -> 1
            else -> x.compareTo(y)
        }
        if (cmp != 0) return cmp
    }
    return 0
}

/**
 * The best upgrade among [releases] for someone running [currentVersion], or
 * null if they are already current. Pure so the choice is testable without a
 * network: the caller supplies whatever the query returned.
 */
internal fun pickUpdate(currentVersion: String, releases: List<Release>): Release? {
    val visible = channelsVisibleTo(currentVersion)
    return releases
        .filter { it.channel in visible && it.url.isNotBlank() }
        .filter { compareVersions(it.version, currentVersion) > 0 }
        .maxWithOrNull { x, y -> compareVersions(x.version, y.version) }
}

/**
 * Reads the published `releases` table.
 *
 * Deliberately does NOT go through [SupabaseClient]: that authorizes as the
 * signed-in user and throws when there isn't one, but `releases` is granted to
 * `anon` precisely so an update check works in local mode. Checking for updates
 * is the last thing that should require an account.
 */
object Updates {
    private const val LIMIT = 30

    /**
     * Outcome of a check. [NotConfigured] is a distinct case on purpose: a
     * from-source APK ships blank Supabase defaults, and reporting that as
     * "you have the newest build" would be a confident lie about a check that
     * never ran.
     */
    sealed interface Result {
        data class Available(val release: Release) : Result
        object UpToDate : Result
        object NotConfigured : Result
    }

    /** Network call — must not run on the main thread. Throws on failure. */
    fun check(ctx: Context, currentVersion: String = BuildConfig.VERSION_NAME): Result {
        val base = Prefs.supabaseUrl(ctx)
        val key = Prefs.anonKey(ctx)
        if (base.isEmpty() || key.isEmpty()) return Result.NotConfigured
        val found = latest(ctx, currentVersion, base, key)
        return if (found == null) Result.UpToDate else Result.Available(found)
    }

    private fun latest(
        ctx: Context,
        currentVersion: String,
        base: String,
        key: String,
    ): Release? {
        val url = "$base/rest/v1/releases?platform=eq.android" +
            "&select=version,channel,url,notes&order=published_at.desc&limit=$LIMIT"
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = "GET"
        conn.connectTimeout = 15_000
        conn.readTimeout = 20_000
        conn.setRequestProperty("apikey", key)
        conn.setRequestProperty("Authorization", "Bearer $key")
        val body = try {
            val code = conn.responseCode
            // A refusal is a failed check, not an absence of updates — let it
            // surface as an error rather than a reassuring "you're current".
            if (code !in 200..299) throw java.io.IOException("releases: HTTP $code")
            conn.inputStream.use { it.readBytes().decodeToString() }
        } finally {
            conn.disconnect()
        }
        val array = JSONArray(body)
        val releases = (0 until array.length()).mapNotNull { i ->
            val row = array.optJSONObject(i) ?: return@mapNotNull null
            val version = row.optString("version").trim()
            if (version.isEmpty()) null else Release(
                version = version,
                channel = row.optString("channel").trim().ifEmpty { "stable" },
                url = row.optString("url").trim(),
                notes = row.optString("notes").trim(),
            )
        }
        return pickUpdate(currentVersion, releases)
    }
}
