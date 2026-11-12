"""sudo 授权通道：一次校验、短期内存复用、密码只走 askpass。

历史问题：AI 用 `sudo -S ...` 时工具层没把 stdin 送进子进程，反复报「未提供密码」；
最后退化成 `echo '密码' | sudo -S ...`，密码明文进了命令行、日志和审批界面。

这里的约定：
- 密码只保存在后端进程内存里（带 TTL，默认 10 分钟，[security].sudo_ttl 可配）；
- 交给 sudo 时只通过 SUDO_ASKPASS 助手 + 子进程环境变量，不进 argv、不写文件；
- 校验、执行、审计、日志、命令回显里的密码一律打码；
- 没有可用授权时只登记一次待授权请求并广播给界面，由界面输入密码，
  不再让模型在对话里反复索要。

askpass 助手本身是一个只读环境变量的空壳脚本，不含任何密码。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from . import devlog

# 密码通过这个环境变量交给 askpass 助手；助手只打印它，不落盘
ASKPASS_ENV = "DECKMEIDO_SUDO_PASSWORD"
DEFAULT_TTL = 600.0          # 默认 10 分钟
MIN_TTL = 60.0
MAX_TTL = 86400.0
MAX_ATTEMPTS = 3             # 单次待授权请求里允许连续输错几次
SILENT_PROBE_TTL = 60.0      # `sudo -n true` 的探测缓存

_SUDO_WRAPPERS = {"env", "nohup", "nice", "time", "command", "exec", "stdbuf", "setsid"}
_ASKPASS_SCRIPT = '#!/bin/sh\nprintf \'%s\\n\' "${DECKMEIDO_SUDO_PASSWORD-}"\n'

_lock = threading.RLock()
_cache: dict | None = None     # {"password","expires","source","verified_at"}
_pending: dict | None = None   # {"id","source","command","created","attempts","last_error"}
_hook = None
_ttl = DEFAULT_TTL
_probe: tuple[float, bool] | None = None
_askpass_path_cache: Path | None = None


class SudoError(Exception):
    """带分类原因的 sudo 失败；reason 给前端/工具层判断，message 直接展示"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def configure(cfg: dict | None) -> None:
    """从配置读取 TTL（秒），越界夹到合理区间；配置缺失用默认值"""
    global _ttl
    try:
        raw = float(((cfg or {}).get("security") or {}).get("sudo_ttl", DEFAULT_TTL))
    except (TypeError, ValueError):
        raw = DEFAULT_TTL
    if raw <= 0:
        raw = DEFAULT_TTL
    _ttl = max(MIN_TTL, min(MAX_TTL, raw))


def set_hook(hook) -> None:
    """main 启动时注入：把待授权/授权结果转成 WS 广播"""
    global _hook
    _hook = hook


def ttl() -> float:
    return _ttl


def _notify(payload: dict) -> None:
    hook = _hook
    if hook:
        try:
            hook(payload)
        except Exception as exc:
            devlog.debug("sudo_auth._notify", error=repr(exc))
            pass


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


# ---------- askpass 助手 ----------

def askpass_path() -> Path:
    """返回（首次调用时创建）askpass 助手脚本路径。

    脚本只打印环境变量，文件本身不含密码；目录 0700、文件 0700，
    名字随机生成，避免在 /tmp 里被预置同名文件劫持。
    """
    global _askpass_path_cache
    if _askpass_path_cache and _askpass_path_cache.exists():
        return _askpass_path_cache
    base = Path(tempfile.mkdtemp(prefix="deckmeido-askpass-"))
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    path = base / "askpass.sh"
    path.write_text(_ASKPASS_SCRIPT, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    _askpass_path_cache = path
    return path


def sudo_env(password: str) -> dict:
    """sudo 子进程要带的环境：askpass 路径、强制走 askpass、密码本体"""
    return {
        "SUDO_ASKPASS": str(askpass_path()),
        "SUDO_ASKPASS_REQUIRE": "force",
        ASKPASS_ENV: str(password),
    }


# ---------- 授权缓存 ----------

def cached_password(now: float | None = None) -> str | None:
    """取当前有效的缓存密码；过期就顺手清掉。只在内存里，不对外暴露"""
    global _cache
    with _lock:
        if not _cache:
            return None
        if _now(now) >= float(_cache.get("expires") or 0):
            _cache = None
            return None
        return _cache.get("password") or None


def cache_status(now: float | None = None) -> dict:
    """给界面/工具看的缓存状态，绝不含密码"""
    global _cache
    with _lock:
        if not _cache or _now(now) >= float(_cache.get("expires") or 0):
            _cache = None
            return {"available": False, "expires_in": 0, "source": ""}
        return {
            "available": True,
            "expires_in": max(0, int(float(_cache["expires"]) - _now(now))),
            "source": str(_cache.get("source") or ""),
            "ttl": int(_ttl),
        }


def store(password: str, source: str = "", now: float | None = None) -> None:
    """校验通过后写进内存缓存（TTL 内复用）"""
    global _cache, _pending
    now = _now(now)
    with _lock:
        _cache = {"password": str(password), "expires": now + _ttl,
                  "source": str(source or ""), "verified_at": now}
        _pending = None


def invalidate(reason: str = "") -> None:
    """主动作废缓存（例如 sudo 报密码已过期）"""
    global _cache
    with _lock:
        _cache = None
    _notify({"state": "expired", "message": reason or "sudo 授权已失效"})


def pending_status() -> dict | None:
    with _lock:
        return dict(_pending) if _pending else None


def pending_command(now: float | None = None) -> dict | None:
    """取出被拦下、还值得续跑的命令：{"command", "source", "created"}。

    没有待授权请求、或它已经过期、或它本来就只是「用户主动提前授权」
    （command 为空）时返回 None，调用方据此决定是否触发续跑。
    """
    with _lock:
        if not _pending:
            return None
        command = str(_pending.get("command") or "").strip()
        if not command:
            return None
        if _now(now) >= float(_pending.get("expires") or 0):
            return None
        return {"command": command,
                "source": str(_pending.get("source") or ""),
                "created": float(_pending.get("created") or 0)}


def clear_all() -> None:
    """仅测试用：清空缓存与待授权请求"""
    global _cache, _pending, _probe
    with _lock:
        _cache = None
        _pending = None
        _probe = None


# ---------- 命令判定 ----------

def command_uses_sudo(command: str) -> bool:
    """命令链里是否有真正以 sudo/doas 开头的子命令（echo 'sudo' 不算）"""
    for seg in re.split(r"&&|\|\||[;|\n]", command or ""):
        try:
            words = shlex.split(seg, comments=False, posix=True)
        except ValueError:
            words = seg.split()
        while words and (words[0] in _SUDO_WRAPPERS
                         or re.match(r"^[A-Za-z_]\w*=", words[0])):
            words = words[1:]
        if words and os.path.basename(words[0]) in ("sudo", "doas"):
            return True
    return False


def silent_sudo(now: float | None = None) -> bool:
    """免密 sudo 是否可用（只探 `sudo -n true`，不读密码、不弹窗）"""
    global _probe
    now = _now(now)
    if _probe and now - _probe[0] < SILENT_PROBE_TTL:
        return _probe[1]
    ok = False
    try:
        rc = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=3.0, env={**os.environ}).returncode
        ok = rc == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _probe = (now, ok)
    return ok


_PIPE_SUDO_RE = re.compile(
    r"(?is)\b(?:echo|printf)\b[^|;&\n]{0,200}\|\s*((?:sudo|doas)\b[^|;&\n]*)")


def inline_sudo_password(command: str) -> bool:
    """命令是否把密码用 echo/printf 管道喂给 `sudo -S`（明文进 argv，必须拒绝）"""
    for match in _PIPE_SUDO_RE.finditer(command or ""):
        if re.search(r"(?<![\w.-])-S\b", match.group(1)):
            return True
    return False


def normalize_command(command: str) -> str:
    """把 `sudo -S` 改成 `sudo -A`，强制走 askpass，不再从 stdin 读密码"""
    return re.sub(r"(?<![\w.-])sudo\s+-S\b", "sudo -A", command or "")


# ---------- 校验与错误分类 ----------

_ASKPASS_HINTS = ("no askpass program specified", "askpass", "a terminal is required",
                  "no tty present", "must have a tty")
_NO_SUDOERS_HINTS = ("not in the sudoers file", "may not run sudo",
                     "is not allowed to run sudo", "not allowed to run sudo")
_BAD_PW_HINTS = ("incorrect password attempt", "sorry, try again",
                 "a password is required", "incorrect password")


def classify_failure(rc: int, output: str, timed_out: bool = False) -> tuple[str, str]:
    """把 sudo 失败拆成可读分类：bad_password / no_sudoers / askpass / timeout / failed"""
    low = (output or "").lower()
    if timed_out:
        return "timeout", "sudo 校验超时，请重试"
    if any(s in low for s in _NO_SUDOERS_HINTS):
        return ("no_sudoers",
                "当前用户不在 sudoers 中，无法通过密码提权；"
                "请换有 sudo 权限的账号，或由管理员把该用户加入 sudoers")
    if any(s in low for s in _BAD_PW_HINTS):
        return "bad_password", "管理员密码不正确"
    if any(s in low for s in _ASKPASS_HINTS):
        return ("askpass",
                "sudo 无法通过 askpass 取到密码（可能未安装 sudo 或不支持 SUDO_ASKPASS）")
    if rc == 127:
        return "askpass", "找不到 sudo 命令，无法提权"
    line = next((l.strip() for l in (output or "").splitlines() if l.strip()), "")
    return "failed", f"sudo 执行失败（退出码 {rc}）" + (f"：{line[:120]}" if line else "")


def _run_sudo(args: list[str], password: str, timeout: float) -> tuple[int, str]:
    """以 askpass 方式跑 sudo；stdin 全关，避免任何交互式等待"""
    try:
        proc = subprocess.run(
            ["sudo", "-A"] + args,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, env={**os.environ, **sudo_env(password)})
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "找不到 sudo 命令"
    except subprocess.TimeoutExpired:
        raise SudoError("timeout", "sudo 校验超时，请重试") from None
    except OSError as exc:
        raise SudoError("askpass", f"启动 sudo 失败：{exc}") from exc


def verify(password: str, timeout: float = 15.0) -> tuple[bool, str, str]:
    """用 `sudo -A -v` 校验密码（只刷新凭据，不执行任何命令）。

    返回 (ok, reason, message)；密码只通过环境变量交给 askpass 助手。
    """
    if not str(password or "").strip():
        return False, "empty", "请输入管理员密码"
    try:
        rc, out = _run_sudo(["-v"], str(password), timeout)
    except SudoError as exc:
        return False, exc.reason, exc.message
    if rc == 0:
        return True, "", ""
    reason, message = classify_failure(rc, out)
    return False, reason, message


# ---------- 待授权请求 ----------

def _request_message(source: str) -> str:
    minutes = max(1, int(round(_ttl / 60)))
    return ("这条命令需要管理员权限（sudo），但当前没有可用授权，所以没有执行。"
            f"授权入口已经打开：请在输入框上方的「需要授权」行输入一次管理员密码，"
            f"通过后 {minutes} 分钟内 sudo 命令会直接复用，不会再问。"
            "不要在对话里把密码明文发给我，也不要使用 echo 密码 | sudo -S 这类写法。"
            "授权完成后会自动重新执行这条命令，不需要你再催我。")


def prepare(command: str, source: str = "run_command",
            now: float | None = None) -> dict:
    """执行 sudo 命令前的准备：

    - 不是 sudo 命令 → mode=none；
    - 免密 sudo 可用 → mode=silent；
    - 内存里有有效授权 → mode=askpass（密码由调用方放进子进程环境）；
    - 否则登记一条待授权请求、广播给界面，并返回 need_auth（不执行命令）。
    """
    global _pending
    if not command_uses_sudo(command):
        return {"ok": True, "mode": "none"}
    if silent_sudo(now):
        return {"ok": True, "mode": "silent"}
    password = cached_password(now)
    if password:
        return {"ok": True, "mode": "askpass", "password": password}
    with _lock:
        _pending = {
            "id": f"{int(_now(now) * 1000)}",
            "source": str(source or ""),
            "command": command,
            "created": _now(now),
            "attempts": 0,
            "last_error": "",
            "expires": _now(now) + _ttl,
        }
        info = dict(_pending)
    _notify({"state": "need_password", "message": "命令需要管理员授权",
             "source": info["source"],
             "command": redact_command(info["command"]),
             "ttl": int(_ttl)})
    _audit_auth("deny", source, "need_password", "等待管理员输入密码")
    return {"ok": False, "reason": "need_auth", "message": _request_message(source),
            "pending": {**info, "command": redact_command(info["command"])}}


def provide_password(password: str, source: str = "ui") -> dict:
    """界面提交密码：校验通过后写进内存缓存，供后续 sudo 命令复用。

    失败时按原因返回可读提示；连续输错达到上限后结束本次待授权请求。
    """
    global _pending
    if not isinstance(password, str) or not password.strip():
        return {"ok": False, "reason": "empty", "error": "请输入管理员密码"}
    ok, reason, message = verify(password)
    if ok:
        # store() 会清空 _pending，续跑要用的命令必须在它之前取出来
        held = pending_command()
        store(password, source)
        _notify({"state": "ok", "expires_in": int(_ttl), "ttl": int(_ttl)})
        _audit_auth("ok", source, "verified", f"授权 {int(_ttl)}s 内复用")
        res = {"ok": True, "expires_in": int(_ttl), "ttl": int(_ttl)}
        if held:
            # 交给调用方（main）去续跑；用户主动授权时这里为空
            res["resume"] = held
        return res
    with _lock:
        if _pending is None:
            # 用户可能在命令触发前就主动授权；失败次数同样要计数
            _pending = {
                "id": f"{int(_now() * 1000)}",
                "source": str(source or ""),
                "command": "",
                "created": _now(),
                "attempts": 0,
                "last_error": "",
                "expires": _now() + _ttl,
            }
        _pending["attempts"] = int(_pending.get("attempts") or 0) + 1
        left = MAX_ATTEMPTS - _pending["attempts"]
        _pending["last_error"] = message
    if reason == "no_sudoers":
        _notify({"state": "need_password", "message": message, "error": message})
        _audit_auth("deny", source, reason, message)
        return {"ok": False, "reason": reason, "error": message}
    if left <= 0:
        with _lock:
            _pending = None
        msg = f"{message}，连续错误已达 {MAX_ATTEMPTS} 次，本次授权已结束"
        _notify({"state": "failed", "message": msg, "error": msg})
        _audit_auth("deny", source, reason, msg)
        return {"ok": False, "reason": "too_many_attempts", "error": msg}
    text = f"{message}，还可再试 {left} 次"
    _notify({"state": "need_password", "message": text, "error": text,
             "remaining": left})
    _audit_auth("deny", source, reason, text)
    return {"ok": False, "reason": reason, "error": text, "remaining": left}


# ---------- 脱敏 ----------

_PW_FLAG_RE = re.compile(r"(?is)((?:^|[;&|(\s])echo\s+(['\"]))(.+?)(\2\s*\|\s*sudo)")
_ENV_PW_RE = re.compile(rf"(?i)({re.escape(ASKPASS_ENV)}\s*=\s*)(\S+)")


def _audit_auth(result: str, source: str, reason: str, detail: str = "") -> None:
    """审计授权成功/失败；detail 只放固定分类文案，绝不含密码"""
    try:
        from . import audit

        audit.record("sudo", "auth", result, reason=reason, detail=detail,
                     extra={"source": str(source or "")})
    except Exception as exc:
        devlog.debug("sudo_auth._audit_auth", error=repr(exc))
        pass


def redact_text(text: str, secrets=()) -> str:
    """把文本里的密码明文字面量替换成 ***；secrets 可传已知密码"""
    out = str(text or "")
    for secret in secrets or ():
        s = str(secret or "")
        if s and len(s) >= 2:
            out = out.replace(s, "***")
            out = out.replace(shlex.quote(s), "***")
    raw = cached_password()
    if raw and len(raw) >= 2:
        out = out.replace(raw, "***")
        out = out.replace(shlex.quote(raw), "***")
    out = _ENV_PW_RE.sub(r"\1***", out)
    out = _PW_FLAG_RE.sub(r"\1***\4", out)
    return out


def redact_command(command: str) -> str:
    """审计/日志/回显用：去掉命令里可能内联的密码"""
    return redact_text(command)


def redact_stdin(stdin: str) -> str:
    """标准输入是密码时（旧的 sudo -S 用法），整个打码"""
    return "***" if str(stdin or "").strip() else ""
