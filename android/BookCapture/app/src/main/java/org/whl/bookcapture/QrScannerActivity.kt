package org.whl.bookcapture

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import org.whl.bookcapture.databinding.ActivityQrScannerBinding
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * A deliberately narrow QR reader for physical collection labels. It returns
 * text only: Home resolves that text against BookCollection.tagId, so a QR can
 * never launch a URL or be confused with the collection's internal UUID.
 */
class QrScannerActivity : AppCompatActivity() {

    private lateinit var binding: ActivityQrScannerBinding
    private lateinit var analyzerExecutor: ExecutorService
    private var cameraProvider: ProcessCameraProvider? = null
    private val delivered = AtomicBoolean(false)

    private val scanner: BarcodeScanner by lazy {
        BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder()
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                .build(),
        )
    }

    private val cameraPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) bindCamera() else {
            Toast.makeText(this, R.string.qr_scanner_permission_denied, Toast.LENGTH_LONG).show()
            finish()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityQrScannerBinding.inflate(layoutInflater)
        setContentView(binding.root)
        analyzerExecutor = Executors.newSingleThreadExecutor()
        binding.closeQrScanner.setOnClickListener { finish() }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            bindCamera()
        } else {
            cameraPermission.launch(Manifest.permission.CAMERA)
        }
    }

    private fun bindCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            if (isFinishing || isDestroyed) return@addListener
            try {
                val provider = providerFuture.get()
                cameraProvider = provider
                val preview = Preview.Builder().build().also {
                    it.surfaceProvider = binding.qrPreview.surfaceProvider
                }
                val analysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                analysis.setAnalyzer(analyzerExecutor, ::analyze)
                provider.unbindAll()
                provider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    analysis,
                )
            } catch (_: Exception) {
                Toast.makeText(this, R.string.qr_scanner_unavailable, Toast.LENGTH_LONG).show()
                finish()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    @androidx.annotation.OptIn(markerClass = [ExperimentalGetImage::class])
    private fun analyze(frame: ImageProxy) {
        if (delivered.get()) {
            frame.close()
            return
        }
        val mediaImage = frame.image
        if (mediaImage == null) {
            frame.close()
            return
        }
        val image = InputImage.fromMediaImage(mediaImage, frame.imageInfo.rotationDegrees)
        try {
            scanner.process(image)
                .addOnSuccessListener { barcodes ->
                    val raw = barcodes.asSequence()
                        .mapNotNull { it.rawValue?.trim() }
                        .firstOrNull { it.isNotEmpty() }
                    if (raw != null && delivered.compareAndSet(false, true)) {
                        setResult(RESULT_OK, Intent().putExtra(EXTRA_TAG_ID, raw))
                        finish()
                    }
                }
                .addOnCompleteListener { frame.close() }
        } catch (_: Exception) {
            frame.close()
        }
    }

    override fun onDestroy() {
        cameraProvider?.unbindAll()
        scanner.close()
        analyzerExecutor.shutdown()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_TAG_ID = "collection_tag_id"
    }
}
