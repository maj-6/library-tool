package org.whl.bookcapture

import android.content.Context
import android.content.SharedPreferences

/** Settings: the Supabase project this phone uploads to + a device label. */
object Prefs {
    private fun sp(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences("bookcapture", Context.MODE_PRIVATE)

    fun supabaseUrl(ctx: Context): String =
        sp(ctx).getString("supabase_url", "")!!.trim().trimEnd('/')

    fun supabaseKey(ctx: Context): String =
        sp(ctx).getString("supabase_key", "")!!.trim()

    fun deviceName(ctx: Context): String {
        val v = sp(ctx).getString("device_name", "")!!.trim()
        return v.ifEmpty { android.os.Build.MODEL ?: "phone" }
    }

    fun configured(ctx: Context): Boolean =
        supabaseUrl(ctx).isNotEmpty() && supabaseKey(ctx).isNotEmpty()

    fun save(ctx: Context, url: String, key: String, device: String) {
        sp(ctx).edit()
            .putString("supabase_url", url.trim())
            .putString("supabase_key", key.trim())
            .putString("device_name", device.trim())
            .apply()
    }
}
