package com.deckmeido.app.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * 设备令牌的本地存储。
 *
 * 后端下发的是 HttpOnly Cookie，Android 端把它当作设备凭据保存。这里用
 * Android Keystore 里的 AES-256-GCM 密钥加密后再写 SharedPreferences，
 * 避免令牌在明文文件里裸奔，同时不引入额外的加密库。
 */
class SecurePrefs(context: Context) {

    private val prefs = context.getSharedPreferences("deckmeido_secrets", Context.MODE_PRIVATE)

    private fun key(): SecretKey? = runCatching {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val existing = store.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry
        if (existing != null) {
            existing.secretKey
        } else {
            val generator = KeyGenerator.getInstance("AES", "AndroidKeyStore")
            generator.init(
                KeyGenParameterSpec.Builder(
                    ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build(),
            )
            generator.generateKey()
        }
    }.getOrNull()

    fun put(name: String, value: String) {
        val secretKey = key() ?: return
        runCatching {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, secretKey)
            val payload = cipher.iv + cipher.doFinal(value.toByteArray(Charsets.UTF_8))
            prefs.edit()
                .putString(name, Base64.encodeToString(payload, Base64.NO_WRAP))
                .apply()
        }
    }

    fun get(name: String): String? {
        val raw = prefs.getString(name, null) ?: return null
        val secretKey = key() ?: return null
        return runCatching {
            val bytes = Base64.decode(raw, Base64.NO_WRAP)
            if (bytes.size <= IV_SIZE) return@runCatching null
            val iv = bytes.copyOfRange(0, IV_SIZE)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, secretKey, GCMParameterSpec(128, iv))
            String(cipher.doFinal(bytes, IV_SIZE, bytes.size - IV_SIZE), Charsets.UTF_8)
        }.getOrElse {
            // 密钥失效（刷机、恢复备份等）时解不开，清掉旧值让用户重新配对
            prefs.edit().remove(name).apply()
            null
        }
    }

    fun remove(name: String) {
        prefs.edit().remove(name).apply()
    }

    companion object {
        const val DEVICE_TOKEN = "device_token"
        private const val ALIAS = "deckmeido_device_token"
        private const val IV_SIZE = 12
    }
}
