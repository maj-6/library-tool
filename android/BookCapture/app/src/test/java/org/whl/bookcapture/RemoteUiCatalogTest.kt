package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest
import java.util.Base64
import java.util.Locale

class RemoteUiCatalogTest {
    private fun response(
        revision: Long = 4,
        strings: JSONObject = JSONObject().put("home_new_scan", "New scan"),
        icons: JSONObject = JSONObject(),
    ): String = JSONArray().put(
        JSONObject().put("revision", revision).put(
            "catalog",
            JSONObject().put("schema", REMOTE_UI_SCHEMA)
                .put("strings", strings).put("icons", icons),
        ),
    ).toString()

    @Test
    fun parsesBoundedStringsAndHashedPngs() {
        val bytes = byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47)
        val hash = MessageDigest.getInstance("SHA-256")
            .digest(bytes).joinToString("") { "%02x".format(it) }
        val icons = JSONObject().put(
            "app_menu_button",
            JSONObject().put("mime", "image/png").put("sha256", hash)
                .put("data", Base64.getEncoder().encodeToString(bytes)),
        )

        val parsed = parseRemoteUiResponse(response(icons = icons))!!

        assertEquals(4, parsed.revision)
        assertEquals("New scan", parsed.strings["home_new_scan"])
        assertArrayEquals(bytes, parsed.icons["app_menu_button"]!!.bytes)
    }

    @Test
    fun emptyCloudResultMeansNoCatalog() {
        assertNull(parseRemoteUiResponse("[]"))
    }

    @Test
    fun rejectsExecutableOrUnverifiedIconData() {
        val badMime = JSONObject().put(
            "app_menu_button",
            JSONObject().put("mime", "image/svg+xml")
                .put("sha256", "0".repeat(64)).put("data", "PHN2Zy8+"),
        )
        assertThrows(IllegalArgumentException::class.java) {
            parseRemoteUiResponse(response(icons = badMime))
        }

        val badHash = JSONObject().put(
            "app_menu_button",
            JSONObject().put("mime", "image/png")
                .put("sha256", "0".repeat(64)).put("data", "iVBORw=="),
        )
        assertThrows(IllegalArgumentException::class.java) {
            parseRemoteUiResponse(response(icons = badHash))
        }
    }

    @Test
    fun rejectsNamesThatCannotBeAndroidResourceEntries() {
        assertThrows(IllegalArgumentException::class.java) {
            parseRemoteUiResponse(
                response(strings = JSONObject().put("Bad.Name", "ignored")),
            )
        }
    }

    @Test
    fun invalidRemoteFormatFallsBackToPackagedTemplate() {
        assertEquals(
            "3 pages",
            formatRemoteUiText(Locale.US, "%q pages", "%d pages", 3),
        )
    }

    @Test
    fun runtimeCoversMaterialButtonsAndLateInflatedSurfaces() {
        val catalog = File("src/main/java/org/whl/bookcapture/RemoteUiCatalog.kt").readText()
        assertTrue(catalog.contains("view is MaterialButton"))
        assertTrue(catalog.contains("view.icon = it"))
        assertTrue(catalog.contains("fun apply(dialog: Dialog)"))

        val home = File("src/main/java/org/whl/bookcapture/HomeActivity.kt").readText()
        val capture = File("src/main/java/org/whl/bookcapture/MainActivity.kt").readText()
        val detail = File("src/main/java/org/whl/bookcapture/EntryDetailActivity.kt").readText()
        assertTrue(home.contains("RemoteUiCatalog.apply(row)"))
        assertTrue(home.contains("RemoteUiCatalog.apply(dialog)"))
        assertTrue(capture.contains("RemoteUiCatalog.apply(row)"))
        assertTrue(capture.contains("RemoteUiCatalog.apply(dialog)"))
        assertTrue(detail.contains("RemoteUiCatalog.apply(column)"))
        assertTrue(detail.contains("RemoteUiCatalog.apply(dialog)"))
    }

    @Test
    fun shippedSourceCatalogHasARealAppMenuPng() {
        val sourceFile = File("../remote-ui/catalog.json")
        val source = JSONObject(sourceFile.readText())
        assertTrue(source.getLong("revision") >= 3)
        val relative = source.getJSONObject("icons").getString("appMenu")
        val png = requireNotNull(sourceFile.parentFile).resolve(relative).canonicalFile
        assertTrue(png.isFile)
        assertTrue(png.length() in 1..REMOTE_UI_MAX_ICON_BYTES.toLong())
        assertArrayEquals(
            byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a),
            png.inputStream().use { it.readNBytes(8) },
        )
    }
}
