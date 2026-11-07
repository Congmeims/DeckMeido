package com.deckmeido.app.ui

import android.app.Application
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.deckmeido.app.audio.VoicePlayer
import com.deckmeido.app.audio.VoiceRecorder
import com.deckmeido.app.data.ApiClient
import com.deckmeido.app.data.ApiException
import com.deckmeido.app.data.AuthInfo
import com.deckmeido.app.data.ChatMessage
import com.deckmeido.app.data.ContentUriRequestBody
import com.deckmeido.app.data.FsEntry
import com.deckmeido.app.data.FsListing
import com.deckmeido.app.data.PairSession
import com.deckmeido.app.data.SecurePrefs
import com.deckmeido.app.data.SessionItem
import com.deckmeido.app.data.TerminalConfirmException
import com.deckmeido.app.data.TerminalConfirmPrompt
import com.deckmeido.app.data.TerminalSessionInfo
import com.deckmeido.app.data.TerminalSettings
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MultipartBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** 底部五项导航。顺序就是底部导航与翻页顺序。 */
enum class Tab(val label: String) {
    Chat("聊天"),
    Sessions("会话"),
    Files("文件"),
    Terminal("终端"),
    Settings("设置"),
}

sealed class ConnState {
    data object Idle : ConnState()
    data object Connecting : ConnState()
    data object Online : ConnState()
    data class Offline(val reason: String) : ConnState()
}

/**
 * P0 的全部客户端状态与协议编排。
 *
 * 关键约束（后端是全局单轮模型）：`running` 为真时再发 chat 会并行开新轮，
 * 所以发送统一走 outbox 队列，只有收到「本轮确实结束」的 status 才放行下一条。
 */
class DeckMeidoViewModel(app: Application) : AndroidViewModel(app) {

    private val prefs = app.getSharedPreferences("deckmeido", Context.MODE_PRIVATE)
    private val vault = SecurePrefs(app)
    private val api = ApiClient(vault)
    private val main = Handler(Looper.getMainLooper())

    // ---------- 连接 ----------
    var baseUrlInput by mutableStateOf(prefs.getString(KEY_SERVER, "").orEmpty())
    var connState by mutableStateOf<ConnState>(ConnState.Idle)
    var authInfo by mutableStateOf<AuthInfo?>(null)
    var connecting by mutableStateOf(false)
    var notice by mutableStateOf<String?>(null)

    // ---------- 配对 ----------
    var pairing by mutableStateOf<PairSession?>(null)
    var pinInput by mutableStateOf("")
    var rememberDevice by mutableStateOf(true)
    var pairingBusy by mutableStateOf(false)

    // ---------- 扫码配对 ----------
    var qrBusy by mutableStateOf(false)

    // ---------- 语音 ----------
    var recording by mutableStateOf(false)
    var recordSeconds by mutableStateOf(0)
    var sttBusy by mutableStateOf(false)
    var ttsLoading by mutableStateOf(false)
    /** 音频已加载：播放中或暂停中（两者互斥，暂停时 ttsPaused 为真） */
    var ttsActive by mutableStateOf(false)
    var ttsPaused by mutableStateOf(false)

    private val recorder = VoiceRecorder()
    private val player = VoicePlayer()
    private var recordJob: Job? = null

    // ---------- 主导航 ----------
    /** 已通过设备凭据：显示五项底部导航；否则显示连接/扫码页 */
    var mainReady by mutableStateOf(false)
    var tab by mutableStateOf(Tab.Sessions)
    /** 扫码页是独立全屏页，不参与底部导航 */
    var scanOpen by mutableStateOf(false)

    // ---------- 会话 ----------
    var sessions by mutableStateOf<List<SessionItem>>(emptyList())
    var sessionsLoading by mutableStateOf(false)
    var switchingSession by mutableStateOf(false)
    var currentSid by mutableStateOf("")

    // ---------- 文件管理 ----------
    var fsListing by mutableStateOf<FsListing?>(null)
    var fsPath by mutableStateOf("")
    var fsLoading by mutableStateOf(false)
    var fsOpBusy by mutableStateOf(false)
    var fsError by mutableStateOf<String?>(null)

    // ---------- 远程终端（契约见 docs/remote-terminal-api.md） ----------
    /** null = 还没探测；false = 掌机侧总开关关闭，页面显示空状态而不是网络错误 */
    var termEnabled by mutableStateOf<Boolean?>(null)
    var termSettings by mutableStateOf<TerminalSettings?>(null)
    var termSessions by mutableStateOf<List<TerminalSessionInfo>>(emptyList())
    var termLoading by mutableStateOf(false)
    var termError by mutableStateOf<String?>(null)
    /** 当前打开的会话；null 时显示会话列表 */
    var termActive by mutableStateOf<TerminalSessionInfo?>(null)
    var termOutput by mutableStateOf("")
    var termCursor by mutableStateOf(0)
    var termExited by mutableStateOf(false)
    var termTruncated by mutableStateOf(false)
    /** WS 是否在线；false 时顶部显示「已断线，正在重连/轮询」 */
    var termLive by mutableStateOf(false)
    var termBusy by mutableStateOf(false)
    var termStopping by mutableStateOf(false)
    var termCommand by mutableStateOf("")
    var termHistory by mutableStateOf<List<String>>(emptyList())
    var termConfirm by mutableStateOf<TerminalConfirmPrompt?>(null)

    private var termWs: WebSocket? = null
    private var termWsSid: String? = null
    private var termRetry = 0
    private var termReconnectJob: Job? = null
    private var termPingJob: Job? = null
    private var termPollJob: Job? = null
    private var termConfirmFromInput = false
    private var termDecoder = newTermDecoder()
    private var termPendingBytes = ByteArray(0)

    // ---------- 聊天 ----------
    var messages by mutableStateOf<List<ChatMessage>>(emptyList())
    var streamingText by mutableStateOf("")
    var thoughtText by mutableStateOf("")
    var toolNote by mutableStateOf("")
    var askQuestion by mutableStateOf<String?>(null)
    var askOptions by mutableStateOf<List<String>>(emptyList())
    var running by mutableStateOf(false)
    var runningSince by mutableStateOf(0.0)
    var inputText by mutableStateOf("")
    var queuedCount by mutableStateOf(0)
    var stopping by mutableStateOf(false)

    private val outbox = ArrayDeque<String>()

    /** 已发出 chat、还没收到 assistant_message：用来挡住并行的第二轮。 */
    var awaitingReply by mutableStateOf(false)
    private var dispatchAt = 0L
    private var activeTurnId: String? = null

    private var socket: WebSocket? = null
    private var wantConnection = false
    private var reconnectAttempt = 0
    private var reconnectJob: Job? = null
    private var heartbeatJob: Job? = null
    private var lastAlive = 0L
    private var pairJob: Job? = null

    init {
        val saved = baseUrlInput.trim()
        if (saved.isNotEmpty()) {
            connect(saved, auto = true)
        }
    }

    // ---------- 连接与配对 ----------

    fun connect(raw: String = baseUrlInput, auto: Boolean = false) {
        if (connecting) return
        if (!api.setBaseUrl(raw)) {
            notice = "地址格式不对，例如 192.168.1.20:18765"
            return
        }
        baseUrlInput = api.baseUrl
        prefs.edit().putString(KEY_SERVER, api.baseUrl).apply()

        viewModelScope.launch {
            connecting = true
            connState = ConnState.Connecting
            try {
                val info = withContext(Dispatchers.IO) { api.probe() }
                authInfo = info
                if (info.authenticated) {
                    enterOnline()
                } else {
                    connState = ConnState.Idle
                    pairing = null
                    if (!info.lanEnable) {
                        notice = "后端未开启局域网访问，请先在 Deck 设置里打开"
                    } else if (!auto) {
                        notice = null
                    }
                }
            } catch (e: Exception) {
                connState = ConnState.Idle
                notice = friendly(e)
            } finally {
                connecting = false
            }
        }
    }

    fun beginPairing() {
        if (pairingBusy) return
        viewModelScope.launch {
            pairingBusy = true
            notice = null
            try {
                pairing = withContext(Dispatchers.IO) { api.pairStart() }
                pinInput = ""
                startPairPolling()
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                pairingBusy = false
            }
        }
    }

    fun cancelPairing() {
        pairJob?.cancel()
        pairJob = null
        pairing = null
        pinInput = ""
    }

    // ---------- 扫码配对 ----------

    fun startScan() {
        notice = null
        scanOpen = true
    }

    fun cancelScan() {
        scanOpen = false
    }

    /**
     * 扫到二维码：先探地址是否可达，再用一次性 token 换设备凭据。
     * token 由 Deck 生成、90 秒有效、只能换一次；失败按后端 code 给出明确原因。
     */
    fun onQrScanned(raw: String) {
        if (qrBusy) return
        val payload = ApiClient.parseQrPayload(raw)
        if (payload == null) {
            notice = "这不是 DeckMeido 的配对二维码"
            return
        }
        viewModelScope.launch {
            qrBusy = true
            notice = null
            try {
                if (!api.setBaseUrl(payload.address)) {
                    notice = "二维码里的地址无效"
                    return@launch
                }
                baseUrlInput = api.baseUrl
                prefs.edit().putString(KEY_SERVER, api.baseUrl).apply()

                val info = try {
                    withContext(Dispatchers.IO) { api.probe() }
                } catch (e: Exception) {
                    notice = "地址不可达：请确认手机与 Deck 在同一局域网或同一 Tailscale 网络"
                    return@launch
                }
                authInfo = info
                if (info.authenticated) {
                    // 这台手机已经有有效凭据，二维码不用再换一次
                    enterOnline()
                    return@launch
                }
                if (!info.lanEnable) {
                    notice = "后端未开启局域网访问，请先在 Deck 设置里打开"
                    return@launch
                }

                try {
                    withContext(Dispatchers.IO) {
                        api.pairQrRedeem(payload.token, rememberDevice, "Android 手机")
                    }
                } catch (e: ApiException) {
                    notice = qrError(e)
                    return@launch
                } catch (e: Exception) {
                    notice = "地址不可达：配对请求没有送达 Deck"
                    return@launch
                }

                val after = withContext(Dispatchers.IO) { api.probe() }
                authInfo = after
                if (after.authenticated) {
                    pairing = null
                    pinInput = ""
                    enterOnline()
                } else {
                    notice = "扫码配对未生效，请让 Deck 刷新二维码后重试"
                }
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                qrBusy = false
            }
        }
    }

    private fun qrError(e: ApiException): String = when (e.code) {
        "expired" -> "二维码已过期，请在 Deck 上刷新后重新扫码"
        "used" -> "二维码已被使用，请刷新后重新扫码"
        "revoked" -> "二维码已被 Deck 撤销，请刷新后重新扫码"
        "invalid" -> "二维码无效或已被刷新，请在 Deck 上生成新的二维码"
        "too_many" -> "尝试过于频繁，请等一分钟再试"
        else -> e.message.ifBlank { "扫码配对失败" }
    }

    fun confirmPairing() {
        val session = pairing ?: return
        val pin = pinInput.trim()
        if (pin.length != 4 || pin.any { !it.isDigit() }) {
            notice = "请输入掌机上显示的 4 位 PIN"
            return
        }
        if (pairingBusy) return
        viewModelScope.launch {
            pairingBusy = true
            notice = null
            try {
                withContext(Dispatchers.IO) {
                    api.pairConfirm(session.pairId, pin, rememberDevice)
                }
                pairJob?.cancel()
                val info = withContext(Dispatchers.IO) { api.probe() }
                authInfo = info
                if (info.authenticated) {
                    pairing = null
                    enterOnline()
                } else {
                    notice = "配对未生效，请重新发起配对"
                }
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                pairingBusy = false
            }
        }
    }

    private fun startPairPolling() {
        pairJob?.cancel()
        pairJob = viewModelScope.launch {
            while (isActive) {
                delay(1500)
                val current = pairing ?: break
                try {
                    val fresh = withContext(Dispatchers.IO) { api.pairStatus(current.pairId) }
                    pairing = fresh
                    if (fresh.state in setOf("expired", "denied", "failed", "paired")) {
                        break
                    }
                } catch (e: Exception) {
                    break
                }
            }
        }
    }

    private fun enterOnline() {
        mainReady = true
        tab = Tab.Sessions
        wantConnection = true
        startSocket()
        refreshSessions()
    }

    fun disconnect() {
        wantConnection = false
        reconnectJob?.cancel()
        reconnectJob = null
        heartbeatJob?.cancel()
        heartbeatJob = null
        socket?.close(1000, "bye")
        socket = null
        connState = ConnState.Idle
        mainReady = false
        scanOpen = false
        tab = Tab.Sessions
    }

    /** 清除本机设备凭据：下次必须重新配对（凭据本身仍留在掌机设备表里）。 */
    fun forgetDevice() {
        disconnect()
        api.clearToken()
        authInfo = authInfo?.copy(authenticated = false)
        notice = "已清除本机凭据，请重新配对"
    }

    // ---------- WebSocket ----------

    private fun startSocket() {
        if (!wantConnection || socket != null) return
        connState = ConnState.Connecting
        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                main.post {
                    if (socket !== webSocket) return@post
                    reconnectAttempt = 0
                    connState = ConnState.Online
                    lastAlive = System.currentTimeMillis()
                    wsSend(JSONObject().put("type", "history"))
                    startHeartbeat()
                    refreshSessions()
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
                main.post { handleWs(obj) }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                main.post { wsDown(webSocket, code, reason) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post { wsDown(webSocket, -1, t.message ?: "连接中断") }
            }
        }
        val ws = api.newWebSocket(listener)
        if (ws == null) {
            connState = ConnState.Offline("服务器地址无效")
            return
        }
        socket = ws
    }

    private fun wsDown(ws: WebSocket?, code: Int, reason: String) {
        if (ws != null && socket !== ws) return
        socket = null
        heartbeatJob?.cancel()
        heartbeatJob = null
        if (code == 4401) {
            // 后端明确说「未配对」：凭据被撤销或过期，清掉重配
            wantConnection = false
            api.clearToken()
            connState = ConnState.Idle
            authInfo = AuthInfo(false, false, authInfo?.lanEnable ?: true, authInfo?.version.orEmpty())
            pairing = null
            mainReady = false
            scanOpen = false
            notice = "设备凭据已失效，请重新配对"
            return
        }
        if (!wantConnection) {
            connState = ConnState.Idle
            return
        }
        scheduleReconnect(reason.ifBlank { "连接已断开" })
    }

    private fun scheduleReconnect(reason: String) {
        reconnectJob?.cancel()
        val wait = minOf(15_000L, 1_000L shl minOf(reconnectAttempt, 4))
        reconnectAttempt++
        connState = ConnState.Offline(reason)
        reconnectJob = viewModelScope.launch {
            delay(wait)
            startSocket()
        }
    }

    private fun startHeartbeat() {
        heartbeatJob?.cancel()
        heartbeatJob = viewModelScope.launch {
            while (isActive) {
                delay(20_000)
                val ws = socket ?: break
                if (System.currentTimeMillis() - lastAlive > 45_000) {
                    ws.cancel()
                    wsDown(ws, -1, "心跳超时")
                    break
                }
                ws.send("""{"type":"ping"}""")
            }
        }
    }

    private fun wsSend(obj: JSONObject) {
        socket?.send(obj.toString())
    }

    // ---------- 会话列表 / 只读历史 ----------

    fun refreshSessions() {
        if (connState !is ConnState.Online) return
        viewModelScope.launch {
            sessionsLoading = true
            try {
                val result = withContext(Dispatchers.IO) { api.sessions() }
                sessions = result.items
                if (currentSid.isBlank()) currentSid = result.current
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                sessionsLoading = false
            }
        }
    }

    /**
     * 点会话直接进聊天（不再有独立只读预览页）：切换是全局动作，
     * 正在生成回答时不允许切，避免把别人的一轮打断。
     */
    fun openSession(sid: String) {
        if (sid.isBlank()) return
        if (sid == currentSid) {
            tab = Tab.Chat
            wsSend(JSONObject().put("type", "history"))
            return
        }
        if (running || awaitingReply) {
            notice = "还有一轮回答在生成，等结束再切换"
            return
        }
        if (switchingSession) return
        viewModelScope.launch {
            switchingSession = true
            try {
                withContext(Dispatchers.IO) { api.switchSession(sid) }
                currentSid = sid
                streamingText = ""
                thoughtText = ""
                toolNote = ""
                askQuestion = null
                askOptions = emptyList()
                tab = Tab.Chat
                wsSend(JSONObject().put("type", "history"))
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                switchingSession = false
            }
        }
    }

    // ---------- 发送与排队 ----------

    fun send() {
        val text = inputText.trim()
        if (text.isEmpty()) return
        inputText = ""
        enqueueOrSend(text)
    }

    fun answerAsk(option: String) {
        askQuestion = null
        askOptions = emptyList()
        enqueueOrSend(option)
    }

    private fun enqueueOrSend(text: String) {
        if (running || awaitingReply || connState !is ConnState.Online) {
            outbox.addLast(text)
            queuedCount = outbox.size
            if (connState !is ConnState.Online) {
                notice = "当前未连接，已放入待发送队列"
            }
            return
        }
        dispatch(text)
    }

    private fun dispatch(text: String) {
        val ws = socket
        if (ws == null) {
            outbox.addFirst(text)
            queuedCount = outbox.size
            notice = "连接已断开，正在重连"
            return
        }
        val turnId = UUID.randomUUID().toString()
        activeTurnId = turnId
        dispatchAt = System.currentTimeMillis()
        awaitingReply = true
        streamingText = ""
        thoughtText = ""
        toolNote = ""
        askQuestion = null
        askOptions = emptyList()
        ws.send(
            JSONObject()
                .put("type", "chat")
                .put("text", text)
                .put("client", "android")
                .put("device", ApiClient.DEVICE_LABEL)
                .put("turn_id", turnId)
                .toString(),
        )
    }

    private fun maybeDispatchNext() {
        if (outbox.isEmpty()) {
            queuedCount = 0
            return
        }
        if (running || awaitingReply || connState !is ConnState.Online) return
        val next = outbox.removeFirst()
        queuedCount = outbox.size
        dispatch(next)
    }

    fun clearQueue() {
        outbox.clear()
        queuedCount = 0
    }

    fun stop() {
        if (stopping) return
        outbox.clear()
        queuedCount = 0
        viewModelScope.launch {
            stopping = true
            try {
                withContext(Dispatchers.IO) { api.cancel() }
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                stopping = false
            }
        }
    }

    // ---------- 文件管理 ----------

    /** 列目录；path 为空回到工作区。错误一律保留后端原文（含 403 forbidden）。 */
    fun loadFiles(path: String = fsPath) {
        if (connState !is ConnState.Online) {
            fsError = "未连接：请先在设置里恢复连接"
            return
        }
        viewModelScope.launch {
            fsLoading = true
            fsError = null
            try {
                val listing = withContext(Dispatchers.IO) { api.browse(path) }
                fsListing = listing
                fsPath = listing.path
            } catch (e: ApiException) {
                fsError = e.message.ifBlank { "读取目录失败" }
            } catch (e: Exception) {
                fsError = friendly(e)
            } finally {
                fsLoading = false
            }
        }
    }

    fun enterFsEntry(entry: FsEntry) {
        if (!entry.isDir) return
        loadFiles(childPath(fsPath, entry.name))
    }

    fun fsGoParent() {
        val parent = fsListing?.parent ?: return
        loadFiles(parent)
    }

    /** 下载文件本体：落到应用专属下载目录，路径在提示里写清楚（无需存储权限）。 */
    fun downloadEntry(entry: FsEntry) {
        if (entry.isDir || fsOpBusy) return
        val source = childPath(fsPath, entry.name)
        viewModelScope.launch {
            fsOpBusy = true
            try {
                val app = getApplication<Application>()
                val dir = app.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                    ?: app.filesDir
                val dest = File(dir, entry.name)
                val size = withContext(Dispatchers.IO) { api.download(source, dest) }
                notice = "已保存 ${entry.name}（${formatBytes(size)}）到：${dest.absolutePath}"
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                fsOpBusy = false
            }
        }
    }

    fun deleteEntry(entry: FsEntry) {
        runFsOp("delete", childPath(fsPath, entry.name))
    }

    fun renameEntry(entry: FsEntry, newName: String) {
        val name = newName.trim()
        if (name.isEmpty() || name == entry.name) return
        runFsOp("rename", childPath(fsPath, entry.name), childPath(fsPath, name))
    }

    /** 上传：后端 /api/upload-to 已支持（原样搬运，不抽正文、不进对话上下文）。 */
    fun uploadSelection(uri: Uri) {
        if (fsOpBusy) return
        val dir = fsPath
        val resolver = getApplication<Application>().contentResolver
        val name = queryDisplayName(resolver, uri) ?: uri.lastPathSegment ?: "upload.bin"
        val mime = resolver.getType(uri).orEmpty()
        viewModelScope.launch {
            fsOpBusy = true
            try {
                val body = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("file", name, ContentUriRequestBody(resolver, uri, mime))
                    .build()
                withContext(Dispatchers.IO) { api.uploadTo(dir, body) }
                notice = "已上传 $name 到 $dir"
                loadFiles(dir)
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                fsOpBusy = false
            }
        }
    }

    private fun runFsOp(op: String, path: String, path2: String = "") {
        if (fsOpBusy) return
        viewModelScope.launch {
            fsOpBusy = true
            try {
                val result = withContext(Dispatchers.IO) { api.fsOp(op, path, path2) }
                if (result.ok) {
                    notice = if (op == "delete") "已删除 ${path.substringAfterLast('/')}" else "已重命名"
                    loadFiles(fsPath)
                } else {
                    // 后端原文（403 read_only_zone / 只读挂载 / 重名…），不静默失败
                    val code = result.reason.takeIf { it.isNotBlank() }
                    notice = result.error.ifBlank { "操作失败" } +
                        if (code != null) "（$code）" else ""
                }
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                fsOpBusy = false
            }
        }
    }

    private fun queryDisplayName(
        resolver: android.content.ContentResolver,
        uri: Uri,
    ): String? {
        val cursor = runCatching { resolver.query(uri, null, null, null, null) }
            .getOrNull() ?: return null
        return cursor.use {
            val idx = it.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            if (idx < 0 || !it.moveToFirst()) null else it.getString(idx)
        }
    }

    /** 后端认的是绝对路径，直接按 / 拼；Windows 开发机的 M:/ 路径也按同一方式拼。 */
    private fun childPath(base: String, name: String): String {
        val root = base.trimEnd('/')
        return if (root.isEmpty()) name else "$root/$name"
    }

    // ---------- 远程终端 ----------

    /** 进入终端页 / 重连后刷新：未开会话时按开关拉列表，已开会话时刷新会话状态。 */
    fun refreshTerminal() {
        viewModelScope.launch {
            termLoading = true
            termError = null
            try {
                val active = termActive
                if (active != null) {
                    val info = withContext(Dispatchers.IO) { api.terminalSession(active.id) }
                    termActive = info
                    if (!info.alive) {
                        termExited = true
                        pullTermPage()
                    }
                    return@launch
                }
                // /api/config 总能读：先区分「开关关闭」与「网络错误」
                val enabled = withContext(Dispatchers.IO) {
                    api.config().optBoolean("terminal_remote_shell", false)
                }
                termEnabled = enabled
                if (enabled) loadTermSessions()
            } catch (e: Exception) {
                failTerm(e, "无法读取终端会话")
            } finally {
                termLoading = false
            }
        }
    }

    private suspend fun loadTermSessions() {
        val result = withContext(Dispatchers.IO) { api.terminalSessions() }
        termEnabled = result.enabled
        termSettings = result.settings
        termSessions = result.sessions
    }

    /** cwd 留空 = 用掌机家目录；cols/rows 固定 80×30，后续可用 resize 调。 */
    fun createTerminalSession(cwd: String = "") {
        if (termBusy) return
        viewModelScope.launch {
            termBusy = true
            termError = null
            try {
                val info = withContext(Dispatchers.IO) { api.createTerminalSession(cwd) }
                termSessions = termSessions + info
                openTerminalSession(info)
            } catch (e: Exception) {
                // 429 too_many_sessions / 400 cwd_missing / 500 spawn_failed 都显示后端原文
                failTerm(e, "新建终端会话失败")
            } finally {
                termBusy = false
            }
        }
    }

    fun openTerminalSession(info: TerminalSessionInfo) {
        termActive = info
        termOutput = ""
        termCursor = 0
        termExited = !info.alive
        termTruncated = info.truncated
        termError = null
        termConfirm = null
        termConfirmFromInput = false
        termRetry = 0
        termDecoder = newTermDecoder()
        termPendingBytes = ByteArray(0)
        if (info.alive) {
            connectTermSocket()
        } else {
            // 已结束的会话也能回看输出，不再连 WS
            viewModelScope.launch {
                runCatching { pullTermPage() }
            }
        }
    }

    /** 只关 WS 回列表，不终止掌机上的会话。 */
    fun backToTerminalList() {
        closeTermSocket()
        termActive = null
        termConfirm = null
        termError = null
        viewModelScope.launch {
            try {
                loadTermSessions()
            } catch (e: Exception) {
                failTerm(e, "无法读取终端会话")
            }
        }
    }

    fun sendTermCommand() {
        val info = termActive ?: return
        val command = termCommand.trim()
        if (command.isEmpty() || termBusy) return
        viewModelScope.launch {
            termBusy = true
            try {
                val obj = withContext(Dispatchers.IO) { api.terminalExec(info.id, command, false) }
                afterExecAccepted(obj, command)
            } catch (e: TerminalConfirmException) {
                // 危险命令：把命令原文交给界面弹一次二次确认，绝不静默重试
                termConfirmFromInput = false
                termConfirm = e.prompt
            } catch (e: Exception) {
                failTerm(e, "命令提交失败")
            } finally {
                termBusy = false
            }
        }
    }

    /** 危险命令确认后：用后端返回的命令原文，带 confirm=true 原样重提。 */
    fun confirmTermCommand() {
        val prompt = termConfirm ?: return
        val info = termActive ?: return
        val fromInput = termConfirmFromInput
        termConfirm = null
        viewModelScope.launch {
            termBusy = true
            try {
                if (fromInput) {
                    withContext(Dispatchers.IO) {
                        api.terminalInput(info.id, prompt.command, "utf-8", confirm = true)
                    }
                    termCommand = ""
                } else {
                    val obj = withContext(Dispatchers.IO) {
                        api.terminalExec(info.id, prompt.command, true)
                    }
                    afterExecAccepted(obj, prompt.command)
                }
            } catch (e: Exception) {
                failTerm(e, "命令提交失败")
            } finally {
                termConfirmFromInput = false
                termBusy = false
            }
        }
    }

    fun dismissTermConfirm() {
        termConfirm = null
        termConfirmFromInput = false
    }

    private fun afterExecAccepted(obj: JSONObject, command: String) {
        termCommand = ""
        termHistory = (listOf(command) + termHistory.filter { it != command }).take(MAX_TERM_HISTORY)
        obj.optJSONObject("session")?.let { row ->
            val info = api.parseTerminalSession(row)
            if (termActive?.id == info.id) {
                termActive = info
                termExited = !info.alive
            }
        }
    }

    /** 交互按键（Ctrl+C / Tab）：走 /input，不走 exec。 */
    fun sendTermInput(data: String) {
        val info = termActive ?: return
        if (data.isEmpty()) return
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) { api.terminalInput(info.id, data) }
            } catch (e: TerminalConfirmException) {
                termConfirmFromInput = true
                termConfirm = e.prompt
            } catch (e: Exception) {
                failTerm(e, "写入终端失败")
            }
        }
    }

    fun terminateTerminal() {
        val info = termActive ?: return
        if (termStopping) return
        viewModelScope.launch {
            termStopping = true
            try {
                withContext(Dispatchers.IO) { api.terminateTerminalSession(info.id) }
                termExited = true
                val after = withContext(Dispatchers.IO) {
                    runCatching { api.terminalSession(info.id) }.getOrNull()
                }
                if (after != null && termActive?.id == after.id) termActive = after
                notice = "已终止终端会话"
                runCatching { pullTermPage() }
            } catch (e: Exception) {
                // 404 session_not_found / 409 session_ended：显示原文并回会话列表
                failTerm(e, "终止失败")
            } finally {
                termStopping = false
            }
        }
    }

    fun useTermHistory(command: String) {
        termCommand = command
    }

    // ---------- 终端：输出订阅 ----------

    private fun connectTermSocket() {
        val info = termActive ?: return
        closeTermWebSocket()
        termWsSid = info.id
        val sid = info.id
        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                main.post {
                    if (termWsSid != sid) return@post
                    termLive = true
                    termRetry = 0
                    termPollJob?.cancel()
                    termPollJob = null
                    startTermPing()
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                main.post {
                    if (termWsSid != sid) return@post
                    handleTermEvent(text)
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                main.post { if (termWsSid == sid) scheduleTermReconnect() }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post { if (termWsSid == sid) scheduleTermReconnect() }
            }
        }
        val ws = api.newTerminalWebSocket(sid, termCursor, listener)
        if (ws == null) {
            termLive = false
            scheduleTermReconnect()
        } else {
            termWs = ws
        }
    }

    /** 只关 WS 相关（轮询里的重连会用到，不能顺手把轮询自己取消）。 */
    private fun closeTermWebSocket() {
        termReconnectJob?.cancel()
        termReconnectJob = null
        termPingJob?.cancel()
        termPingJob = null
        val ws = termWs
        termWs = null
        termWsSid = null
        termLive = false
        runCatching { ws?.close(1000, "bye") }
    }

    /** 离开终端页 / ViewModel 销毁：WS、重连、轮询全部收掉。 */
    private fun closeTermSocket() {
        closeTermWebSocket()
        termPollJob?.cancel()
        termPollJob = null
    }

    private fun startTermPing() {
        termPingJob?.cancel()
        termPingJob = viewModelScope.launch {
            while (isActive) {
                delay(30_000)
                val ws = termWs ?: break
                ws.send(JSONObject().put("type", "ping").toString())
            }
        }
    }

    /** 断线：先补一次 REST 续读，再按指数退避带当前游标重连；5 次后转轮询。 */
    private fun scheduleTermReconnect() {
        termLive = false
        if (termExited || termActive == null) return
        if (termRetry >= MAX_TERM_WS_RETRY) {
            startTermPolling()
            return
        }
        if (termReconnectJob?.isActive == true) return
        val wait = 1_000L shl termRetry.coerceAtMost(4)
        termRetry++
        termReconnectJob = viewModelScope.launch {
            delay(wait)
            termReconnectJob = null
            if (termExited || termActive == null) return@launch
            try {
                pullTermPage()
            } catch (_: Exception) {
                // 续读失败也继续尝试 WS
            }
            if (!termLive && termActive != null) connectTermSocket()
        }
    }

    private fun startTermPolling() {
        if (termPollJob?.isActive == true) return
        termPollJob = viewModelScope.launch {
            var loops = 0
            try {
                while (isActive && !termExited && termActive != null) {
                    try {
                        pullTermPage()
                    } catch (e: ApiException) {
                        if (e.code == "session_not_found" || e.code == "session_ended") {
                            failTerm(e, "会话已结束")
                            break
                        }
                    } catch (_: Exception) {
                    }
                    loops++
                    // 轮询期间每 12 秒再试一次 WS，成功就停轮询
                    if (loops % 8 == 0) {
                        termRetry = 0
                        connectTermSocket()
                        if (termLive) break
                    }
                    delay(1_500)
                }
            } finally {
                termPollJob = null
            }
        }
    }

    private suspend fun pullTermPage() {
        val info = termActive ?: return
        val page = withContext(Dispatchers.IO) { api.terminalOutput(info.id, termCursor) }
        val bos = java.io.ByteArrayOutputStream()
        page.chunks.forEach { bos.write(it) }
        appendTermText(decodeTerminalBytes(bos.toByteArray()))
        termCursor = page.nextCursor
        if (page.truncated) termTruncated = true
        page.session?.let { session ->
            if (termActive?.id == session.id) {
                termActive = session
                termExited = !session.alive
            }
        }
    }

    private fun handleTermEvent(text: String) {
        val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
        when (obj.optString("type")) {
            "hello" -> {
                obj.optJSONObject("session")?.let { row ->
                    val info = api.parseTerminalSession(row)
                    if (termActive?.id == info.id) {
                        termActive = info
                        termExited = !info.alive
                        termTruncated = info.truncated
                    }
                }
                val cursor = obj.optInt("cursor", termCursor)
                if (cursor > termCursor) termCursor = cursor
            }

            "output" -> {
                val data = obj.optString("data")
                if (data.isNotBlank()) {
                    val raw = runCatching {
                        android.util.Base64.decode(data, android.util.Base64.DEFAULT)
                    }.getOrNull()
                    if (raw != null) {
                        appendTermText(decodeTerminalBytes(raw))
                        val seq = obj.optInt("seq", -1)
                        if (seq >= 0 && seq + 1 > termCursor) termCursor = seq + 1
                    }
                }
            }

            "cursor" -> {
                val next = obj.optInt("next_cursor", -1)
                if (next > termCursor) termCursor = next
                if (obj.optBoolean("dropped")) termTruncated = true
            }

            "truncated" -> termTruncated = true

            "confirm_required" -> {
                termConfirmFromInput = false
                termConfirm = TerminalConfirmPrompt(
                    command = obj.optString("command"),
                    level = obj.optString("level"),
                    rule = obj.optString("rule"),
                    reason = obj.optString("reason").ifBlank { "危险命令需要确认" },
                )
            }

            "exec_started" -> Unit

            "exited" -> {
                termExited = true
                termLive = false
            }

            "error" -> {
                val code = obj.optString("code")
                val reason = obj.optString("reason").ifBlank { "终端连接出错" }
                when (code) {
                    "remote_shell_disabled" -> {
                        termEnabled = false
                        closeTermSocket()
                        termActive = null
                    }

                    "session_not_found", "session_ended" -> {
                        closeTermSocket()
                        termActive = null
                        termError = reason
                    }

                    else -> notice = reason
                }
            }
        }
    }

    private fun newTermDecoder() = Charsets.UTF_8.newDecoder()
        .onMalformedInput(java.nio.charset.CodingErrorAction.REPLACE)
        .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPLACE)

    /**
     * base64 块可能落在多字节字符中间：同一个 decoder 跨块续解码，
     * 未消费的尾部字节留到下一块，避免中文 / emoji 变成乱码。
     */
    private fun decodeTerminalBytes(bytes: ByteArray): String {
        if (bytes.isEmpty()) return ""
        val input = java.nio.ByteBuffer.wrap(termPendingBytes + bytes)
        val out = java.nio.CharBuffer.allocate(input.remaining() * 2 + 64)
        runCatching { termDecoder.decode(input, out, false) }
        out.flip()
        val text = StringBuilder(out.remaining())
        while (out.hasRemaining()) text.append(out.get())
        termPendingBytes = ByteArray(input.remaining()).also { input.get(it) }
        return text.toString()
    }

    private fun appendTermText(raw: String) {
        if (raw.isEmpty()) return
        val cleaned = ANSI_RE.replace(raw, "").replace("\r\n", "\n")
        val builder = StringBuilder(termOutput)
        cleaned.split('\r').forEachIndexed { index, part ->
            if (index > 0) {
                // 裸 \r 是进度行回到行首：清掉当前最后一行再写
                val cut = builder.lastIndexOf("\n")
                builder.setLength(cut + 1)
            }
            builder.append(part)
        }
        if (builder.length > MAX_TERM_OUTPUT_CHARS) {
            builder.delete(0, builder.length - MAX_TERM_OUTPUT_CHARS)
        }
        termOutput = builder.toString()
    }

    private fun failTerm(e: Exception, fallback: String) {
        val apiError = e as? ApiException
        if (apiError != null && apiError.status == 401) {
            closeTermSocket()
            mainReady = false
            notice = apiError.message.ifBlank { "凭据已失效，请重新配对" }
            return
        }
        val text = (apiError?.message ?: friendly(e)).ifBlank { fallback }
        if (apiError != null &&
            (apiError.code == "session_not_found" || apiError.code == "session_ended")
        ) {
            // 会话已结束/过期：显示可读原因并回到会话列表
            closeTermSocket()
            termActive = null
            termConfirm = null
            termError = text
            viewModelScope.launch {
                try {
                    loadTermSessions()
                } catch (_: Exception) {
                }
            }
            return
        }
        if (apiError != null && apiError.code == "remote_shell_disabled") {
            closeTermSocket()
            termActive = null
            termEnabled = false
            return
        }
        if (termActive != null) notice = text else termError = text
    }

    // ---------- WS 事件 ----------

    private fun handleWs(obj: JSONObject) {
        lastAlive = System.currentTimeMillis()
        when (obj.optString("type")) {
            "pong" -> Unit

            "history" -> {
                val sid = obj.optString("sid")
                if (sid.isNotBlank()) currentSid = sid
                messages = ApiClient.parseMessages(obj.optJSONArray("messages"))
                val partial = obj.optJSONObject("partial")
                val partialText = partial?.optString("text").orEmpty()
                streamingText = partialText
                if (partialText.isBlank()) {
                    thoughtText = ""
                    toolNote = ""
                }
                awaitingReply = false
                activeTurnId = null
            }

            "status" -> obj.optJSONObject("data")?.let { applyStatus(it) }

            "user_message" -> {
                if (sameSid(obj)) {
                    val text = obj.optString("text")
                    if (text.isNotBlank()) {
                        messages = messages + ChatMessage("user", text, obj.optDouble("ts", 0.0))
                        streamingText = ""
                        thoughtText = ""
                        toolNote = ""
                    }
                }
            }

            "stream_start" -> if (mine(obj)) {
                streamingText = ""
                thoughtText = ""
                toolNote = ""
            }

            "delta" -> if (mine(obj)) {
                if (obj.optBoolean("final", false)) {
                    val reply = obj.optString("reply")
                    if (reply.isNotBlank()) streamingText = reply
                } else {
                    streamingText += obj.optString("text")
                }
            }

            "thought_start" -> if (mine(obj)) thoughtText = ""
            "thought_delta" -> if (mine(obj)) thoughtText += obj.optString("text")
            "thought_end" -> Unit

            "tool_activity" -> if (mine(obj)) toolNote = obj.optString("text")
            "tool_done" -> if (mine(obj)) toolNote = ""

            "ask" -> if (mine(obj)) {
                askQuestion = obj.optString("question")
                askOptions = parseStrings(obj.optJSONArray("options"))
            }

            "assistant_message" -> {
                if (sameSid(obj)) {
                    val text = obj.optString("text")
                    if (text.isNotBlank()) {
                        appendAssistant(text, obj.optDouble("ts", 0.0))
                    }
                    streamingText = ""
                    thoughtText = ""
                    toolNote = ""
                    awaitingReply = false
                    activeTurnId = null
                }
            }

            "session_changed" -> {
                val sid = obj.optString("current")
                if (sid.isNotBlank() && sid != currentSid) {
                    currentSid = sid
                    messages = emptyList()
                    streamingText = ""
                    thoughtText = ""
                    toolNote = ""
                }
            }

            "system" -> {
                val text = obj.optString("text")
                if (text.isNotBlank()) {
                    messages = messages + ChatMessage("system", text, obj.optDouble("ts", 0.0))
                }
            }
        }
    }

    private fun applyStatus(data: JSONObject) {
        val nowRunning = data.optBoolean("running", false)
        running = nowRunning
        runningSince = data.optDouble("running_since", 0.0)
        if (nowRunning) return

        if (awaitingReply) {
            // 刚发出 chat 时可能收到一条过期的 status=false，等几秒再认定本轮真的结束了，
            // 否则会把排队消息立刻发出去，和后端并行开轮。
            if (System.currentTimeMillis() - dispatchAt >= 6_000) {
                val tail = streamingText.trim()
                if (tail.isNotEmpty()) {
                    appendAssistant(tail, System.currentTimeMillis() / 1000.0)
                }
                streamingText = ""
                thoughtText = ""
                toolNote = ""
                awaitingReply = false
                activeTurnId = null
            }
        }
        maybeDispatchNext()
    }

    private fun appendAssistant(text: String, ts: Double) {
        val last = messages.lastOrNull()
        if (last != null && last.role == "assistant" && last.content == text) return
        messages = messages + ChatMessage("assistant", text, ts)
    }

    private fun sameSid(obj: JSONObject): Boolean {
        val sid = obj.optString("sid")
        return sid.isBlank() || sid == currentSid
    }

    private fun mine(obj: JSONObject): Boolean {
        if (!sameSid(obj)) return false
        val turnId = obj.optString("turn_id")
        if (turnId.isBlank()) return true
        val active = activeTurnId ?: return true
        return turnId == active
    }

    private fun parseStrings(arr: org.json.JSONArray?): List<String> {
        if (arr == null) return emptyList()
        val out = ArrayList<String>()
        for (i in 0 until arr.length()) {
            val text = arr.optString(i)
            if (text.isNotBlank()) out.add(text)
        }
        return out
    }

    fun clearNotice() {
        notice = null
    }

    // ---------- 语音：录音转文字 ----------

    fun startRecording() {
        if (recording || sttBusy) return
        stopSpeaking()
        if (!recorder.start()) {
            notice = "麦克风启动失败：可能被其他应用占用，或系统没有录音设备"
            return
        }
        recording = true
        recordSeconds = 0
        recordJob = viewModelScope.launch {
            while (isActive) {
                delay(200)
                recordSeconds = recorder.seconds()
                // 录到 60 秒上限时录音器会自己停，这里统一收尾
                if (!recorder.isRecording() || recordSeconds >= VoiceRecorder.MAX_SECONDS) {
                    finishRecording()
                    break
                }
            }
        }
    }

    /** 结束录音并上传 STT；识别结果只追加到输入框，不自动发送。 */
    fun finishRecording() {
        if (!recording) return
        recordJob?.cancel()
        recordJob = null
        recording = false
        recordSeconds = 0
        val wav = recorder.stop()
        if (wav == null) {
            notice = "录音太短（至少 1 秒），没有可识别的内容"
            return
        }
        viewModelScope.launch {
            sttBusy = true
            try {
                val text = withContext(Dispatchers.IO) { api.sttTranscribe(wav) }.trim()
                if (text.isEmpty()) {
                    notice = "没有识别到文字，请再说一遍"
                } else {
                    inputText = if (inputText.isBlank()) text else "${inputText.trimEnd()} $text"
                }
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                sttBusy = false
            }
        }
    }

    fun cancelRecording() {
        if (!recording) return
        recordJob?.cancel()
        recordJob = null
        recorder.cancel()
        recording = false
        recordSeconds = 0
    }

    fun onMicPermissionDenied() {
        notice = "没有麦克风权限：语音输入需要在系统设置里允许录音"
    }

    // ---------- 语音：播报 ----------

    fun speak(text: String) {
        val clean = text.trim()
        if (clean.isEmpty() || ttsLoading) return
        stopSpeaking()
        viewModelScope.launch {
            ttsLoading = true
            try {
                val bytes = withContext(Dispatchers.IO) { api.ttsSay(clean) }
                if (bytes.isEmpty()) {
                    notice = "语音合成返回了空音频"
                    return@launch
                }
                val file = withContext(Dispatchers.IO) {
                    player.write(getApplication<Application>().cacheDir, bytes)
                }
                player.play(
                    file,
                    onStarted = {
                        ttsActive = true
                        ttsPaused = false
                        ttsLoading = false
                    },
                    onFinished = {
                        ttsActive = false
                        ttsPaused = false
                    },
                    onFailed = { message ->
                        ttsActive = false
                        ttsPaused = false
                        notice = message
                    },
                )
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                ttsLoading = false
            }
        }
    }

    fun toggleSpeaking() {
        if (!ttsActive) return
        if (ttsPaused) {
            if (player.resume()) ttsPaused = false
        } else if (player.pause()) {
            ttsPaused = true
        }
    }

    fun stopSpeaking() {
        player.stop()
        ttsActive = false
        ttsPaused = false
    }

    private fun friendly(e: Exception): String {
        if (e is ApiException) {
            return if (e.status == 0) e.message else e.message.ifBlank { "请求失败" }
        }
        val text = e.message.orEmpty()
        return if (text.isBlank()) "连接失败，请检查地址与网络" else "连接失败：$text"
    }

    override fun onCleared() {
        wantConnection = false
        reconnectJob?.cancel()
        heartbeatJob?.cancel()
        pairJob?.cancel()
        recordJob?.cancel()
        closeTermSocket()
        recorder.cancel()
        player.stop()
        socket?.close(1000, "bye")
        socket = null
        super.onCleared()
    }

    companion object {
        private const val KEY_SERVER = "server_url"
        /** 终端滚动区只保留最近这些字符（约 400~500 行），避免超长输出拖慢排版 */
        private const val MAX_TERM_OUTPUT_CHARS = 48_000
        /** 最近命令列表长度 */
        private const val MAX_TERM_HISTORY = 20
        /** WS 连续失败次数上限，超过后改为 REST 轮询续读 */
        private const val MAX_TERM_WS_RETRY = 5
        /** 去掉终端控制序列：OSC / CSI / 字符集切换 */
        private val ANSI_RE = Regex(
            "\u001B\\][^\u0007]*(?:\u0007|\u001B\\\\)" + "|" +
                "\u001B\\[[0-9;?<]*[ -/]*[@-~]" + "|" +
                "\u001B[()][A-Za-z0-9]",
        )
    }
}
