package com.deckmeido.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * DeckMeido 后端的 REST / WebSocket 访问层。
 *
 * 鉴权和网页完全一致：配对成功后后端下发 `deckmeido_device` Cookie，
 * 这里用 CookieJar 把它加密落盘，之后的每个请求和 WS 握手都自动带上。
 */
class ApiClient(private val vault: SecurePrefs) {

    @Volatile
    var baseUrl: String = ""
        private set

    private val cookieJar = VaultCookieJar(vault) { baseUrl.toHttpUrlOrNull()?.host ?: "" }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    // WebSocket 是长连接，不能沿用 REST 的 readTimeout / callTimeout。
    private val wsHttp: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    /** 地址归一化：允许省略 scheme，去掉末尾斜杠，只保留 scheme://host[:port]。 */
    fun setBaseUrl(raw: String): Boolean {
        val url = parseBaseUrl(raw) ?: return false
        baseUrl = url
        return true
    }

    fun hasToken(): Boolean = vault.get(SecurePrefs.DEVICE_TOKEN) != null

    fun clearToken() {
        vault.remove(SecurePrefs.DEVICE_TOKEN)
    }

    val host: String
        get() = baseUrl.toHttpUrlOrNull()?.host ?: ""

    // ---------- REST ----------

    suspend fun probe(): AuthInfo = withContext(Dispatchers.IO) {
        val obj = exec("GET", "/api/auth/session")
        AuthInfo(
            authenticated = obj.optBoolean("authenticated", false),
            local = obj.optBoolean("local", false),
            lanEnable = obj.optBoolean("lan_enable", true),
            version = obj.optString("version_display", obj.optString("version")),
        )
    }

    suspend fun pairStart(): PairSession = withContext(Dispatchers.IO) {
        parsePair(exec("POST", "/api/pair/start", JSONObject()))
    }

    suspend fun pairStatus(pairId: String): PairSession = withContext(Dispatchers.IO) {
        parsePair(exec("GET", "/api/pair/status", query = mapOf("pair_id" to pairId)))
    }

    suspend fun pairConfirm(pairId: String, pin: String, remember: Boolean) {
        withContext(Dispatchers.IO) {
            exec(
                "POST",
                "/api/pair/confirm",
                JSONObject()
                    .put("pair_id", pairId)
                    .put("pin", pin)
                    .put("remember", remember),
            )
        }
    }

    suspend fun sessions(): SessionsResult = withContext(Dispatchers.IO) {
        val obj = exec("GET", "/api/sessions")
        val items = ArrayList<SessionItem>()
        val arr = obj.optJSONArray("items")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val row = arr.optJSONObject(i) ?: continue
                val id = row.optString("id")
                if (id.isBlank()) continue
                items.add(
                    SessionItem(
                        id = id,
                        title = row.optString("title").ifBlank { "未命名会话" },
                        updated = row.optDouble("updated", 0.0).toLong(),
                        messageCount = row.optInt("messages", 0),
                    ),
                )
            }
        }
        SessionsResult(current = obj.optString("current"), items = items)
    }

    suspend fun history(sid: String): HistoryPreview = withContext(Dispatchers.IO) {
        val obj = exec("GET", "/api/sessions/$sid/history")
        HistoryPreview(
            id = obj.optString("id").ifBlank { sid },
            title = obj.optString("title").ifBlank { "会话" },
            messages = parseMessages(obj.optJSONArray("messages")),
        )
    }

    suspend fun switchSession(sid: String) {
        withContext(Dispatchers.IO) {
            exec("POST", "/api/sessions/switch", JSONObject().put("id", sid))
        }
    }

    suspend fun cancel() {
        withContext(Dispatchers.IO) {
            exec(
                "POST",
                "/api/cancel",
                JSONObject(),
                query = mapOf("client" to "android", "device" to DEVICE_LABEL),
            )
        }
    }

    fun newWebSocket(listener: WebSocketListener): WebSocket? {
        val base = baseUrl.toHttpUrlOrNull() ?: return null
        val url = base.newBuilder().encodedPath("/ws").build()
        val request = Request.Builder()
            .url(url)
            .header("User-Agent", UA)
            .build()
        return wsHttp.newWebSocket(request, listener)
    }

    // ---------- 内部 ----------

    private fun exec(
        method: String,
        path: String,
        body: JSONObject? = null,
        query: Map<String, String> = emptyMap(),
    ): JSONObject {
        val base = baseUrl.toHttpUrlOrNull() ?: throw ApiException(0, "服务器地址无效")
        val url = base.newBuilder().apply {
            encodedPath(path)
            query.forEach { (k, v) -> addQueryParameter(k, v) }
        }.build()

        val builder = Request.Builder()
            .url(url)
            .header("User-Agent", UA)
        when (method) {
            "POST" -> builder.post((body?.toString() ?: "{}").toRequestBody(JSON))
            "DELETE" -> builder.delete()
            else -> builder.get()
        }

        http.newCall(builder.build()).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val detail = runCatching { JSONObject(text).optString("detail") }
                    .getOrNull()
                    .orEmpty()
                throw ApiException(
                    response.code,
                    detail.ifBlank { "请求失败（HTTP ${response.code}）" },
                )
            }
            if (text.isBlank()) return JSONObject()
            return runCatching { JSONObject(text) }
                .getOrElse { throw ApiException(response.code, "后端返回了无法解析的数据") }
        }
    }

    private fun parsePair(obj: JSONObject): PairSession = PairSession(
        pairId = obj.optString("pair_id"),
        state = obj.optString("state", "pending"),
        name = obj.optString("name"),
        ip = obj.optString("ip"),
        expiresIn = obj.optInt("expires_in", 0),
        attempts = obj.optInt("attempts", 0),
    )

    private class VaultCookieJar(
        private val vault: SecurePrefs,
        private val hostProvider: () -> String,
    ) : CookieJar {

        override fun loadForRequest(url: HttpUrl): List<Cookie> {
            val token = vault.get(SecurePrefs.DEVICE_TOKEN) ?: return emptyList()
            if (url.host != hostProvider()) return emptyList()
            return listOfNotNull(Cookie.parse(url, "$COOKIE_NAME=$token; Path=/"))
        }

        override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
            val cookie = cookies.firstOrNull { it.name == COOKIE_NAME } ?: return
            vault.put(SecurePrefs.DEVICE_TOKEN, cookie.value)
        }
    }

    companion object {
        const val COOKIE_NAME = "deckmeido_device"
        const val DEVICE_LABEL = "android-app"
        const val UA = "DeckMeidoAndroid/0.1.0 (Android)"
        private val JSON = "application/json; charset=utf-8".toMediaType()

        fun parseBaseUrl(raw: String): String? {
            var text = raw.trim()
            if (text.isEmpty()) return null
            if (!text.startsWith("http://") && !text.startsWith("https://")) {
                text = "http://$text"
            }
            val url = text.toHttpUrlOrNull() ?: return null
            if (url.host.isBlank()) return null
            val base = url.newBuilder()
                .encodedPath("/")
                .query(null)
                .fragment(null)
                .build()
            return base.toString().trimEnd('/')
        }

        fun parseMessages(arr: JSONArray?): List<ChatMessage> {
            if (arr == null) return emptyList()
            val out = ArrayList<ChatMessage>()
            for (i in 0 until arr.length()) {
                val row = arr.optJSONObject(i) ?: continue
                val role = row.optString("role")
                if (role != "user" && role != "assistant") continue
                val content = row.optString("content")
                if (content.isBlank()) continue
                out.add(ChatMessage(role, content, row.optDouble("ts", 0.0)))
            }
            return out
        }
    }
}
