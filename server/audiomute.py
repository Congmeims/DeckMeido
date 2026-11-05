"""录音期间临时静音系统输出

Deck 上游戏音频和麦克风是一套：按住说话时如果游戏还在外放，
拾音里全是环境余音。这里在录音开始时把默认输出静音，录完立刻恢复。

工具优先级：wpctl（WirePlumber，SteamOS 3.x 默认）→ pactl（PulseAudio 兼容）。
两者都不可用、或命令执行失败时**静默降级**：录音照常进行，只是不静音，
绝不因为静音失败去拦录音。

恢复只做「解除静音」，不改音量数值：音量是个浮点数，读回来再写回去
中间一旦失败，用户的音量就被永久改了；静音是二值状态，读一次原状态、
写一次回去，最坏情况也只是保持静音（按一下音量键就能恢复）。

另有一个兜底定时器：前端崩了、页面被强制切走导致没发恢复请求时，
AUTO_RESTORE_SEC 秒后自动解除，不把用户的机器一直静着。
"""
from __future__ import annotations

import shutil
import subprocess
import threading

# 兜底自动恢复时间（秒）。一次语音输入最长 60 秒，留一点余量
AUTO_RESTORE_SEC = 90

_WSINK = "@DEFAULT_AUDIO_SINK@"   # wpctl 的默认输出
_PSINK = "@DEFAULT_SINK@"         # pactl 的默认输出

_lock = threading.Lock()
_enabled = True
_clicks = 0          # 同时进行的录音数：只有降到 0 才真正恢复
_tool = ""           # 本次实际用的工具（wpctl / pactl）
_prev_muted = False  # 静音前的状态：本来就静音的话，恢复时保持静音
_timer: threading.Timer | None = None


def set_enabled(flag: bool) -> None:
    """设置开关（[stt] duck_output）；关掉时立刻把已静音的恢复回去"""
    global _enabled
    _enabled = bool(flag)
    if not _enabled:
        restore(force=True)


def enabled() -> bool:
    return _enabled


def tool() -> str:
    """当前可用的静音工具；都没有时返回空串"""
    if shutil.which("wpctl"):
        return "wpctl"
    if shutil.which("pactl"):
        return "pactl"
    return ""


def _run(cmd: list[str], timeout: float = 3.0):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _wpctl_muted() -> bool | None:
    try:
        out = _run(["wpctl", "get-volume", _WSINK])
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return "[MUTED]" in (out.stdout or "")


def _pactl_muted() -> bool | None:
    try:
        out = _run(["pactl", "get-sink-mute", _PSINK])
    except Exception:
        return None
    if out.returncode != 0:
        return None
    text = (out.stdout or "").strip().lower()
    if "yes" in text:
        return True
    if "no" in text:
        return False
    return None


def _set_muted(t: str, muted: bool) -> bool:
    cmd = (
        ["wpctl", "set-mute", _WSINK, "1" if muted else "0"]
        if t == "wpctl"
        else ["pactl", "set-sink-mute", _PSINK, "1" if muted else "0"]
    )
    try:
        return _run(cmd).returncode == 0
    except Exception:
        return False


def duck() -> dict:
    """开始录音：静音默认输出。第一个录音才真正静音，重复调用只加计数"""
    global _clicks, _tool, _prev_muted, _timer
    with _lock:
        if not _enabled:
            return {"ok": True, "ducked": False, "reason": "已在设置里关闭"}
        _clicks += 1
        if _clicks > 1:
            return {"ok": True, "ducked": True, "tool": _tool}
        t = tool()
        muted = _wpctl_muted() if t == "wpctl" else (_pactl_muted() if t else None)
        # wpctl 在但读不到（不是 PipeWire 会话）：再试 pactl
        if t == "wpctl" and muted is None and shutil.which("pactl"):
            t = "pactl"
            muted = _pactl_muted()
        if not t or muted is None:
            _clicks = 0
            return {"ok": True, "ducked": False, "reason": "找不到可用的音频控制工具"}
        _tool = t
        _prev_muted = muted
        if not _set_muted(t, True):
            _clicks = 0
            _tool = ""
            return {"ok": True, "ducked": False, "reason": "静音命令执行失败"}
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(AUTO_RESTORE_SEC, restore, kwargs={"force": True})
        _timer.daemon = True
        _timer.start()
        return {"ok": True, "ducked": True, "tool": t}


def restore(force: bool = False) -> dict:
    """录音结束（或取消/异常）：解除静音。force=True 时忽略计数直接恢复"""
    global _clicks, _tool, _timer
    with _lock:
        if force:
            _clicks = 0
        elif _clicks <= 0:
            return {"ok": True, "restored": False}
        else:
            _clicks -= 1
            if _clicks > 0:
                return {"ok": True, "restored": False}
        if _timer is not None:
            _timer.cancel()
            _timer = None
        if not _tool:
            return {"ok": True, "restored": False}
        t = _tool
        was = _prev_muted
        _tool = ""
        # 录音前就是静音的，用户显然不想要外放，别自作主张帮他打开
        if not was and not _set_muted(t, False):
            return {"ok": True, "restored": False, "reason": "解除静音失败"}
        return {"ok": True, "restored": True, "tool": t, "was_muted": was}


def status() -> dict:
    """给 /api/stt 用：开关状态 + 这台机器能不能静音"""
    with _lock:
        t = tool()
        return {
            "enabled": _enabled,
            "tool": t,
            "available": bool(t),
            "active": _clicks > 0,
        }
