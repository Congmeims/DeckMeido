"""QQ 远程控制通道（OneBot 11 / NapCat WebSocket 客户端）。

部署形态是「Deck 主动连出去」：Deck 作为客户端连到公网上的 NapCat
WebSocket 服务器，本机不开任何入站端口。NapCat 重启、网络抖动都靠
指数退避重连自愈。

这个模块刻意不 import server.main —— 一是避免循环依赖，二是方便单测：
跑一轮对话、抓一张画面、查设备状态这三件事都由 main 注入，测试里换成假实现。

安全模型：
  - 只处理白名单里的管理员 QQ 私聊；别人的私聊静默忽略，一个字都不回；
  - 群聊必须被 @ 才处理，而且只能聊天（工具层按只读上下文拦死设备操作）；
  - 命令（/status /shot /cancel）只在管理员私聊里解析；
  - 每分钟条数、单条长度都设上限，超限的静默丢弃、不回发；
  - 任何异常只回一句笼统的话，细节写本地日志。
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import secrets
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PREFIX = "meido"
DEFAULT_RATE_PER_MINUTE = 20
DEFAULT_MAX_MESSAGE_CHARS = 2000
DEFAULT_REPLY_CHUNK_CHARS = 500
DEFAULT_CONFIRM_TIMEOUT = 300.0
DEFAULT_RECONNECT_MIN = 1.0
DEFAULT_RECONNECT_MAX = 60.0
DEFAULT_PING_INTERVAL = 30.0
DEFAULT_PING_TIMEOUT = 15.0
MAX_REPLY_CHUNKS = 5
TRUNCATED_NOTE = "\n……（内容过长，已截断）"

# 和 policy._CONFIRM_RE 保持一致：QQ 侧回「确认」才放行挂起的危险操作
_CONFIRM_RE = re.compile(
    r"^(好|好的|行|可以|确认|确定|是|是的|继续|执行|重启|关机|ok|okay|yes|y|sure|go)"
    r"[。！!.]?$",
    re.IGNORECASE,
)

_CMD_RE = re.compile(r"^/([A-Za-z]+)(?:\s+([\s\S]+))?$")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([0-9]+)[^\]]*\]")


# ---------- 配置 ----------

@dataclass(frozen=True)
class QQConfig:
    enable: bool = False
    ws_url: str = ""
    access_token: str = ""
    admin_qq: tuple[str, ...] = ()
    allow_groups: tuple[str, ...] = ()
    rate_per_minute: int = DEFAULT_RATE_PER_MINUTE
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    reply_chunk_chars: int = DEFAULT_REPLY_CHUNK_CHARS
    confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT
    reconnect_min: float = DEFAULT_RECONNECT_MIN
    reconnect_max: float = DEFAULT_RECONNECT_MAX
    ping_interval: float = DEFAULT_PING_INTERVAL
    ping_timeout: float = DEFAULT_PING_TIMEOUT


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "y"):
            return True
        if v in ("0", "false", "no", "off", "n", ""):
            return False
    return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    try:
        items = list(value)
    except TypeError:
        return ()
    return tuple(str(v).strip() for v in items if str(v).strip())


def parse_config(raw: dict | None) -> QQConfig:
    """把 config.toml 的 [qq] 段转成配置对象：字段缺失/类型不对都取默认值"""
    raw = raw if isinstance(raw, dict) else {}
    return QQConfig(
        enable=_as_bool(raw.get("enable", False)),
        ws_url=str(raw.get("ws_url") or "").strip(),
        access_token=str(raw.get("access_token") or "").strip(),
        admin_qq=_as_str_list(raw.get("admin_qq")),
        allow_groups=_as_str_list(raw.get("allow_groups")),
        rate_per_minute=_as_int(raw.get("rate_per_minute", DEFAULT_RATE_PER_MINUTE),
                                DEFAULT_RATE_PER_MINUTE),
        max_message_chars=_as_int(raw.get("max_message_chars", DEFAULT_MAX_MESSAGE_CHARS),
                                  DEFAULT_MAX_MESSAGE_CHARS),
        reply_chunk_chars=_as_int(raw.get("reply_chunk_chars", DEFAULT_REPLY_CHUNK_CHARS),
                                  DEFAULT_REPLY_CHUNK_CHARS),
        confirm_timeout=_as_float(raw.get("confirm_timeout", DEFAULT_CONFIRM_TIMEOUT),
                                  DEFAULT_CONFIRM_TIMEOUT),
        reconnect_min=_as_float(raw.get("reconnect_min", DEFAULT_RECONNECT_MIN),
                                DEFAULT_RECONNECT_MIN),
        reconnect_max=_as_float(raw.get("reconnect_max", DEFAULT_RECONNECT_MAX),
                                DEFAULT_RECONNECT_MAX),
        ping_interval=_as_float(raw.get("ping_interval", DEFAULT_PING_INTERVAL),
                                DEFAULT_PING_INTERVAL),
        ping_timeout=_as_float(raw.get("ping_timeout", DEFAULT_PING_TIMEOUT),
                               DEFAULT_PING_TIMEOUT),
    )


def validate_config(cfg: QQConfig) -> str | None:
    """启动前校验：返回中文错误说明，None 表示可以连。绝不静默失败"""
    if not cfg.enable:
        return None
    if not cfg.access_token:
        return ("未配置 access_token。NapCat 的 WebSocket 服务器必须留访问令牌，"
                "否则公网入口等于对所有人敞开，已停止连接。")
    parts = urlsplit(cfg.ws_url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        return ("ws_url 无效（必须以 ws:// 或 wss:// 开头并带主机名），"
                "例如 wss://napcat.example.com/ws，已停止连接。")
    if not cfg.admin_qq:
        return ("admin_qq 没配。没有管理员就等于谁都不能用，"
                "已停止连接；请先填上你自己的 QQ 号。")
    return None


def _mask_url(url: str) -> str:
    """状态接口里回显地址时抹掉 userinfo 和查询串，别把令牌一类的东西漏出去"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return ""


# ---------- 前缀与指令解析 ----------

def prefix_for(nickname: str) -> str:
    """触发前缀就是软件里配置的昵称；昵称为空时退回默认词"""
    return (nickname or "").strip() or DEFAULT_PREFIX


@dataclass
class ParsedCommand:
    kind: str          # status / shot / cancel / chat
    text: str = ""     # chat 时的正文


def parse_command(text: str, nickname: str) -> ParsedCommand | None:
    """解析「昵称 + 子命令或正文」。不匹配前缀返回 None

    规则：忽略首尾空白、ASCII 不区分大小写，前缀后可以直接跟子命令，
    也可以空格再接正文。
    """
    body = (text or "").strip()
    prefix = prefix_for(nickname)
    if len(body) < len(prefix):
        return None
    if body[:len(prefix)].casefold() != prefix.casefold():
        return None
    rest = body[len(prefix):].strip()
    if not rest:
        return ParsedCommand("chat", "")
    m = _CMD_RE.match(rest)
    if m and m.group(1).lower() in ("status", "shot", "cancel"):
        return ParsedCommand(m.group(1).lower(), (m.group(2) or "").strip())
    return ParsedCommand("chat", rest)


def strip_prefix(text: str, nickname: str) -> str:
    """群聊里已经靠 @ 触发，再额外剥掉一层前缀，免得聊天正文里带着它"""
    body = (text or "").strip()
    prefix = prefix_for(nickname)
    if len(body) >= len(prefix) and body[:len(prefix)].casefold() == prefix.casefold():
        return body[len(prefix):].strip()
    return body


def is_confirm_text(text: str) -> bool:
    return bool(_CONFIRM_RE.match((text or "").strip()))


# ---------- 事件归一化与门控 ----------

@dataclass
class MessageEvent:
    uin: str
    text: str
    group_id: str | None = None
    at_bot: bool = False
    message_id: str = ""
    self_id: str = ""


def _segments(data) -> tuple[str, list[str]]:
    """从 OneBot 的 message 字段取纯文本，并返回被 @ 的 QQ 号列表"""
    if isinstance(data, list):
        texts: list[str] = []
        ats: list[str] = []
        for seg in data:
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            payload = seg.get("data") or {}
            if kind == "text":
                texts.append(str(payload.get("text") or ""))
            elif kind == "at":
                ats.append(str(payload.get("qq") or ""))
        return "".join(texts), ats
    raw = str(data or "")
    ats = _CQ_AT_RE.findall(raw)
    raw = _CQ_AT_RE.sub("", raw)
    return raw, ats


def normalize_event(data: dict) -> MessageEvent | None:
    """把 OneBot 的 message 事件转成内部结构；不是消息类事件返回 None"""
    if not isinstance(data, dict) or data.get("post_type") != "message":
        return None
    mtype = data.get("message_type")
    if mtype not in ("private", "group"):
        return None
    uin = str(data.get("user_id") or "").strip()
    if not uin:
        return None
    self_id = str(data.get("self_id") or "").strip()
    text, ats = _segments(data.get("message"))
    if not text.strip() and data.get("raw_message"):
        text, ats = _segments(data.get("raw_message"))
    at_bot = bool(self_id) and self_id in ats
    group_id = None
    if mtype == "group":
        group_id = str(data.get("group_id") or "").strip() or None
    return MessageEvent(
        uin=uin,
        text=text,
        group_id=group_id,
        at_bot=at_bot,
        message_id=str(data.get("message_id") or ""),
        self_id=self_id,
    )


@dataclass
class Route:
    action: str          # ignore / command / chat
    kind: str = ""       # command 时是 status / shot / cancel
    text: str = ""
    readonly: bool = True
    reason: str = ""


def route_message(cfg: QQConfig, ev: MessageEvent, nickname: str) -> Route:
    """权限判定与指令路由（纯函数，方便单测）

    管理员私聊：带前缀进指令/聊天，不带前缀直接当聊天；
    非管理员私聊：静默忽略；
    群聊：必须在允许的群里、必须 @ 机器人，而且只能聊天 ——
    连管理员在群里也不解析命令，避免在公共场合误触发设备操作。
    """
    is_admin = ev.uin in cfg.admin_qq
    if ev.group_id is None:
        if not is_admin:
            return Route("ignore", reason="非管理员私聊")
        parsed = parse_command(ev.text, nickname)
        if parsed is None:
            return Route("chat", text=ev.text, readonly=False)
        if parsed.kind == "chat":
            return Route("chat", text=parsed.text, readonly=False)
        return Route("command", kind=parsed.kind, readonly=False)

    if not cfg.allow_groups or ev.group_id not in cfg.allow_groups:
        return Route("ignore", reason="群不在白名单")
    if not ev.at_bot:
        return Route("ignore", reason="群里没有 @ 机器人")
    return Route("chat", text=strip_prefix(ev.text, nickname), readonly=True)


# ---------- 限流 / 截断 / 退避 ----------

class RateLimiter:
    """按发送者做 60 秒滑动窗口限流。<=0 视为不限流"""

    def __init__(self, per_minute: int, window: float = 60.0, clock=time.monotonic):
        self.per_minute = int(per_minute or 0)
        self.window = float(window)
        self.clock = clock
        self._hits: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True


def truncate_text(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit]


def split_reply(text: str, limit: int = DEFAULT_REPLY_CHUNK_CHARS,
                max_chunks: int = MAX_REPLY_CHUNKS) -> list[str]:
    """把长回复切成几条，单条不超过 limit；超过条数上限就截断并标注"""
    body = (text or "").strip()
    if not body:
        return []
    if limit <= 0:
        limit = DEFAULT_REPLY_CHUNK_CHARS
    chunks: list[str] = []
    rest = body
    while rest and len(chunks) < max_chunks:
        if len(rest) <= limit:
            chunks.append(rest)
            rest = ""
            break
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        piece = rest[:cut].rstrip()
        if piece:
            chunks.append(piece)
        rest = rest[cut:].lstrip("\n")
    if rest and chunks:
        chunks[-1] = chunks[-1] + TRUNCATED_NOTE
    return chunks


class Backoff:
    """指数退避：base, base*factor, base*factor^2 … 封顶 cap，带抖动"""

    def __init__(self, base: float = DEFAULT_RECONNECT_MIN,
                 cap: float = DEFAULT_RECONNECT_MAX,
                 factor: float = 2.0, jitter: float = 0.2,
                 rand=random.random):
        self.base = max(0.1, float(base or DEFAULT_RECONNECT_MIN))
        self.cap = max(self.base, float(cap or DEFAULT_RECONNECT_MAX))
        self.factor = max(1.1, float(factor))
        self.jitter = max(0.0, min(0.5, float(jitter)))
        self.rand = rand

    def reset(self) -> None:
        return None

    def delay(self, attempt: int) -> float:
        attempt = max(0, int(attempt))
        try:
            raw = min(self.cap, self.base * (self.factor ** attempt))
        except OverflowError:
            raw = self.cap
        if self.jitter:
            # 只往下抖：上限不能被抖动突破，NapCat 刚起来时也不至于等太久
            low = raw * (1.0 - self.jitter)
            return low + (raw - low) * float(self.rand())
        return raw


# ---------- 通道本体 ----------

class QQBot:
    def __init__(self, *, runner, screenshot, status_provider, nickname,
                 pending_clear=None, connect_factory=None, sleep=None,
                 clock=time.time):
        """
        runner: async (text, *, sid, device, readonly, turn_id) -> {"reply", "pending"}
        screenshot: async () -> (bytes|None, 说明)
        status_provider: async () -> dict（核心/模式等，/status 用）
        nickname: () -> str（软件里配置的昵称，作为触发前缀）
        pending_clear: (device, sid) -> None（确认超时后清掉挂起的危险操作）
        """
        self._runner = runner
        self._screenshot = screenshot
        self._status_provider = status_provider
        self._nickname = nickname
        self._pending_clear = pending_clear
        self._connect_factory = connect_factory
        self._sleep = sleep or asyncio.sleep
        self._clock = clock

        self._cfg = QQConfig()
        self._task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._ws = None
        self._stopping = False
        self._booted = False
        self._state = "disabled"
        self._detail = "已关闭（[qq] enable = false）"
        self._last_error = ""
        self._attempt = 0
        self._connected_at = 0.0
        self._last_heartbeat = 0.0
        self._last_message_at = 0.0

        self._limiter = RateLimiter(self._cfg.rate_per_minute)
        self._backoff = Backoff(self._cfg.reconnect_min, self._cfg.reconnect_max)
        self._pending_calls: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._turn_task: asyncio.Task | None = None
        self._pending_conf: dict[str, dict] = {}
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._stats = {
            "received": 0, "ignored": 0, "rate_limited": 0,
            "commands": 0, "turns": 0, "replies": 0, "errors": 0,
            "reconnects": 0,
        }

    # -- 生命周期 --

    def _drop_pending(self) -> None:
        """丢掉在等确认的操作：停机/改配置/超时时，旧操作不能留着等下一次确认"""
        for device, info in list(self._pending_conf.items()):
            if self._pending_clear is not None:
                try:
                    self._pending_clear(device, str(info.get("sid") or ""))
                except Exception as e:
                    print(f"[DeckMeido][qq] 清理挂起操作失败：{e!r}", file=sys.stderr)
        self._pending_conf.clear()

    def configure(self, cfg: QQConfig | dict | None) -> None:
        cfg = cfg if isinstance(cfg, QQConfig) else parse_config(cfg)
        if cfg == self._cfg:
            return
        self._cfg = cfg
        self._limiter = RateLimiter(cfg.rate_per_minute, clock=time.monotonic)
        self._backoff = Backoff(cfg.reconnect_min, cfg.reconnect_max)
        self._drop_pending()
        err = validate_config(cfg)
        if not cfg.enable:
            self._state = "disabled"
            self._detail = "已关闭（[qq] enable = false）"
        elif err:
            self._state = "misconfigured"
            self._detail = err
        else:
            self._detail = "待连接"
        running = self._task is not None and not self._task.done()
        # 只有「启动过」之后才自动重启：import 阶段 configure 不该拉起连接，
        # 真正的启动入口是 start()
        if self._booted and (running or (cfg.enable and not err)):
            self._schedule_restart()

    def _schedule_restart(self) -> asyncio.Task | None:
        """按当前配置重连；同一时间只允许一个重载任务，避免保存后连点两次

        返回正在跑（或刚建好）的重载任务，调用方可以 await 它来等新状态落定。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._restart_task is not None and not self._restart_task.done():
            return self._restart_task
        self._restart_task = loop.create_task(self._restart())
        return self._restart_task

    async def reconnect(self) -> None:
        """供设置页「重新连接」用：不写配置，只按当前配置重连一次"""
        task = self._schedule_restart()
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            # _restart 内部已经把错误写进状态/日志，这里只保证接口能正常返回
            pass

    async def _restart(self) -> None:
        try:
            await self.stop(keep_detail=True)
            await self.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_error = repr(e)
            print(f"[DeckMeido][qq] 重载失败：{e!r}", file=sys.stderr)

    async def start(self) -> None:
        self._booted = True
        err = validate_config(self._cfg)
        if not self._cfg.enable:
            self._state = "disabled"
            self._detail = "已关闭（[qq] enable = false）"
            return
        if err:
            self._state = "misconfigured"
            self._detail = err
            print(f"[DeckMeido][qq] 未启动：{err}", file=sys.stderr)
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._detail = "正在连接"
        self._task = asyncio.create_task(self._run(), name="qqbot")
        print(f"[DeckMeido][qq] 已启动，目标 {_mask_url(self._cfg.ws_url)}")

    async def stop(self, keep_detail: bool = False) -> None:
        self._stopping = True
        self._drop_pending()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        turn, self._turn_task = self._turn_task, None
        if turn is not None:
            turn.cancel()
            try:
                await turn
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self._dispatch_tasks):
            t.cancel()
        self._dispatch_tasks.clear()
        self._fail_pending_calls()
        self._ws = None
        if not keep_detail:
            self._detail = "已停止"
            if self._state != "misconfigured":
                self._state = "stopped"

    def state(self) -> dict:
        running = self._task is not None and not self._task.done()
        return {
            "enable": self._cfg.enable,
            "running": running,
            "state": self._state,
            "detail": self._detail,
            "url": _mask_url(self._cfg.ws_url),
            "prefix": prefix_for(self._nickname()),
            "admins": len(self._cfg.admin_qq),
            "groups": len(self._cfg.allow_groups),
            "connected_at": self._connected_at or None,
            "last_heartbeat": self._last_heartbeat or None,
            "last_message_at": self._last_message_at or None,
            "last_error": self._last_error,
            "attempts": self._attempt,
            "stats": dict(self._stats),
        }

    # -- 连接与重连 --

    async def _connect(self):
        if self._connect_factory is not None:
            return await self._connect_factory(self._cfg)
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            try:
                from websockets import connect as ws_connect
            except ImportError as e:
                raise RuntimeError("缺少 websockets 依赖，无法连接 NapCat") from e
        headers = {"Authorization": f"Bearer {self._cfg.access_token}"}
        kwargs = dict(
            ping_interval=self._cfg.ping_interval or None,
            ping_timeout=self._cfg.ping_timeout or None,
            open_timeout=15,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        )
        try:
            return await ws_connect(self._cfg.ws_url, additional_headers=headers, **kwargs)
        except TypeError:
            # 老版本 websockets 用的是 extra_headers
            return await ws_connect(self._cfg.ws_url, extra_headers=headers, **kwargs)

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            err = validate_config(self._cfg)
            if err or not self._cfg.enable:
                self._state = "misconfigured" if err else "disabled"
                self._detail = err or "已关闭（[qq] enable = false）"
                return
            self._state = "connecting"
            self._detail = "正在连接 NapCat"
            try:
                ws = await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                self._attempt = attempt
                self._last_error = f"{type(e).__name__}: {e}"
                self._state = "reconnecting"
                self._stats["reconnects"] += 1
                delay = self._backoff.delay(attempt - 1)
                self._detail = f"连接失败，{delay:.0f} 秒后重试（第 {attempt} 次）"
                print(f"[DeckMeido][qq] 连接失败：{self._last_error}；{delay:.1f}s 后重试",
                      file=sys.stderr)
                await self._sleep(delay)
                continue

            self._ws = ws
            self._state = "connected"
            self._detail = "已连接"
            self._last_error = ""
            self._attempt = 0
            self._connected_at = self._clock()
            attempt = 0
            print("[DeckMeido][qq] 已连上 NapCat")
            try:
                await self._reader(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                print(f"[DeckMeido][qq] 连接中断：{self._last_error}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            finally:
                self._ws = None
                self._fail_pending_calls()
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._stopping:
                break
            attempt += 1
            self._attempt = attempt
            self._state = "reconnecting"
            self._stats["reconnects"] += 1
            delay = self._backoff.delay(attempt - 1)
            self._detail = f"已断开，{delay:.0f} 秒后重连（第 {attempt} 次）"
            await self._sleep(delay)
        self._state = "stopped"
        self._detail = "已停止"

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                payload = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                data = json.loads(payload)
            except Exception:
                self._stats["errors"] += 1
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    self._handle_frame(item)

    def _handle_frame(self, data: dict) -> None:
        if "echo" in data and ("retcode" in data or "status" in data):
            fut = self._pending_calls.pop(str(data.get("echo")), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return
        post = data.get("post_type")
        if post == "meta_event":
            self._last_heartbeat = self._clock()
            return
        if post != "message":
            return
        ev = normalize_event(data)
        if ev is None:
            return
        task = asyncio.create_task(self._dispatch(ev))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def wait_idle(self, timeout: float = 3.0, settle: float = 0.05) -> None:
        """等手上的消息都处理完（测试与优雅停机用）"""
        # 先让出一次事件循环：帧可能还排在 WS 队列里，reader 还没把它派发成任务
        await asyncio.sleep(settle)
        deadline = self._clock() + timeout
        while self._dispatch_tasks:
            tasks = list(self._dispatch_tasks)
            if not tasks:
                break
            remain = deadline - self._clock()
            if remain <= 0:
                return
            try:
                await asyncio.wait(tasks, timeout=min(remain, 0.5))
            except Exception:
                return

    # -- 发消息 --

    async def _call_api(self, action: str, params: dict, timeout: float = 15.0):
        ws = self._ws
        if ws is None:
            raise ConnectionError("QQ 通道未连接")
        self._seq += 1
        echo = str(self._seq)
        fut = asyncio.get_running_loop().create_future()
        self._pending_calls[echo] = fut
        payload = {"action": action, "params": params, "echo": echo}
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            data = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_calls.pop(echo, None)
        if not isinstance(data, dict):
            raise RuntimeError("NapCat 响应格式异常")
        if data.get("status") == "failed" or data.get("retcode") not in (0, 1, None):
            raise RuntimeError(f"NapCat 拒绝 {action}：retcode={data.get('retcode')} "
                               f"{data.get('message') or data.get('wording') or ''}")
        return data.get("data")

    def _at_segment(self, uin: str) -> dict:
        return {"type": "at", "data": {"qq": uin}}

    async def _send_text(self, ev: MessageEvent, text: str, at: bool = True,
                         chunk: str | None = None) -> None:
        body = text if chunk is None else chunk
        if ev.group_id is None:
            await self._call_api("send_private_msg",
                                 {"user_id": _to_uid(ev.uin), "message": body})
        else:
            message = []
            if at:
                message.append(self._at_segment(ev.uin))
                body = " " + body
            message.append({"type": "text", "data": {"text": body}})
            await self._call_api("send_group_msg",
                                 {"group_id": _to_uid(ev.group_id), "message": message})
        self._stats["replies"] += 1

    async def _send_chunks(self, ev: MessageEvent, text: str) -> None:
        chunks = split_reply(text, self._cfg.reply_chunk_chars)
        if not chunks:
            chunks = ["（这一轮没有回复）"]
        for i, chunk in enumerate(chunks):
            await self._send_text(ev, chunk, at=(i == 0), chunk=chunk)

    async def _send_image(self, ev: MessageEvent, blob: bytes, caption: str) -> None:
        b64 = base64.b64encode(blob).decode("ascii")
        seg_image = {"type": "image", "data": {"file": f"base64://{b64}"}}
        seg_text = {"type": "text", "data": {"text": caption}}
        if ev.group_id is None:
            await self._call_api("send_private_msg",
                                 {"user_id": _to_uid(ev.uin),
                                  "message": [seg_text, seg_image]}, timeout=30)
        else:
            await self._call_api("send_group_msg",
                                 {"group_id": _to_uid(ev.group_id),
                                  "message": [self._at_segment(ev.uin), seg_text, seg_image]},
                                 timeout=30)
        self._stats["replies"] += 1

    # -- 消息处理 --

    async def _dispatch(self, ev: MessageEvent) -> None:
        try:
            await self._dispatch_inner(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._stats["errors"] += 1
            print(f"[DeckMeido][qq] 处理消息失败：{e!r}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            # 异常细节不外泄：QQ 上只说一句笼统的，详情留着本地日志
            try:
                await self._send_text(ev, "出了点问题，这一条没处理成功（详情见设备日志）。")
            except Exception:
                pass

    async def _dispatch_inner(self, ev: MessageEvent) -> None:
        self._stats["received"] += 1
        self._last_message_at = self._clock()
        cfg = self._cfg
        # 限流超限：静默丢弃，一个字都不回，免得被刷屏时反过来变成放大器
        if not self._limiter.allow(ev.uin):
            self._stats["rate_limited"] += 1
            return
        ev.text = truncate_text(ev.text, cfg.max_message_chars)
        nickname = self._nickname()
        route = route_message(cfg, ev, nickname)
        if route.action == "ignore":
            self._stats["ignored"] += 1
            return
        if route.action == "command":
            self._stats["commands"] += 1
            if route.kind == "status":
                await self._do_status(ev)
                return
            if route.kind == "shot":
                await self._do_shot(ev)
                return
            if route.kind == "cancel":
                await self._do_cancel(ev)
                return
        await self._do_chat(ev, route)

    def _busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    async def _do_chat(self, ev: MessageEvent, route: Route) -> None:
        if self._busy():
            await self._send_text(ev, "上一件事还在处理，稍等一下；要停下就发 /cancel。")
            return
        cfg = self._cfg
        device = device_tag(ev)
        sid = session_sid(ev)
        text = (route.text or "").strip()
        if not text:
            await self._send_text(ev, "在昵称后面跟一句要聊的话，或者用 /status、/shot、/cancel。")
            return
        turn_id = ""
        tracked = self._pending_conf.get(device)
        if tracked:
            if self._clock() >= tracked.get("expires_at", 0):
                self._pending_conf.pop(device, None)
                if self._pending_clear is not None:
                    try:
                        self._pending_clear(device, sid)
                    except Exception as e:
                        print(f"[DeckMeido][qq] 清理超时挂起操作失败：{e!r}", file=sys.stderr)
                await self._send_text(ev, "确认已超时，这次操作已作废。")
                return
            if is_confirm_text(text):
                # 沿用挂起操作那一轮的 turn_id：handle_chat 的认领校验要求
                turn_id = str(tracked.get("turn_id") or "")
            else:
                # 说了别的就当取消：显式撤掉挂起操作，别让它一直留在后端等下一次确认
                self._pending_conf.pop(device, None)
                if self._pending_clear is not None:
                    try:
                        self._pending_clear(device, sid)
                    except Exception as e:
                        print(f"[DeckMeido][qq] 撤销挂起操作失败：{e!r}", file=sys.stderr)
        if not turn_id:
            # 每条消息带一个独立的轮次标识：确认时复用它，其它时候避免和上一轮混淆
            turn_id = "qq-" + secrets.token_hex(6)

        task = asyncio.create_task(
            self._runner(text, sid=sid, device=device,
                         readonly=route.readonly, turn_id=turn_id))
        self._turn_task = task
        self._stats["turns"] += 1
        try:
            result = await task
        except asyncio.CancelledError:
            if task.cancelled():
                # 是 /cancel 或 stop() 掐掉了这一轮，给用户一个明确结论
                try:
                    await self._send_text(ev, "已终止当前操作。")
                except Exception:
                    pass
                return
            raise
        finally:
            if self._turn_task is task:
                self._turn_task = None
        if not isinstance(result, dict):
            result = {"reply": str(result or "")}
        pending = result.get("pending")
        if isinstance(pending, dict) and pending.get("description"):
            self._pending_conf[device] = {
                "turn_id": str(pending.get("turn_id") or ""),
                "sid": sid,
                "description": str(pending.get("description") or ""),
                "expires_at": self._clock() + max(0.0, cfg.confirm_timeout),
            }
        else:
            self._pending_conf.pop(device, None)
        await self._send_chunks(ev, str(result.get("reply") or ""))

    async def _do_cancel(self, ev: MessageEvent) -> None:
        device = device_tag(ev)
        had_pending = self._pending_conf.pop(device, None)
        if had_pending and self._pending_clear is not None:
            try:
                self._pending_clear(device, session_sid(ev))
            except Exception:
                pass
        task = self._turn_task
        if task is not None and not task.done():
            # 终止的结论由正在等这一轮的那个任务统一回（_do_chat 里接 CancelledError），
            # 这里不再重复发一条
            task.cancel()
            return
        await self._send_text(ev, "现在没有正在执行的任务。")

    async def _do_status(self, ev: MessageEvent) -> None:
        info: dict = {}
        try:
            info = await self._status_provider() or {}
        except Exception as e:
            print(f"[DeckMeido][qq] 取设备状态失败：{e!r}", file=sys.stderr)
        lines = [
            "设备状态",
            f"核心：{info.get('core') or '运行中'}",
            f"模式：{info.get('mode') or '未知'}",
            f"QQ：{_state_cn(self._state)}",
        ]
        if info.get("version"):
            lines.append(f"版本：{info['version']}")
        if self._pending_conf:
            lines.append("有一项操作在等确认（发「确认」执行，说别的取消）。")
        await self._send_text(ev, "\n".join(lines))

    async def _do_shot(self, ev: MessageEvent) -> None:
        if self._busy():
            await self._send_text(ev, "正在处理上一条，稍后再截图。")
            return
        try:
            blob, note = await self._screenshot()
        except Exception as e:
            print(f"[DeckMeido][qq] 截图失败：{e!r}", file=sys.stderr)
            await self._send_text(ev, "没截到当前画面（详情见设备日志）。")
            return
        if not blob:
            await self._send_text(ev, f"没截到当前画面：{note or '截图不可用'}")
            return
        await self._send_image(ev, blob, "当前画面")

    # -- 工具 --

    def _fail_pending_calls(self) -> None:
        for echo, fut in list(self._pending_calls.items()):
            if not fut.done():
                fut.set_exception(ConnectionError("QQ 通道断开"))
            self._pending_calls.pop(echo, None)


def _to_uid(value: str):
    return int(value) if str(value).isdigit() else value


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", str(value or "")) or "0"


def session_sid(ev: MessageEvent) -> str:
    """QQ 的会话独立于本地会话；私聊/群聊再各分一份，避免把两种来源混在一起"""
    if ev.group_id:
        return f"qq-g-{_digits(ev.group_id)}-{_digits(ev.uin)}"
    return f"qq-p-{_digits(ev.uin)}"


def device_tag(ev: MessageEvent) -> str:
    """消息来源标记：写进历史，也用于「谁在什么地方」的确认校验"""
    if ev.group_id:
        return f"qq:group:{ev.group_id}:{ev.uin}"
    return f"qq:private:{ev.uin}"


def _state_cn(state: str) -> str:
    return {
        "disabled": "未启用",
        "misconfigured": "配置有误",
        "connecting": "正在连接",
        "connected": "已连接",
        "reconnecting": "重连中",
        "stopped": "已停止",
    }.get(state, state or "未知")
