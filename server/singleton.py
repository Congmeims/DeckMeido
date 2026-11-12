"""后端单实例保护

一份后端独占一个端口。历史上出现过两份后端抢同一个 18765 的情况：
安装脚本装了一个开机自启的 systemd 服务，客户端启动时又自己拉一份，
谁先绑定端口谁对外服务，而两者各自的内存历史互相独立——
重启时被另一个人抢到，用户看到的就是「聊天记录凭空消失」（磁盘上其实还在）。

这里在监听端口之前做一次「端口是否已被占」的检查，第二个后端直接退出，
让「谁在服务」永远只有一个答案。
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from . import devlog

try:                        # Windows 上没有 fcntl，开发机跑测试时退化为旧行为
    import fcntl
except ImportError:         # pragma: no cover
    fcntl = None

# 允许第二个实例接管：自检脚本/重启流程里偶尔需要临时探测一下
ENV_BYPASS = "DECKMEIDO_ALLOW_SECOND_BACKEND"

# 锁文件句柄：进程活着就一直持有，进程退出时内核自动释放
_held_lock = None


def _lock_path() -> Path:
    env = (os.environ.get("DECKMEIDO_DATA_DIR") or "").strip()
    root = Path(env).expanduser() if env else Path(__file__).resolve().parent.parent / "userdata"
    return root / "backend.lock"


def _acquire_lock(port: int, wait: float = 3.0) -> bool:
    """抢启动锁：插件和客户端可能在同一秒各自拉起一份后端。

    没有锁时两边都会先探测到「端口空着」，随后一方 bind 成功、另一方
    Errno 98（005 那版日志里出现过）。这里用 flock 让启动串行化：
    拿到锁的一方继续走探测；拿不到的一方等最多 wait 秒再看端口，
    已经被占就直接退出，绝不并发抢端口。
    """
    global _held_lock
    if fcntl is None:
        return False
    try:
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        deadline = time.time() + wait
        while time.time() < deadline and not already_serving(port):
            time.sleep(0.15)
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    _held_lock = fd
    return True

# 本进程的启动标识，随 /ws 的 history 一起发给前端。
# 前端用它区分「后端进程重启了」（缓存补回有意义）和「只是链路闪断重连」
# （这一轮还在后端跑着，缓存补回只会把同一段内容再贴一遍）
BOOT_ID = f"{os.getpid():x}-{int(time.time() * 1000):x}"


def already_serving(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """端口上是否已经有服务在应答。

    只做 TCP 握手，不发 HTTP：绑定失败但进程还没 accept 完时也能判断出来。
    """
    for probe_host in (host, "0.0.0.0"):
        try:
            with socket.create_connection((probe_host, port), timeout=timeout):
                return True
        except OSError as exc:
            # 连接被拒绝 = 端口空着；地址不可用同样是空着
            if exc.errno in (errno.ECONNREFUSED, errno.EADDRNOTAVAIL):
                continue
            # 超时等其它情况：可能刚好有人在 accept，保守认为有服务
            return True
    return False


# ---------- 失败路径上的占用者取证 ----------
# 只在「端口已被占、本进程即将退出」这条分支调用，正常启动完全不执行，
# 所以允许跑一两次外部命令；总预算 1 秒，超时直接放弃。
# 输出走 stderr：客户端/插件拉起后端时 stderr 会落进 userdata/logs/backend.log，
# 这样即使没人看安装输出，也能事后定位是谁占着端口。信息只留本机，不外发。
_PID_RE = re.compile(r"pid=(\d+)")
_HOLDER_BUDGET = 1.0


def _run(cmd: list[str], timeout: float) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    return done.stdout or ""


def _pids_from_ss(port: int, timeout: float) -> list[int]:
    exe = shutil.which("ss")
    if not exe:
        return []
    pids: list[int] = []
    for line in _run([exe, "-ltnp"], timeout).splitlines():
        if f":{port}" not in line:
            continue
        pids.extend(int(m) for m in _PID_RE.findall(line))
    return pids


def _pids_from_lsof(port: int, timeout: float) -> list[int]:
    exe = shutil.which("lsof")
    if not exe:
        return []
    pids: list[int] = []
    for line in _run([exe, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout).splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _proc_link(pid: int, name: str) -> str:
    """读 /proc/<pid>/cwd 或 exe；目录已被删时 readlink 会带 ' (deleted)'。"""
    try:
        return os.readlink(f"/proc/{pid}/{name}")
    except OSError:
        return ""


def _holder_lines(port: int, budget: float = _HOLDER_BUDGET) -> list[str]:
    """尽力而为地取占用者信息；任何失败都降级成一句提示，绝不抛异常。"""
    try:
        deadline = time.monotonic() + budget
        pids: list[int] = []
        for finder in (_pids_from_ss, _pids_from_lsof):
            remain = deadline - time.monotonic()
            if remain <= 0.1:
                break
            try:
                pids = finder(port, remain)
            except Exception:
                pids = []
            if pids:
                break
        if not pids:
            return [
                "            占用者信息读取失败（没有 ss/lsof 或权限不足），"
                f"手动排查：ss -ltnp | grep :{port}",
            ]
        lines: list[str] = []
        for pid in sorted(set(pids))[:3]:
            cmdline = _proc_cmdline(pid)
            if len(cmdline) > 300:
                cmdline = cmdline[:300] + "…"
            lines.append(f"            占用者 PID {pid}")
            lines.append(f"              命令行：{cmdline or '(读取不到，可能属于其他用户)'}")
            lines.append(f"              工作目录：{_proc_link(pid, 'cwd') or '(读取不到或权限不足)'}")
            lines.append(f"              可执行文件：{_proc_link(pid, 'exe') or '(读取不到或权限不足)'}")
        return lines
    except Exception:
        return [f"            占用者信息读取失败，手动排查：ss -ltnp | grep :{port}"]


def guard(port: int, allow_second: bool = False) -> None:
    """启动前调用：端口已被占就直接结束本进程。

    uvicorn 自己绑定失败也会退出，但会先做完一遍导入、读配置、
    甚至写会话文件（init_sessions 在导入期就跑了），有可能覆盖掉
    正在服务的那个实例的会话。所以必须在更早的位置拦住。
    """
    if allow_second:
        print("[DeckMeido] 单实例检查被显式跳过", file=sys.stderr)
        return
    # 先抢启动锁再探测端口：并发拉起的两份，后到的那份在这里就会等到
    # 前一份开始监听，从而走下面的「已在服务」分支，不再撞 Errno 98
    _acquire_lock(port)
    if not already_serving(port):
        return
    print(
        f"[DeckMeido] 端口 {port} 上已有一份后端在服务，本进程退出。\n"
        f"            要重启后端请先关掉正在运行的那一份（托盘菜单 → 退出）。",
        file=sys.stderr,
    )
    # 补一条占用者取证：安装器只看得到「v未知」，日志里没有 pid 就只能猜。
    # 拿不到也不影响下面的退出码，探测总耗时不超过 1 秒。
    try:
        for line in _holder_lines(port):
            print(line, file=sys.stderr)
    except Exception as exc:
        devlog.debug("singleton.guard", error=repr(exc))
        pass
    raise SystemExit(3)
