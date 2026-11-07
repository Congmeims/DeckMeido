package com.deckmeido.app.audio

import android.media.MediaPlayer
import java.io.File

/**
 * 播报 WAV：后端 `POST /api/tts/say` 回 WAV 字节，这里落成临时文件再播放。
 *
 * MediaPlayer 的回调投递到创建它的 Looper，所以 play() 必须在主线程调用；
 * 写文件这类 IO 由调用方放在 IO 线程。暂停/继续/停止都只在本地控制。
 */
class VoicePlayer {

    private var player: MediaPlayer? = null
    private var current: File? = null

    fun write(dir: File, bytes: ByteArray): File {
        dir.mkdirs()
        val file = File(dir, "tts-${System.currentTimeMillis()}.wav")
        file.writeBytes(bytes)
        return file
    }

    fun play(
        file: File,
        onStarted: () -> Unit,
        onFinished: () -> Unit,
        onFailed: (String) -> Unit,
    ): Boolean {
        stop()
        val mp = MediaPlayer()
        return try {
            mp.setDataSource(file.absolutePath)
            mp.setOnCompletionListener {
                stopInternal()
                onFinished()
            }
            mp.setOnErrorListener { _, what, extra ->
                stopInternal()
                onFailed("播放失败（$what/$extra）")
                true
            }
            mp.setOnPreparedListener {
                onStarted()
                it.start()
            }
            mp.prepareAsync()
            player = mp
            current = file
            true
        } catch (e: Exception) {
            runCatching { mp.release() }
            stopInternal()
            onFailed(e.message ?: "播放失败")
            false
        }
    }

    fun pause(): Boolean {
        val mp = player ?: return false
        return try {
            mp.pause()
            true
        } catch (_: Exception) {
            false
        }
    }

    fun resume(): Boolean {
        val mp = player ?: return false
        return try {
            mp.start()
            true
        } catch (_: Exception) {
            false
        }
    }

    fun stop() {
        stopInternal()
    }

    private fun stopInternal() {
        player?.let {
            runCatching { if (it.isPlaying) it.stop() }
            it.release()
        }
        player = null
        current?.delete()
        current = null
    }
}
