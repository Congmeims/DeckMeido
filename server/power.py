"""桌面模式下阻止息屏 / 休眠

需求：处在**桌面模式**、并且 DeckMeido 正在执行命令或显示操作遮罩时，
屏幕不能黑、机器不能睡。游戏模式（gamescope 在跑）完全不管——那时候
屏幕上本来就不是我们，别去干扰 Steam 自己的电源策略。

实现上是两个「抑制」叠在一起，缺一个都会漏：

1. `systemd-inhibit --what=idle:sleep --mode=block` 起一个 `sleep infinity`
   子进程常驻，抑制随进程存活——进程一死（包括自己被 kill、崩溃）抑制
   自动释放，不会像 D-Bus 租约那样留下永远不放的 cookie。
   注意 SteamOS 上普通用户调这个要交互认证，必然失败（Access denied），
   所以这条只能算加分项，不能让它的失败拖累下面那条。
2. KDE 的 `org.freedesktop.ScreenSaver.Inhibit`。logind 的 idle 抑制只挡
   「按空闲挂起」，Plasma 的息屏/变暗是 PowerDevil 自己按 KIdleTime 算的，
   它认的是 ScreenSaver 的抑制 cookie（视频播放器就是这么防黑屏的）。
   只做第 1 条时屏幕照样会黑，必须补这一条。

两条都是同步外部命令，调用点却在事件循环里（工具执行前后、遮罩开关），
所以 hold/release 只登记原因，真正的挂/收由后台线程做，不阻塞对话。

失败退避：SteamOS 上每条 logind 抑制都是必然失败的（要走 polkit 交互认证），
以前每 2 秒重试一次、每次还先把已经拿到的 KDE cookie 解除重建，日志里
「已恢复息屏（重新申请）」刷了近千行，屏幕却一直是黑的。现在：
  - 只补没挂上的那一条，成功挂上的绝不因为另一条失败被拆掉；
  - KDE cookie 常驻一份，hold/release 往返只收放 logind 那条进程；
  - 整轮都失败时 2s→4s→…→60s 退避，退避期内不再请求 logind，
    免得每次都在日志里留一条必然失败的记录。
  - 每次请求 logind 前先扫一遍 `--who=DeckMeido` 的残留抑制进程，防止
    反复重启后 logind 抑制表被打满（Maximum number of concurrent
    inhibitors reached），那种情况下表现和「权限不足」一模一样。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from . import devlog

WHO = "DeckMeido"
# 持有抑制期间核对间隔：抑制进程被外部杀掉、模式从桌面切到游戏，最多这么久被发现。
# 没有抑制时线程不轮询，只在 hold/release 时被唤醒（见 AwakeKeeper._loop），
# 空闲状态下一个子进程都不会起。
POLL_INTERVAL = 2.0
# 挂抑制失败后的退避：2s → 4s → 8s …，上限 60s。
# 以前失败后每 2 秒重试一次，而每次重试都会先 _drop 再重建，
# 日志里就出现了「未能生效」和「已恢复息屏」严格交替、每 2 秒一轮的刷屏；
# 更要命的是那条 KDE cookie 每轮都被解除重建，等于一直没挂上，屏幕照样黑。
RETRY_MIN = 2.0
RETRY_MAX = 60.0
# 模式判定缓存：切模式后最多这么久才反映过来，避免频繁跑 pw-dump
MODE_TTL = 3.0
# 网页前台登记多久没续期就当作已离开（秒）。前端每 30s 续一次，
# 关页面靠 pagehide 立刻撤销，这里只是页面崩溃/断网时的兜底。
VIEW_TTL = 120.0


def _log(msg: str) -> None:
    print(f"[DeckMeido] {msg}", file=sys.stderr)


def _session_env() -> dict:
    """桌面会话环境：systemd 用户服务里默认没有 DISPLAY / D-Bus 地址"""
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    env.setdefault("DISPLAY", ":0")
    return env


def _kill_proc(proc: subprocess.Popen) -> None:
    """收掉一个 systemd-inhibit 进程（连同它的 sleep 子进程）"""
    try:
        # 杀进程组：systemd-inhibit 的 sleep 子进程要一起走，
        # 否则光杀父进程会留下一个还在占着抑制的 sleep
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=2)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception as exc:
            devlog.debug("power._kill_proc", error=repr(exc))


def _sweep_stale_inhibitors() -> None:
    """清理上一任进程留下的 systemd-inhibit 残留

    logind 的抑制表是有限的（默认 128 条）。服务被 kill -9、崩溃、或者带
    start_new_session 的抑制进程被孤儿化时，抑制不会被回收，反复重启后端就会
    把表占满，此后所有 logind 抑制都返回 "Maximum number of concurrent
    inhibitors reached"，看起来就像「权限不足」。启动前先扫一遍，把挂着
    WHO 名义、但父进程已经不是当前这个后端实例的清理掉。

    只认 `--who=DeckMeido` + `sleep infinity` 这个组合，绝不碰 Steam / 桌面的
    其它抑制；异常一律吞掉，清理失败不影响正常流程。
    """
    if not shutil.which("pgrep"):
        return
    me = os.getpid()
    try:
        r = subprocess.run(["pgrep", "-af", "systemd-inhibit"],
                           capture_output=True, text=True, timeout=5)
    except Exception as exc:
        devlog.debug("power.sweep_stale", error=repr(exc))
        return
    for line in r.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        # 只处理我们自己的那类调用，且不要误杀自己所在的进程
        if "--who=DeckMeido" not in cmd or "sleep infinity" not in cmd:
            continue
        if pid == me:
            continue
        try:
            ppid = int(Path(f"/proc/{pid}/stat").read_text().split()[3])
        except Exception:
            ppid = 0
        # 父进程还活着且不是 1（被 systemd 收养）：可能是并发实例，交给它自己管
        if ppid not in (0, 1, me):
            try:
                Path(f"/proc/{ppid}/stat").exists() and os.kill(ppid, 0)
                continue
            except Exception:
                pass
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as exc:
                devlog.debug("power.sweep_stale", pid=pid, error=repr(exc))
                continue
        devlog.debug("power.sweep_stale_killed", pid=pid)
        _log(f"清理残留的电源抑制进程：pid {pid}")


# 上一次探测到的模式（只有变化时才叫醒 keeper）
_last_desktop = True
# 正在「前台开着网页」的来源标识 -> 最后一次心跳时间（monotonic）。
# 前端可见时每 30s 续一次，切到后台/关页面就撤销。桌面客户端、手机、
# 局域网别的设备都会登记，只要有一个还在前台就算「用户正看着 DeckMeido」。
_views: dict[str, float] = {}
_views_lock = threading.Lock()


def note_view(source: str, active: bool) -> bool:
    """登记/撤销一个前台网页；返回登记后是否还有前台网页

    只有状态真的变了才叫醒 keeper：心跳每 30s 一次，每次叫醒都会让后台线程
    重新跑一遍模式探测，没必要。
    """
    now = time.monotonic()
    with _views_lock:
        before = bool(_views)
        if active:
            _views[source] = now
        else:
            _views.pop(source, None)
        after = bool(_views)
    if before != after:
        keeper.poke()
    return after


def any_view_active() -> bool:
    """有没有前台网页（心跳超时的算已离开）"""
    now = time.monotonic()
    with _views_lock:
        stale = [k for k, at in _views.items() if now - at > VIEW_TTL]
        for k in stale:
            _views.pop(k, None)
        return bool(_views)


def desktop_mode() -> bool:
    """当前是否桌面模式。

    判据和 get_status、depcheck 共用 tools.mode_is_gaming()：PipeWire 里有没有
    gamescope 的视频节点。以前这里是 `pgrep -x gamescope`，而 SteamOS 的 gamescope
    带着一长串参数启动、comm 被截断到 15 字节，pgrep 恒匹配不上 —— 于是人在游戏
    模式里也会被当成桌面模式，白白挂上息屏抑制。
    探测失败按桌面模式处理：宁可多抑制一次，也不要因为判断不出来而在用户盯着屏幕时黑屏。

    探测结果和上一次不同时 poke 一下 keeper：它空闲时阻塞等事件（游戏模式下
    不轮询），从游戏切回桌面不会有任何 hold 动作，不叫醒它就一直挂不上抑制。
    """
    global _last_desktop
    # 延迟导入：tools 在模块顶层 import 了 power，这里不能反过来在顶层 import 它
    from . import tools
    desktop = not tools.mode_is_gaming()
    if desktop != _last_desktop:
        _last_desktop = desktop
        keeper.poke()
    return desktop


class AwakeKeeper:
    """按「原因」持有抑制；没有原因时立刻释放"""

    def __init__(self) -> None:
        self._enabled = True
        self._why = "DeckMeido 正在操作设备"
        self._held: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._cookie: int | None = None
        # 下一轮允许重试的时间点（monotonic）与当前退避间隔
        self._retry_at = 0.0
        self._retry_delay = 0.0
        # 已经为这一轮失败记过日志没有（避免退避重试时反复写同一条）
        self._warned = False
        self._mode_at = 0.0
        self._mode = True
        self._thread: threading.Thread | None = None

    # ---------- 对外接口 ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, on: bool) -> None:
        """开关变化时唤醒后台线程：关掉要立刻释放，打开要立刻补挂"""
        self._enabled = bool(on)
        if self._enabled:
            # 打开时要保证线程在跑：桌面模式下它负责常驻抑制
            self._ensure_thread()
        self._wake.set()

    def hold(self, reason: str, why: str = "") -> None:
        """登记一个「别睡」的原因（command / overlay）"""
        with self._lock:
            new = reason not in self._held
            self._held.add(reason)
            if why:
                self._why = why
        self._ensure_thread()
        if new:
            self._wake.set()

    def release(self, reason: str) -> None:
        with self._lock:
            self._held.discard(reason)
        self._wake.set()

    def release_all(self) -> None:
        with self._lock:
            self._held.clear()
        self._wake.set()

    def poke(self) -> None:
        """外部探测到桌面模式时戳一下，让后台线程立刻重新核对一次

        空闲时线程是阻塞在 _wake.wait() 上的，不戳它就不会醒，模式切过去
        也要等到下一次 hold 才挂上抑制。
        """
        self._mode_at = 0.0
        self._wake.set()

    def shutdown(self) -> None:
        """服务退出：停线程并确定释放，别把抑制留给下一个进程"""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)
        self._drop("服务退出")

    def status(self) -> dict:
        with self._lock:
            held = sorted(self._held)
        return {
            "enabled": self.enabled,
            "reasons": held,
            "active": bool(self._proc is not None and self._proc.poll() is None),
            "desktop_mode": self._mode,
        }

    # ---------- 内部 ----------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="awake-keeper", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """有抑制要压着时定期核对；什么都没有时阻塞等事件

        桌面模式下有前台网页时会常驻抑制（要盯着它别被外部杀掉、回游戏要摘），
        所以那时按 POLL_INTERVAL 定时核对；没有前台网页、手上也没有原因时
        阻塞在事件上不轮询。切回桌面/网页回到前台靠 poke() 叫醒（见
        desktop_mode、note_view）。
        判断条件带上 _cookie：KDE cookie 是常驻的（只在第一次挂上时申请），
        保留它但不轮询就会在机器睡眠唤醒后一直装死，得靠定时核对补新的一份。
        """
        while not self._stop.is_set():
            wanted, _desktop = self._want()
            if wanted or self._proc is not None or self._cookie is not None:
                try:
                    self._reconcile()
                except Exception as e:      # 电源抑制失败绝不能拖垮服务
                    _log(f"保持常亮失败：{e}")
                self._wake.wait(timeout=POLL_INTERVAL)
            else:
                self._wake.wait()
            self._wake.clear()

    def _want(self) -> tuple[bool, bool]:
        """返回（是否要压着抑制，是否处于桌面模式）

        网页/DOM 在前台（桌面客户端、手机、局域网浏览器，谁在前台都算）
        且处于桌面模式时才常驻。游戏模式（gamescope 在跑）一律不干预：
        屏幕上不是我们，息屏/休眠完全按 Steam 里设的时间走，智能音箱那种
        长时间 hold 也不压——用户看不到我们在干嘛，压着只会让屏幕一直亮。
        桌面模式下命令执行、操作遮罩、智能音箱这些原因照常生效。

        注意不能只看 _held：hold 后立刻 release 的那一轮，线程可能还没醒来
        就看到空集合，得靠「进程还活着」把释放做完。
        """
        desktop = self._refresh_mode()
        with self._lock:
            wanted = bool(self._held) or (desktop and any_view_active())
        # 游戏模式下 hold 多来自智能音箱这类「麦克风一直听着」的场景：
        # 那时用户看不到我们在干嘛，压着抑制只会让屏幕一直亮，照旧交给 Steam
        return wanted and self._enabled and desktop, desktop

    def _refresh_mode(self) -> bool:
        now = time.monotonic()
        if now - self._mode_at > MODE_TTL:
            try:
                self._mode = desktop_mode()
            except Exception as e:
                # 判不出来按桌面处理：宁可多抑制一次，也别在用户盯着屏幕时黑屏
                _log(f"模式判定失败，按桌面模式处理：{e}")
                self._mode = True
            self._mode_at = now
        return self._mode

    def _reconcile(self) -> None:
        want, _desktop = self._want()
        alive = self._proc is not None and self._proc.poll() is None
        # 两条都齐了（logind 进程在跑、KDE cookie 在手）就什么都不用做：
        # 以前这里每轮都进 _apply，只要其中一条挂不上（SteamOS 上 logind 必失败），
        # 「已阻止息屏」就会每 2 秒重写一条，日志刷满。
        if want and (not alive or self._cookie is None):
            # 整轮都失败过、还在退避期内：照常轮询，只是这里直接返回
            if self._cookie is None and time.monotonic() < self._retry_at:
                return
            self._apply(self._why)
        elif not want:
            # KDE cookie 常驻不还（见 _drop_proc）：这里只收 logind 那条进程，
            # 否则智能音箱 arm/disarm 往返会不停 Inhibit / UnInhibit 重建 cookie
            self._drop_proc("条件结束" if self.enabled else "功能已关闭")

    def _reset_retry(self) -> None:
        self._retry_at = 0.0
        self._retry_delay = 0.0

    def _schedule_retry(self) -> None:
        """整条挂载失败：按指数退避排下一次，别每 2 秒轰一次 logind"""
        self._retry_delay = min(RETRY_MAX, max(RETRY_MIN, self._retry_delay * 2))
        self._retry_at = time.monotonic() + self._retry_delay

    def _apply(self, why: str) -> None:
        # 只补没挂上的那一条：以前无条件先 _drop，会把上一轮已经拿到手的
        # KDE cookie 一起解除，于是「抑制 → 解除 → 抑制」死循环，屏幕永远黑。
        # 进程只要还活着就沿用；cookie 一旦拿到就常驻，不因为 want 短暂变
        # false 而还回去（那会让 arm/disarm 往返不停重建）。
        # 先挂 KDE 那条：它在 SteamOS 上才是真正挡住息屏的那条，logind 必然
        # 失败，先跑它只会白等一次 subprocess 往返。
        if self._cookie is None:
            self._cookie = self._screensaver_inhibit(why)
        if self._proc is None or self._proc.poll() is not None:
            self._proc = self._start_inhibit(why)
        if self._proc is None and self._cookie is None:
            self._schedule_retry()
            # 只在第一轮失败时记一条，后续重试静默，避免刷屏
            if not self._warned:
                self._warned = True
                _log("电源抑制不可用：logind 与 KDE 都没挂上，息屏可能照常发生"
                     f"（{why}）")
        else:
            # 有任意一条生效就取消退避。注意不能在这里重置 _warned：
            # 它是「这一轮是否已经记过日志」，每轮都清掉就等于每轮都写一条，
            # 正是之前日志刷屏的第二个来源。下一次 _drop 时才复位。
            self._reset_retry()
            # logind 失败、只有 KDE 生效：只记一次，别每轮核对都写一条
            if self._proc is None and not self._warned:
                self._warned = True
                _log(f"已阻止息屏：{why}（仅 KDE，logind 需要交互认证）")

    def _start_inhibit(self, why: str) -> subprocess.Popen | None:
        if not shutil.which("systemd-inhibit"):
            return None
        # 启动前先清一遍上一任留下的残留：logind 抑制表打满时，新抑制会直接
        # 报 Maximum number of concurrent inhibitors reached，看起来像权限问题
        _sweep_stale_inhibitors()
        try:
            proc = subprocess.Popen(
                ["systemd-inhibit", "--what=idle:sleep", "--mode=block",
                 f"--who={WHO}", f"--why={why}", "sleep", "infinity"],
                env=_session_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # 独立进程组：收的时候一次带走 systemd-inhibit 和它的 sleep 子进程
                start_new_session=True,
            )
        except Exception as e:
            _log(f"systemd-inhibit 启动失败：{e}")
            return None
        # 立刻验证它真的挂上了，而不是启动就退出（权限/参数问题会这样）。
        # 这里不写日志：SteamOS 上必失败（要交互认证），重试时按退避节奏
        # 每轮再写一遍只会把日志刷满，由调用方决定要不要记一次。
        time.sleep(0.1)
        if proc.poll() is not None:
            devlog.debug("power.inhibit_unsupported", code=proc.returncode)
            return None
        _log(f"已阻止息屏/休眠：{why}")
        return proc

    def _screensaver_inhibit(self, why: str) -> int | None:
        """KDE 的息屏抑制：拿到 cookie 才算成功（cookie 用于之后解除）"""
        if not shutil.which("gdbus"):
            return None
        try:
            r = subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.ScreenSaver",
                 "--object-path", "/ScreenSaver",
                 "--method", "org.freedesktop.ScreenSaver.Inhibit",
                 WHO, why],
                capture_output=True, text=True, timeout=5, env=_session_env(),
            )
        except Exception:
            return None
        if r.returncode != 0:
            # 非 Plasma 桌面（没有这个接口）属正常，logind 那条已经够用
            return None
        m = re.search(r"uint32\s+(\d+)", r.stdout)
        return int(m.group(1)) if m else None

    def _screensaver_release(self, cookie: int | None) -> None:
        if cookie is None or not shutil.which("gdbus"):
            return
        try:
            subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.ScreenSaver",
                 "--object-path", "/ScreenSaver",
                 "--method", "org.freedesktop.ScreenSaver.UnInhibit",
                 str(cookie)],
                capture_output=True, timeout=5, env=_session_env(),
            )
        except Exception as exc:
            devlog.debug("power._screensaver_release", error=repr(exc))
            pass

    def _drop_proc(self, why: str) -> None:
        """只收 logind 那条进程，KDE cookie 留着

        cookie 常驻一份：智能音箱 arm/disarm、命令起停这些高频往返如果每次都
        UnInhibit + 重新 Inhibit，等于不停拆装抑制，机器恰好在缝隙里就会黑屏，
        而且 cookie 号会一直涨。cookie 本身只是「别息屏」的租约，多留一份没有
        副作用（服务退出时 _drop 会统一还掉）。
        """
        proc, self._proc = self._proc, None
        if proc is not None:
            _kill_proc(proc)
            _log(f"已恢复息屏/休眠（{why}）")

    def _drop(self, why: str) -> None:
        proc, self._proc = self._proc, None
        cookie, self._cookie = self._cookie, None
        self._reset_retry()
        # 解除后允许下一轮重新写一条：「已阻止息屏」只在每次真正挂上时报一次
        self._warned = False
        if proc is not None:
            _kill_proc(proc)
        self._screensaver_release(cookie)
        if proc is not None or cookie is not None:
            _log(f"已恢复息屏/休眠（{why}）")


# 全局单例：tools.py 和 main.py 都用这一份
keeper = AwakeKeeper()
