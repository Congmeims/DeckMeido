package com.deckmeido.app.ui

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.nio.ByteBuffer
import java.util.concurrent.Executors

/**
 * 扫码配对：CameraX 取景 + ZXing core 本机解码（不依赖 Google Play 服务）。
 * 扫到内容后交给 ViewModel：先探地址，再用一次性 token 换设备凭据。
 */
@Composable
fun ScanScreen(vm: DeckMeidoViewModel) {
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED,
        )
    }
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted = it }

    ScreenScaffold(
        title = "扫码配对",
        subtitle = "对准 Deck 上显示的二维码",
        onBack = { vm.cancelScan() },
    ) { padding ->
        Column(
            Modifier
                .padding(padding)
                .fillMaxSize()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .clip(RoundedCornerShape(16.dp))
                    .background(Color.Black),
                contentAlignment = Alignment.Center,
            ) {
                if (granted) {
                    CameraPreview(
                        modifier = Modifier.fillMaxSize(),
                        onDecoded = { vm.onQrScanned(it) },
                    )
                } else {
                    Column(
                        Modifier.padding(24.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        Text(
                            "扫码需要相机权限。",
                            color = Color.White,
                            textAlign = TextAlign.Center,
                        )
                        Button(onClick = { launcher.launch(Manifest.permission.CAMERA) }) {
                            Text("允许使用相机")
                        }
                        Text(
                            "如果系统不再弹窗，请到系统设置里手动开启相机权限。",
                            color = Color(0xFFB0B4BE),
                            style = MaterialTheme.typography.labelSmall,
                            textAlign = TextAlign.Center,
                        )
                    }
                }
            }

            if (vm.qrBusy) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
                Text("正在配对…", style = MaterialTheme.typography.labelMedium)
            } else {
                Text(
                    "二维码 90 秒内有效、只能使用一次；过期或已被使用请在 Deck 上刷新。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                )
            }
        }
    }
}

@Composable
private fun CameraPreview(modifier: Modifier, onDecoded: (String) -> Unit) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val previewView = remember {
        PreviewView(context).apply { scaleType = PreviewView.ScaleType.FILL_CENTER }
    }
    val decoder = remember { Executors.newSingleThreadExecutor() }
    val mainExecutor = remember { ContextCompat.getMainExecutor(context) }
    val latestDecoded by rememberUpdatedState(onDecoded)

    DisposableEffect(lifecycleOwner, previewView) {
        val future = ProcessCameraProvider.getInstance(context)
        var provider: ProcessCameraProvider? = null
        future.addListener({
            try {
                provider = future.get()
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                val analysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                    .build()
                    .also {
                        it.setAnalyzer(
                            decoder,
                            QrAnalyzer { text -> mainExecutor.execute { latestDecoded(text) } },
                        )
                    }
                provider?.unbindAll()
                provider?.bindToLifecycle(
                    lifecycleOwner,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    analysis,
                )
            } catch (_: Exception) {
                // 没有相机 / 被占用：保持黑屏，用户可返回改用地址 + PIN 配对
            }
        }, mainExecutor)

        onDispose {
            provider?.unbindAll()
            decoder.shutdown()
        }
    }

    AndroidView(factory = { previewView }, modifier = modifier)
}

/** 每 200ms 解一帧，避免高分辨率相机把解码线程打满。 */
private class QrAnalyzer(private val onDecoded: (String) -> Unit) : ImageAnalysis.Analyzer {
    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                DecodeHintType.TRY_HARDER to true,
            ),
        )
    }
    private var lastAt = 0L
    private var done = false

    override fun analyze(image: ImageProxy) {
        if (done) {
            image.close()
            return
        }
        val now = System.currentTimeMillis()
        if (now - lastAt < 200) {
            image.close()
            return
        }
        lastAt = now
        try {
            val bitmap = image.toRgbaBitmap() ?: return
            val pixels = IntArray(bitmap.width * bitmap.height)
            bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
            val source = RGBLuminanceSource(bitmap.width, bitmap.height, pixels)
            val text = reader.decodeWithState(BinaryBitmap(HybridBinarizer(source))).text
            if (text.isNotBlank()) {
                done = true
                onDecoded(text)
            }
        } catch (_: NotFoundException) {
            // 取景框里还没有可识别的二维码，继续下一帧
        } catch (_: Exception) {
            // 单帧异常不能打断整次扫码
        } finally {
            reader.reset()
            image.close()
        }
    }
}

/** YUV/RGBA ImageProxy → ARGB Bitmap（按 rowStride 处理补齐，再按旋转角转正）。 */
private fun ImageProxy.toRgbaBitmap(): Bitmap? {
    val plane = planes.firstOrNull() ?: return null
    val rowStride = plane.rowStride
    val pixelStride = plane.pixelStride
    if (rowStride <= 0 || pixelStride <= 0 || width <= 0 || height <= 0) return null
    val buffer = plane.buffer
    val bytes = ByteArray(rowStride * height)
    buffer.rewind()
    buffer.get(bytes)
    val padded = Bitmap.createBitmap(rowStride / pixelStride, height, Bitmap.Config.ARGB_8888)
    padded.copyPixelsFromBuffer(ByteBuffer.wrap(bytes))
    val cropped = if (padded.width == width) {
        padded
    } else {
        Bitmap.createBitmap(padded, 0, 0, width, height)
    }
    val rotation = imageInfo.rotationDegrees
    if (rotation == 0) return cropped
    val matrix = Matrix().apply { postRotate(rotation.toFloat()) }
    return Bitmap.createBitmap(cropped, 0, 0, cropped.width, cropped.height, matrix, false)
}
