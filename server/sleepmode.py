"""睡眠模式：听歌听着睡着了，机器自己关机。

判据是两个条件**同时**成立并持续满 N 分钟（默认 60）：

1. 音乐已经停了（MPRIS 没有处于 Playing）。歌还在放说明人还在听，
   不管多久都不关——那样只会打断正在听的人。
2. 机器真的没人用：`loginctl` 报会话空闲。这是系统级空闲，
   玩游戏、按键、动鼠标都会把空闲时间清零。手柄也能算进来，
   因为 Steam 会把输入打到会话上；比只看网页 mousemove 靠谱。

为什么要两个：只有「音乐停了」时，人可能正在打游戏放静音，关机会毁存档；
只有「没人用」时，人可能正躺着听歌，关机会直接掐掉歌。两个都满足，
才是「歌放完了、人也没动静」这种最常见的听着睡着的情形。

超时后先进入倒计时（默认 60 秒），倒计时期间任何输入或唤醒都能取消，
倒计时结束才真正执行 `systemctl poweroff`。

实现照 power.py 的路子：后台线程轮询，Linux 专属命令一律走 subprocess，
不引入新依赖；所有外部命令失败都只记日志，不能让线程死掉。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from . import media

# 轮询间隔：音乐状态本身有 5 秒缓存，空闲查询是子进程，10 秒一次足够，
# 一小时的判定精度完全够用，也不会给 Deck 添负担。
POLL_INTERVAL = 10.0
# 倒计时期间的最短检查间隔：这段时间要能快速响应取消
COUNTDOWN_POLL = 0.5

DEFAULTS = {
    "enabled": False,
    "idle_minutes": 60,
    "countdown_sec": 60,
}


def _log(msg: str) -> None:
    print(f"[DeckMeido] {msg}", file=sys.stderr)


def _session_env() -> dict:
    """桌面会话环境：systemd 用户服务里默认没有 DISPLAY / D-Bus 地址"""
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    env.setdefault("DISPLAY", ":0")
    return env


def merge_cfg(raw: dict | None) -> dict:
    """把 [sleep] 段规整成可用配置；越界值夹回安全范围。

    idle_minutes 下限 5 分钟：太短会在「歌刚放完暂时没动静」时误关机。
    """
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        if raw.get("enabled") is not None:
            cfg["enabled"] = bool(raw.get("enabled"))
        for key in ("idle_minutes", "countdown_sec"):
            if raw.get(key) is not None:
                try:
                    cfg[key] = int(raw.get(key))
                except (TypeError, ValueError):
                    pass
    cfg["idle_minutes"] = max(5, min(720, int(cfg["idle_minutes"])))
    cfg["countdown_sec"] = max(10, min(600, int(cfg["countdown_sec"])))
    return cfg


def idle_seconds() -> float | None:
    """系统会话的空闲秒数。判不出来返回 None（调用方按「不空闲」处理）。

    loginctl show-session 的 IdleHint / IdleSinceHint 是 logind 自己按输入
    事件算的，键鼠、触摸、手柄（Steam 会注入到会话）都会把它清零。
    拿不到会话号（比如没有图形会话）就直接返回 None，绝不猜。
    """
    if not shutil.which("loginctl"):
        return None
    try:
        sid = os.environ.get("XDG_SESSION_ID") or ""
        if not sid:
            r = subprocess.run(["loginctl", "show-user", "self", "-p", "Display"],
                               capture_output=True, text=True, timeout=3,
                               env=_session_env())
            sid = (r.stdout or "").strip().split("=")[-1]
        if not sid:
            return None
        r = subprocess.run(
            ["loginctl", "show-session", sid, "-p", "IdleHint",
             "-p", "IdleSinceHint", "--value"],
            capture_output=True, text=True, timeout=3, env=_session_env())
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        hint, since = lines[0], lines[1]
        if hint != "yes":
            return 0.0
        # IdleSinceHint 是单调时钟的微秒数（CLOCK_MONOTONIC）
        try:
            since_us = int(since)
        except ValueError:
            return None
        now_us = time.clock_gettime(time.CLOCK_MONOTONIC) * 1_000_000
        return max(0.0, (now_us - since_us) / 1_000_000)
    except Exception as e:
        _log(f"读取会话空闲时间失败：{e}")
        return None


def poweroff() -> bool:
    """执行关机。返回是否成功发起。

    先试免密的 `systemctl poweroff`（SteamOS 上普通用户走 polkit 一般直接放行）。
    失败就退回 `loginctl poweroff`，再不行再试 `poweroff`。
    这里不做 sudo 提权：关机时人可能已经睡着了，没人输密码，
    所以宁可不关，也不能弹一个没人理的授权框。
    """
    for cmd in (["systemctl", "poweroff"], ["loginctl", "poweroff"], ["poweroff"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                               env=_session_env())
        except Exception as e:
            _log(f"{cmd[0]} 关机调用失败：{e}")
            continue
        if r.returncode == 0:
            _log("睡眠模式：已发起关机")
            return True
        _log(f"{cmd[0]} 关机被拒绝（{r.returncode}）："
             f"{((r.stderr or r.stdout) or '').strip()[:200]}")
    _log("睡眠模式：所有关机方式都失败，本次放弃")
    return False


class SleepMode:
    """闲置 + 音乐停 的关机守护线程。

    对外只有 apply(cfg) / status() / cancel()，其余都在线程里。
    状态对前端暴露，用来在眼睛页显示倒计时、在主界面显示剩余时间。
    """

    def __init__(self) -> None:
        self._cfg = dict(DEFAULTS)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # 运行状态（给前端看）：idle_since = 两个条件同时成立的起始时刻
        self._quiet_since: float | None = None
        self._countdown_until: float | None = None
        self._playing = False
        self._idle_s = 0.0
        self._started = False

    # ---------- 对外 ----------

    def apply(self, sleep_cfg: dict | None) -> None:
        """配置热更新。开关一打开就起线程，关掉就停线程。"""
        new = merge_cfg(sleep_cfg)
        with self._lock:
            changed = new != self._cfg
            self._cfg = new
        if not changed and self._started:
            return
        self._reset()
        if new["enabled"]:
            self._ensure_thread()
            _log(f"睡眠模式已开启：闲置且音乐停满 {new['idle_minutes']} 分钟后关机")
        else:
            _log("睡眠模式已关闭")

    def status(self) -> dict:
        """给 /api/status 用：开关、剩余时间、倒计时。"""
        with self._lock:
            cfg = dict(self._cfg)
            quiet = self._quiet_since
            until = self._countdown_until
        now = time.monotonic()
        remain = None
        total = cfg["idle_minutes"] * 60
        if quiet is not None:
            remain = max(0, int(total - (now - quiet)))
        return {
            "enabled": bool(cfg["enabled"]),
            "idle_minutes": cfg["idle_minutes"],
            "countdown_sec": cfg["countdown_sec"],
            # 距离关机还剩多少秒；null = 还没开始计时（音乐在放或有人在使用）
            "remain_sec": remain,
            "playing": self._playing,
            "idle_sec": int(self._idle_s),
            # 倒计时进行中才有值：还剩几秒关机
            "countdown_left": (None if until is None
                               else max(0, int(round(until - now)))),
        }

    def cancel(self) -> bool:
        """取消当前倒计时。眼睛页在任何输入时调用，返回是否真的取消了。"""
        with self._lock:
            had = self._countdown_until is not None
            self._countdown_until = None
            self._quiet_since = None
        return had

    # ---------- 线程 ----------

    def _reset(self) -> None:
        with self._lock:
            self._quiet_since = None
            self._countdown_until = None

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sleep-mode",
                                        daemon=True)
        self._thread.start()
        self._started = True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:      # 守护线程绝不能因为异常退出
                _log(f"睡眠模式检查出错：{e}")
            # 倒计时中要勤快点，方便及时响应取消
            with self._lock:
                in_countdown = self._countdown_until is not None
            self._wake.wait(timeout=COUNTDOWN_POLL if in_countdown else POLL_INTERVAL)
            self._wake.clear()

    def _tick(self) -> None:
        with self._lock:
            cfg = dict(self._cfg)
            until = self._countdown_until
        if not cfg["enabled"]:
            return
        now = time.monotonic()

        # 倒计时进行中：只需要判断有没有被取消，以及到点没到点
        if until is not None:
            if now >= until:
                _log("睡眠模式：倒计时结束，执行关机")
                poweroff()
                self._reset()
            return

        # 两个条件：音乐停 + 会话空闲
        try:
            playing = media.playing()
        except Exception:
            playing = False
        idle = idle_seconds()
        # 判不出空闲就当作「有人在用」，宁可不关，别把人正在用的机器关掉
        idle_ok = idle is not None and idle >= cfg["idle_minutes"] * 60
        with self._lock:
            self._playing = playing
            self._idle_s = idle or 0.0

        if playing or not idle_ok:
            self._reset()
            return

        with self._lock:
            if self._quiet_since is None:
                self._quiet_since = now
                _log("睡眠模式：音乐已停且无人使用，开始计时")
            quiet = self._quiet_since

        if now - quiet >= cfg["idle_minutes"] * 60:
            with self._lock:
                self._countdown_until = now + cfg["countdown_sec"]
            _log(f"睡眠模式：{cfg['countdown_sec']} 秒后关机，"
                 "动一下鼠标或键盘可以取消")


service = SleepMode()
