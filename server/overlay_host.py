"""桌面操作遮罩：子进程的拉起、接管、回收与状态同步

agent 真正操作设备时，桌面上盖一层半透明遮罩；用户点一下遮罩即终止当前操作。
遮罩本体是单独的 overlay.py（Qt/XWayland 进程），本模块只管它这一侧：
什么时候起、什么时候收、鼠标让开/抬高的握手，以及看门狗兜底。

运行态（_overlay_proc / _overlay_state / _overlay_ack / _overlay_lock）统一
由 get_main() 现读，main 侧保留同名对象，其它模块照原样访问即可。
"""
from __future__ import annotations

import asyncio
import glob
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import devlog, paths, power, tools

get_main = None


def init(get_main_fn) -> None:
    """由 main 在模块末尾注入：注入的是「取 main 模块」的函数，不是它的副本"""
    global get_main
    get_main = get_main_fn


def _m():
    return get_main()


def _xauthority() -> str:
    """当前可用的 X 授权文件

    Xwayland 每次会话都会换一个 xauth_XXXXXX 文件，后端进程环境里那份
    早晚会失效。这里先认环境里的路径（存在才算数），否则按修改时间取
    最新的一份；用 setdefault 是不够的——环境里留着旧路径时新文件永远
    不会被找到，遮罩会一直报 "Authorization required"。
    """
    cur = os.environ.get("XAUTHORITY", "")
    if cur and Path(cur).exists():
        return cur
    cands = glob.glob(f"/run/user/{os.getuid()}/xauth_*")
    cands = [c for c in cands if Path(c).exists()]
    if cands:
        return max(cands, key=lambda p: Path(p).stat().st_mtime)
    home = Path.home() / ".Xauthority"
    return str(home) if home.exists() else ""


def _overlay_env() -> dict:
    """桌面会话环境：systemd 用户服务里默认没有 DISPLAY / XAUTHORITY"""
    m = _m()
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    auth = _xauthority()
    if auth:
        env["XAUTHORITY"] = auth
    else:
        env.pop("XAUTHORITY", None)
    env["DECKMEIDO_API"] = f"http://127.0.0.1:{m.CFG['server'].get('port', 18765)}"
    # 走 Xwayland（xcb）：Wayland 原生窗口没有「不进任务栏」的协议，遮罩必须走 X11 才能隐藏
    env["QT_QPA_PLATFORM"] = "xcb"
    return env


def ensure_overlay() -> None:
    """遮罩进程常驻，按需显示；崩溃后自动重启

    用 --force 启动：先让可能残留的旧遮罩自己退位，再起新的一层。
    否则后端重启留下的孤儿遮罩会越积越多，操作时叠出好几层。

    起完之后要核对「这一层真的拿到套接字了」：overlay 的 force 接管
    先 ping 旧实例、再等它死透，接口正常时这一步不该失败；但万一旧实例
    卡在 X11 调用里没退，新进程会自己退出，此时 _overlay_proc 是个
    已退出的句柄，后续 passthrough / lift 的等待会立刻返回（当它没跑）。
    所以起进程这一步会阻塞到最多 6 秒等接管结果，调用方要在
    事件循环之外调它（见 set_overlay_async 用 asyncio.to_thread 包起来）。
    """
    m = _m()
    # 两轮对话同时开始工具调用时，别各自拉起一层遮罩
    with m._overlay_lock:
        _ensure_overlay_locked()


def _ensure_overlay_locked() -> None:
    m = _m()
    proc = m._overlay_proc
    if proc is not None and proc.poll() is None:
        return

    def spawn() -> bool:
        """拉起一层遮罩并确认它活着且有套接字"""
        try:
            log = open(paths.logs_dir() / "overlay.log", "ab")
        except OSError:
            log = subprocess.DEVNULL
        try:
            new_proc = subprocess.Popen(
                [sys.executable, str(m.BASE_DIR / "overlay.py"), "--force"],
                cwd=str(m.BASE_DIR), env=_overlay_env(),
                stdout=log, stderr=log, start_new_session=True,
            )
        except Exception:
            return False
        finally:
            try:
                log.close()
            except Exception as exc:
                devlog.debug("overlay_host.spawn", error=repr(exc))
                pass
        # 接管要等旧实例退位，给足时间；退出码非 0 说明没抢到
        for _ in range(40):          # 最多 6 秒
            if new_proc.poll() is not None:
                _ = new_proc.poll()  # 非 0 = 没接管成功
                return new_proc.returncode == 0
            time.sleep(0.15)
        m._overlay_proc = new_proc
        return True

    for _ in range(2):               # 失败一次再补一次
        if spawn():
            return


def stop_overlay() -> None:
    """服务退出时收掉自己拉起的遮罩，避免留下孤儿进程

    两处兜底，都是踩过的坑：
    1. 只 terminate 不 kill：遮罩若卡在 X11 调用里不响应 SIGTERM，
       这个同步 wait 就会一直占着 AnyIO worker 线程，把整个后端的
       shutdown 拖死——uvicorn 收不到中断，端口释放不掉，下次启动
       要么被单实例守卫挡回，要么去抢 socket。所以超时后必须 kill。
    2. 兜底按 pid 清理：进程句柄可能已经丢了（后端重启过），此时靠
       _overlay_proc 一个都收不到，孤儿就留在桌面上继续画遮罩。
    """
    m = _m()
    proc = m._overlay_proc
    m._overlay_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception as exc:
                devlog.debug("overlay_host.stop_overlay", error=repr(exc))
                pass
    # 句柄之外的残留（含拿不到套接字的聋子孤儿）统一清掉
    for pid in _overlay_pids():
        try:
            if proc is not None and pid == proc.pid:
                continue
            os.kill(pid, signal.SIGKILL)
        except Exception as exc:
            devlog.debug("overlay_host.stop_overlay", error=repr(exc))
            pass
    # 关键：僵尸子进程必须回收（waitpid）。
    # overlay.py 被 --force 接管时会被别人 SIGKILL，它成了僵尸挂在 CMD_START 的
    # 事件循环里没被 wait 过；若不 reap，主进程被 SIGTERM 后 uvicorn 的 shutdown
    # 会一直卡在「Waiting for background tasks to complete」，端口不释放，
    # 表现为一个既不服务也不退出的幽灵后端（还会被单实例守卫挡着不让重启）。
    # 只回收我们自己那个 pid：waitpid(-1) 会把 asyncio 管着的命令进程一起摘走
    if proc is not None:
        _reap_children(proc.pid)


def _reap_children(only_pid: int | None = None) -> None:
    """回收已退出但还没 wait 的子进程（僵尸）。

    `waitpid(-1)` 会摘走**任意**一个子进程 —— 包括 asyncio 子进程管理器
    正在管着的 run_command 进程。那些进程的退出码需要由事件循环回收，
    被这里抢走后 asyncio 会一直等不到终止状态，命令永远「不结束」。
    所以只回收我们自己拉起的那个 pid（遮罩），没有指定就什么都不做。
    """
    if only_pid is None:
        return
    try:
        os.waitpid(only_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _overlay_pids() -> list[int]:
    """找出本机所有 overlay.py 进程（含孤儿）"""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", str(_m().BASE_DIR / "overlay.py")],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.split():
            try:
                pids.append(int(line))
            except ValueError:
                continue
    except Exception as exc:
        devlog.debug("overlay_host._overlay_pids", error=repr(exc))
        pass
    return pids


def set_overlay(show: bool, text: str = "") -> None:
    m = _m()
    state = m._overlay_state
    state["show"] = show
    if text:
        state["text"] = text
    if not show:
        state["since"] = 0.0
        # 收起遮罩 = 这一轮不再往屏幕上写正文了（终止 / 收尾 / 开始操作桌面）：
        # 攒着没发的字先推出去，保证界面上的正文与随后的系统提示顺序正确。
        # 运行在事件循环里时不能直接建任务（这里也可能是异步任务之外的路径），
        # 所以只在有事件循环时交给 call_soon；没有就留给收尾的 flush
        try:
            asyncio.get_running_loop().call_soon(m.flush_delta_now, "")
        except RuntimeError:
            pass
        # 不要在这里顺手把 passthrough 清掉：可能正好有一次点击注入在跑，清了之后
        # 遮罩会重新冒出来挨这一下点击，于是又被当成「用户终止」。passthrough 只由
        # set_overlay_passthrough 收
        # 遮罩收起：不再需要为它压着电源抑制（命令还在跑的话那条原因仍在，
        # 计数归零才真的释放，见 power.AwakeKeeper）
        power.keeper.release("overlay")
    else:
        # 记下「从什么时候开始要它亮」。看门狗据此判断是不是有哪条异常路径
        # 漏了 set_overlay(False)，把遮罩永远留在屏幕上。
        if not state.get("since"):
            state["since"] = time.time()
        # 遮罩亮着 = 用户可能正盯着看操作过程，别在这时候息屏
        power.keeper.hold("overlay", text or "正在思考")
        ensure_overlay()


async def set_overlay_async(show: bool, text: str = "") -> None:
    """set_overlay 的异步版本：拉起遮罩那段（最长 6 秒）丢到线程里做

    直接 await 这个而不是调 set_overlay：后者会 fork 进程并轮询等它接管，
    在事件循环里跑会把所有 ws 广播和心跳按住。
    """
    if not show:
        set_overlay(False, text)
        return
    state = _m()._overlay_state
    state["show"] = True
    if text:
        state["text"] = text
    if not state.get("since"):
        state["since"] = time.time()
    power.keeper.hold("overlay", text or "正在思考")
    await asyncio.to_thread(ensure_overlay)


# 遮罩最多连亮这么久；超过就认为状态没被复位（异常/取消路径逃逸），强制收掉。
# 一整轮最长思考时间可能有一两分钟（多轮工具 + 模型推理），这里给足余量；
# 正常结束时 handle_chat 的 finally 会收掉，用不到这个兜底。
_OVERLAY_MAX_HOLD = 300.0


def _overlay_watchdog() -> None:
    """兜底线程：遮罩亮着太久就强制收起，别让它一直挡住屏幕。

    遮罩在「会动屏幕」的工具执行期间常显（见 tools.touches_screen），正常收尾由
    调用方 set_overlay(False)；取消、异常、工具执行到一半被终止，任一环节漏掉，
    遮罩就会一直盖在那儿。这里按 _OVERLAY_MAX_HOLD 兜一个上限。
    """
    while True:
        time.sleep(1.0)
        try:
            state = _m()._overlay_state
            if not state.get("show"):
                continue
            since = state.get("since") or 0.0
            if since and (time.time() - since) > _OVERLAY_MAX_HOLD:
                print("[DeckMeido] 遮罩超时未复位，已强制收起", file=sys.stderr)
                set_overlay(False)
        except Exception as exc:
            devlog.debug("overlay_host._overlay_watchdog", error=repr(exc))
            pass


def set_overlay_passthrough(on: bool) -> None:
    """让遮罩暂时让开，使 agent 的点击能落到下面的窗口。

    遮罩窗口跑在 XWayland 里，Wayland 合成器做命中测试时看不见 X11 的
    input shape，所以「窗口标志让开」是不成立的；遮罩进程收到这个标志后
    会把窗口整块 hide()，并回报「确实藏好了」。这里必须等那个回报再放行，
    否则点击会打在还没撤下去的遮罩上，被当成「用户终止」。

    tools 侧只把它当回调登记（ON_OVERLAY_PASSTHROUGH），原来的写法是
    fire-and-forget，所以这里保持同步、不等 ack；等 ack 的版本是
    _set_overlay_passthrough_wait（唯一调用点也用它）。
    """
    m = _m()
    m._overlay_state["passthrough"] = on
    m._overlay_ack["passthrough"] = False


async def _set_overlay_passthrough_wait(on: bool) -> None:
    """让遮罩让开并等到它确认已隐藏（点击注入前用这个）"""
    m = _m()
    set_overlay_passthrough(on)
    if not on:
        return
    proc = m._overlay_proc
    if proc is None or proc.poll() is not None:
        return  # 遮罩没跑，不用等
    for _ in range(80):  # 最多等 800ms（hide + 合成器撤层需要几帧）
        if m._overlay_ack["passthrough"]:
            return
        await asyncio.sleep(0.01)


def set_overlay_pointer(on: bool) -> None:
    """预告「下一步会按下鼠标键」，让遮罩别抢这一下点击。

    遮罩是 XWayland 窗口，非穿透状态下会把点击吃掉（=用户终止）。而
    set_overlay_passthrough 在遮罩还没显示时什么都不做，工具内部自己去调也来
    不及。所以这里把意图做成后端状态，遮罩进程看到 pointer 立刻转 25ms 快轮询，
    显示后马上 hide 并回报，agent 随后注入的点击就落在下面的窗口上。
    """
    _m()._overlay_state["pointer"] = bool(on)


async def set_overlay_lift(on: bool) -> None:
    """截图期间把遮罩收起来，拍完再放回来。

    打开时要等遮罩真正隐藏（它每 25ms 轮询并回报 ack），否则这张截图里还是有遮罩。
    """
    m = _m()
    m._overlay_state["lift"] = on
    if not on:
        return
    proc = m._overlay_proc
    if proc is None or proc.poll() is not None:
        return  # 遮罩没跑，不用等
    for _ in range(20):  # 最多等 200ms
        if m._overlay_ack["lift"]:
            return
        await asyncio.sleep(0.01)


def start_watchdog() -> None:
    """起看门狗线程（main 在导入期调用一次）"""
    tools.ON_OVERLAY_PASSTHROUGH = set_overlay_passthrough
    tools.ON_OVERLAY_LIFT = set_overlay_lift
    threading.Thread(target=_overlay_watchdog, name="overlay-watchdog",
                     daemon=True).start()
