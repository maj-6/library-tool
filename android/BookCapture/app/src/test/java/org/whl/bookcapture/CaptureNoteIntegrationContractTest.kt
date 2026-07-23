package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class CaptureNoteIntegrationContractTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"
    private val appNs = "http://schemas.android.com/apk/res-auto"

    @Test
    fun noteButtonIsAnAccessibleTopRightPreviewTouchTarget() {
        val layout = xml("src/main/res/layout/activity_main.xml")
        val button = elementById(layout, "btnNote")

        assertEquals("androidx.appcompat.widget.AppCompatImageButton", button.tagName)
        assertEquals("@style/WhlIconButton", button.getAttribute("style"))
        assertTrue(dp(button, "layout_width") >= 48f)
        assertTrue(dp(button, "layout_height") >= 48f)
        assertEquals("@id/preview", button.getAttributeNS(appNs, "layout_constraintTop_toTopOf"))
        assertEquals("@id/preview", button.getAttributeNS(appNs, "layout_constraintEnd_toEndOf"))
        assertEquals("@drawable/ic_note", button.getAttributeNS(appNs, "srcCompat"))
        assertTrue(button.getAttributeNS(androidNs, "contentDescription").isNotBlank())
    }

    @Test
    fun noteOverlayIsCompactTranslucentScrollableAndRowOriented() {
        val capture = xml("src/main/res/layout/activity_main.xml")
        val overlay = elementById(capture, "noteOverlay")
        assertEquals("gone", overlay.getAttributeNS(androidNs, "visibility"))
        assertTrue(dp(overlay, "layout_height") <= 180f)
        assertEquals("@drawable/whl_note_overlay", overlay.getAttributeNS(androidNs, "background"))
        assertEquals("@id/preview", overlay.getAttributeNS(appNs, "layout_constraintTop_toTopOf"))
        assertNotNull(elementById(capture, "noteUnclassified"))
        val rows = elementById(capture, "noteRows")
        assertEquals("ScrollView", (rows.parentNode as Element).tagName)

        val overlayShape = xml("src/main/res/drawable/whl_note_overlay.xml")
        val solid = overlayShape.getElementsByTagName("solid").item(0) as Element
        val color = solid.getAttributeNS(androidNs, "color")
        assertTrue("overlay color must use #AARRGGBB", color.matches(Regex("#[0-9A-Fa-f]{8}")))
        val alpha = color.substring(1, 3).toInt(16)
        assertTrue("overlay must remain translucent", alpha in 1..254)

        val row = xml("src/main/res/layout/item_capture_note_row.xml")
        val root = row.documentElement
        assertTrue(dp(root, "minHeight") <= 32f)
        for (id in listOf("noteField", "noteValue")) {
            val text = elementById(row, id)
            assertEquals("1", text.getAttributeNS(androidNs, "maxLines"))
            assertTrue(sp(text, "textSize") <= 12f)
        }

        val source = source("MainActivity")
        assertTrue(source.contains("binding.noteRows.removeAllViews()"))
        assertTrue(source.contains("R.layout.item_capture_note_row"))
        assertTrue(source.contains("binding.noteUnclassified"))
    }

    @Test
    fun controllerGrammarAndPolicyExposeEveryNewVoiceCommand() {
        val controller = source("VoiceController")
        for (spoken in listOf("restart", "undo", "edit", "notes", "end notes")) {
            assertTrue("voice grammar is missing $spoken", controller.contains("\"$spoken\""))
        }
        assertTrue(controller.contains("StateAwareVoiceCommandPolicy.evaluate("))
        val main = source("MainActivity")
        assertTrue(main.contains("\"notes\" ->"))
        assertTrue(main.contains("\"edit\" -> reopenLastScannedBook()"))
        assertTrue(main.contains("VoiceCommandState.NOTE_ACTIVE"))
        assertTrue(main.contains("transcriptBeforeCommand"))
        val editSource = main.substringAfter("private fun reopenLastScannedBook()")
            .substringBefore("private fun restartCapture()")
        for (resource in listOf(
            "capture_edit_already_open_error", "capture_edit_finish_current",
            "capture_edit_busy_error", "capture_edit_busy_status", "capture_edit_reopening",
            "capture_edit_failed_error", "capture_edit_failed_status", "capture_edit_reopened",
            "capture_edit_none_error", "capture_edit_none_status",
        )) {
            assertTrue(editSource.contains("R.string.$resource"))
        }
        assertFalse(editSource.contains("setStatus(\""))
        assertFalse(editSource.contains("cues.error(\""))

        assertEquals(
            PolicyVoiceCommand.RESTART,
            StateAwareVoiceCommandPolicy.evaluate(
                "restart",
                VoiceCommandState.IDLE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
        assertEquals(
            PolicyVoiceCommand.UNDO,
            StateAwareVoiceCommandPolicy.evaluate(
                "undo",
                VoiceCommandState.IDLE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
        assertEquals(
            PolicyVoiceCommand.NOTES,
            StateAwareVoiceCommandPolicy.evaluate(
                "notes",
                VoiceCommandState.IDLE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
        assertEquals(
            PolicyVoiceCommand.END_NOTES,
            StateAwareVoiceCommandPolicy.evaluate(
                "dictated text, end notes",
                VoiceCommandState.NOTE_ACTIVE,
                VoiceRecognitionStability.FINAL,
            )?.command,
        )
    }

    @Test
    fun cameraAndMicrophonePermissionFlowsRemainIndependent() {
        val manifest = xml("src/main/AndroidManifest.xml")
        val permissionNames = elements(manifest, "uses-permission")
            .map { it.getAttributeNS(androidNs, "name") }
        assertTrue("android.permission.CAMERA" in permissionNames)
        assertTrue("android.permission.RECORD_AUDIO" in permissionNames)

        val microphone = elements(manifest, "uses-feature")
            .first { it.getAttributeNS(androidNs, "name") == "android.hardware.microphone" }
        assertEquals("false", microphone.getAttributeNS(androidNs, "required"))

        val main = source("MainActivity")
        assertTrue(main.contains("CAMERA_PERMISSION_REQUEST ->"))
        assertTrue(main.contains("VOICE_PERMISSION_REQUEST ->"))
        assertTrue(main.contains("arrayOf(Manifest.permission.CAMERA)"))
        assertTrue(main.contains("arrayOf(Manifest.permission.RECORD_AUDIO)"))
        val combinedPermissionRequest = Regex(
            "arrayOf\\(([^)]*)\\)",
            setOf(RegexOption.DOT_MATCHES_ALL),
        ).findAll(main).map { it.groupValues[1] }.any { arguments ->
            arguments.contains("Manifest.permission.CAMERA") &&
                arguments.contains("Manifest.permission.RECORD_AUDIO")
        }
        assertFalse(
            "camera startup must never request microphone permission in the same array",
            combinedPermissionRequest,
        )
        assertTrue(main.contains("private var notePermissionRequested"))
        assertTrue(main.contains("private var voicePermissionRequestedForEnablement"))
        assertTrue(main.contains("val requestedForNote = notePermissionRequested"))
        assertTrue(main.contains("val requestedForVoice = voicePermissionRequestedForEnablement"))
        assertTrue(main.contains("private fun requestStartVoiceNote()"))
        assertTrue(main.contains("notePermissionRequested = true"))
        assertTrue(main.contains("if (cameraPermissionGranted()) initAfterCameraPermission()"))
    }

    @Test
    fun restartAndUndoCanSurviveABusyCaptureAsPendingCommands() {
        val prefs = source("Prefs")
        val setter = functionWindow(prefs, "fun setPendingCaptureCommand", "fun pendingCaptureCommand")
        val getter = functionWindow(prefs, "fun pendingCaptureCommand", "fun clearPendingCaptureCommand")
        val declaredCommands = Regex(
            "PENDING_CAPTURE_COMMANDS\\s*=\\s*setOf\\(([^)]*)\\)",
        ).find(prefs)?.groupValues?.get(1).orEmpty()
        assertTrue(setter.contains("PENDING_CAPTURE_COMMANDS"))
        assertTrue(setter.contains("pending_capture_target_page"))
        assertTrue(getter.contains("PENDING_CAPTURE_COMMANDS"))
        for (command in listOf("done", "cancel", "restart", "undo")) {
            assertTrue(
                "pending-command policy rejects $command",
                declaredCommands.contains("\"$command\""),
            )
        }

        val main = source("MainActivity")
        val commandFunction = functionWindow(main, "private fun command(", "private fun restartCapture()")
        val busyGate = functionWindow(commandFunction, "val waitsForCapture", "when (word)")
        assertTrue(busyGate.contains("\"restart\""))
        assertTrue(busyGate.contains("\"undo\""))
        assertTrue(busyGate.contains("Prefs.setPendingCaptureCommand"))
        assertTrue(main.contains("\"restart\" ->"))
        assertTrue(main.contains("\"undo\" ->"))

        val pendingRunner = functionWindow(main, "private fun runPending()", "private fun finishAfterAcceptedCapturesIfReady")
        assertTrue(pendingRunner.contains("val cmd = pendingCommand ?: return"))
        assertTrue(pendingRunner.contains("deferredUndoDisposition"))
        assertTrue(pendingRunner.contains("pendingCaptureTargetPage"))
        assertTrue(pendingRunner.contains("command(cmd"))
        assertTrue(pendingRunner.contains("Prefs.clearPendingCaptureCommand"))
    }

    @Test
    fun noteTextIsCheckpointedAndButtonStopDrainsBeforeFinalSave() {
        val main = source("MainActivity")
        assertTrue(main.contains("private fun checkpointVoiceNote"))
        assertTrue(main.contains("checkpointVoiceNote(checkNotNull(voiceNoteDraft))"))
        assertTrue(main.contains("transcriber.finish { finalTranscript"))
        assertTrue(main.contains("private fun completeVoiceNoteNow"))
        val onPause = functionWindow(main, "override fun onPause()", "override fun onStop()")
        assertTrue(onPause.contains("finishVoiceNote("))
        assertFalse(onPause.contains("drain = false"))
        assertTrue(main.contains("CaptureNotes.remove(session.entryDir(entryId), noteId)"))
        assertTrue(main.contains("val shouldSave = save && !voiceNoteDiscardPending"))
        assertTrue(main.contains("voiceNoteDiscardPending = discardFailed"))
        val notes = source("CaptureNotes")
        assertTrue(notes.contains("CAPTURE_NOTE_DISCARD_MARKER_PREFIX"))
        assertTrue(notes.contains("withoutDiscardedNotes(dir, readDocument(dir, manifest))"))
        assertTrue(notes.indexOf("markDiscarded(dir, removed.id)") < notes.indexOf("writeDocument(dir, current.copy"))
        assertTrue(main.indexOf("if (saveFailed || discardFailed)") < main.indexOf("voiceNoteDraft = null", main.indexOf("private fun completeVoiceNoteNow")))

        val transcriber = source("MistralRealtimeTranscriber")
        assertTrue(transcriber.contains("input_audio.flush"))
        assertTrue(transcriber.contains("input_audio.end"))
        assertTrue(transcriber.contains("postDelayed(drainTimeout"))
        assertTrue(transcriber.contains("if (draining.get()) completeDrain(finalText)"))
        val configure = functionWindow(
            transcriber,
            "private fun configureAndRecord",
            "@SuppressLint(\"MissingPermission\")",
        )
        assertTrue(configure.contains("synchronized(sendMonitor)"))
        assertTrue(configure.indexOf("session.update") < configure.indexOf("startAudio(webSocket)"))
    }

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()

    private fun functionWindow(source: String, start: String, end: String): String {
        val from = source.indexOf(start)
        val until = source.indexOf(end, from + start.length)
        assertTrue("missing source marker: $start", from >= 0)
        assertTrue("missing source marker after $start: $end", until > from)
        return source.substring(from, until)
    }

    private fun dp(element: Element, attribute: String): Float =
        dimension(element, attribute, "dp")

    private fun sp(element: Element, attribute: String): Float =
        dimension(element, attribute, "sp")

    private fun dimension(element: Element, attribute: String, suffix: String): Float {
        val value = element.getAttributeNS(androidNs, attribute)
        assertTrue("$attribute must be a $suffix dimension, got $value", value.endsWith(suffix))
        return value.removeSuffix(suffix).toFloat()
    }

    private fun xml(path: String): Document {
        val factory = DocumentBuilderFactory.newInstance()
        factory.isNamespaceAware = true
        return factory.newDocumentBuilder().parse(File(path))
    }

    private fun elementById(document: Document, id: String): Element =
        requireNotNull(elements(document, "*").firstOrNull {
            it.getAttributeNS(androidNs, "id") == "@+id/$id"
        }) { "Missing view id $id" }

    private fun elements(document: Document, tagName: String): List<Element> {
        val nodes = document.getElementsByTagName(tagName)
        return (0 until nodes.length).map { nodes.item(it) as Element }
    }
}
