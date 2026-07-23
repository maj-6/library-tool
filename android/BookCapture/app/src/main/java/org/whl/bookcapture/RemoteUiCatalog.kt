package org.whl.bookcapture

import android.app.Activity
import android.app.Application
import android.app.Dialog
import android.content.Context
import android.content.res.Configuration
import android.graphics.BitmapFactory
import android.graphics.drawable.BitmapDrawable
import android.os.Bundle
import android.view.Menu
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.ImageView
import android.widget.TextView
import com.google.android.material.button.MaterialButton
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.lang.ref.WeakReference
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.Base64
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap

internal const val REMOTE_UI_SCHEMA = 1
internal const val REMOTE_UI_MAX_RESPONSE_BYTES = 768 * 1024
internal const val REMOTE_UI_MAX_ICON_BYTES = 128 * 1024

internal data class RemoteUiIcon(
    val mime: String,
    val sha256: String,
    val bytes: ByteArray,
)

internal data class RemoteUiSnapshot(
    val revision: Long,
    val strings: Map<String, String>,
    val icons: Map<String, RemoteUiIcon>,
) {
    companion object {
        val EMPTY = RemoteUiSnapshot(0, emptyMap(), emptyMap())
    }
}

// String resources conventionally use snake_case; view/menu IDs in this app
// use lowerCamelCase. Both are valid Android resource entry names.
private val remoteUiKey = Regex("[a-z][A-Za-z0-9_]{0,95}")
private val sha256Hex = Regex("[0-9a-f]{64}")

internal fun formatRemoteUiText(
    locale: Locale,
    remoteTemplate: String,
    packagedTemplate: String,
    vararg args: Any,
): String = runCatching { String.format(locale, remoteTemplate, *args) }
    .getOrElse { String.format(locale, packagedTemplate, *args) }

/** Parse and validate the exact PostgREST response cached by the app. */
internal fun parseRemoteUiResponse(body: String): RemoteUiSnapshot? {
    require(body.toByteArray(Charsets.UTF_8).size <= REMOTE_UI_MAX_RESPONSE_BYTES) {
        "remote UI response is too large"
    }
    val rows = JSONArray(body)
    if (rows.length() == 0) return null
    val row = rows.getJSONObject(0)
    val revision = row.getLong("revision")
    require(revision > 0) { "remote UI revision must be positive" }
    val catalog = row.getJSONObject("catalog")
    require(catalog.optInt("schema", -1) == REMOTE_UI_SCHEMA) {
        "unsupported remote UI schema"
    }

    val strings = linkedMapOf<String, String>()
    val stringObject = catalog.optJSONObject("strings") ?: JSONObject()
    require(stringObject.length() <= 500) { "too many remote strings" }
    for (key in stringObject.keys()) {
        require(remoteUiKey.matches(key)) { "invalid remote string key: $key" }
        val value = stringObject.get(key)
        require(value is String) { "remote string $key is not text" }
        require(value.length <= 4096) { "remote string $key is too long" }
        strings[key] = value
    }

    val icons = linkedMapOf<String, RemoteUiIcon>()
    val iconObject = catalog.optJSONObject("icons") ?: JSONObject()
    require(iconObject.length() <= 100) { "too many remote icons" }
    for (key in iconObject.keys()) {
        require(remoteUiKey.matches(key)) { "invalid remote icon key: $key" }
        val value = iconObject.getJSONObject(key)
        val mime = value.optString("mime").trim().lowercase(Locale.ROOT)
        require(mime == "image/png") { "remote icon $key must be a PNG" }
        val expectedHash = value.optString("sha256").trim().lowercase(Locale.ROOT)
        require(sha256Hex.matches(expectedHash)) { "remote icon $key has an invalid hash" }
        val bytes = try {
            Base64.getDecoder().decode(value.getString("data"))
        } catch (exc: IllegalArgumentException) {
            throw IllegalArgumentException("remote icon $key is not valid base64", exc)
        }
        require(bytes.isNotEmpty() && bytes.size <= REMOTE_UI_MAX_ICON_BYTES) {
            "remote icon $key has an invalid size"
        }
        val actualHash = MessageDigest.getInstance("SHA-256")
            .digest(bytes).joinToString("") { "%02x".format(it) }
        require(actualHash == expectedHash) { "remote icon $key failed its hash check" }
        icons[key] = RemoteUiIcon(mime, expectedHash, bytes)
    }
    return RemoteUiSnapshot(revision, strings, icons)
}

internal class RemoteUiValueCache<K, V> {
    private data class Entry<K, V>(val key: K, val value: V)

    @Volatile
    private var entry: Entry<K, V>? = null

    fun getOrBuild(key: K, build: () -> V): V {
        entry?.takeIf { it.key == key }?.let { return it.value }
        synchronized(this) {
            entry?.takeIf { it.key == key }?.let { return it.value }
            return build().also { entry = Entry(key, it) }
        }
    }

    fun clear() {
        synchronized(this) {
            entry = null
        }
    }
}

/**
 * A deliberately small runtime overlay for packaged Android resources.
 *
 * Android cannot replace resources inside an installed APK. This catalog
 * instead overrides visible text and in-app ImageViews, MaterialButton icons,
 * and menu icons by resource entry name.
 * The signed launcher icon remains build-managed; downloaded PNGs are bounded,
 * hashed, decoded locally, and never executed.
 */
object RemoteUiCatalog {
    private data class PackagedStringCacheKey(
        val revision: Long,
        val packageName: String,
        val configuration: Configuration,
    )

    private const val CACHE_NAME = "remote_ui_catalog.json"
    private val lock = Any()
    private val drawableCache = ConcurrentHashMap<String, BitmapDrawable>()
    private val packagedStringCache =
        RemoteUiValueCache<PackagedStringCacheKey, Map<String, String>>()
    @Volatile private var loaded = false
    @Volatile private var current = RemoteUiSnapshot.EMPTY

    enum class Refresh { CHANGED, UNCHANGED, EMPTY }

    fun initialize(ctx: Context) {
        if (loaded) return
        synchronized(lock) {
            if (loaded) return
            current = runCatching {
                val cache = File(ctx.filesDir, CACHE_NAME)
                if (cache.isFile) parseRemoteUiResponse(cache.readText()) else null
            }.getOrNull() ?: RemoteUiSnapshot.EMPTY
            loaded = true
        }
    }

    /** Network call; callers must use a worker thread. */
    fun refresh(ctx: Context, base: String, publishableKey: String): Refresh {
        initialize(ctx)
        val url = base.trimEnd('/') +
            "/rest/v1/android_ui_catalog?id=eq.current&select=revision,catalog&limit=1"
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = "GET"
        conn.connectTimeout = 15_000
        conn.readTimeout = 20_000
        conn.setRequestProperty("apikey", publishableKey)
        // Legacy anon keys are JWTs. Modern publishable keys identify the app
        // through `apikey` alone and are not valid Bearer tokens.
        if (publishableKey.count { it == '.' } == 2) {
            conn.setRequestProperty("Authorization", "Bearer $publishableKey")
        }
        val body = try {
            val code = conn.responseCode
            if (code !in 200..299) throw IOException("android_ui_catalog: HTTP $code")
            conn.inputStream.use { input ->
                val output = ByteArrayOutputStream()
                val buffer = ByteArray(8192)
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    output.write(buffer, 0, read)
                    if (output.size() > REMOTE_UI_MAX_RESPONSE_BYTES) {
                        throw IOException("android_ui_catalog response is too large")
                    }
                }
                output.toString(Charsets.UTF_8.name())
            }
        } finally {
            conn.disconnect()
        }
        val snapshot = parseRemoteUiResponse(body) ?: return Refresh.EMPTY
        if (snapshot.revision == current.revision) return Refresh.UNCHANGED

        synchronized(lock) {
            if (snapshot.revision == current.revision) return Refresh.UNCHANGED
            val target = File(ctx.filesDir, CACHE_NAME)
            val temporary = File(ctx.filesDir, "$CACHE_NAME.tmp")
            temporary.writeText(body)
            if (!temporary.renameTo(target)) {
                target.delete()
                check(temporary.renameTo(target)) { "could not install remote UI catalog" }
            }
            current = snapshot
            drawableCache.clear()
            packagedStringCache.clear()
        }
        RemoteUiLifecycle.applyCurrent()
        return Refresh.CHANGED
    }

    /** Best effort is intentional: a catalog outage must not hide APK updates. */
    fun refreshBestEffort(ctx: Context, base: String, publishableKey: String): Refresh? =
        runCatching { refresh(ctx, base, publishableKey) }.getOrNull()

    fun text(ctx: Context, resourceId: Int, vararg args: Any): String {
        initialize(ctx)
        val name = runCatching { ctx.resources.getResourceEntryName(resourceId) }.getOrNull()
        val template = name?.let(current.strings::get) ?: ctx.getString(resourceId)
        if (args.isEmpty()) return template
        val locale = ctx.resources.configuration.locales[0]
        return formatRemoteUiText(locale, template, ctx.getString(resourceId), *args)
    }

    fun apply(root: View) {
        initialize(root.context)
        val snapshot = current
        if (snapshot == RemoteUiSnapshot.EMPTY) return
        val packaged = packagedStringOverrides(root.context, snapshot)
        visit(root) { view ->
            val idName = view.id.takeIf { it != View.NO_ID }?.let {
                runCatching { view.resources.getResourceEntryName(it) }.getOrNull()
            }
            if (view is TextView) {
                val direct = idName?.let(snapshot.strings::get)
                if (view !is EditText) {
                    direct?.let { view.text = it }
                        ?: packaged[view.text.toString()]?.let { view.text = it }
                }
                packaged[view.hint?.toString()]?.let { view.hint = it }
                val content = idName?.let { snapshot.strings["${it}_description"] }
                    ?: packaged[view.contentDescription?.toString()]
                content?.let { view.contentDescription = it }
            }
            if (view is ImageView && idName != null) {
                drawable(root.context, snapshot.icons[idName])?.let(view::setImageDrawable)
                snapshot.strings["${idName}_description"]?.let {
                    view.contentDescription = it
                }
            }
            if (view is MaterialButton && idName != null) {
                drawable(root.context, snapshot.icons[idName])?.let { view.icon = it }
            }
        }
    }

    /** Apply after show(), when Android has inflated the title and action buttons. */
    fun apply(dialog: Dialog) {
        dialog.window?.decorView?.let(::apply)
    }

    fun apply(ctx: Context, menu: Menu) {
        initialize(ctx)
        val snapshot = current
        val packaged = packagedStringOverrides(ctx, snapshot)
        for (i in 0 until menu.size()) {
            val item = menu.getItem(i)
            val idName = item.itemId.takeIf { it != View.NO_ID }?.let {
                runCatching { ctx.resources.getResourceEntryName(it) }.getOrNull()
            }
            val text = idName?.let(snapshot.strings::get) ?: packaged[item.title.toString()]
            text?.let { item.title = it }
            drawable(ctx, idName?.let(snapshot.icons::get))?.let { item.icon = it }
            if (item.hasSubMenu()) apply(ctx, item.subMenu!!)
        }
    }

    private fun packagedStringOverrides(
        ctx: Context,
        snapshot: RemoteUiSnapshot,
    ): Map<String, String> {
        val key = PackagedStringCacheKey(
            revision = snapshot.revision,
            packageName = ctx.packageName,
            configuration = Configuration(ctx.resources.configuration),
        )
        return packagedStringCache.getOrBuild(key) {
            buildMap {
                for ((name, replacement) in snapshot.strings) {
                    val id = ctx.resources.getIdentifier(name, "string", ctx.packageName)
                    if (id == 0) continue
                    val packaged = runCatching { ctx.getString(id) }.getOrNull() ?: continue
                    // Formatted text is routed through text(); guessing its current
                    // arguments from an already-rendered label would be unsafe.
                    if ('%' !in packaged) put(packaged, replacement)
                }
            }
        }
    }

    private fun drawable(ctx: Context, icon: RemoteUiIcon?): BitmapDrawable? {
        if (icon == null) return null
        drawableCache[icon.sha256]?.let { return it }
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(icon.bytes, 0, icon.bytes.size, bounds)
        if (bounds.outWidth !in 1..1024 || bounds.outHeight !in 1..1024 ||
            bounds.outWidth.toLong() * bounds.outHeight > 1_048_576L) return null
        val bitmap = BitmapFactory.decodeByteArray(icon.bytes, 0, icon.bytes.size) ?: return null
        return BitmapDrawable(ctx.resources, bitmap).also {
            drawableCache[icon.sha256] = it
        }
    }

    private fun visit(root: View, block: (View) -> Unit) {
        block(root)
        if (root is ViewGroup) {
            for (i in 0 until root.childCount) visit(root.getChildAt(i), block)
        }
    }
}

/** Applies cached overrides to every screen without making Activities inherit a base class. */
class BookCaptureApplication : Application(), Application.ActivityLifecycleCallbacks {
    override fun onCreate() {
        super.onCreate()
        RemoteUiCatalog.initialize(this)
        registerActivityLifecycleCallbacks(this)
    }

    override fun onActivityResumed(activity: Activity) {
        RemoteUiLifecycle.active = WeakReference(activity)
        RemoteUiCatalog.apply(activity.window.decorView)
    }

    override fun onActivityPaused(activity: Activity) {
        if (RemoteUiLifecycle.active?.get() === activity) RemoteUiLifecycle.active = null
    }

    override fun onActivityCreated(activity: Activity, state: Bundle?) = Unit
    override fun onActivityStarted(activity: Activity) = Unit
    override fun onActivitySaveInstanceState(activity: Activity, state: Bundle) = Unit
    override fun onActivityStopped(activity: Activity) = Unit
    override fun onActivityDestroyed(activity: Activity) = Unit
}

private object RemoteUiLifecycle {
    @Volatile var active: WeakReference<Activity>? = null

    fun applyCurrent() {
        val activity = active?.get() ?: return
        activity.runOnUiThread { RemoteUiCatalog.apply(activity.window.decorView) }
    }
}
