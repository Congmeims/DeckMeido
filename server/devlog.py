"""开发者模式日志与统一脱敏。

默认关闭：不额外增加日志量，现有输出保持原样。
打开后：root logger 拉到 DEBUG，写 userdata/logs/debug-<时间>.log，
并把 HTTP / WebSocket / 工具调用 / LLM / STT / TTS 等细节记进去。

脱敏与开关无关：所有经过本模块的落盘文本都先过 redact()。后端启动时
还会把 sys.stdout / sys.stderr 包一层，保证 traceback、uvicorn 访问日志
这些「顺手写出去」的内容也不会把凭据带进文件。

轮转：单文件 10 MB，保留 2 个旧文件，总计约 30 MB。Deck 通常是 64 GB /
256 GB 存储，30 MB 可以忽略；同时足够装下几轮带请求/响应正文的调试日志。
超过上限自动丢最旧的，不会写满存储。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 2
# 单个字段最多 4000 字：一条 LLM 回复或工具输出够看，又不会一条日志几十 MB
MAX_FIELD = 4000
LOGGER_NAME = "deckmeido.dev"

_ENABLED = False
_HANDLER: RotatingFileHandler | None = None
_STREAMS_WRAPPED = False
_ORIG_LEVELS: dict[str, int] = {}

_LOOPBACK = {"127.0.0.1", "0.0.0.0", "::1", "localhost"}

_IPV4_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\d{1,3}\b")
_QUERY_RE = re.compile(r"([?&][^=&\s#]+=)([^&\s#]*)")
_AUTH_LINE_RE = re.compile(r"(?i)(authorization\s*:\s*)([^\r\n]+)")
_COOKIE_LINE_RE = re.compile(r"(?i)(cookie\s*:\s*)([^\r\n]+)")
_BEARER_RE = re.compile(r"(?i)((?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]+)")
_KEY_RE = re.compile(
    r'(?i)("?(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|'
    r"token|secret|password|passwd)\"?\s*[:=]\s*\"?(?!\*\*\*))([^\"\s,&;}]+)"
)
_QQ_RE = re.compile(r'(?i)("?(?:admin_qq|qq|uin|user_id)"?\s*[:=]\s*"?)(\d{5,12})')
_SK_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})\b")


def _mask_ip(match: re.Match[str]) -> str:
    full = match.group(0)
    if full in _LOOPBACK:
        return full
    # 只保留 /24 网段：192.168.1.23 -> 192.168.1.x
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}.x"


def redact(value: Any) -> str:
    """统一脱敏入口：凭据、Cookie、URL 查询值、非回环 IP、QQ 号。"""
    if value is None:
        return ""
    text = str(value)
    try:
        text = _AUTH_LINE_RE.sub(r"\1***", text)
        text = _COOKIE_LINE_RE.sub(r"\1***", text)
        text = _BEARER_RE.sub(r"\1***", text)
        text = _KEY_RE.sub(r"\1***", text)
        text = _QQ_RE.sub(r"\1***", text)
        text = _SK_RE.sub("sk-***", text)
        # URL 查询参数的值一律打码，键名保留，便于看出请求带了哪些参数
        text = _QUERY_RE.sub(r"\1***", text)
        text = _IPV4_RE.sub(_mask_ip, text)
    except Exception:
        # 脱敏函数自身出问题也不能把原文写出去
        return "（日志脱敏失败，内容已省略）"
    return text


def _field(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    text = redact(text)
    if len(text) > MAX_FIELD:
        text = text[:MAX_FIELD] + f"…（共 {len(text)} 字，已截断）"
    return text


def enabled() -> bool:
    return _ENABLED


class _RedactFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _RedactingStream:
    """把 stdout/stderr 的每一段写入先脱敏再交给原流。"""

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        try:
            return self._stream.write(redact(data))
        except Exception:
            return 0

    def writelines(self, lines):
        try:
            return self._stream.writelines([redact(x) for x in lines])
        except Exception:
            return None

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_redacting_streams() -> None:
    global _STREAMS_WRAPPED
    if _STREAMS_WRAPPED:
        return
    try:
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name, None)
            if stream is not None and not isinstance(stream, _RedactingStream):
                setattr(sys, name, _RedactingStream(stream))
        _STREAMS_WRAPPED = True
    except Exception:
        pass


def _ensure_handler() -> logging.Logger | None:
    global _HANDLER
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if _HANDLER is not None:
        return logger
    try:
        from . import paths

        out_dir = paths.logs_dir()
        path = out_dir / f"debug-{time.strftime('%Y%m%d-%H%M%S')}.log"
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_RedactFormatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        _HANDLER = handler
    except Exception:
        return None
    return logger


def _apply_levels(on: bool) -> None:
    names = ("", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore")
    for name in names:
        logger = logging.getLogger(name)
        if on:
            _ORIG_LEVELS.setdefault(name, logger.level)
            logger.setLevel(logging.DEBUG)
        else:
            if name in _ORIG_LEVELS:
                logger.setLevel(_ORIG_LEVELS[name])


def configure(cfg: dict) -> None:
    """按 config.toml 的 [runtime].dev_mode 开关；保存配置后调用即刻生效。"""
    global _ENABLED
    runtime = (cfg or {}).get("runtime") or {}
    _ENABLED = bool(runtime.get("dev_mode"))
    if _ENABLED:
        _ensure_handler()
    _apply_levels(_ENABLED)


def apply_levels() -> None:
    """uvicorn 启动后可能重设日志级别，启动钩子里再拉一次。"""
    _apply_levels(_ENABLED)


def log(event: str, **fields) -> None:
    """写一条开发者日志；未开启时直接返回，不产生任何额外开销。"""
    if not _ENABLED:
        return
    logger = _ensure_handler()
    if logger is None:
        return
    try:
        parts = [f"event={redact(event)}"]
        parts.extend(f"{key}={_field(value)}" for key, value in fields.items())
        logger.debug(" ".join(parts))
    except Exception:
        pass


def env_summary() -> dict:
    """只摘与运行方式有关的变量；值本身仍会过脱敏。"""
    wanted = (
        "DECKMEIDO_PORT", "DECKMEIDO_DIR", "DECKMEIDO_DATA_DIR",
        "DECKMEIDO_MIRROR", "DECKMEIDO_SKIP_GUI", "DECKMEIDO_INSTALL_STT",
        "DECKMEIDO_INSTALL_TTS", "DECKMEIDO_ALLOW_SECOND_BACKEND",
        "DECKMEIDO_NONINTERACTIVE", "HOME", "USER",
    )
    out = {key: os.environ[key] for key in wanted if os.environ.get(key)}
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def startup_info() -> dict:
    return {
        "argv": list(sys.argv),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "env": env_summary(),
    }
