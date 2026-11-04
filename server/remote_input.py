"""跨屏键鼠接管：把 PC 浏览器的相对鼠标/键盘事件注入到 Deck。

浏览器侧只发相对位移和 KeyboardEvent.code，这里负责三件事：
  1. 会话状态（未接管 / 接管中 / 已结束）、心跳与超时；
  2. 位移合并（~16ms 一批），避免每个 mousemove 都起一个进程；
  3. 注入后端：优先 ydotoold（和桌面工具同一条通路），没有时直写 /dev/uinput。

为什么优先 ydotool：ydotoold 由 install.sh 以 deck 用户的 systemd user 单元托管，
socket 就在 /run/user/<uid>/.ydotool_socket，后端（同样以 deck 运行）直接能用；
它内部已经处理好了 uinput 设备注册与 gamescope 的兼容。直写 uinput 是兜底：
后端不是 root 时通常打不开 /dev/uinput（插件才是 root），所以只在前者不可用时尝试。

所有失败都抛出带错误码的 RemoteInputError，前端据此显示明确原因，不静默吞掉。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import time

# fcntl 只在 Linux 上有；Windows 开发机跑单元测试时也要能 import 本模块
try:
    import fcntl
except ImportError:  # pragma: no cover - 仅 Windows 开发机
    fcntl = None

# ---------- 错误码 ----------
RI_DISABLED = "RI_DISABLED"          # 配置里没开启
RI_UNAUTHORIZED = "RI_UNAUTHORIZED"  # 令牌不对
RI_BUSY = "RI_BUSY"                  # 已有人在接管
RI_NO_YDOTOOL = "RI_NO_YDOTOOL"      # ydotool/ydotoold 不可用
RI_NO_UINPUT = "RI_NO_UINPUT"        # /dev/uinput 打不开
RI_INJECT_FAILED = "RI_INJECT_FAILED"  # 注入命令执行失败
RI_TIMEOUT = "RI_TIMEOUT"            # 心跳或时长超时
RI_RELEASED = "RI_RELEASED"          # 正常释放
RI_DISCONNECTED = "RI_DISCONNECTED"  # 连接异常断开
RI_BAD_MSG = "RI_BAD_MSG"            # 协议消息不合法

# 心跳：浏览器每 3 秒发一次 ping，10 秒收不到就认定断线
HEARTBEAT_S = 10.0
# 位移合并窗口：约一帧，保证跟手又不至于刷进程
FLUSH_INTERVAL = 0.016
# 单条消息的位移上限，防止异常客户端把指针甩飞
MAX_ABS_MOVE = 4096
# 积压位移上限：事件循环被卡住时最多攒这么多，超出直接丢掉
MAX_PENDING_MOVE = 20000
DEFAULT_MAX_MINUTES = 30

_KEY_ESC = 1
_BTN_LEFT, _BTN_RIGHT, _BTN_MIDDLE = 0x110, 0x111, 0x112

# 浏览器 KeyboardEvent.code → Linux KEY_*（/usr/include/linux/input-event-codes.h）
# 只收录常用键；映射不到的直接忽略并记日志，绝不猜测。
KEYMAP = {
    # 字母
    "KeyQ": 16, "KeyW": 17, "KeyE": 18, "KeyR": 19, "KeyT": 20,
    "KeyY": 21, "KeyU": 22, "KeyI": 23, "KeyO": 24, "KeyP": 25,
    "KeyA": 30, "KeyS": 31, "KeyD": 32, "KeyF": 33, "KeyG": 34,
    "KeyH": 35, "KeyJ": 36, "KeyK": 37, "KeyL": 38,
    "KeyZ": 44, "KeyX": 45, "KeyC": 46, "KeyV": 47, "KeyB": 48,
    "KeyN": 49, "KeyM": 50,
    # 数字
    "Digit1": 2, "Digit2": 3, "Digit3": 4, "Digit4": 5, "Digit5": 6,
    "Digit6": 7, "Digit7": 8, "Digit8": 9, "Digit9": 10, "Digit0": 11,
    "Minus": 12, "Equal": 13,
    # 编辑/控制
    "Backspace": 14, "Tab": 15, "Enter": 28, "Escape": _KEY_ESC,
    "Space": 57, "CapsLock": 58, "Delete": 111, "Insert": 110,
    "Home": 102, "End": 107,
    "PageUp": 104, "PageDown": 109,
    "ArrowLeft": 105, "ArrowRight": 106, "ArrowUp": 103, "ArrowDown": 108,
    # 修饰键
    "ControlLeft": 29, "ControlRight": 97,
    "ShiftLeft": 42, "ShiftRight": 54,
    "AltLeft": 56, "AltRight": 100,
    "MetaLeft": 125, "MetaRight": 126,
    # 符号
    "Comma": 51, "Period": 52, "Slash": 53,
    "Semicolon": 39, "Quote": 40, "Backquote": 41,
    "BracketLeft": 26, "BracketRight": 27, "Backslash": 43,
    # 功能键
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    # 小键盘（常用几项）
    "Numpad0": 82, "Numpad1": 79, "Numpad2": 80, "Numpad3": 81,
    "Numpad4": 75, "Numpad5": 76, "Numpad6": 77,
    "Numpad7": 71, "Numpad8": 72, "Numpad9": 73,
    "NumpadAdd": 78, "NumpadSubtract": 74, "NumpadMultiply": 55,
    "NumpadDivide": 98, "NumpadEnter": 96, "NumpadDecimal": 83,
    "PrintScreen": 99,
}

# 测试用：交付要求里点名必须覆盖的键
REQUIRED_CODES = (
    [f"Key{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [f"Digit{i}" for i in range(10)]
    + ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]
    + [f"F{i}" for i in range(1, 13)]
    + ["Enter", "Escape", "Tab", "Space", "Backspace", "Delete",
       "Home", "End", "PageUp", "PageDown",
       "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight",
       "AltLeft", "AltRight", "MetaLeft", "MetaRight",
       "Comma", "Period", "Slash", "Semicolon", "Quote", "Backquote",
       "Minus", "Equal", "BracketLeft", "BracketRight", "Backslash"]
)


class RemoteInputError(Exception):
    """带错误码的注入失败；main.py 把它转成 {"t":"state","code":...}"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def enabled(cfg: dict) -> bool:
    """唯一开关：enabled 打开即允许局域网来源"""
    return bool(cfg.get("enabled", False))


def access_error(cfg: dict, client_ip: str, token: str, provided: str) -> str | None:
    """连接前的总校验：令牌 → 开关。

    cfg 是 [remote_input] 段；token 是当前有效令牌（空令牌一律拒绝）。
    """
    if not (token or "").strip() or (provided or "").strip().lower() != token.strip().lower():
        return RI_UNAUTHORIZED
    if not enabled(cfg):
        return RI_DISABLED
    return None


# ---------- ydotool 注入 ----------
# 按下/抬起用 ydotool click 的位掩码：0x40=左键按下、0x80=左键抬起，右/中依次 +1。
_BTN_DOWN = {0: "0x40", 1: "0x41", 2: "0x42"}
_BTN_UP = {0: "0x80", 1: "0x81", 2: "0x82"}


class YdotoolBackend:
    kind = "ydotool"

    def __init__(self):
        self._env = dict(os.environ)
        uid = getattr(os, "getuid", lambda: 0)()
        self._sock = (self._env.get("YDOTOOL_SOCKET")
                      or f"/run/user/{uid}/.ydotool_socket")
        self._env["YDOTOOL_SOCKET"] = self._sock

    @staticmethod
    def available() -> bool:
        return shutil.which("ydotool") is not None

    async def start(self):
        if not self.available():
            raise RemoteInputError(RI_NO_YDOTOOL, "未安装 ydotool")
        if not os.path.exists(self._sock):
            raise RemoteInputError(
                RI_NO_YDOTOOL,
                f"ydotoold 没在运行（{self._sock} 不存在）")
        return self

    async def _run(self, *args: str):
        proc = await asyncio.create_subprocess_exec(
            "ydotool", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        out, err = await self._communicate(proc)
        if proc.returncode != 0:
            detail = (out + err).decode(errors="replace").strip()
            raise RemoteInputError(
                RI_INJECT_FAILED, f"ydotool {args[0]} 失败：{detail or proc.returncode}")

    @staticmethod
    async def _communicate(proc):
        # 单条命令最多等 3 秒：ydotoold 卡死时不能让整个接管会话挂住
        try:
            return await asyncio.wait_for(proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise RemoteInputError(RI_INJECT_FAILED, "ydotool 执行超时")

    async def move(self, dx: int, dy: int):
        if dx or dy:
            await self._run("mousemove", "--", str(dx), str(dy))

    async def button(self, b: int, down: bool):
        code = (_BTN_DOWN if down else _BTN_UP).get(b)
        if code:
            await self._run("click", code)

    async def wheel(self, d: int):
        # 浏览器 deltaY 向下为正，Linux REL_WHEEL 向上为正，所以取反
        if d:
            await self._run("mousemove", "-w", "-x", "0", "-y", str(-d))

    async def key(self, code: int, down: bool):
        await self._run("key", f"{code}:{1 if down else 0}")

    async def release_all(self, keys: set[int], buttons: set[int]):
        if keys:
            args = [f"{code}:0" for code in keys]
            try:
                await self._run("key", *args)
            except RemoteInputError as exc:
                print(f"[DeckMeido] 释放按键失败：{exc.detail}", file=sys.stderr)
        if buttons:
            args = [_BTN_UP[b] for b in buttons if b in _BTN_UP]
            if args:
                try:
                    await self._run("click", *args)
                except RemoteInputError as exc:
                    print(f"[DeckMeido] 释放鼠标键失败：{exc.detail}", file=sys.stderr)

    async def close(self):
        return


# ---------- 直写 /dev/uinput 兜底 ----------
_UI_DEV_CREATE = 0x5501
_UI_DEV_DESTROY = 0x5502
_UI_SET_EVBIT = 0x40045564
_UI_SET_KEYBIT = 0x40045565
_UI_SET_RELBIT = 0x40045566
_UI_DEV_SETUP = 0x405C5503
_EV_SYN, _EV_KEY, _EV_REL = 0x00, 0x01, 0x02
_REL_X, _REL_Y, _REL_HWHEEL, _REL_WHEEL = 0x00, 0x01, 0x06, 0x08


class UinputBackend:
    kind = "uinput"
    _ENUM_WAIT = 0.2   # 内核注册新设备节点需要一点时间，早了会丢第一帧

    def __init__(self):
        self._fd: int | None = None

    async def start(self):
        if fcntl is None:
            raise RemoteInputError(RI_NO_UINPUT, "当前系统不支持 /dev/uinput")
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise RemoteInputError(
                RI_NO_UINPUT,
                f"打不开 /dev/uinput（后端需要 root 或 input 组权限）：{exc}")
        try:
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_SYN)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_REL)
            for code in set(KEYMAP.values()) | {_BTN_LEFT, _BTN_RIGHT, _BTN_MIDDLE}:
                fcntl.ioctl(fd, _UI_SET_KEYBIT, code)
            for code in (_REL_X, _REL_Y, _REL_WHEEL, _REL_HWHEEL):
                fcntl.ioctl(fd, _UI_SET_RELBIT, code)
            # uinput_setup：id{bustype,vendor,product,version} + name(80) + ff_effects_max
            fcntl.ioctl(
                fd, _UI_DEV_SETUP,
                struct.pack("HHHH80sI", 0x06, 0x1209, 0x0003, 0x0001,
                            b"DeckMeido Remote Input", 0),
            )
            fcntl.ioctl(fd, _UI_DEV_CREATE)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        await asyncio.sleep(self._ENUM_WAIT)

    def _emit(self, typ: int, code: int, value: int):
        if self._fd is not None:
            os.write(self._fd, struct.pack("llHHi", 0, 0, typ, code, value))

    def _syn(self):
        self._emit(_EV_SYN, 0, 0)

    async def move(self, dx: int, dy: int):
        if not (dx or dy):
            return
        if dx:
            self._emit(_EV_REL, _REL_X, dx)
        if dy:
            self._emit(_EV_REL, _REL_Y, dy)
        self._syn()

    async def button(self, b: int, down: bool):
        code = {0: _BTN_LEFT, 1: _BTN_RIGHT, 2: _BTN_MIDDLE}.get(b)
        if code is None:
            return
        self._emit(_EV_KEY, code, 1 if down else 0)
        self._syn()

    async def wheel(self, d: int):
        if d:
            self._emit(_EV_REL, _REL_WHEEL, -d)  # 与浏览器方向对齐
            self._syn()

    async def key(self, code: int, down: bool):
        self._emit(_EV_KEY, code, 1 if down else 0)
        self._syn()

    async def release_all(self, keys: set[int], buttons: set[int]):
        try:
            for code in keys:
                self._emit(_EV_KEY, code, 0)
            for b in buttons:
                code = {0: _BTN_LEFT, 1: _BTN_RIGHT, 2: _BTN_MIDDLE}.get(b)
                if code is not None:
                    self._emit(_EV_KEY, code, 0)
            if keys or buttons:
                self._syn()
        except OSError as exc:
            print(f"[DeckMeido] uinput 释放按键失败：{exc}", file=sys.stderr)

    async def close(self):
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, _UI_DEV_DESTROY)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class InputSession:
    """一次接管会话：持有按下的键、合并位移、结束后统一释放。"""

    def __init__(self, backend, max_minutes: int = DEFAULT_MAX_MINUTES,
                 owner: str = ""):
        self.backend = backend
        self.owner = owner
        self.started = time.monotonic()
        self.last_seen = self.started
        self.max_minutes = max(1, min(int(max_minutes or DEFAULT_MAX_MINUTES), 120))
        self.held_keys: set[int] = set()
        self.held_buttons: set[int] = set()
        self.pending_dx = 0
        self.pending_dy = 0
        self.closed = False
        self.end_code = ""
        self.stop_event = asyncio.Event()
        self._flush_task: asyncio.Task | None = None
        # 位移批处理与按键注入必须串行：否则「移动后立刻点击」可能先落点击
        self._io_lock = asyncio.Lock()

    @property
    def deadline(self) -> float:
        return self.started + self.max_minutes * 60

    def touch(self):
        self.last_seen = time.monotonic()

    def heartbeat_expired(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) - self.last_seen > HEARTBEAT_S

    def expired(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) >= self.deadline

    def start_flush(self):
        if self._flush_task is None and not self.closed:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        while not self.closed:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                await self.flush_moves()
            except RemoteInputError as exc:
                # 注入失败要让读循环看见并结束会话，否则会一直卡在后台
                print(f"[DeckMeido] 鼠标注入失败：{exc.detail}", file=sys.stderr)
                self.end_code = exc.code
                self.stop_event.set()
                break

    def add_move(self, dx: int, dy: int):
        self.pending_dx = max(-MAX_PENDING_MOVE,
                              min(MAX_PENDING_MOVE, self.pending_dx + int(dx)))
        self.pending_dy = max(-MAX_PENDING_MOVE,
                              min(MAX_PENDING_MOVE, self.pending_dy + int(dy)))

    async def flush_moves(self):
        async with self._io_lock:
            await self._flush_pending()

    async def _flush_pending(self):
        dx, dy = self.pending_dx, self.pending_dy
        if not dx and not dy:
            return
        self.pending_dx = self.pending_dy = 0
        await self.backend.move(dx, dy)

    async def button(self, b: int, down: bool):
        async with self._io_lock:
            await self._flush_pending()
            if down:
                self.held_buttons.add(b)
            else:
                self.held_buttons.discard(b)
            await self.backend.button(b, down)

    async def key(self, code: int, down: bool):
        async with self._io_lock:
            await self._flush_pending()
            if down:
                self.held_keys.add(code)
            else:
                self.held_keys.discard(code)
            await self.backend.key(code, down)

    async def wheel(self, d: int):
        async with self._io_lock:
            await self._flush_pending()
            await self.backend.wheel(d)

    async def handle(self, msg: dict) -> str:
        """处理一条协议消息，返回空串表示继续；返回错误码表示要结束会话。"""
        if not isinstance(msg, dict):
            raise RemoteInputError(RI_BAD_MSG, "消息不是对象")
        t = msg.get("t")
        if t == "ping":
            return ""
        if t == "move":
            try:
                dx = int(msg.get("dx") or 0)
                dy = int(msg.get("dy") or 0)
            except (TypeError, ValueError):
                raise RemoteInputError(RI_BAD_MSG, "move 参数不是整数")
            self.add_move(max(-MAX_ABS_MOVE, min(MAX_ABS_MOVE, dx)),
                          max(-MAX_ABS_MOVE, min(MAX_ABS_MOVE, dy)))
            return ""
        if t == "btn":
            b, p = msg.get("b"), msg.get("p")
            if b not in (0, 1, 2) or not isinstance(p, bool):
                raise RemoteInputError(RI_BAD_MSG, "btn 参数不合法")
            await self.button(int(b), p)
            return ""
        if t == "wheel":
            try:
                d = int(msg.get("d") or 0)
            except (TypeError, ValueError):
                raise RemoteInputError(RI_BAD_MSG, "wheel 参数不是整数")
            await self.wheel(max(-5, min(5, d)))
            return ""
        if t == "key":
            code = msg.get("code")
            p = msg.get("p")
            if not isinstance(code, str) or not isinstance(p, bool):
                raise RemoteInputError(RI_BAD_MSG, "key 参数不合法")
            linux_code = KEYMAP.get(code)
            if linux_code is None:
                print(f"[DeckMeido] 忽略未映射的按键：{code}", file=sys.stderr)
                return ""
            await self.key(linux_code, p)
            return ""
        if t == "release":
            return RI_RELEASED
        raise RemoteInputError(RI_BAD_MSG, f"未知消息类型：{t}")

    async def end(self, code: str) -> bool:
        """释放所有按下的键并关闭设备；可重复调用。"""
        if self.closed:
            return False
        self.closed = True
        self.end_code = self.end_code or code
        self.stop_event.set()
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._flush_task = None
        try:
            async with self._io_lock:
                await self.backend.release_all(set(self.held_keys), set(self.held_buttons))
        except Exception as exc:
            print(f"[DeckMeido] 释放输入状态失败：{exc}", file=sys.stderr)
        self.held_keys.clear()
        self.held_buttons.clear()
        try:
            await self.backend.close()
        except Exception as exc:
            print(f"[DeckMeido] 关闭注入后端失败：{exc}", file=sys.stderr)
        return True


class RemoteInputManager:
    """全局只有一个接管会话；状态变化通过 hook 广播给所有页面。"""

    def __init__(self):
        self.session: InputSession | None = None
        self._lock = asyncio.Lock()
        self._hook = None

    @property
    def active(self) -> bool:
        return self.session is not None

    def set_hook(self, hook):
        """hook(state: dict) 是 async 函数，由 main.py 接到 WS 广播。"""
        self._hook = hook

    def status(self) -> dict:
        s = self.session
        return {
            "active": bool(s),
            "by": s.owner if s else "",
            "backend": s.backend.kind if s else "",
            "started": s.started if s else 0,
            "remaining": max(0, int(s.deadline - time.monotonic())) if s else 0,
        }

    async def _notify(self, state: str, code: str = ""):
        if self._hook is None:
            return
        try:
            await self._hook({"state": state, "code": code,
                              **self.status()})
        except Exception as exc:
            print(f"[DeckMeido] 接管状态广播失败：{exc}", file=sys.stderr)

    async def begin(self, cfg: dict, owner: str = "") -> InputSession:
        async with self._lock:
            if self.session is not None:
                raise RemoteInputError(RI_BUSY, "已经有人在接管这台 Deck")
            candidates = []
            if YdotoolBackend.available():
                candidates.append(YdotoolBackend())
            candidates.append(UinputBackend())
            backend = None
            last: RemoteInputError | None = None
            for cand in candidates:
                try:
                    await cand.start()
                    backend = cand
                    break
                except RemoteInputError as exc:
                    last = exc
                    try:
                        await cand.close()
                    except Exception:
                        pass
                except Exception as exc:
                    last = RemoteInputError(RI_INJECT_FAILED, str(exc))
            if backend is None:
                raise last or RemoteInputError(RI_INJECT_FAILED, "没有可用的注入后端")
            max_minutes = int(cfg.get("max_minutes", DEFAULT_MAX_MINUTES) or DEFAULT_MAX_MINUTES)
            sess = InputSession(backend, max_minutes, owner)
            sess.start_flush()
            self.session = sess
            print(f"[DeckMeido] 跨屏接管开始：来源 {owner or '本机'}，"
                  f"后端 {backend.kind}，上限 {sess.max_minutes} 分钟", file=sys.stderr)
            await self._notify("active")
            return sess

    async def finish(self, code: str) -> bool:
        async with self._lock:
            sess = self.session
            if sess is None:
                return False
            self.session = None
            await sess.end(code)
            print(f"[DeckMeido] 跨屏接管结束：{code}", file=sys.stderr)
            await self._notify("ended", code)
            return True


manager = RemoteInputManager()
