package com.deckmeido.app.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream

/**
 * 录音：AudioRecord 采集 16kHz / 单声道 / 16bit PCM，自己拼 WAV 头。
 *
 * 后端 STT 只接受「单声道 16bit WAV」，所以不用 MediaRecorder（它只能出
 * amr/m4a 等格式，还得再转码）。上限 60 秒：60s × 16k × 2B ≈ 1.9MB，
 * 远低于后端 8MB 的限制；太短（<1s）直接判为无效录音。
 */
class VoiceRecorder {

    companion object {
        const val SAMPLE_RATE = 16000
        const val MAX_SECONDS = 60
        const val MIN_SECONDS = 1
        private const val CHANNELS = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
        private const val BYTES_PER_SECOND = SAMPLE_RATE * 2
    }

    private var record: AudioRecord? = null
    private var thread: Thread? = null
    @Volatile
    private var recording = false
    @Volatile
    private var captured = 0
    private val pcm = ByteArrayOutputStream()

    fun isRecording(): Boolean = recording

    /** 已录秒数（按字节数换算，不依赖系统时钟，录音卡顿时也准确）。 */
    fun seconds(): Int = captured / BYTES_PER_SECOND

    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (recording) return false
        val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNELS, ENCODING)
        if (minBuffer <= 0) return false
        val bufferSize = minBuffer * 2
        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.MIC,
                SAMPLE_RATE,
                CHANNELS,
                ENCODING,
                bufferSize,
            )
        } catch (_: Exception) {
            return false
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return false
        }
        synchronized(this) {
            pcm.reset()
            captured = 0
        }
        try {
            rec.startRecording()
        } catch (_: Exception) {
            rec.release()
            return false
        }
        if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            rec.release()
            return false
        }
        record = rec
        recording = true
        thread = Thread {
            val buffer = ByteArray(bufferSize)
            while (recording) {
                val read = rec.read(buffer, 0, buffer.size)
                if (read <= 0) continue
                synchronized(this) {
                    val maxBytes = MAX_SECONDS * BYTES_PER_SECOND
                    val room = maxBytes - captured
                    if (read >= room) {
                        if (room > 0) {
                            pcm.write(buffer, 0, room)
                            captured += room
                        }
                        recording = false   // 到时长上限，自动收尾
                    } else {
                        pcm.write(buffer, 0, read)
                        captured += read
                    }
                }
            }
        }.also { it.start() }
        return true
    }

    /** 结束并返回 WAV；不足 MIN_SECONDS 或没有声音时返回 null。 */
    fun stop(): ByteArray? {
        if (record == null && thread == null) return null
        recording = false
        thread?.join(1500)
        thread = null
        record?.let {
            runCatching { it.stop() }
            it.release()
        }
        record = null
        val data = synchronized(this) { pcm.toByteArray() }
        if (data.size < MIN_SECONDS * BYTES_PER_SECOND) return null
        return wav(data)
    }

    fun cancel() {
        stop()
    }

    fun release() {
        cancel()
    }

    private fun wav(pcmData: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(pcmData.size + 44)
        out.write("RIFF".toByteArray())
        writeInt(out, 36 + pcmData.size)
        out.write("WAVE".toByteArray())
        out.write("fmt ".toByteArray())
        writeInt(out, 16)
        writeShort(out, 1)                        // PCM
        writeShort(out, 1)                        // 单声道
        writeInt(out, SAMPLE_RATE)
        writeInt(out, BYTES_PER_SECOND)           // 每秒字节数
        writeShort(out, 2)                        // 块对齐
        writeShort(out, 16)                       // 位深
        out.write("data".toByteArray())
        writeInt(out, pcmData.size)
        out.write(pcmData)
        return out.toByteArray()
    }

    private fun writeInt(out: ByteArrayOutputStream, value: Int) {
        out.write(value and 0xFF)
        out.write((value shr 8) and 0xFF)
        out.write((value shr 16) and 0xFF)
        out.write((value shr 24) and 0xFF)
    }

    private fun writeShort(out: ByteArrayOutputStream, value: Int) {
        out.write(value and 0xFF)
        out.write((value shr 8) and 0xFF)
    }
}
