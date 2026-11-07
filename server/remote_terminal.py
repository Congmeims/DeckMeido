"""远程终端会话：真 PTY、多会话、危险命令确认与审计。

设计要点：
- 每个会话是一个常驻 shell（保持 cwd / 环境变量），而不是每次命令新建进程。
- POSIX 上通过 ptyprocess（基于 pty.openpty）拉起真 PTY；缺失时退回 pty.openpty。
  只有开发机（Windows）才使用管道后端做测试，目标机 SteamOS 永远走 PTY。
- 命令先过 policy 的危险判定，命中后必须显式确认才写入 shell。
- 每次 exec 注入一个带随机 token 的退出码标记，解析后写审计（含起止时间、退出码）。
- 输出按块缓存并广播给订阅者，超过单会话上限后截断并标记，绝不让编码异常影响连接。
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import audit, policy

DEFAULT_IDLE_TIMEOUT_S = 900.0
DEFAULT_MAX_SESSIONS = 3
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
MIN_IDLE_TIMEOUT_S = 5.0
MAX_IDLE_TIMEOUT_S = 86_400.0
MIN_MAX_SESSIONS = 1
MAX_MAX_SESSIONS = 10
MIN_MAX_OUTPUT_BYTES = 1_024
MAX_MAX_OUTPUT_BYTES = 16 * 1_048_576

MAX_COMMAND_CHARS = 4_096
MAX_INPUT_BYTES = 16 * 1_024
MAX_SHELL_CHARS = 160
READ_CHUNK = 4_096
ENDED_RETENTION_S = 300.0
MAX_SESSION_RECORDS = 64
DEFAULT_OUTPUT_PAGE_BYTES = 256 * 1_024
MAX_OUTPUT_PAGE_BYTES = 1_048_576

_MARKER_PREFIX = b"__DM_EXIT__"
# 允许标记行前面带提示符（Windows cmd 会输出 "C:\...>__DM_EXIT__..."），
# 整行（含提示符）一起从给客户端的输出里剔掉。
_MARKER_RE = re.compile(
    rb"[^\r\n]*__DM_EXIT__([0-9a-f]{6,32}):(-?\d+)[^\r\n]*(?:\r?\n|$)")


class TerminalError(Exception):
    """可直接映射成 HTTP 错误分支的终端异常。"""

    def __init__(self, code: str, reason: str, status: int = 400,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status = status
        self.extra = extra or {}


@dataclass(frozen=True)
class TerminalSettings:
    enabled: bool = False
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    max_sessions: int = DEFAULT_MAX_SESSIONS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    shell: str = ""
    cwd: str = ""

    def public(self) -> dict[str, Any]:
        """给设置页 / 客户端看的安全配置视图。"""
        return {
            "remote_shell": self.enabled,
            "idle_timeout_seconds": self.idle_timeout_s,
            "max_sessions": self.max_sessions,
            "max_output_bytes": self.max_output_bytes,
            "shell": self.shell,
            "cwd": self.cwd,
        }


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def settings_from(cfg: dict[str, Any] | None) -> TerminalSettings:
    """从 config.toml 的 [terminal] 段解析设置，任何脏值都夹到安全范围。"""
    section = {}
    if isinstance(cfg, dict):
        raw = cfg.get("terminal")
        if isinstance(raw, dict):
            section = raw
    return TerminalSettings(
        enabled=_as_bool(section.get("remote_shell"), False),
        idle_timeout_s=_clamp_float(
            section.get("idle_timeout_seconds"), DEFAULT_IDLE_TIMEOUT_S,
            MIN_IDLE_TIMEOUT_S, MAX_IDLE_TIMEOUT_S),
        max_sessions=_clamp_int(
            section.get("max_sessions"), DEFAULT_MAX_SESSIONS,
            MIN_MAX_SESSIONS, MAX_MAX_SESSIONS),
        max_output_bytes=_clamp_int(
            section.get("max_output_bytes"), DEFAULT_MAX_OUTPUT_BYTES,
            MIN_MAX_OUTPUT_BYTES, MAX_MAX_OUTPUT_BYTES),
        shell=str(section.get("shell") or "").strip()[:MAX_SHELL_CHARS],
        cwd=str(section.get("cwd") or "").strip(),
    )


def assess_command(command: str) -> policy.RiskDecision:
    """复用 policy 的危险判定（含 sh -c 递归），再补上电源操作。"""
    risk = policy.classify_remote_command(command)
    if risk.level != "normal":
        return risk
    action = policy.power_action(command) or policy.steamosctl_action(command)
    if action:
        return policy.RiskDecision(
            "hard_confirm", "power_action", f"{action} 属于电源/会话切换操作")
    return policy.REMOTE_NORMAL


# ---------------------------------------------------------------- 进程句柄


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except Exception:
        try:
            os.kill(pid, sig)
        except Exception:
            pass


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        import fcntl
        import struct
        import termios
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


def _load_ptyprocess() -> Any:
    try:
        import ptyprocess  # type: ignore

        if hasattr(ptyprocess, "PtyProcess"):
            return ptyprocess
    except Exception:
        pass
    return None


class _Handle:
    """把 ptyprocess / openpty / 管道三种后端收敛成同一套接口。"""

    kind = "pipe"
    supports_resize = False

    pid: int

    def read(self, size: int) -> bytes:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def write(self, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:
        return

    def poll(self) -> int | None:  # pragma: no cover
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
        raise NotImplementedError

    def signal_term(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def signal_kill(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        return


class _PtyProcessHandle(_Handle):
    kind = "pty"
    supports_resize = True

    def __init__(self, proc: Any) -> None:
        self._proc = proc
        self.pid = int(proc.pid)

    def read(self, size: int) -> bytes:
        try:
            return self._proc.read(size)
        except EOFError:
            return b""
        except Exception:
            return b""

    def write(self, data: bytes) -> None:
        self._proc.write(data)

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def poll(self) -> int | None:
        try:
            if self._proc.isalive():
                return None
            status = self._proc.exitstatus
            return status if status is not None else -1
        except Exception:
            return -1

    def wait(self, timeout: float | None = None) -> int:
        return int(self._proc.wait(timeout))

    def signal_term(self) -> None:
        _kill_group(self.pid, signal.SIGTERM)

    def signal_kill(self) -> None:
        _kill_group(self.pid, signal.SIGKILL)

    def close(self) -> None:
        try:
            self._proc.close()
        except Exception:
            pass


class _OpenptyHandle(_Handle):
    kind = "pty"
    supports_resize = True

    def __init__(self, proc: subprocess.Popen, master_fd: int) -> None:
        self._proc = proc
        self._master_fd = master_fd
        self.pid = int(proc.pid)
        self._closed = False

    def read(self, size: int) -> bytes:
        try:
            return os.read(self._master_fd, size)
        except Exception:
            return b""

    def write(self, data: bytes) -> None:
        os.write(self._master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        _set_winsize(self._master_fd, rows, cols)

    def poll(self) -> int | None:
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return int(self._proc.wait(timeout))

    def signal_term(self) -> None:
        _kill_group(self.pid, signal.SIGTERM)

    def signal_kill(self) -> None:
        _kill_group(self.pid, signal.SIGKILL)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._master_fd)
        except Exception:
            pass


class _PipeHandle(_Handle):
    kind = "pipe"
    supports_resize = False

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self.pid = int(proc.pid)

    def read(self, size: int) -> bytes:
        try:
            return self._proc.stdout.read(size) or b""
        except Exception:
            return b""

    def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def poll(self) -> int | None:
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return int(self._proc.wait(timeout))

    def signal_term(self) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.pid)],
                    capture_output=True, timeout=5, check=False)
            except Exception:
                pass
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

    def signal_kill(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass

    def close(self) -> None:
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def _shell_argv(shell: str) -> list[str]:
    if shell:
        try:
            return shlex.split(shell, posix=(os.name != "nt"))
        except ValueError:
            return [shell]
    if os.name == "nt":
        return [os.environ.get("COMSPEC") or "cmd.exe", "/Q", "/D"]
    for candidate in ("/bin/bash", "/usr/bin/bash", os.environ.get("SHELL") or ""):
        if candidate and os.path.exists(candidate):
            return [candidate]
    return ["/bin/sh"]


def _spawn(settings: TerminalSettings, cwd: str, rows: int,
           cols: int) -> tuple[_Handle, list[str]]:
    argv = _shell_argv(settings.shell)
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    if os.name == "posix":
        ptyproc = _load_ptyprocess()
        if ptyproc is not None:
            try:
                proc = ptyproc.PtyProcess.spawn(
                    argv, cwd=cwd, env=env, dimensions=(rows, cols))
                return _PtyProcessHandle(proc), argv
            except Exception:
                pass
        import pty

        master, slave = pty.openpty()
        _set_winsize(slave, rows, cols)
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True)
        os.close(slave)
        return _OpenptyHandle(proc, master), argv
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    return _PipeHandle(proc), argv


# ---------------------------------------------------------------- 会话


class RemoteSession:
    """一个常驻 shell 会话。所有方法都在同一个事件循环里调用。"""

    def __init__(self, sid: str, handle: _Handle, settings: TerminalSettings,
                 owner: str, source: str, cwd: str, cols: int, rows: int,
                 shell: str = "") -> None:
        self.id = sid
        self.pid = handle.pid
        self._handle = handle
        self._settings = settings
        self.owner = owner
        self.source = source
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.shell = shell
        self.backend = handle.kind

        now = time.time()
        self.created = now
        self.started = now
        self.last_activity = now
        self.ended: float | None = None
        self.state = "running"
        self.exit_code: int | None = None
        self.terminated = False
        self._ending_reason: str | None = None

        self.output_total = 0
        self.truncated = False
        self._next_seq = 0
        self._ring: deque[tuple[int, bytes]] = deque()
        self._ring_bytes = 0
        self._marker_buf = b""
        self._listeners: set[asyncio.Queue] = set()
        self.completed_execs = 0

        self.pending_execs: dict[str, dict[str, Any]] = {}
        self.last_command: dict[str, Any] | None = None

        self._reader_task: asyncio.Task | None = None

    # -- 基本状态 -------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.state == "running"

    def touch(self) -> None:
        self.last_activity = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pid": self.pid,
            "state": self.state,
            "exit_code": self.exit_code,
            "created": self.created,
            "started": self.started,
            "last_activity": self.last_activity,
            "idle_seconds": round(max(0.0, time.time() - self.last_activity), 1),
            "ended": self.ended,
            "cols": self.cols,
            "rows": self.rows,
            "cwd": self.cwd,
            "shell": self.shell,
            "backend": self.backend,
            "resize": self._handle.supports_resize,
            "output_total": self.output_total,
            "output_next_seq": self._next_seq,
            "truncated": self.truncated,
            "terminated": self.terminated,
            "completed_execs": self.completed_execs,
            "last_command": dict(self.last_command) if self.last_command else None,
        }

    # -- 读写 -----------------------------------------------------

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while self.state == "running":
                chunk = await loop.run_in_executor(None, self._handle.read, READ_CHUNK)
                if not chunk:
                    break
                self._process_chunk(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._finish()

    def _process_chunk(self, chunk: bytes) -> None:
        data = self._marker_buf + chunk
        self._marker_buf = b""
        pieces: list[bytes] = []
        pos = 0
        for match in _MARKER_RE.finditer(data):
            pieces.append(data[pos:match.start()])
            self._finish_exec(match.group(1).decode("ascii"), int(match.group(2)))
            pos = match.end()
        pieces.append(data[pos:])
        out = b"".join(pieces)
        # 标记行可能被分块截断，先扣住尾部，下一块再拼接识别。
        idx = out.rfind(_MARKER_PREFIX)
        if idx >= 0 and len(out) - idx <= 40 and b"\n" not in out[idx:]:
            self._marker_buf = out[idx:]
            out = out[:idx]
        if out:
            self._store_output(out)

    def _store_output(self, data: bytes) -> None:
        if self.truncated or not data:
            return
        cap = self._settings.max_output_bytes
        remaining = cap - self.output_total
        if remaining <= 0:
            self._mark_truncated()
            return
        cut = False
        if len(data) > remaining:
            data = data[:remaining]
            cut = True
        self.output_total += len(data)
        seq = self._next_seq
        self._next_seq += 1
        self._ring.append((seq, data))
        self._ring_bytes += len(data)
        while self._ring_bytes > cap and self._ring:
            _, old = self._ring.popleft()
            self._ring_bytes -= len(old)
        self._emit({
            "type": "output",
            "seq": seq,
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
        })
        if cut:
            self._mark_truncated()

    def _mark_truncated(self) -> None:
        if self.truncated:
            return
        self.truncated = True
        self._emit({
            "type": "truncated",
            "total_bytes": self.output_total,
            "limit": self._settings.max_output_bytes,
        })

    def _emit(self, event: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except Exception:
                pass

    def _write(self, data: bytes) -> None:
        if not self.alive:
            raise TerminalError("session_ended", "会话已结束，不能再写入", 409)
        try:
            self._handle.write(data)
        except Exception as exc:
            self._finish()
            raise TerminalError("write_failed", f"写入终端失败：{exc}", 502)
        self.touch()

    def run(self, command: str, confirm: bool, owner: str,
            source: str) -> dict[str, Any]:
        if not self.alive:
            raise TerminalError(
                "session_ended", f"会话已结束（state={self.state}）", 409)
        cmd = str(command or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not cmd.strip():
            raise TerminalError("empty_command", "命令不能为空", 400)
        if len(cmd) > MAX_COMMAND_CHARS:
            raise TerminalError(
                "command_too_long", f"命令过长（上限 {MAX_COMMAND_CHARS} 字符）", 413)
        risk = assess_command(cmd)
        if risk.level != "normal" and not confirm:
            return {
                "need_confirm": True,
                "level": risk.level,
                "rule": risk.rule,
                "reason": risk.reason,
                "command": cmd,
            }
        token = uuid.uuid4().hex[:12]
        record = {
            "token": token,
            "command": cmd,
            "started": time.time(),
            "confirmed": bool(confirm),
            "owner": owner or self.owner,
            "source": source or self.source,
        }
        self.pending_execs[token] = record
        if os.name == "nt":
            marker = f"echo __DM_EXIT__{token}:%errorlevel%"
            payload = f"{cmd}\r\n{marker}\r\n".encode("utf-8", "surrogateescape")
        else:
            marker = f"printf '\\n__DM_EXIT__{token}:%d\\n' \"$?\""
            payload = f"{cmd}\n{marker}\n".encode("utf-8", "surrogateescape")
        self._write(payload)
        return {
            "need_confirm": False,
            "token": token,
            "level": risk.level,
            "rule": risk.rule,
            "reason": risk.reason,
            "confirmed": bool(confirm),
            "session": self.snapshot(),
        }

    def feed(self, data: bytes, confirm: bool, owner: str,
             source: str) -> dict[str, Any]:
        """原始 stdin 写入。请求里带换行的行会按命令做危险判定。"""
        if not self.alive:
            raise TerminalError(
                "session_ended", f"会话已结束（state={self.state}）", 409)
        if len(data) > MAX_INPUT_BYTES:
            raise TerminalError(
                "input_too_large", f"单次输入过大（上限 {MAX_INPUT_BYTES} 字节）", 413)
        text = data.decode("utf-8", "replace")
        for line in re.split(r"\r\n|\r|\n", text):
            if not line.strip():
                continue
            risk = assess_command(line)
            if risk.level != "normal" and not confirm:
                return {
                    "need_confirm": True,
                    "level": risk.level,
                    "rule": risk.rule,
                    "reason": risk.reason,
                    "command": line,
                }
        self._write(data)
        return {"need_confirm": False, "bytes": len(data), "session": self.snapshot()}

    def resize(self, cols: int, rows: int) -> None:
        cols = max(20, min(500, int(cols)))
        rows = max(5, min(300, int(rows)))
        if self._handle.supports_resize:
            self._handle.resize(rows, cols)
            self.cols = cols
            self.rows = rows
            self.touch()

    # -- 输出读取 -------------------------------------------------

    def read_output(self, cursor: int, max_bytes: int) -> tuple[list[tuple[int, bytes]], int, bool]:
        first = self._ring[0][0] if self._ring else self._next_seq
        dropped = cursor < first
        cursor = max(cursor, first)
        chunks: list[tuple[int, bytes]] = []
        total = 0
        for seq, data in self._ring:
            if seq < cursor:
                continue
            if total + len(data) > max_bytes:
                break
            chunks.append((seq, data))
            total += len(data)
        next_cursor = chunks[-1][0] + 1 if chunks else cursor
        return chunks, next_cursor, dropped

    def subscribe(self, cursor: int, max_bytes: int = DEFAULT_OUTPUT_PAGE_BYTES) -> asyncio.Queue:
        """返回带历史回放的事件队列；队列以 None 结束。"""
        queue: asyncio.Queue = asyncio.Queue()
        high = self._next_seq
        self._listeners.add(queue)
        chunks, next_cursor, dropped = self.read_output(cursor, max_bytes)
        for seq, data in chunks:
            if seq >= high:
                break
            queue.put_nowait({
                "type": "output",
                "seq": seq,
                "encoding": "base64",
                "data": base64.b64encode(data).decode("ascii"),
            })
        queue.put_nowait({"type": "cursor", "next_cursor": next_cursor, "dropped": dropped})
        if self.truncated:
            queue.put_nowait({
                "type": "truncated",
                "total_bytes": self.output_total,
                "limit": self._settings.max_output_bytes,
            })
        if not self.alive:
            queue.put_nowait({
                "type": "exited",
                "state": self.state,
                "exit_code": self.exit_code,
            })
            queue.put_nowait(None)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    # -- 结束与审计 -----------------------------------------------

    def _finish_exec(self, token: str, exit_code: int, terminated: bool = False) -> None:
        record = self.pending_execs.pop(token, None)
        if not record:
            return
        ended = time.time()
        self.completed_execs += 1
        self.last_command = {
            "token": token,
            "exit_code": exit_code,
            "started": record["started"],
            "ended": ended,
            "confirmed": record["confirmed"],
            "terminated": terminated,
        }
        result = "ok" if (exit_code == 0 and not terminated) else "fail"
        try:
            audit.record(
                "terminal", "exec", result, ip=record["owner"],
                reason="exit_code", detail=record["command"],
                extra={
                    "session_id": self.id,
                    "source": record["source"],
                    "started": record["started"],
                    "ended": ended,
                    "exit_code": exit_code,
                    "confirmed": record["confirmed"],
                    "terminated": terminated,
                })
        except Exception:
            pass

    def _flush_pending(self, reason: str) -> None:
        code = self.exit_code if self.exit_code is not None else -1
        terminated = reason in ("terminated", "timeout", "shutdown", "disabled")
        for token in list(self.pending_execs):
            self._finish_exec(token, code, terminated=terminated)

    def _finish(self, reason: str | None = None) -> None:
        if self.state != "running":
            return
        self.state = reason or self._ending_reason or "exited"
        self.ended = time.time()
        try:
            code = self._handle.poll()
            if code is None:
                try:
                    code = self._handle.wait(0.2)
                except Exception:
                    code = -1
            self.exit_code = int(code)
        except Exception:
            self.exit_code = -1
        self._handle.close()
        self._flush_pending(self.state)
        self._emit({
            "type": "exited",
            "state": self.state,
            "exit_code": self.exit_code,
        })
        for queue in list(self._listeners):
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        self._listeners.clear()

    async def terminate(self, reason: str = "terminated") -> bool:
        if self.state != "running":
            return False
        self.terminated = reason in ("terminated", "timeout", "shutdown", "disabled")
        self._ending_reason = reason
        try:
            self._handle.signal_term()
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._handle.wait, 1.0)
        except Exception:
            try:
                self._handle.signal_kill()
            except Exception:
                pass
            try:
                await asyncio.to_thread(self._handle.wait, 0.5)
            except Exception:
                pass
        self._finish(reason)
        return True


# ---------------------------------------------------------------- 管理器


class RemoteTerminalManager:
    def __init__(self, settings: TerminalSettings | None = None) -> None:
        self.settings = settings or TerminalSettings()
        self._sessions: dict[str, RemoteSession] = {}
        self._reaper: asyncio.Task | None = None

    def configure(self, cfg: dict[str, Any] | None) -> None:
        self.settings = settings_from(cfg)

    def get(self, sid: str) -> RemoteSession | None:
        return self._sessions.get(sid)

    def sessions(self) -> list[RemoteSession]:
        return list(self._sessions.values())

    async def create(self, owner: str = "", source: str = "", cwd: str | None = None,
                     cols: int = 120, rows: int = 36) -> RemoteSession:
        settings = self.settings
        if not settings.enabled:
            raise TerminalError(
                "remote_shell_disabled",
                "远程终端未开启：[terminal] remote_shell = false，可在设置页打开",
                403)
        await self.reap()
        self._cleanup_ended()
        running = [s for s in self._sessions.values() if s.alive]
        if len(running) >= settings.max_sessions:
            raise TerminalError(
                "too_many_sessions",
                f"并发终端会话已达上限（{settings.max_sessions} 个），请先终止旧会话",
                429)
        raw_cwd = (cwd or settings.cwd or "").strip()
        if raw_cwd:
            target = Path(raw_cwd).expanduser()
        else:
            target = Path.home()
        try:
            target = target.resolve()
        except Exception:
            pass
        if not target.exists() or not target.is_dir():
            raise TerminalError(
                "cwd_missing", f"工作目录不存在或不是目录：{target}", 400)
        cols = max(20, min(500, int(cols or 120)))
        rows = max(5, min(300, int(rows or 36)))
        loop = asyncio.get_running_loop()
        try:
            # 同线程拉起 PTY：fork 后子进程只做 exec，避免在工作线程里 fork
            handle, argv = _spawn(settings, str(target), rows, cols)
        except Exception as exc:
            raise TerminalError("spawn_failed", f"启动 shell 失败：{exc}", 500)
        sid = uuid.uuid4().hex[:12]
        session = RemoteSession(
            sid, handle, settings, owner, source, str(target), cols, rows,
            shell=" ".join(argv))
        session._reader_task = loop.create_task(session._read_loop())
        self._sessions[sid] = session
        try:
            audit.record(
                "terminal", "open", "ok", ip=owner, reason="session_open",
                detail="", extra={
                    "session_id": sid,
                    "source": source,
                    "shell": " ".join(argv),
                    "backend": session.backend,
                })
        except Exception:
            pass
        self._ensure_reaper()
        return session

    async def terminate(self, sid: str, reason: str = "terminated") -> RemoteSession | None:
        session = self._sessions.get(sid)
        if session is None:
            return None
        await session.terminate(reason)
        return session

    async def close_all(self, reason: str = "shutdown") -> int:
        closed = 0
        for session in list(self._sessions.values()):
            if session.alive:
                await session.terminate(reason)
                closed += 1
        return closed

    def shutdown_sync(self, reason: str = "shutdown") -> None:
        """进程退出 / 测试收尾用：不等事件循环，直接强杀。"""
        for session in list(self._sessions.values()):
            if session.alive:
                session._ending_reason = reason
                session.terminated = True
                try:
                    session._handle.signal_kill()
                except Exception:
                    pass

    async def reap(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        expired = [
            session for session in self._sessions.values()
            if session.alive and now - session.last_activity >= self.settings.idle_timeout_s
        ]
        for session in expired:
            await session.terminate("timeout")
        return [session.id for session in expired]

    def _cleanup_ended(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        ended = [
            (session.ended or 0.0, sid) for sid, session in self._sessions.items()
            if not session.alive
        ]
        for _, sid in ended:
            session = self._sessions.get(sid)
            if session and session.ended and now - session.ended > ENDED_RETENTION_S:
                self._sessions.pop(sid, None)
        if len(self._sessions) > MAX_SESSION_RECORDS:
            ordered = sorted(
                ((session.ended or session.created), sid)
                for sid, session in self._sessions.items())
            for _, sid in ordered[:len(self._sessions) - MAX_SESSION_RECORDS]:
                self._sessions.pop(sid, None)

    def _ensure_reaper(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reaper is not None and not self._reaper.done():
            if self._reaper.get_loop() is loop:
                return
        self._reaper = loop.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while True:
            if not any(session.alive for session in self._sessions.values()):
                self._cleanup_ended()
                return
            interval = min(5.0, max(0.5, self.settings.idle_timeout_s / 4.0))
            await asyncio.sleep(interval)
            try:
                await self.reap()
            except Exception:
                pass


manager = RemoteTerminalManager()
