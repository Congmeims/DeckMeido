"""DeckMeido 原生客户端（PySide6 + QWebEngineView）

内嵌与网页端同一个界面（http://127.0.0.1:18765），两端功能界面永远一致：
以后每次更新只改 server/static/index.html 一处。
启动时自动检测后端核心，未运行则拉起。
URL 带 native=1 标记，网页会据此把「点开图片」改为调用系统默认工具。
窗口最小化后驻留系统托盘，点托盘图标可还原，右键菜单可退出。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from PySide6.QtCore import (QEvent, QLibraryInfo, QObject, QProcess, Qt, QThread,
                            QTimer, QTranslator, QUrl, Signal)
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

# 安装目录：默认取本文件的两级上层（client/main.py → 仓库根），
# 这样装到任意路径都能用；DECKMEIDO_DIR 可显式覆盖
DECKMEIDO_DIR = Path(
    os.environ.get("DECKMEIDO_DIR") or Path(__file__).resolve().parent.parent
)

# 客户端写开发者日志时复用后端同一条脱敏函数，避免各写一套正则
try:
    if str(DECKMEIDO_DIR) not in sys.path:
        sys.path.insert(0, str(DECKMEIDO_DIR))
    from server.devlog import redact as _dev_redact
except Exception:
    def _dev_redact(value) -> str:
        return str(value)


def _resolve_data_dir() -> Path:
    """数据根：DECKMEIDO_DATA_DIR 优先，其次 <安装目录>/userdata/（与后端一致）。"""
    env = (os.environ.get("DECKMEIDO_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return DECKMEIDO_DIR / "userdata"


CONFIG_PATH = _resolve_data_dir() / "config.toml"


def _server_cfg() -> dict:
    """读 [server] 段；读不到就返回空字典，由下面的默认值兜底。"""
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("server") or {}
    except Exception:
        return {}


def _resolve_port(server_cfg: dict) -> str:
    """端口优先级：DECKMEIDO_PORT > config.toml [server].port > 18765。

    起后端与健康检查（API 基址）都用这一个结果，不会再出现「后端换端口、
    客户端还去问旧端口」的半动态状态。
    """
    for candidate in (os.environ.get("DECKMEIDO_PORT"), server_cfg.get("port")):
        try:
            port = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            return str(port)
    return "18765"


_SERVER_CFG = _server_cfg()
PORT = _resolve_port(_SERVER_CFG)
API = f"http://127.0.0.1:{PORT}"

# Steam Deck 桌面 1280x800，窗口不超过它
WINDOW_W, WINDOW_H = 1240, 760


_DATA_DIR = _resolve_data_dir()
# 运行期文件统一放数据根：代码目录升级时会被整份覆盖，日志不能放在那里
BACKEND_LOG = _DATA_DIR / "logs" / "backend.log"
_BACKEND_LOG_MAX = 5 * 1024 * 1024   # 5 MB，只留一代 backend.log.1
_log_handle = None

# 插件大屏页「关闭核心」留下的标记：存在时 watchdog 不自动重拉，保证
# 「关闭后保持停止、只有用户点启动才起来」。插件点「启动」会删掉它；
# 手动打开客户端属于显式启动，不受这个标记限制。
CORE_STOP_MARKER = _DATA_DIR / "core.stopped"
# 网页端点了「重启后端」只会落这个标记（后端不杀自己），由客户端看门狗重放
RESTART_REQUEST = _DATA_DIR / "restart.request"

# 客户端崩溃日志：切游戏模式时 gamescope 会话重建会掐断 Wayland 连接，
# Qt 在那种情况下是 abort()，Python 层 try/except 拦不住，只有 faulthandler
# 能在进程死前把栈写下来。没有它就只能看到一句 "Wayland connection broke"。
CRASH_LOG = _DATA_DIR / "logs" / "client-crash.log"

# 开发者模式：客户端只在启动时记一条「自己是谁、怎么起来的」。
# 文件名带 client 前缀，避免和后端的 debug-<时间>.log 混在一起。
_CLIENT_DEBUG_LOG: Path | None = None


def _dev_log(event: str, **fields) -> None:
    if _CLIENT_DEBUG_LOG is None:
        return
    try:
        parts = [f"event={event}"]
        for key, value in fields.items():
            text = _dev_redact(value)
            if len(text) > 1000:
                text = text[:1000] + "…"
            parts.append(f"{key}={text}")
        with open(_CLIENT_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + " ".join(parts) + "\n")
    except Exception:
        pass


def _enable_crash_log() -> None:
    """把 C 层致命信号（SIGSEGV / SIGABRT 等）的栈写到文件里"""
    global _CLIENT_DEBUG_LOG
    try:
        import faulthandler

        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            CRASH_LOG.parent.chmod(0o700)
        except OSError:
            pass
        f = open(CRASH_LOG, "a", encoding="utf-8")
        try:
            os.chmod(CRASH_LOG, 0o600)
        except OSError:
            pass
        faulthandler.enable(file=f, all_threads=True)
        f.write(f"\n===== client start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        f.flush()
        globals()["_crash_handle"] = f  # 保持引用，别让 GC 把文件关掉
        try:
            cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if bool((cfg.get("runtime") or {}).get("dev_mode", False)):
                _CLIENT_DEBUG_LOG = CRASH_LOG.parent / (
                    f"debug-client-{time.strftime('%Y%m%d-%H%M%S')}.log")
        except Exception:
            pass
    except Exception:
        pass



_config_missing_warned = False
_config_error = ""


def check_config() -> None:
    """确认后端配置文件可读；读不到时记录明确原因，交给界面提示

    客户端与本机后端同机，走回环免鉴权，不再需要从配置里读令牌。
    """
    global _config_missing_warned, _config_error
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _config_error = f"未找到 {CONFIG_PATH}"
    except Exception as exc:
        _config_error = f"读取 {CONFIG_PATH} 失败：{exc}"
    else:
        _config_error = "" if isinstance(cfg.get("server"), dict) else f"{CONFIG_PATH} 里没有 [server] 段"
        if not _config_error:
            return
    if not _config_missing_warned:
        _config_missing_warned = True
        print(f"[DeckMeido] 读不到配置：{_config_error}（请检查数据目录）", file=sys.stderr)


def display_unavailable_reason() -> str:
    """返回显示环境不可用的原因；可用时返回空串。

    切模式时环境里可能还留着上一个会话的 DISPLAY：gamescope 的 Xwayland
    在切到桌面时会被拆掉，而 WAYLAND_DISPLAY 没有带过来，Qt 只能选 xcb，
    平台初始化失败后直接 abort()（SIGABRT → coredump → DrKonqi 弹窗）。
    在构造 QApplication 之前先把不可用的环境识别出来，写清楚原因后干净退出。
    """
    wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
    display = (os.environ.get("DISPLAY") or "").strip()
    runtime = (os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}").rstrip("/")
    if wayland:
        sock = f"{runtime}/{wayland}"
        if not os.path.exists(sock):
            return f"WAYLAND_DISPLAY={wayland} 指向的 socket 不存在：{sock}"
        return ""
    if not display:
        return "WAYLAND_DISPLAY 与 DISPLAY 都未设置，没有可用的图形会话"
    if display.startswith(":"):
        num = display[1:].split(".")[0]
        sock = f"/tmp/.X11-unix/X{num}"
        if not os.path.exists(sock):
            return f"DISPLAY={display} 不可达：{sock} 不存在（X 服务器未就绪）"
    xauth = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
    if os.path.exists(xauth):
        auth_note = ""
    elif os.environ.get("XAUTHORITY"):
        auth_note = f"XAUTHORITY 指向的文件不存在：{xauth}"
    else:
        auth_note = "XAUTHORITY 未设置且默认的 ~/.Xauthority 不存在"
    probe = None
    probe_name = ""
    for argv in (["xdpyinfo"], ["xset", "q"]):
        try:
            probe = subprocess.run(argv, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, timeout=3)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return f"DISPLAY={display} 探测超时（{' '.join(argv)} 3 秒无响应）"
        probe_name = " ".join(argv)
        break
    if probe is None:
        # 没有探测工具：授权文件也缺失时直接判定不可用，否则交给 Qt 尝试
        return f"DISPLAY={display} 无法探测（缺少 xdpyinfo / xset，且{auth_note}）" if auth_note else ""
    if probe.returncode == 0:
        return ""
    detail = (probe.stderr or b"").decode("utf-8", "replace").strip()
    detail = detail.splitlines()[0] if detail else ""
    notes = [note for note in (auth_note, detail) if note]
    reason = f"DISPLAY={display} 不可连（{probe_name} 失败）"
    return reason + ("：" + "；".join(notes) if notes else "")


def api_json(path: str, timeout: float = 5.0, post: bool = False) -> dict:
    """调后端接口，返回解析后的 JSON（空响应按空对象处理）"""
    url = f"{API}{path}"
    data = b"" if post else None
    with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


class _Worker(QThread):
    """把可能阻塞的操作挪到后台线程，别让托盘菜单把 Qt 主线程冻住"""

    result = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.result.emit(self._fn())
        except Exception as exc:
            self.result.emit(exc)


# 活跃的后台线程：退出前必须等它们结束。
# QThread 在 QApplication 析构时仍在运行，Qt 会直接 fatal abort（SIGABRT），
# Python 层 try/except 拦不住。实测崩溃路径就是「点重启核心 → 立刻点退出」：
# 重启线程还在等后端应答，进程已经在拆 QApplication 了。
_workers: list[_Worker] = []
_quitting = False
# 托盘「重启」：客户端与后端整体重来。launch.sh 见到这个退出码会立刻拉起新实例，
# 不再按「异常退出」退避；没有 launch.sh 时由本进程派生延迟启动器
CLIENT_RESTART_EXIT = 75
_restart_requested = False


def run_worker(fn, on_result, parent) -> None:
    worker = _Worker(fn, parent)
    _workers.append(worker)

    def _finished() -> None:
        try:
            _workers.remove(worker)
        except ValueError:
            pass

    worker.finished.connect(_finished)
    worker.result.connect(on_result)
    worker.finished.connect(worker.deleteLater)
    worker.start()


def _backend_log_fd():
    """后端 stdout/stderr 的去处：写文件，而不是 /dev/null

    以前一律 DEVNULL，后端一崩连一行 traceback 都留不下——12:52 那次
    崩溃到现在都查不出死因，就是因为证据被自己丢掉了。
    句柄在父进程里持有到退出为止，避免每次 Popen 漏一个 fd。
    """
    global _log_handle
    if _log_handle is not None:
        return _log_handle.fileno()
    try:
        BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            BACKEND_LOG.parent.chmod(0o700)
        except OSError:
            pass
        if BACKEND_LOG.exists() and BACKEND_LOG.stat().st_size > _BACKEND_LOG_MAX:
            os.replace(BACKEND_LOG, BACKEND_LOG.with_name("backend.log.1"))
        _log_handle = open(BACKEND_LOG, "ab", buffering=0)
        try:
            os.chmod(BACKEND_LOG, 0o600)
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _log_handle.write(f"\n===== backend start {stamp} =====\n".encode())
        return _log_handle.fileno()
    except Exception:
        _log_handle = None
        return subprocess.DEVNULL


def backend_alive(timeout: float = 10.0) -> bool:
    """后端是否在应答（问 /api/status，确认是「我们的」后端）

    超时从 2 秒放宽到 10 秒：后端忙着流式生成、跑 to_thread 里的命令时，
    2 秒内没回话是常态，旧值会把「忙」误判成「死」，于是又拉起一份。
    """
    url = f"{API}/api/status"
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def ensure_backend() -> bool:
    """后端未运行则拉起，返回是否可用

    这是整套服务唯一的起点：桌面快捷方式 → 客户端 → 后端 → 遮罩。
    后端自己也带单实例守卫（server/singleton.py），即便被别处重复拉起，
    第二个也会立刻退出，不会出现两份后端抢端口、各存一份历史的情况。
    """
    try:
        urllib.request.urlopen(f"{API}/", timeout=2)
        return True
    except Exception:
        pass
    print(f"[DeckMeido] 后端未运行，正在拉起，日志：{BACKEND_LOG}", file=sys.stderr)
    # 手动打开客户端属于用户显式启动：清掉插件留下的停止标记，
    # 之后 watchdog 才能继续保活
    try:
        CORE_STOP_MARKER.unlink(missing_ok=True)
    except Exception:
        pass
    kicker = _start_backend()
    # 记下句柄交给调用方，watchdog 才认得出「这是我刚拉起来的那个」
    global _STARTED_BACKEND
    _STARTED_BACKEND = kicker
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{API}/", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


_STARTED_BACKEND = None


def _start_backend():
    """真正执行 subprocess.Popen 的那一步，单独抽出来让 ensure_backend 能记句柄"""
    python = DECKMEIDO_DIR / ".venv/bin/python"
    if not python.exists():
        print(f"[DeckMeido] 找不到解释器 {python}，请重跑 install.sh", file=sys.stderr)
        return None
    try:
        # 每次拉起都重新读配置：设置页切换 lan_enable 后重启后端即可生效
        host = "0.0.0.0" if bool(_server_cfg().get("lan_enable", False)) else "127.0.0.1"
        argv = [str(python), "-m", "uvicorn", "server.main:app",
                "--host", host, "--port", PORT,
                # 和插件拉起时保持一致：默认 20s 心跳在 Deck 卡顿时会误判掉线
                "--ws-ping-interval", "60", "--ws-ping-timeout", "120"]
        _dev_log("backend.spawn", argv=argv, cwd=str(DECKMEIDO_DIR))
        return subprocess.Popen(
            argv,
            cwd=DECKMEIDO_DIR,
            stdout=_backend_log_fd(), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:
        # 以前直接 return None，用户只能看到「等了 15 秒还没起来」，
        # 真正的原因（权限/缺文件）全丢了
        print(f"[DeckMeido] 拉起后端失败：{exc}", file=sys.stderr)
        return None


def _listener_on_port(port: str) -> int | None:
    """查出正在监听该端口的进程 pid（查不到返回 None）

    退出时用：后端是自己 spawn 的、又放在新会话里，即便这里没记句柄，
    也能靠端口找到它，保证「关掉客户端就把服务一起收走」。
    """
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    # 例：users:(("python",pid=23248,fd=6))
    for chunk in out.split("pid="):
        digits = chunk.split(",")[0].strip()
        if digits.isdigit():
            return int(digits)
    return None


def checkpoint_backend() -> str:
    """退出前请后端把未落盘的内容刷下去（正在生成的半截回答、内存历史）

    返回状态字符串，仅用于日志：ok / skipped / failed:xxx。
    这一步失败不能挡住退出流程，所以异常一律吞掉。
    """
    url = f"{API}/api/checkpoint"
    try:
        with urllib.request.urlopen(url, data=b"", timeout=4) as r:
            body = r.read().decode("utf-8", "replace")
        return f"ok {body[:120]}"
    except Exception as e:
        return f"failed:{e}"


def stop_backend() -> None:
    """收掉本机后端（含它拉起的遮罩），别让服务在客户端退出后继续裸奔

    只收「自己拉起来的那个」：启动时后端已经在跑（用户手动起的、或上次
    客户端被 kill 留下的），就不归本次退出管——按端口一律 SIGTERM 会把
    别人正在用的后端连同那一轮回答一起干掉。
    """
    proc = _STARTED_BACKEND
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    # 先请它把正在生成的回答与内存历史落盘，再让它退出。
    # 以前直接 SIGTERM：那一轮回答还在流式生成中，进程一死就整段没了。
    print(f"[DeckMeido] 退出前 checkpoint：{checkpoint_backend()}", file=sys.stderr)
    try:
        os.kill(pid, 15)  # SIGTERM：uvicorn 会走 shutdown 钩子收掉遮罩
    except ProcessLookupError:
        return
    except Exception:
        return
    for _ in range(80):  # 最多等 8 秒（shutdown 钩子里还要落一次盘，别太早 SIGKILL）
        if proc.poll() is not None or _listener_on_port(PORT) is None:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except Exception:
        pass


def acquire_single_instance() -> QLocalServer | None:
    """单实例锁：全局只允许一个客户端在跑。

    已经有实例在运行时，把「唤起主窗口」的请求发给它，本次直接退出
    （返回 None）；本次是第一个实例则返回持有本地套接字的 QLocalServer
    （调用方必须保持引用，否则会被回收）。

    用 QLocalServer 而不是锁文件，好处是进程崩溃后系统自动释放，
    不会留下死锁。
    """
    # 名字带上安装目录指纹：不同安装位置可以各跑一个，互不打架
    name = "DeckMeido-" + hashlib.sha1(
        str(DECKMEIDO_DIR).encode("utf-8")
    ).hexdigest()[:12]

    def poke_existing() -> bool:
        """连上已有实例并让它显示窗口，成功返回 True"""
        sock = QLocalSocket()
        sock.connectToServer(name)
        if not sock.waitForConnected(300):
            return False
        sock.write(b"show")
        # 只 flush：disconnectFromServer 会等挂起数据写完，
        # 而对端可能收完就断开，此时再调 waitForBytesWritten 会被 Qt 拒并打印
        # "not allowed in UnconnectedState" 警告（try/except 拦不住）
        sock.flush()
        sock.disconnectFromServer()
        return True

    if poke_existing():
        return None

    # 连不上：要么真没有实例，要么上次崩溃留下了残留的套接字文件，清掉再监听
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        # 极小概率和另一个正在启动的实例撞车：它抢先了就让位给它
        if poke_existing():
            return None
        # 实在监听不了（权限/路径异常）：宁可放弃单实例保护，也让客户端能起来
        print(f"[DeckMeido] 单实例监听失败：{server.errorString()}", file=sys.stderr)
    return server


def install_translators(app: QApplication) -> None:
    """给 Qt 内置界面（右键菜单、对话框等）装上中文翻译

    QWebEngineView 的右键菜单（Reload / Save page / Back…）是 Qt/Chromium
    自带的，不走网页内容，所以改网页没用——必须在 QApplication 上装
    QTranslator 才能变成中文。翻译 .qm 随 PySide6 一起装着，只是没人加载。
    """
    # 允许用 DECKMEIDO_QT_LANG 覆盖，默认跟随系统；系统若为英文则退回 zh_CN
    lang = os.environ.get("DECKMEIDO_QT_LANG")
    if not lang:
        for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
            val = os.environ.get(var, "")
            if val.lower().startswith("zh"):
                lang = "zh_CN" if "cn" in val.lower() or "hans" in val.lower() else "zh_TW"
                break
    if not lang:
        lang = "zh_CN"

    base = os.environ.get("DECKMEIDO_QT_TRANSLATIONS") or QLibraryInfo.path(
        QLibraryInfo.LibraryPath.TranslationsPath
    )
    # Qt 本体 + WebEngine（右键菜单在 WebEngine 里）+ 基础控件，三个都要装
    for stem in ("qtbase", "qtwebengine", "qt", "qtmultimedia", "qtwebsockets"):
        path = os.path.join(base, f"{stem}_{lang}.qm")
        if not os.path.exists(path):
            continue
        tr = QTranslator(app)
        if tr.load(path):
            app.installTranslator(tr)


def apply_app_font(app: QApplication) -> None:
    """和插件端对齐字体：插件不设 font-family，继承的是 Steam 游戏模式界面字体

    SteamOS 上是 Motiva Sans（Steam 客户端 UI 字体），中文由 Noto Sans CJK 兜底。
    客户端跑在桌面/SteamOS 上，按同样的顺序取：先 Motiva Sans，没有就退到系统
    自带的 Noto Sans CJK / Segoe UI / 微软雅黑，最后交给平台的 sans-serif。
    这样 Deck 上两端观感一致，Windows 开发机上也不会因为缺字变成方框。
    """
    font = QFont()
    font.setFamilies([
        "Motiva Sans",          # Steam 游戏模式界面字体（插件端继承的就是它）
        "Noto Sans CJK SC",     # SteamOS 自带中文字体
        "Noto Sans",
        "Segoe UI",             # Windows 开发机
        "Microsoft YaHei",
        "sans-serif",
    ])
    app.setFont(font)


def _r1_log(msg: str) -> None:
    """R1 按住说话的运行日志：stderr 一行（现场可直接看）+ 开发者日志"""
    print(f"[DeckMeido] R1 按住说话：{msg}", file=sys.stderr)
    _dev_log("r1", msg=msg)


class R1Trigger(QObject):
    """把右肩键（R1）转成网页里的按住说话。

    Qt6 没有 QGamepad，客户端走 SDL2（ctypes）读手柄：
    - 游戏模式下 Steam Input 会独占物理手柄，但它通过 uinput 暴露一只
      虚拟 X360 手柄；SDL2 的 evdev 后端读到的是这只虚拟手柄，所以
      gamescope 下仍能拿到 R1，不依赖 X11/Qt 的手柄支持。
    - 只读不抓取（不做 EVIOCGRAB），也不改键位，不抢游戏/系统的输入。
    - 拿不到 libSDL2、没有手柄或读取失败时记一行日志并降级为不启用，
      不影响客户端其它功能。
    """

    pressed = Signal()
    released = Signal()

    _SUBSYSTEMS = 0x00000200 | 0x00002000 | 0x00004000  # JOYSTICK | GAMECONTROLLER | EVENTS
    # SDL_CONTROLLER_BUTTON_RIGHTSHOULDER = 10。
    # 已在 Deck 上用 SDL_GameControllerGetStringForButton 逐值枚举确认：
    # 9=leftshoulder、10=rightshoulder，不要凭记忆改这个数字。
    _BUTTON_R1 = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sdl = None
        self._ctrl = None
        self._down = False
        self._miss = 0
        self._missing_logged = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)

    def start(self) -> bool:
        try:
            import ctypes
            import ctypes.util

            names = [ctypes.util.find_library("SDL2") or "",
                     "libSDL2-2.0.so.0", "SDL2.dll"]
            sdl = None
            for name in names:
                if not name:
                    continue
                try:
                    sdl = ctypes.CDLL(name)
                    break
                except OSError:
                    continue
            if sdl is None:
                _r1_log("未找到 SDL2 运行库，按住说话不可用")
                return False
            sdl.SDL_Init.argtypes = [ctypes.c_uint32]
            sdl.SDL_Init.restype = ctypes.c_int
            sdl.SDL_QuitSubSystem.argtypes = [ctypes.c_uint32]
            sdl.SDL_NumJoysticks.restype = ctypes.c_int
            sdl.SDL_IsGameController.argtypes = [ctypes.c_int]
            sdl.SDL_IsGameController.restype = ctypes.c_int
            sdl.SDL_GameControllerOpen.argtypes = [ctypes.c_int]
            sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
            sdl.SDL_GameControllerGetAttached.argtypes = [ctypes.c_void_p]
            sdl.SDL_GameControllerGetAttached.restype = ctypes.c_int
            sdl.SDL_GameControllerGetButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
            sdl.SDL_GameControllerGetButton.restype = ctypes.c_int
            sdl.SDL_GameControllerClose.argtypes = [ctypes.c_void_p]
            rc = sdl.SDL_Init(self._SUBSYSTEMS)
            if rc != 0:
                _r1_log(f"SDL_Init 失败（错误码 {rc}），按住说话不可用")
                return False
            self._sdl = sdl
            self._open()
            self._timer.start()
            return True
        except Exception as exc:
            self._sdl = None
            _r1_log(f"初始化失败，按住说话不可用：{exc!r}")
            return False

    def _open(self) -> bool:
        if self._ctrl or not self._sdl:
            return bool(self._ctrl)
        try:
            for i in range(max(0, self._sdl.SDL_NumJoysticks())):
                if not self._sdl.SDL_IsGameController(i):
                    continue
                ctrl = self._sdl.SDL_GameControllerOpen(i)
                if ctrl:
                    self._ctrl = ctrl
                    self._down = False
                    self._missing_logged = False
                    _r1_log(f"已连接手柄（设备序号 {i}）")
                    return True
        except Exception as exc:
            _r1_log(f"枚举手柄失败：{exc!r}")
        if not self._missing_logged:
            self._missing_logged = True
            _r1_log("没有可用的手柄，按住说话暂不可用（接上后会自动重试）")
        return False

    def _poll(self) -> None:
        sdl = self._sdl
        if not sdl:
            self._timer.stop()
            return
        try:
            sdl.SDL_PumpEvents()
            if self._ctrl and not sdl.SDL_GameControllerGetAttached(self._ctrl):
                sdl.SDL_GameControllerClose(self._ctrl)
                self._ctrl = None
                self._down = False
                _r1_log("手柄已断开，等待重新连接")
            if not self._ctrl:
                # 约 1 秒重试一次，手柄晚到或热插拔后能自己接上
                self._miss += 1
                if self._miss >= 25:
                    self._miss = 0
                    self._open()
                return
            value = int(sdl.SDL_GameControllerGetButton(self._ctrl, self._BUTTON_R1))
            if value not in (0, 1):
                _r1_log(f"读取 R1 返回异常值 {value}，停止轮询")
                self._timer.stop()
                return
            if not self._down and value == 1:
                self._down = True
                _r1_log("按下")
                self.pressed.emit()
            elif self._down and value == 0:
                self._down = False
                _r1_log("松开")
                self.released.emit()
        except Exception as exc:
            _r1_log(f"读取 R1 失败，停止轮询：{exc!r}")
            self._timer.stop()

    def stop(self) -> None:
        self._timer.stop()
        try:
            if self._sdl:
                if self._ctrl:
                    self._sdl.SDL_GameControllerClose(self._ctrl)
                self._sdl.SDL_QuitSubSystem(self._SUBSYSTEMS)
        except Exception:
            pass
        self._ctrl = None
        self._down = False


# 重启预算：等旧核心退出最多 8 秒（_stop_for_restart 的 80×0.1），
# 再给客户端退出/拉起留 1 秒，倒计时提示按 9 秒走。
RESTART_BUDGET_SEC = 9


class RestartToast(QWidget):
    """重启倒计时提示（无边框、置顶）

    Qt 托盘气泡不能显示剩余秒数、也不会按预算自动消失，所以自己画一块：
    - 成功：倒计时到点前就切换成「重启成功」，0.9 秒后关闭，随后退出进程；
    - 超时/失败：停掉倒计时并显示「仍在重启」或失败原因，不再假装成功；
      失败提示不自动消失，由用户点击卡片关闭，或下一次重启时重置。
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "RestartToast { background: rgba(16,18,26,0.96);"
            " border: 1px solid rgba(120,200,255,0.85); border-radius: 12px; }"
            "QLabel { color: #eaf4ff; font-size: 13px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        self._label = QLabel("", self)
        self._label.setWordWrap(True)
        # 让点击落在卡片本身：失败提示常驻后靠点击关闭，别被 QLabel 吃掉
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._label)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._mode = "idle"
        self._left = 0
        self._fail_text = ""

    def _place(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        self.move(geo.center().x() - self.width() // 2,
                  geo.top() + max(24, geo.height() // 5))

    def _refresh(self) -> None:
        if self._mode == "running":
            text = f"正在重启客户端与核心…\n剩余 {self._left} 秒，完成后自动关闭"
            self._label.setStyleSheet("")
        elif self._mode == "overdue":
            text = "仍在重启客户端与核心…\n已超过预计时间，正在等待结果，请稍候"
            self._label.setStyleSheet("")
        elif self._mode == "success":
            text = "重启成功，正在拉起新客户端…"
            self._label.setStyleSheet("")
        else:
            text = (self._fail_text or "重启失败") + "\n点击此卡片关闭"
            self._label.setStyleSheet("color: #ff8f86;")
        self._label.setText(text)
        self._place()

    def _tick(self) -> None:
        if self._mode != "running":
            return
        self._left -= 1
        if self._left <= 0:
            # 到达预算还没结果：停下来等人看，不自动消失伪装成功
            self._mode = "overdue"
            self._timer.stop()
        self._refresh()

    def start(self, budget: int = RESTART_BUDGET_SEC) -> None:
        self._mode = "running"
        self._left = max(1, int(budget))
        self._refresh()
        self.show()
        self.raise_()
        self._timer.start()

    def finish_success(self) -> None:
        self._mode = "success"
        self._timer.stop()
        self._refresh()
        self.show()
        QTimer.singleShot(900, self.close)

    def finish_failure(self, message: str) -> None:
        self._mode = "failure"
        self._timer.stop()
        self._fail_text = "重启失败：" + str(message)
        self._refresh()
        self.show()
        self.raise_()
        # 失败结果必须停住：只有成功才自动消失。这里不再定时关闭，
        # 由用户点击卡片手动关闭，或下一次重启（start）重置。

    def mousePressEvent(self, event) -> None:
        """失败/超时提示常驻，点击卡片即关闭；倒计时与成功态不响应点击"""
        if self._mode in ("failure", "overdue"):
            self.close()
            return
        super().mousePressEvent(event)


# 系统原生通知（freedesktop / Plasma），托盘「重启」优先走这条
NOTIFY_SERVICE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def _notify_log(msg: str) -> None:
    print(f"[DeckMeido] 重启通知：{msg}", file=sys.stderr)
    _dev_log("restart.notify", msg=msg)


class NativeRestartNotice(QObject):
    """托盘「重启」的系统原生通知：开始一条、结束时同 id 替换成结果。

    为什么不用 QtDBus：Notify / CloseNotification 的 id 是 uint32，PySide6 6.x
    没有 QVariant，Python int 会被编码成 int32，Plasma 直接回
    "No such method 'Notify' ... (signature '…i')"。gdbus 按接口内省签名编参，
    这里用子进程调它，Deck（glib2）自带，不引入新依赖。

    与应用内卡片的对应关系（两条路径不会同时出现）：
    - 开始：发「正在重启」一条，超时 12s；秒数只写托盘 tooltip，不刷通知；
    - 成功：同 id 替换成「已重启」，3s 自动消失（对应卡片「0.9 秒关闭」，
      随后照旧 1.1 秒退出进程）；
    - 失败：同 id 替换成失败原因，timeout=0 + urgency=critical（常驻），
      带 default 动作，点击通知或托盘菜单的「重启失败详情」看完整原因；
    - 超时：同 id 替换成「仍在重启」，常驻等人看；
    - 发不出去（没有通知服务 / D-Bus 不可用 / gdbus 不存在）：start() 返回
      False，由调用方回退应用内 RestartToast，并已记日志。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._id = 0
        self._detail = ""
        self._monitor = None
        self._buf = ""
        self._keep = False

    @staticmethod
    def _call(method: str, args: list, timeout: float = 4.0) -> str | None:
        cmd = ["gdbus", "call", "--session", "--dest", NOTIFY_SERVICE,
               "--object-path", NOTIFY_PATH, "--method", NOTIFY_SERVICE + "." + method,
               *[str(a) for a in args]]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            _notify_log(f"{method} 调用失败：{exc!r}")
            return None
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
            _notify_log(f"{method} 失败：{reason[:200]}")
            return None
        return proc.stdout.strip()

    def _notify(self, summary: str, body: str, expire_ms: int, urgency: int,
                with_detail: bool) -> int | None:
        actions = "['default','查看详情']" if with_detail else "[]"
        out = self._call("Notify", [
            "DeckMeido", self._id, "", summary, body, actions,
            "{'urgency': <byte %d>}" % urgency, expire_ms,
        ])
        if out is None:
            return None
        m = re.search(r"uint32\s+(\d+)", out)
        if not m:
            _notify_log(f"解析通知 id 失败：{out[:120]}")
            return None
        return int(m.group(1))

    def _start_monitor(self) -> None:
        """监听 ActionInvoked，点通知能看详情；只在原生通知期间跑"""
        if self._monitor is not None:
            return
        proc = QProcess(self)
        proc.setProgram("gdbus")
        proc.setArguments(["monitor", "--session", "--dest", NOTIFY_SERVICE,
                           "--object-path", NOTIFY_PATH])
        proc.readyReadStandardOutput.connect(self._read_monitor)
        proc.start()
        self._monitor = proc

    def _read_monitor(self) -> None:
        if self._monitor is None:
            return
        self._buf += self._monitor.readAllStandardOutput().data().decode("utf-8", "replace")
        lines = self._buf.split("\n")
        self._buf = lines.pop()
        for line in lines:
            m = re.search(r"ActionInvoked \(uint32 (\d+), '([^']*)'\)", line)
            if not m or not self._id:
                continue
            if int(m.group(1)) == self._id and m.group(2) == "default":
                self.clicked.emit()

    def start(self) -> bool:
        nid = self._notify("正在重启 DeckMeido",
                           "客户端与核心正在重启，请稍候…", 12000, 1, False)
        if nid is None:
            _notify_log("原生通知发不出去，回退应用内提示卡")
            return False
        self._id = nid
        self._keep = False
        self._detail = ""
        self._start_monitor()
        _notify_log(f"开始通知已发出 id={nid}")
        return True

    def success(self) -> bool:
        if not self._id:
            return False
        nid = self._notify("DeckMeido 已重启",
                           "客户端与核心已重启，正在拉起新客户端…", 3000, 1, False)
        if nid is None:
            _notify_log("成功结果没发出去，改回应用内提示")
            return False
        self._id = nid
        self._keep = True   # 进程马上退出，别把这条成功通知一起关掉
        _notify_log(f"成功结果已替换 id={nid}")
        return True

    def fail(self, detail: str) -> bool:
        if not self._id:
            return False
        self._detail = str(detail)
        body = self._detail if len(self._detail) <= 200 else self._detail[:200] + "…"
        nid = self._notify("重启失败", body + "\n点击通知查看详情", 0, 2, True)
        if nid is None:
            _notify_log("失败结果没发出去，改回应用内提示")
            return False
        self._id = nid
        _notify_log(f"失败结果已替换 id={nid}（常驻，可点开详情）")
        return True

    def overdue(self) -> None:
        if not self._id:
            return
        nid = self._notify("仍在重启 DeckMeido", "已超过预计时间，正在等待结果…", 0, 1, False)
        if nid is not None:
            self._id = nid
            _notify_log(f"超时提示已替换 id={nid}")
        else:
            _notify_log("超时提示没发出去，继续等结果")

    def detail(self) -> str:
        return self._detail

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.kill()
            self._monitor.waitForFinished(1000)
            self._monitor = None
        if self._id and not self._keep:
            self._call("CloseNotification", [self._id])
        self._id = 0


def main() -> int:
    _enable_crash_log()
    _dev_log("client.start", argv=sys.argv, cwd=os.getcwd(), pid=os.getpid(),
             env={k: os.environ[k] for k in (
                 "DECKMEIDO_DATA_DIR", "DECKMEIDO_PORT", "DISPLAY",
                 "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE")
                 if os.environ.get(k)})
    reason = display_unavailable_reason()
    if reason:
        line = f"[display] 显示环境不可用：{reason}；不启动 Qt，避免 coredump 与崩溃弹窗"
        print(f"[DeckMeido] {line}", file=sys.stderr)
        _dev_log("client.display_unavailable", reason=reason)
        try:
            handle = globals().get("_crash_handle")
            if handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + line + "\n")
                handle.flush()
        except Exception:
            pass
        return 1
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("DeckMeido")
    apply_app_font(app)
    install_translators(app)
    # 关掉窗口时先最小化到托盘，由托盘菜单决定是否真退出
    app.setQuitOnLastWindowClosed(False)

    # 被 kill / 注销 / systemd 停止时也要走正常退出流程，
    # 否则 aboutToQuit 不触发，本机的后端和遮罩就成了没人管的孤儿。
    # 注意：Qt 事件循环里不能直接干重活，转成 queued 调用再退。
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            # 信号处理函数在字节码间隙执行，直接调 Qt 有风险，
            # 转成一次 queued 调用交给事件循环
            signal.signal(sig, lambda *_: QTimer.singleShot(0, app.quit))
        except Exception:
            pass

    # ---- 单实例：先查重再加载 WebEngine，重复点击瞬间返回，不浪费内存 ----
    single = acquire_single_instance()
    if single is None:
        return 0

    try:
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        QMessageBox.critical(
            None, "DeckMeido",
            "缺少 PySide6-WebEngine 组件，请在终端运行：\n"
            f"{DECKMEIDO_DIR}/.venv/bin/pip install PySide6-WebEngine",
        )
        return 1

    # Qt 6.8 起权限对象改由 QWebEnginePermission 承载；更早的版本没有这个类
    try:
        from PySide6.QtWebEngineCore import QWebEnginePermission
    except ImportError:
        QWebEnginePermission = None

    def _enum_member(owner, attr: str):
        """按 Qt 版本查枚举：新版权限在 QWebEnginePermission.PermissionType，
        旧版仍在 QWebEnginePage.Feature / PermissionPolicy 上"""
        holders = (
            owner,
            getattr(owner, "PermissionType", None),
            getattr(owner, "Feature", None),
            getattr(owner, "PermissionPolicy", None),
        )
        for holder in holders:
            if holder is None:
                continue
            value = getattr(holder, attr, None)
            if value is not None:
                return value
        return None

    def _permission_type(name: str):
        value = _enum_member(QWebEnginePermission, name) if QWebEnginePermission else None
        return value if value is not None else _enum_member(QWebEnginePage, name)

    def _permission_policy(allow: bool):
        return _enum_member(
            QWebEnginePage,
            "PermissionGrantedByUser" if allow else "PermissionDeniedByUser",
        )

    class WebPage(QWebEnginePage):
        """自动授予麦克风权限，让语音输入在客户端里可用

        权限回调的接口随 Qt 版本变过一次：Qt 6.8 起是 permissionRequested
        信号，参数 QWebEnginePermission（用 permissionType() 判断、
        grant()/deny() 答复）；6.8 以前只有 featurePermissionRequested 信号，
        要用 setFeaturePermission() 答复。

        旧实现把 permissionRequested 写成了同名方法——在 6.8 上这只是遮蔽了
        信号（Qt 内部照样拒绝权限）、并且调用的是 QWebEnginePermission 上
        不存在的 feature()；在更老的 Qt 上则完全没有接 featurePermissionRequested。
        两种版本都没能把麦克风授权出去，桌面模式里的 getUserMedia 因此一直被拒。
        这里按版本分别接信号，只做授权，不碰页面加载与 R2 的任何逻辑。
        """

        def __init__(self, parent=None):
            super().__init__(parent)
            self._allowed = tuple(
                t for t in (
                    _permission_type("MediaAudioCapture"),
                    _permission_type("MediaAudioVideoCapture"),
                    _permission_type("ClipboardReadWrite"),
                ) if t is not None
            )
            # Qt >= 6.8：permissionRequested(QWebEnginePermission)
            signal = getattr(self, "permissionRequested", None)
            if signal is not None:
                try:
                    signal.connect(self._answer)
                except Exception as exc:
                    _dev_log("web.permission.connect", api="permissionRequested", error=exc)
            else:
                # Qt < 6.8：featurePermissionRequested(QUrl, QWebEnginePage.Feature)
                # 6.8 起旧信号仍可能被发出，但两套枚举不通用；新版在就用新版，
                # 避免旧分支把已经 grant 的请求再按旧枚举判成 deny
                signal = getattr(self, "featurePermissionRequested", None)
                if signal is not None:
                    try:
                        signal.connect(self._answer_feature)
                    except Exception as exc:
                        _dev_log("web.permission.connect", api="featurePermissionRequested", error=exc)

        def _answer(self, permission) -> None:
            """Qt 6.8+：用 QWebEnginePermission 的 grant()/deny() 直接答复"""
            try:
                ptype = permission.permissionType()
            except Exception as exc:
                _dev_log("web.permission.error", api="permissionRequested", error=exc)
                return
            allow = ptype in self._allowed
            try:
                if allow:
                    permission.grant()
                else:
                    permission.deny()
                _dev_log("web.permission", qt="6.8+", type=ptype, granted=allow)
            except Exception as exc:
                _dev_log("web.permission.error", api="permissionRequested", type=ptype, error=exc)

        def _answer_feature(self, origin, feature) -> None:
            """Qt < 6.8：setFeaturePermission(url, feature, policy) 答复"""
            allow = feature in self._allowed
            policy = _permission_policy(allow)
            if policy is None:
                _dev_log("web.permission.error", api="featurePermissionRequested",
                         feature=feature, error="缺少 PermissionPolicy 枚举")
                return
            try:
                self.setFeaturePermission(origin, feature, policy)
                _dev_log("web.permission", qt="<6.8", feature=feature, granted=allow)
            except Exception as exc:
                _dev_log("web.permission.error", api="featurePermissionRequested",
                         feature=feature, error=exc)

    if not ensure_backend():
        QMessageBox.critical(None, "DeckMeido", f"后端核心启动失败，请检查 {DECKMEIDO_DIR} 是否完整（可重跑 install.sh）。")
        return 1

    view = QWebEngineView()
    view.setPage(WebPage(view))
    view.setWindowTitle("DeckMeido")
    # 屏蔽网页区右键菜单（Reload/Save page 等 Qt 自带项，在本客户端无用）
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    view.resize(WINDOW_W, WINDOW_H)
    icon_path = DECKMEIDO_DIR / "assets" / "logo.png"
    icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
    view.setWindowIcon(icon)
    check_config()
    if _config_error:
        QMessageBox.warning(
            None,
            "DeckMeido",
            "读不到配置，请检查数据目录：\n"
            f"{CONFIG_PATH}\n\n原因：{_config_error}",
        )
    view.load(QUrl(f"{API}/?native=1"))
    view.show()
    # 缩放只作用于这个网页，不影响系统里其他 Qt 程序；网页端（浏览器打开）保持原样。
    # 取 1.1 而非 1.2：右终端栏的隐藏断点是 max-width:1080px，1.2 时 CSS 视口
    # ≈1240/1.2=1033px 会掉进断点把右栏收成抽屉；1.1 时 CSS 视口≈1127px，
    # 稳过断点，三栏布局（1.5:6.25:2.25）在客户端里才和网页端一致。
    view.setZoomFactor(1.1)

    # ---- R1 按住说话：SDL2 读右肩键，网页里的开关决定是否启用 ----
    # 只在客户端窗口激活时把事件转给网页；网页再判断语音是否可用，
    # 任何一步不满足都不拦截 R1（SDL 只读，不做抓取或改键）。
    # 事件名沿用网页里的 dmR2（start/stop），协议不变。
    r1 = R1Trigger(view)

    def _r1_send(kind: str) -> None:
        if not view.isActiveWindow():
            return
        view.page().runJavaScript(
            "window.dmR2 && window.dmR2('%s')" % kind,
            lambda result: _r1_log(f"网页 {kind} -> {result!r}"),
        )

    r1.pressed.connect(lambda: _r1_send("start"))
    r1.released.connect(lambda: _r1_send("stop"))
    r1.start()
    app.aboutToQuit.connect(r1.stop)

    # ---- 系统托盘：最小化后仍可在托盘找回 ----
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("DeckMeido")

    def restore() -> None:
        if view.isFullScreen():
            # 全屏时 showNormal() 会顺带把窗口还原成小窗，相当于「恢复」把用户的
            # 全屏给取消了；全屏状态下只提窗，不动状态
            view.showFullScreen()
        else:
            view.showNormal()
        view.raise_()
        view.activateWindow()

    tray.activated.connect(
        lambda reason: restore() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )
    tray.show()

    # ---- 重复启动时唤起已有窗口 ----
    # 用户再点一次图标 → 新进程连上这里的本地套接字就退出，
    # 这个已有实例收到连接后把窗口提到前台，而不是又开一个托盘图标。
    def on_second_instance() -> None:
        while single.hasPendingConnections():
            conn = single.nextPendingConnection()
            if conn is None:
                break
            conn.disconnected.connect(conn.deleteLater)
            restore()
            conn.disconnectFromServer()

    single.newConnection.connect(on_second_instance)

    backend = {"proc": _STARTED_BACKEND}

    def kick_backend() -> None:
        global _STARTED_BACKEND
        backend["proc"] = _start_backend()
        # watchdog 重拉的后端同样是我们 spawn 的，退出时要一并收走
        _STARTED_BACKEND = backend["proc"]

    restart_state = {"running": False, "native": False}

    def _terminate_work():
        """终止当前这一轮：复用后端 /api/cancel（取消对话任务 + 砍登记的子进程树）"""
        try:
            resp = api_json("/api/cancel?client=client&device=native", timeout=8.0, post=True)
        except Exception as exc:
            return ("error", f"终止请求失败：{exc}")
        if not resp.get("ok"):
            return ("idle", "当前没有正在执行的操作。")
        # cancel() 只负责发取消信号，阻塞在子进程管道上的轮次未必立刻收尾；
        # 给 6 秒观察窗口，仍未结束就如实提示用户去点「重启」
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not _quitting:
            try:
                if not api_json("/api/status", timeout=2.0).get("running"):
                    return ("ok", "已终止当前操作。")
            except Exception:
                # 后端正在收尾时 /api/status 可能暂时不回话，继续等
                pass
            time.sleep(0.4)
        return ("busy", "已发出终止请求，但本轮仍在执行；可点「重启」重启核心。")

    def report_terminate(result) -> None:
        if isinstance(result, Exception):
            tray.showMessage("DeckMeido", f"终止失败：{result}", icon, 4000)
            return
        kind, message = result
        # 成功时界面本身会恢复正常状态，不再额外弹托盘提示
        if kind != "ok":
            tray.showMessage("DeckMeido", message, icon, 5000)

    def _stop_for_restart() -> bool:
        """重启专用：先停掉本机核心

        退出时的 stop_backend 只收自己 spawn 的，避免误杀别人的服务；
        这里是用户明确点「重启」，端口上的那份就是本机核心，一并收掉。
        """
        proc = _STARTED_BACKEND
        pid = proc.pid if proc is not None and proc.poll() is None else _listener_on_port(PORT)
        if pid is None:
            return True
        print(f"[DeckMeido] 重启前 checkpoint：{checkpoint_backend()}", file=sys.stderr)
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            return True
        except Exception as exc:
            print(f"[DeckMeido] 停止旧核心失败：{exc}", file=sys.stderr)
            return False
        for _ in range(80):
            if _quitting or _listener_on_port(PORT) is None:
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, 9)
        except Exception:
            pass
        return _listener_on_port(PORT) is None

    def _restart_work():
        restart_state["running"] = True
        try:
            if _quitting:
                return RuntimeError("客户端正在退出，已取消重启。")
            if not _stop_for_restart():
                return RuntimeError("旧核心没有退出，重启已中止。")
            if _quitting:
                return RuntimeError("客户端正在退出，已取消重启。")
            proc = _start_backend()
            if proc is None:
                return RuntimeError(f"拉起新核心失败，详见 {BACKEND_LOG}。")
            for _ in range(20):
                if backend_alive(timeout=1.0):
                    return proc
                if _quitting:
                    # 退出时不要再等新核心，否则线程会活过 QApplication 的析构
                    return RuntimeError("客户端正在退出，已取消等待新核心。")
                time.sleep(0.5)
            return RuntimeError(f"新核心 10 秒内没有应答，详见 {BACKEND_LOG}。")
        finally:
            restart_state["running"] = False

    restart_toast = RestartToast()
    app.aboutToQuit.connect(restart_toast.close)

    restart_notice = NativeRestartNotice()
    app.aboutToQuit.connect(restart_notice.stop)
    restart_tip = {"left": 0}

    def _restart_tip_tick() -> None:
        restart_tip["left"] -= 1
        if restart_tip["left"] <= 0:
            tip_timer.stop()
            tray.setToolTip("DeckMeido · 重启中（已超过预计时间）")
            restart_notice.overdue()
            return
        tray.setToolTip(f"DeckMeido · 重启中，剩余 {restart_tip['left']} 秒")

    tip_timer = QTimer(app)
    tip_timer.setInterval(1000)
    tip_timer.timeout.connect(_restart_tip_tick)

    def show_restart_detail() -> None:
        """点原生通知或托盘菜单都落到这里：把完整失败原因摊开"""
        text = restart_notice.detail() or "没有可显示的重启失败原因。"
        QMessageBox.information(None, "重启失败详情",
                                f"{text}\n\n后端日志：{BACKEND_LOG}")

    restart_notice.clicked.connect(show_restart_detail)

    def report_restart(result) -> None:
        global _STARTED_BACKEND
        if isinstance(result, Exception):
            tray.showMessage("DeckMeido", str(result), icon, 5000)
            return
        _STARTED_BACKEND = result
        backend["proc"] = result
        tray.showMessage("DeckMeido", "核心已重启。", icon, 2500)

    def terminate_ops() -> None:
        run_worker(_terminate_work, report_terminate, app)

    def restart_core() -> None:
        if restart_state["running"]:
            return
        run_worker(_restart_work, report_restart, app)

    def _schedule_relaunch() -> None:
        """安排「当前进程退出后」拉起新客户端

        由 launch.sh 起的实例直接退出码 75 交给外层循环；否则派生一个
        延迟启动器（新会话、脱离父进程），等单实例套接字随旧进程释放后再起。
        不在进程内 exec：一半环境半套线程最容易触发 Wayland/Qt 的 abort。
        """
        if os.environ.get("DECKMEIDO_CLIENT_WRAPPER") == "1":
            return
        main_py = str(Path(__file__).resolve())
        launch_code = (
            "import subprocess, sys, time\n"
            "time.sleep(1.2)\n"
            "subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL,\n"
            "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            "                 start_new_session=True, close_fds=True)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", launch_code, sys.executable, main_py, *sys.argv[1:]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        )

    def _restart_client_work():
        restart_state["running"] = True
        try:
            if _quitting:
                return RuntimeError("客户端正在退出，已取消重启。")
            # 端口上的核心也一并收掉，否则新客户端会直接连回旧核心
            if not _stop_for_restart():
                return RuntimeError("旧核心没有退出，重启已中止。")
            return "ok"
        finally:
            restart_state["running"] = False

    def _restart_tip_reset() -> None:
        tip_timer.stop()
        tray.setToolTip("DeckMeido")

    def _restart_failed(detail: str) -> None:
        act_restart.setEnabled(True)
        act_restart_detail.setVisible(True)
        tray.setToolTip(f"DeckMeido · 重启失败：{detail[:60]}")
        # 原生通知能替换就替换（常驻 + 可点详情）；替换失败才退回应用内卡片
        if restart_state["native"] and restart_notice.fail(detail):
            return
        restart_notice.stop()
        restart_toast.finish_failure(detail)

    def report_restart_client(result) -> None:
        global _restart_requested
        act_restart.setText("重启")
        _restart_tip_reset()
        # 用户在这几秒里又点了「退出」：以退出为准，不再拉起任何新实例
        if _quitting:
            restart_notice.stop()
            restart_toast.close()
            return
        if isinstance(result, Exception):
            _restart_failed(str(result))
            return
        try:
            _schedule_relaunch()
        except Exception as exc:
            _restart_failed(str(exc))
            return
        _restart_requested = True
        # 成功路径：原生通知同 id 换成「已重启」，3 秒自动消失后就该退出进程了
        # （对应旧卡片「0.9 秒自动关闭」；语义不变，只是载体换成了系统通知）
        if restart_state["native"] and restart_notice.success():
            pass
        else:
            restart_toast.finish_success()
        QTimer.singleShot(1100, app.quit)

    def restart_client() -> None:
        if restart_state["running"] or _quitting:
            return
        act_restart.setEnabled(False)
        act_restart.setText("重启中…")
        act_restart_detail.setVisible(False)
        # 倒计时从真正开始重启时起算：预算 = 等核心退出 8s + 退出/拉起 1s
        restart_state["native"] = restart_notice.start()
        if restart_state["native"]:
            # 秒数只在托盘 tooltip 里走，不每秒刷系统通知
            restart_tip["left"] = RESTART_BUDGET_SEC
            tray.setToolTip(f"DeckMeido · 重启中，剩余 {RESTART_BUDGET_SEC} 秒")
            tip_timer.start()
        else:
            restart_toast.start(RESTART_BUDGET_SEC)
        run_worker(_restart_client_work, report_restart_client, app)

    # 托盘菜单顺序固定：终止 → 显示 → 重启 → 退出
    menu = QMenu()
    act_terminate = QAction("终止", menu)
    act_terminate.triggered.connect(terminate_ops)
    act_show = QAction("显示", menu)
    act_show.triggered.connect(restore)
    act_restart = QAction("重启", menu)
    act_restart.setToolTip("重启客户端与核心")
    act_restart.triggered.connect(restart_client)
    # 只在重启失败后出现在托盘菜单里：点它等于点失败通知，看完整原因
    act_restart_detail = QAction("重启失败详情…", menu)
    act_restart_detail.setVisible(False)
    act_restart_detail.triggered.connect(show_restart_detail)
    act_quit = QAction("退出", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_terminate)
    menu.addSeparator()
    menu.addAction(act_show)
    menu.addAction(act_restart)
    menu.addAction(act_restart_detail)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)

    watch_state = {"last_kick": 0.0, "misses": 0}

    def watchdog() -> None:
        """后端挂了就自动重拉。

        以前这个职责在 systemd（Restart=on-failure）；现在服务只由客户端拉起，
        客户端就得自己看着——否则后端崩一次，界面就一直连不上。

        这里**不发 HTTP**：watchdog 跑在 Qt 主线程上，探活一旦超时就会把界面
        冻住（旧版 2 秒硬超时，后端忙着流式生成时既卡界面又容易误判成「已死」）。
        改成看进程句柄 + 端口监听，都是毫秒级返回。

        插件大屏页点过「关闭核心」时（CORE_STOP_MARKER 在），这里必须原地不动：
        自动重拉等于把用户的「关闭」作废。只有用户显式点「启动」才会清标记。
        """
        # 手动「重启」期间旧核心已停、新核心还没起来，这里不能插一脚再拉一份
        if restart_state["running"]:
            watch_state["misses"] = 0
            return
        # 网页端点「重启后端」：后端只落标记，这里消费掉并走「重启」流程
        if RESTART_REQUEST.exists():
            try:
                RESTART_REQUEST.unlink()
            except OSError:
                pass
            watch_state["misses"] = 0
            restart_core()
            return
        if CORE_STOP_MARKER.exists():
            watch_state["misses"] = 0
            return
        proc = backend["proc"]
        if proc is not None:
            if proc.poll() is None:
                watch_state["misses"] = 0
                return  # 句柄在、进程活
        elif _listener_on_port(PORT) is not None:
            watch_state["misses"] = 0
            return  # 没句柄（启动时连的是既有后端），但端口有人在听
        # 到这里只能说明「句柄没了/端口没人听」，还不能断定后端真死：
        # `ss` 偶尔会慢一拍或查不到，直接 kick 就会在回答生成到一半时
        # 杀掉后端（用户视角：回复又丢了）。要求连续两次探活失败才动手。
        watch_state["misses"] = watch_state.get("misses", 0) + 1
        if watch_state["misses"] < 2:
            return
        # 60 秒冷却，避免反复拉起失败时刷屏、刷通知
        now = time.monotonic()
        if now - watch_state["last_kick"] < 60:
            return
        watch_state["last_kick"] = now
        watch_state["misses"] = 0
        kick_backend()
        if backend["proc"] is None:
            # 拉起失败别再 restore 打扰用户：托盘提示一下，下一轮再试
            tray.showMessage("DeckMeido", f"后端重启失败，详见 {BACKEND_LOG}", icon, 3000)
            return
        # 不 restore()：用户可能正在别的窗口里干活，硬把本窗口提到前台很烦，
        # 托盘给个提示就够，需要时点托盘图标回来
        tray.showMessage("DeckMeido", "后端已自动重启。", icon, 2500)

    timer = QTimer()
    timer.setInterval(5000)
    timer.timeout.connect(watchdog)
    timer.start()

    def on_minimized(state) -> None:
        # 全屏 ↔ 普通切换也会走到这里，那不算「最小化」，别把窗口藏进托盘
        if state & Qt.WindowFullScreen:
            return
        # Steam Deck 桌面状态栏空间有限，最小化就直接收进托盘
        if state & Qt.WindowMinimized and not view.isMaximized():
            view.hide()
            tray.showMessage("DeckMeido", "已最小化到托盘，点击托盘图标可恢复。", icon, 2000)

    # 事件过滤器对象必须留引用：installEventFilter 不持有所有权（Qt 只存裸指针），
    # 这里如果写成 installEventFilter(_MinimizeWatcher(...))，Watcher 会被 GC，
    # 窗口状态一变就访问已释放对象
    watcher = _MinimizeWatcher(on_minimized)
    watcher.setParent(app)
    view.installEventFilter(watcher)

    def on_quit() -> None:
        global _quitting
        # 先让后台线程收尾，再拆 Qt 对象：活跃的 QThread 被析构会 abort。
        # 「重启」线程要等旧核心按 SIGTERM→SIGKILL 收摊（最多 8 秒），
        # 这里只给 3 秒会让它活过 QApplication 析构，所以重启期间放宽时限
        _quitting = True
        timeout = 9.0 if (_restart_requested or restart_state["running"]) else 3.0
        deadline = time.monotonic() + timeout
        for w in list(_workers):
            left = max(1, int((deadline - time.monotonic()) * 1000))
            w.wait(left)
        tray.hide()
        # 客户端是唯一入口，退出时把自己拉起的后端一并收走（后端会再收掉遮罩），
        # 否则下次启动就成了「旧后端还在跑、新客户端连过去」的混乱状态
        stop_backend()

    app.aboutToQuit.connect(on_quit)

    # 信号处理（转 queued 调用再 app.quit）已在 main() 开头统一注册，这里不要再注册一遍：
    # 在事件循环里直接调 app.quit() 有重入风险，且会覆盖掉前面那份正确的处理函数。

    code = app.exec()
    # 托盘「重启」：由 launch.sh 起的实例用约定退出码 75 交给外层循环，
    # 它会跳过崩溃退避、立即拉起新实例；直接启动时新实例已由
    # _schedule_relaunch 派生，这里正常返回 0，免得被别处当成崩溃重拉一份
    if _restart_requested and os.environ.get("DECKMEIDO_CLIENT_WRAPPER") == "1":
        return CLIENT_RESTART_EXIT
    return code


def _MinimizeWatcher(callback):
    """把「窗口最小化」事件转给回调（避免额外继承一份 QWebEngineView）"""

    class Watcher(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.WindowStateChange:
                callback(obj.windowState())
            return False

    return Watcher()


if __name__ == "__main__":
    sys.exit(main())
