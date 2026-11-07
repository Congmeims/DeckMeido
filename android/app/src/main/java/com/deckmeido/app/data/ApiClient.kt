package com.deckmeido.app.data

import android.content.ContentResolver
import android.net.Uri
import android.util.Base64
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
import okio.BufferedSink
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
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

    /**
     * 扫码换设备凭据：接口本身不鉴权，靠二维码里的一次性 token。
     * 成功后后端下发与 PIN 配对完全相同的设备 Cookie，由 CookieJar 落盘。
     */
    suspend fun pairQrRedeem(token: String, remember: Boolean, name: String) {
        withContext(Dispatchers.IO) {
            exec(
                "POST",
                "/api/pair/qr/redeem",
                JSONObject()
                    .put("token", token)
                    .put("remember", remember)
                    .put("name", name),
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

    /**
     * 录音 WAV（16k 单声道 16bit）→ 文字。请求体直接是音频字节，不走 multipart；
     * 后端转录完只回文字，客户端填进输入框，不自动发送。
     */
    suspend fun sttTranscribe(wav: ByteArray): String = withContext(Dispatchers.IO) {
        val obj = execJson(
            method = "POST",
            path = "/api/stt/transcribe",
            body = wav.toRequestBody("audio/wav".toMediaType()),
        )
        obj.optString("text")
    }

    /** 语音播报：后端合成 WAV 字节返回，手机本地播放（暂停/继续/停止都在本地控制）。 */
    suspend fun ttsSay(text: String): ByteArray = withContext(Dispatchers.IO) {
        execBytes(
            method = "POST",
            path = "/api/tts/say",
            body = JSONObject().put("text", text).toString().toRequestBody(JSON),
        )
    }

    // ---------- 文件管理 ----------

    /**
     * 列单层目录（/api/browse）。
     *
     * 403 禁止浏览、目录不存在、断链软链接都以 FsListing.error 带回后端原文，
     * 不在这里吞成一句通用提示；只有 401（凭据失效）照旧抛给上层重新配对。
     */
    suspend fun browse(path: String = ""): FsListing = withContext(Dispatchers.IO) {
        val out = call("GET", "/api/browse", null, mapOf("path" to path))
        val obj = runCatching { JSONObject(out.text.orEmpty()) }.getOrNull()
        if (obj == null) {
            if (!out.ok) throw out.error!!
            throw ApiException(200, "后端返回了无法解析的数据")
        }
        if (!out.ok && out.error?.status == 401) throw out.error
        val entries = ArrayList<FsEntry>()
        val arr = obj.optJSONArray("entries")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val row = arr.optJSONObject(i) ?: continue
                val name = row.optString("name")
                if (name.isBlank()) continue
                entries.add(
                    FsEntry(
                        name = name,
                        isDir = row.optBoolean("dir", false),
                        size = if (row.has("size")) row.optLong("size") else null,
                        mtime = if (row.has("mtime")) row.optDouble("mtime") else null,
                        kind = row.optString("kind", "other"),
                        isLink = row.optBoolean("link", false),
                        broken = row.optBoolean("broken", false),
                        linkTarget = row.optString("link_target"),
                    ),
                )
            }
        }
        val detail = obj.optString("error").ifBlank { out.error?.message.orEmpty() }
        FsListing(
            path = obj.optString("path").ifBlank { path },
            parent = if (obj.isNull("parent")) null else obj.optString("parent").ifBlank { null },
            entries = entries,
            zone = obj.optString("zone", "workspace"),
            readonly = obj.optBoolean("readonly", false),
            zoneLabel = obj.optString("zone_label"),
            zoneReason = obj.optString("zone_reason"),
            select = obj.optString("select"),
            error = detail.ifBlank { null },
            linkBroken = obj.optBoolean("link_broken", false),
            linkTarget = obj.optString("link_target"),
        )
    }

    /** 写操作（rename / delete / mkdir / copy / move）；403 时保留后端 error 原文。 */
    suspend fun fsOp(op: String, path: String, path2: String = ""): FsOpResult =
        withContext(Dispatchers.IO) {
            val out = call(
                "POST",
                "/api/fs",
                JSONObject()
                    .put("op", op)
                    .put("path", path)
                    .put("path2", path2)
                    .toString()
                    .toRequestBody(JSON),
                emptyMap(),
            )
            val obj = runCatching { JSONObject(out.text.orEmpty()) }.getOrNull()
            if (obj == null) {
                if (!out.ok && out.error?.status == 401) throw out.error!!
                return@withContext FsOpResult(false, out.error?.message ?: "请求失败")
            }
            if (!out.ok && out.error?.status == 401) throw out.error!!
            FsOpResult(
                ok = obj.optBoolean("ok", false),
                error = obj.optString("error").ifBlank { out.error?.message.orEmpty() },
                reason = obj.optString("reason"),
            )
        }

    /**
     * 下载文件本体：走 /api/preview?raw=1 流式写到 dest，避免大文件整块进内存。
     * 返回写入字节数。
     */
    suspend fun download(path: String, dest: java.io.File): Long = withContext(Dispatchers.IO) {
        val base = baseUrl.toHttpUrlOrNull()
            ?: throw ApiException(0, "服务器地址无效")
        val url = base.newBuilder()
            .encodedPath("/api/preview")
            .addQueryParameter("path", path)
            .addQueryParameter("raw", "1")
            .build()
        val request = Request.Builder().url(url).header("User-Agent", UA).get().build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                val text = response.body?.string().orEmpty()
                val obj = runCatching { JSONObject(text) }.getOrNull()
                val msg = obj?.optString("detail").orEmpty()
                    .ifBlank { obj?.optString("error").orEmpty() }
                    .ifBlank { "下载失败（HTTP ${response.code}）" }
                val reason = obj?.optString("reason").orEmpty()
                throw ApiException(
                    response.code,
                    msg + if (reason.isNotBlank()) "（$reason）" else "",
                )
            }
            val body = response.body ?: throw ApiException(0, "下载失败：响应为空")
            dest.parentFile?.mkdirs()
            body.byteStream().use { input ->
                dest.outputStream().use { output -> input.copyTo(output) }
            }
        }
        dest.length()
    }

    /** 上传到指定目录（/api/upload-to）：原样搬运，不抽取正文、不进对话上下文。 */
    suspend fun uploadTo(dir: String, body: okhttp3.RequestBody) = withContext(Dispatchers.IO) {
        val out = call("POST", "/api/upload-to", body, mapOf("dir" to dir))
        if (out.ok) return@withContext
        // 403 的拒绝原因写在 error / reason（不是 detail），必须原样带出
        val obj = runCatching { JSONObject(out.text.orEmpty()) }.getOrNull()
        val error = obj?.optString("error").orEmpty().ifBlank {
            out.error?.message ?: "上传失败"
        }
        val reason = obj?.optString("reason").orEmpty()
        throw ApiException(
            out.error?.status ?: 0,
            error + if (reason.isNotBlank()) "（$reason）" else "",
        )
    }

    // ---------- 远程终端（契约见 docs/remote-terminal-api.md） ----------

    /** 总开关在 /api/config 的 terminal_remote_shell（默认关闭）。 */
    suspend fun config(): JSONObject = withContext(Dispatchers.IO) {
        exec("GET", "/api/config")
    }

    /** 会话列表 + 开关 + 默认值；开关关闭时 REST 会 403 remote_shell_disabled。 */
    suspend fun terminalSessions(): TerminalSessionsResult = withContext(Dispatchers.IO) {
        val obj = exec("GET", "/api/terminal/sessions")
        TerminalSessionsResult(
            enabled = obj.optBoolean("enabled", false),
            settings = obj.optJSONObject("settings")?.let { parseTerminalSettings(it) },
            sessions = parseSessionList(obj.optJSONArray("sessions")),
        )
    }

    suspend fun createTerminalSession(
        cwd: String = "",
        source: String = "android",
    ): TerminalSessionInfo = withContext(Dispatchers.IO) {
        val out = terminalCall(
            "POST",
            "/api/terminal/sessions",
            JSONObject()
                .put("cwd", cwd)
                .put("cols", 80)
                .put("rows", 30)
                .put("source", source),
        )
        if (!out.ok) terminalFail(out)
        val row = JSONObject(out.text.orEmpty()).optJSONObject("session")
            ?: throw ApiException(200, "后端没有返回终端会话")
        parseTerminalSession(row)
    }

    suspend fun terminalSession(sid: String): TerminalSessionInfo = withContext(Dispatchers.IO) {
        val obj = exec("GET", "/api/terminal/sessions/$sid")
        obj.optJSONObject("session")?.let { parseTerminalSession(it) }
            ?: throw ApiException(200, "后端没有返回终端会话")
    }

    /**
     * 提交一条命令。428 转成 [TerminalConfirmException] 交给界面弹二次确认，
     * 界面带 confirm=true 原样重提；这里绝不静默重试。
     */
    suspend fun terminalExec(
        sid: String,
        command: String,
        confirm: Boolean,
    ): JSONObject = withContext(Dispatchers.IO) {
        val out = terminalCall(
            "POST",
            "/api/terminal/sessions/$sid/exec",
            JSONObject()
                .put("command", command)
                .put("confirm", confirm)
                .put("source", "android"),
        )
        if (!out.ok) {
            if (out.error?.status == 428) throw parseConfirm(out.text, command)
            terminalFail(out)
        }
        JSONObject(out.text.orEmpty())
    }

    /** 原始 stdin 写入：交互程序的 Ctrl+C / Tab 等按键走这里。 */
    suspend fun terminalInput(
        sid: String,
        data: String,
        encoding: String = "utf-8",
        confirm: Boolean = false,
    ) = withContext(Dispatchers.IO) {
        val out = terminalCall(
            "POST",
            "/api/terminal/sessions/$sid/input",
            JSONObject()
                .put("data", data)
                .put("encoding", encoding)
                .put("confirm", confirm),
        )
        if (!out.ok) {
            if (out.error?.status == 428) throw parseConfirm(out.text, data)
            terminalFail(out)
        }
    }

    /** 断线续读：按游标分页拉取 base64 输出块。 */
    suspend fun terminalOutput(
        sid: String,
        cursor: Int,
        maxBytes: Int = 65536,
    ): TerminalOutputPage = withContext(Dispatchers.IO) {
        val out = call(
            "GET",
            "/api/terminal/sessions/$sid/output",
            null,
            mapOf("cursor" to cursor.toString(), "max_bytes" to maxBytes.toString()),
        )
        if (!out.ok) terminalFail(out)
        val obj = JSONObject(out.text.orEmpty())
        val chunks = ArrayList<ByteArray>()
        obj.optJSONArray("chunks")?.let { arr ->
            for (i in 0 until arr.length()) {
                val row = arr.optJSONObject(i) ?: continue
                val raw = row.optString("data")
                chunks.add(
                    runCatching { Base64.decode(raw, Base64.DEFAULT) }.getOrDefault(ByteArray(0)),
                )
            }
        }
        TerminalOutputPage(
            chunks = chunks,
            nextCursor = obj.optInt("next_cursor", cursor),
            dropped = obj.optBoolean("dropped", false),
            truncated = obj.optBoolean("truncated", false),
            session = obj.optJSONObject("session")?.let { parseTerminalSession(it) },
        )
    }

    suspend fun terminateTerminalSession(sid: String) = withContext(Dispatchers.IO) {
        val out = terminalCall("POST", "/api/terminal/sessions/$sid/terminate", JSONObject())
        if (!out.ok) terminalFail(out)
    }

    /** 输出订阅：WS /ws/terminal/{id}?cursor=N，带设备 Cookie 走同一个客户端。 */
    fun newTerminalWebSocket(
        sid: String,
        cursor: Int,
        listener: WebSocketListener,
    ): WebSocket? {
        val base = baseUrl.toHttpUrlOrNull() ?: return null
        val url = base.newBuilder()
            .encodedPath("/ws/terminal/$sid")
            .addQueryParameter("cursor", cursor.toString())
            .build()
        val request = Request.Builder().url(url).header("User-Agent", UA).build()
        return wsHttp.newWebSocket(request, listener)
    }

    private fun terminalCall(method: String, path: String, body: JSONObject): RawResult =
        call(method, path, body.toString().toRequestBody(JSON), emptyMap())

    private fun terminalFail(out: RawResult): Nothing {
        val obj = runCatching { JSONObject(out.text.orEmpty()) }.getOrNull()
        val status = out.error?.status ?: 0
        val reason = obj?.optString("reason").orEmpty()
            .ifBlank { out.error?.message.orEmpty() }
            .ifBlank { "终端请求失败（HTTP $status）" }
        val code = obj?.optString("error_code").orEmpty()
            .ifBlank { out.error?.code.orEmpty() }
        throw ApiException(status, reason, code)
    }

    private fun parseConfirm(text: String?, fallbackCommand: String): TerminalConfirmException {
        val obj = runCatching { JSONObject(text.orEmpty()) }.getOrNull()
        return TerminalConfirmException(
            TerminalConfirmPrompt(
                command = obj?.optString("command").orEmpty().ifBlank { fallbackCommand },
                level = obj?.optString("level").orEmpty(),
                rule = obj?.optString("rule").orEmpty(),
                reason = obj?.optString("reason").orEmpty().ifBlank { "危险命令需要确认" },
            ),
        )
    }

    private fun parseTerminalSettings(obj: JSONObject) = TerminalSettings(
        // settings.public() 用的键是 remote_shell / idle_timeout_seconds
        enabled = obj.optBoolean("remote_shell", obj.optBoolean("enabled", false)),
        idleTimeout = obj.optInt(
            "idle_timeout_seconds",
            obj.optInt("idle_timeout", 0),
        ),
        maxSessions = obj.optInt("max_sessions", 3),
        maxOutputBytes = obj.optLong("max_output_bytes"),
        shell = obj.optString("shell"),
        cwd = obj.optString("cwd"),
    )

    private fun parseSessionList(arr: JSONArray?): List<TerminalSessionInfo> {
        if (arr == null) return emptyList()
        val out = ArrayList<TerminalSessionInfo>()
        for (i in 0 until arr.length()) {
            val row = arr.optJSONObject(i) ?: continue
            out.add(parseTerminalSession(row))
        }
        return out
    }

    /** WS 的 hello / REST 的 session 快照都用这一份解析。 */
    fun parseTerminalSession(row: JSONObject): TerminalSessionInfo {
        val last = row.optJSONObject("last_command")
        return TerminalSessionInfo(
            id = row.optString("id"),
            pid = row.optInt("pid"),
            state = row.optString("state", "running"),
            exitCode = if (row.has("exit_code") && row.isNull("exit_code").not()) {
                row.optInt("exit_code")
            } else {
                null
            },
            created = row.optDouble("created"),
            started = row.optDouble("started"),
            cwd = row.optString("cwd"),
            cols = row.optInt("cols"),
            rows = row.optInt("rows"),
            backend = row.optString("backend"),
            outputTotal = row.optLong("output_total"),
            outputNextSeq = row.optInt("output_next_seq"),
            truncated = row.optBoolean("truncated"),
            terminated = row.optBoolean("terminated"),
            completedExecs = row.optInt("completed_execs"),
            lastExitCode = last?.let { if (it.has("exit_code") && it.isNull("exit_code").not()) it.optInt("exit_code") else null },
        )
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
    ): JSONObject = execJson(
        method,
        path,
        if (method == "POST") (body?.toString() ?: "{}").toRequestBody(JSON) else null,
        query,
    )

    private fun execJson(
        method: String,
        path: String,
        body: okhttp3.RequestBody? = null,
        query: Map<String, String> = emptyMap(),
    ): JSONObject {
        val text = execText(method, path, body, query)
        if (text.isBlank()) return JSONObject()
        return runCatching { JSONObject(text) }
            .getOrElse { throw ApiException(200, "后端返回了无法解析的数据") }
    }

    private fun execBytes(
        method: String,
        path: String,
        body: okhttp3.RequestBody? = null,
        query: Map<String, String> = emptyMap(),
    ): ByteArray = call(method, path, body, query, binary = true).let { bytes ->
        if (!bytes.ok) throw bytes.error!!
        bytes.data ?: ByteArray(0)
    }

    private fun execText(
        method: String,
        path: String,
        body: okhttp3.RequestBody? = null,
        query: Map<String, String> = emptyMap(),
    ): String = call(method, path, body, query).let { out ->
        if (!out.ok) throw out.error!!
        out.text.orEmpty()
    }

    private class RawResult(
        val ok: Boolean,
        val text: String? = null,
        val data: ByteArray? = null,
        val error: ApiException? = null,
    )

    /** 统一的请求/错误处理：HTTP 非 2xx 时按后端的 detail + code 抛 ApiException。 */
    private fun call(
        method: String,
        path: String,
        body: okhttp3.RequestBody?,
        query: Map<String, String>,
        binary: Boolean = false,
    ): RawResult {
        val base = baseUrl.toHttpUrlOrNull()
            ?: return RawResult(false, error = ApiException(0, "服务器地址无效"))
        val url = base.newBuilder().apply {
            encodedPath(path)
            query.forEach { (k, v) -> addQueryParameter(k, v) }
        }.build()

        val builder = Request.Builder()
            .url(url)
            .header("User-Agent", UA)
        when (method) {
            "POST" -> builder.post(body ?: "{}".toRequestBody(JSON))
            "DELETE" -> builder.delete()
            else -> builder.get()
        }

        http.newCall(builder.build()).execute().use { response ->
            if (!response.isSuccessful) {
                val text = response.body?.string().orEmpty()
                val error = runCatching { JSONObject(text) }.getOrNull()
                // 错误原文三种口径：普通接口 detail、文件接口 error、终端接口 reason
                val detail = error?.optString("detail").orEmpty()
                    .ifBlank { error?.optString("error").orEmpty() }
                    .ifBlank { error?.optString("reason").orEmpty() }
                return RawResult(
                    false,
                    // 非 2xx 也把原始 body 带回去：/api/browse 与 /api/fs 的错误
                    // 写在 error / reason 字段里，不是 detail
                    text = text,
                    error = ApiException(
                        response.code,
                        detail.ifBlank { "请求失败（HTTP ${response.code}）" },
                        error?.optString("error_code").orEmpty()
                            .ifBlank { error?.optString("code").orEmpty() },
                    ),
                )
            }
            val bytesBody = response.body
            if (binary) {
                return RawResult(true, data = bytesBody?.bytes() ?: ByteArray(0))
            }
            return RawResult(true, text = bytesBody?.string().orEmpty())
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

        /**
         * 解析 Deck 端二维码。
         *
         * 支持两种形式：
         * - `deckmeido://pair?v=1&u=<编码后的后端地址>&t=<token>`（Deck 默认生成）
         * - `http(s)://host:port/pair?token=<token>`（手写/旧链接）
         * 地址是局域网 IP、Tailscale 100.x 还是 MagicDNS 名称都原样交给 parseBaseUrl。
         */
        fun parseQrPayload(raw: String): QrPairPayload? {
            val text = raw.trim()
            if (text.isEmpty()) return null
            val uri = runCatching { java.net.URI(text) }.getOrNull() ?: return null
            val scheme = uri.scheme?.lowercase() ?: return null
            val params = parseQuery(uri.rawQuery)
            val token = params["t"] ?: params["token"] ?: ""
            if (token.isBlank()) return null
            val address = when (scheme) {
                "deckmeido" -> params["u"].orEmpty()
                "http", "https" -> {
                    if (uri.path.orEmpty().trimEnd('/') != "/pair") return null
                    val host = uri.host ?: return null
                    val port = if (uri.port > 0) ":${uri.port}" else ""
                    "$scheme://$host$port"
                }
                else -> return null
            }
            val base = parseBaseUrl(address) ?: return null
            return QrPairPayload(base, token)
        }

        private fun parseQuery(raw: String?): Map<String, String> {
            if (raw.isNullOrBlank()) return emptyMap()
            return raw.split('&').mapNotNull { part ->
                val i = part.indexOf('=')
                if (i <= 0) return@mapNotNull null
                val key = java.net.URLDecoder.decode(part.substring(0, i), "UTF-8")
                val value = java.net.URLDecoder.decode(part.substring(i + 1), "UTF-8")
                key to value
            }.toMap()
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

/**
 * 把系统文件选择器返回的 Uri 直接流式写进 multipart，不先读进内存。
 * 400~500MB 的文件走这里也不会把手机内存打满。
 */
internal class ContentUriRequestBody(
    private val resolver: ContentResolver,
    private val uri: Uri,
    private val mime: String,
) : okhttp3.RequestBody() {

    override fun contentType() = mime.takeIf { it.isNotBlank() }?.toMediaType()

    override fun contentLength(): Long = -1L

    override fun writeTo(sink: BufferedSink) {
        val input = resolver.openInputStream(uri) ?: throw IOException("无法读取所选文件")
        input.use { ins ->
            val buffer = ByteArray(8192)
            while (true) {
                val n = ins.read(buffer)
                if (n <= 0) break
                sink.write(buffer, 0, n)
            }
        }
    }
}
