package org.whl.bookcapture

import android.content.Context
import java.io.IOException

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
    /**
     * "Check for updates" is intentionally overloaded as a remote interface
     * resource sync while APK distribution is not certified. The semantic
     * release helpers above remain for the eventual installer, but this path
     * must not offer or install an APK yet.
     */
    sealed interface Result {
        object UiUpdated : Result
        object UiCurrent : Result
        object NotConfigured : Result
    }

    internal fun resultFor(refresh: RemoteUiCatalog.Refresh): Result = when (refresh) {
        RemoteUiCatalog.Refresh.CHANGED -> Result.UiUpdated
        RemoteUiCatalog.Refresh.UNCHANGED -> Result.UiCurrent
        RemoteUiCatalog.Refresh.EMPTY ->
            throw IOException("the remote interface catalog is empty")
    }

    /** Network call — must not run on the main thread. Throws on failure. */
    fun check(ctx: Context): Result {
        val base = Prefs.supabaseUrl(ctx)
        val key = Prefs.anonKey(ctx)
        if (base.isEmpty() || key.isEmpty()) return Result.NotConfigured
        return resultFor(RemoteUiCatalog.refresh(ctx, base, key))
    }
}
