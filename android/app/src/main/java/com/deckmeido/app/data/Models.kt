package com.deckmeido.app.data

data class AuthInfo(
    val authenticated: Boolean,
    val local: Boolean,
    val lanEnable: Boolean,
    val version: String,
)

data class PairSession(
    val pairId: String,
    val state: String,
    val name: String,
    val ip: String,
    val expiresIn: Int,
    val attempts: Int,
)

data class SessionItem(
    val id: String,
    val title: String,
    val updated: Long,
    val messageCount: Int,
)

/** 历史里只关心能直接显示的部分：role / content / ts。 */
data class ChatMessage(
    val role: String,
    val content: String,
    val ts: Double = 0.0,
)

data class HistoryPreview(
    val id: String,
    val title: String,
    val messages: List<ChatMessage>,
)

data class SessionsResult(
    val current: String,
    val items: List<SessionItem>,
)

/** 二维码里的内容：后端地址 + 一次性 token（token 短有效期、只能用一次）。 */
data class QrPairPayload(
    val address: String,
    val token: String,
)

/** /api/browse 的一条目录项。mtime 缺失（旧版后端）时界面显示"—"。 */
data class FsEntry(
    val name: String,
    val isDir: Boolean,
    val size: Long? = null,
    val mtime: Double? = null,
    val kind: String = "other",
    val isLink: Boolean = false,
    val broken: Boolean = false,
    val linkTarget: String = "",
)

/**
 * /api/browse 的一层目录。
 *
 * zone / readonly 全部来自后端（fszones 三级分区），客户端不自己写路径正则；
 * error 保留后端原文，禁止浏览（403 forbidden）、目录不存在、断链软链接都靠它显示。
 */
data class FsListing(
    val path: String,
    val parent: String? = null,
    val entries: List<FsEntry> = emptyList(),
    val zone: String = "workspace",
    val readonly: Boolean = false,
    val zoneLabel: String = "",
    val zoneReason: String = "",
    val select: String = "",
    val error: String? = null,
    val linkBroken: Boolean = false,
    val linkTarget: String = "",
)

/** /api/fs 写操作结果：失败时 error 是后端原文，reason 是机器可读原因。 */
data class FsOpResult(
    val ok: Boolean,
    val error: String = "",
    val reason: String = "",
)

/** 远程终端的可配置项（GET /api/terminal/sessions 的 settings 字段）。 */
data class TerminalSettings(
    val enabled: Boolean,
    val idleTimeout: Int,
    val maxSessions: Int,
    val maxOutputBytes: Long,
    val shell: String,
    val cwd: String,
)

/** 一个常驻 shell 会话（与 docs/remote-terminal-api.md §3.2 对应）。 */
data class TerminalSessionInfo(
    val id: String,
    val pid: Int,
    val state: String,
    val exitCode: Int?,
    val created: Double,
    val started: Double,
    val cwd: String,
    val cols: Int,
    val rows: Int,
    val backend: String,
    val outputTotal: Long,
    val outputNextSeq: Int,
    val truncated: Boolean,
    val terminated: Boolean,
    val completedExecs: Int,
    val lastExitCode: Int?,
) {
    val alive: Boolean get() = state == "running"
}

/** GET /api/terminal/sessions 的整包。 */
data class TerminalSessionsResult(
    val enabled: Boolean,
    val settings: TerminalSettings?,
    val sessions: List<TerminalSessionInfo>,
)

/** GET .../output 的一页输出；chunks 已按 base64 解码成字节。 */
data class TerminalOutputPage(
    val chunks: List<ByteArray>,
    val nextCursor: Int,
    val dropped: Boolean,
    val truncated: Boolean,
    val session: TerminalSessionInfo?,
)

/** 危险命令的二次确认信息（428 confirm_required 原样带给界面）。 */
data class TerminalConfirmPrompt(
    val command: String,
    val level: String,
    val rule: String,
    val reason: String,
)

/** 428：必须让用户看过命令原文后，带 confirm=true 原样重提，不能静默重试。 */
class TerminalConfirmException(val prompt: TerminalConfirmPrompt) :
    Exception(prompt.reason.ifBlank { "危险命令需要确认" })

/** code 是后端给的机器可读原因（expired / used / revoked / invalid / too_many）。 */
class ApiException(
    val status: Int,
    override val message: String,
    val code: String = "",
) : Exception(message)
