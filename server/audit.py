"""鉴权与配对审计：进程内环形缓冲，只读暴露给设置页。

只记「谁在什么时候做了什么、结果如何」，绝不记录 PIN 或设备令牌：
调用方传进来的 reason 必须是固定文案，不能拼接用户输入。
"""
from __future__ import annotations

import threading
import time
from collections import deque

MAX_EVENTS = 200

_lock = threading.Lock()
_events: deque[dict] = deque(maxlen=MAX_EVENTS)


_RESERVED = {"ts", "kind", "action", "result", "ip", "reason", "detail"}


def record(kind: str, action: str, result: str, ip: str = "", reason: str = "",
           detail: str = "", extra: dict | None = None) -> dict:
    """记一条审计事件（最新的在前），返回写入的那条。

    extra 用于终端这类需要结构化字段（退出码/起止时间/是否确认）的场景，
    不允许覆盖基础字段。
    """
    ev = {
        "ts": time.time(),
        "kind": kind,
        "action": action,
        "result": result,
        "ip": ip,
        "reason": reason,
        "detail": detail,
    }
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key in _RESERVED:
                continue
            ev[key] = value
    with _lock:
        _events.appendleft(ev)
    return ev


def recent(limit: int = 50) -> list[dict]:
    """最近 limit 条，最新的在前。"""
    n = max(0, min(int(limit or 0), MAX_EVENTS))
    with _lock:
        return list(_events)[:n] if n else []


def clear() -> None:
    """仅测试用：清空缓冲。"""
    with _lock:
        _events.clear()
