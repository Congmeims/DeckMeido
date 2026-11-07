package com.deckmeido.app.ui

import android.app.Application
import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.deckmeido.app.data.ApiClient
import com.deckmeido.app.data.ApiException
import com.deckmeido.app.data.AuthInfo
import com.deckmeido.app.data.HistoryPreview
import com.deckmeido.app.data.PairSession
import com.deckmeido.app.data.SecurePrefs
import com.deckmeido.app.data.SessionItem
import com.deckmeido.app.data.ChatMessage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.UUID

enum class Screen { Connect, Sessions, Preview, Chat }

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

    // ---------- 会话 ----------
    var screen by mutableStateOf(Screen.Connect)
    var sessions by mutableStateOf<List<SessionItem>>(emptyList())
    var sessionsLoading by mutableStateOf(false)
    var currentSid by mutableStateOf("")

    // ---------- 只读预览 ----------
    var preview by mutableStateOf<HistoryPreview?>(null)
    var previewLoading by mutableStateOf(false)
    var previewSid by mutableStateOf<String?>(null)

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
        screen = Screen.Sessions
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
        screen = Screen.Connect
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
            screen = Screen.Connect
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

    fun openPreview(sid: String) {
        previewSid = sid
        preview = null
        screen = Screen.Preview
        viewModelScope.launch {
            previewLoading = true
            try {
                preview = withContext(Dispatchers.IO) { api.history(sid) }
            } catch (e: Exception) {
                notice = friendly(e)
                screen = Screen.Sessions
            } finally {
                previewLoading = false
            }
        }
    }

    fun switchToPreviewSession() {
        val sid = previewSid ?: return
        if (running || awaitingReply) {
            notice = "还有一轮回答在生成，等结束再切换"
            return
        }
        viewModelScope.launch {
            previewLoading = true
            try {
                withContext(Dispatchers.IO) { api.switchSession(sid) }
                currentSid = sid
                streamingText = ""
                thoughtText = ""
                toolNote = ""
                askQuestion = null
                askOptions = emptyList()
                screen = Screen.Chat
                wsSend(JSONObject().put("type", "history"))
            } catch (e: Exception) {
                notice = friendly(e)
            } finally {
                previewLoading = false
            }
        }
    }

    fun enterChat() {
        screen = Screen.Chat
        wsSend(JSONObject().put("type", "history"))
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

    // ---------- 导航 ----------

    fun back(): Boolean {
        when (screen) {
            Screen.Chat -> {
                screen = Screen.Sessions
                refreshSessions()
            }
            Screen.Preview -> screen = Screen.Sessions
            Screen.Sessions -> screen = Screen.Connect
            Screen.Connect -> return false
        }
        return true
    }

    fun clearNotice() {
        notice = null
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
        socket?.close(1000, "bye")
        socket = null
        super.onCleared()
    }

    companion object {
        private const val KEY_SERVER = "server_url"
    }
}
