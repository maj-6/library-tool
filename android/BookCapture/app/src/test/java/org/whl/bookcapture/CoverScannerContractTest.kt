package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CoverScannerContractTest {

    @Test
    fun coverRecognitionUsesPinnedMistralOcr41OffTheMainThread() {
        val scanner = source("CoverScannerActivity")
        val pipeline = source("Pipeline")

        assertEquals("mistral-ocr-latest", Pipeline.CAPTURE_OCR_MODEL)
        assertEquals("mistral-ocr-4-1", Pipeline.COVER_OCR_MODEL)
        assertTrue(pipeline.contains(".put(\"model\", model)"))
        assertTrue(scanner.contains("Prefs.mistralKey(this).trim()"))
        assertTrue(scanner.contains("withContext(Dispatchers.IO)"))
        assertTrue(scanner.contains("Pipeline.coverOcr(target, mistralKey)"))
        assertTrue(scanner.contains("catch (cancelled: CancellationException)"))
        assertTrue(scanner.contains("throw cancelled"))
        assertTrue(scanner.contains("recognitionJob?.cancel()"))
        assertTrue(scanner.contains("finally {"))
        assertTrue(scanner.contains("target.name + \".tmp\""))
        assertFalse(scanner.contains("TextRecognition"))
        assertFalse(scanner.contains("InputImage"))

        val home = source("HomeActivity")
        val launch = home.substringAfter("binding.inspectScanCover.setOnClickListener")
            .substringBefore("binding.inspectViewModes.addOnButtonCheckedListener")
        assertTrue(launch.contains("Prefs.mistralKey(this).isBlank()"))
        assertTrue(launch.contains("cover_scanner_requires_mistral_key"))
    }

    @Test
    fun queueCaptureHandsOffBeforeOcrWhileInspectStillReadsDirectly() {
        val scanner = source("CoverScannerActivity")
        val queueCapture = scanner.substringAfter("private fun queueCapturedPhoto(")
            .substringBefore("private fun renderQueueReady(")
        val inspectRecognition = scanner.substringAfter("private fun recognizeCover(")
            .substringBefore("private fun queueCapturedPhoto(")

        assertTrue(
            queueCapture.indexOf("ScanSearchQueue.enqueueProcessing(") in
                0 until queueCapture.indexOf("ScanSearchOcrWorker.enqueue("),
        )
        assertFalse(queueCapture.contains("Pipeline.coverOcr("))
        assertTrue(inspectRecognition.contains("Pipeline.coverOcr(target, mistralKey)"))
        assertFalse(inspectRecognition.contains("ScanSearchOcrWorker.enqueue("))
    }

    @Test
    fun queueNotificationPermissionIsOptionalAndApiGated() {
        val scanner = source("CoverScannerActivity")
        val request = scanner.substringAfter("private fun requestQueueNotificationPermission()")
            .substringBefore("private fun handleQueueVoiceCommand(")

        assertTrue(scanner.contains("Manifest.permission.POST_NOTIFICATIONS"))
        assertTrue(request.contains("Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU"))
        assertTrue(request.contains("notificationPermissionRequested"))
        assertTrue(request.contains("notificationPermission.launch("))
        assertFalse(request.contains("finishCancelled()"))
    }

    @Test
    fun buildKeepsOfflineQrDecoderButDoesNotBundleTextRecognition() {
        val gradle = File("build.gradle.kts").readText()

        assertTrue(gradle.contains("com.google.mlkit:barcode-scanning"))
        assertFalse(gradle.contains("com.google.mlkit:text-recognition"))
    }

    @Test
    fun coverCopyDisclosesMistralUploadAndNeverPromisesOnDeviceRecognition() {
        val strings = File("src/main/res/values/strings.xml").readText()
        val help = strings.lineSequence().single { it.contains("cover_scanner_help") }
        val reading = strings.lineSequence().single { it.contains("cover_scanner_reading") }

        assertTrue(help.contains("Mistral OCR 4.1"))
        assertTrue(reading.contains("Mistral OCR 4.1"))
        assertFalse(help.contains("on this device"))
        assertFalse(reading.contains("on this device"))
    }

    @Test
    fun returnedCoverTextIsBoundedAndSanitized() {
        val oversized = "  Flora\u0000\r\n" + "x".repeat(
            CoverScannerActivity.MAX_RECOGNIZED_TEXT_CHARS,
        )

        val bounded = CoverScannerActivity.boundedCoverText(oversized)

        assertEquals(CoverScannerActivity.MAX_RECOGNIZED_TEXT_CHARS, bounded.length)
        assertTrue(bounded.startsWith("Flora \n"))
        assertFalse(bounded.contains('\u0000'))
    }

    @Test
    fun placeholderOnlyMistralMarkdownIsNotReadableCoverText() {
        assertFalse(
            CoverScannerActivity.hasReadableCoverText("![img-0.jpeg](img-0.jpeg)"),
        )
        assertTrue(CoverScannerActivity.hasReadableCoverText("Herbs"))
    }

    private fun source(name: String): String =
        File("src/main/java/org/whl/bookcapture/$name.kt").readText()
}
