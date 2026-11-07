"""媒体播控：直接调 Decky Music 的 MPRIS2 服务。

Decky Music 的 `bin/player` 进程自己注册 MPRIS2（插件 README「系统媒体控制」一节：
蓝牙耳机按键、桌面媒体控件都走 MPRIS2，外部动作由它上送内部 bridge 处理）。
所以我们只调这个标准接口，不碰插件私有的 `player.sock` 帧协议——那是插件内部
实现，socket 名字里还带实例号，随版本变化。

安全边界（不允许变成任意 shell）：

* 只有一张固定动作白名单（`_METHODS` + `VOLUME_ACTIONS`），未知动作直接拒绝；
* 动作不带任何外部参数：音量步进、duck 比例都是模块内常量；
* 用户说的话只在 `match_intent()` 里做关键词匹配，匹配结果是动作名，
  永远不会被拼进命令行。

降级：没有 busctl / 没有播放器 / 命令失败都抛 `MediaError`，`reason` 是给人看
的中文原因，`speech` 是一句可以直接念出来的短提示。`duck()` / `restore()`
永远不抛异常：压不下音量就当作没压，不能影响唤醒主流程。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
CMD_TIMEOUT = 3.0

# 音量加减步进；duck 压到原来的这个比例
VOLUME_STEP = 0.1
DUCK_RATIO = 0.25

# 会改动音量状态的动作：执行前必须先恢复 duck，否则「恢复原音量」会把
# 用户刚要求的加减冲掉
VOLUME_ACTIONS = ("volume_up", "volume_down", "mute", "unmute")

# 固定动作 → MPRIS 方法名
_METHODS = {
    "next": "Next",
    "previous": "Previous",
    "pause": "Pause",
    "play": "Play",
    "stop": "Stop",
}

# 动作 → 一句极短语音反馈
FEEDBACK = {
    "next": "切了",
    "previous": "回去了",
    "pause": "暂停了",
    "play": "继续",
    "stop": "停了",
    "volume_up": "大声了",
    "volume_down": "小声了",
    "mute": "静音了",
    "unmute": "开声音了",
}

# 口头指令 → 动作。顺序决定优先级：先看「停止播放」，否则会被「播放」抢走；
# 「取消静音」必须排在「静音」前面。
_RULES = (
    ("stop", ("停止播放", "停止", "停掉", "别放了", "stop")),
    ("pause", ("暂停", "停一下", "先停", "pause")),
    ("next", ("下一首", "下一个", "下一曲", "换一首", "换首歌", "换一个", "切歌", "切一首", "next")),
    ("previous", ("上一首", "上一个", "上一曲", "前一首", "往回切", "previous", "prev")),
    ("unmute", ("取消静音", "解除静音", "打开声音", "恢复声音", "unmute")),
    ("mute", ("静音", "别出声", "mute")),
    ("volume_down", ("小点声", "小声点", "声音小点", "音量调小", "调小音量",
                     "音量小一点", "小声", "quieter", "volume down")),
    ("volume_up", ("大点声", "大声点", "声音大点", "音量调大", "调大音量",
                   "音量大一点", "大声", "louder", "volume up")),
    ("play", ("继续播放", "继续", "接着放", "播放", "开始播放", "resume", "play")),
)

# 太长的句子不当作口令：避免「下一首歌唱的是什么」这类问题被误执行
MAX_INTENT_CHARS = 20
# 带疑问词的句子一律交给模型：口令是祈使句，问句不该被当成指令
_QUESTION_RE = re.compile(r"什么|为什么|怎么|哪|吗|多少|谁|是不是")
_PUNCT_RE = re.compile(r"[\s，。！？、,.!?；;：:~“”\"'（）()<>《》…\-]+")

_lock = threading.RLock()
_duck_prev: float | None = None
_duck_value: float | None = None
_muted_prev: float | None = None


class MediaError(RuntimeError):
    """播控失败：reason 给人看，speech 直接念。"""

    def __init__(self, reason: str, speech: str = "操作失败"):
        super().__init__(reason)
        self.reason = reason
        self.speech = speech


def _env() -> dict:
    """继承后端环境；缺 DBUS 地址时按当前用户补上。"""
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        try:
            path = f"/run/user/{os.getuid()}/bus"
        except AttributeError:
            return env
        if os.path.exists(path):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={path}"
    return env


def _run(cmd: list[str], timeout: float = CMD_TIMEOUT):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=_env())
    except FileNotFoundError as exc:
        raise MediaError("这台机器没有 busctl，无法控制播放器", "控制不了") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("播放器没有响应（命令超时）", "没反应") from exc
    except OSError as exc:
        raise MediaError(f"调用播放器失败：{exc}", "操作失败") from exc


def _busctl(*args: str) -> str:
    proc = _run(["busctl", "--user", *args])
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        last = text.splitlines()[-1] if text else ""
        raise MediaError(f"播放器命令失败：{last or '未知原因'}", "操作失败")
    return proc.stdout or ""


def _service_name() -> str:
    """找当前 MPRIS 服务。优先 Decky Music，其次任何注册了 MPRIS 的播放器。"""
    out = _busctl("list", "--no-pager", "--no-legend")
    names = [line.split()[0] for line in out.splitlines()
             if line.split() and line.split()[0].startswith(MPRIS_PREFIX)]
    if not names:
        raise MediaError("没找到播放器（Decky Music 的 MPRIS 服务不在）", "没有播放器")
    for name in names:
        if "decky_music" in name:
            return name
    return names[0]


def available() -> bool:
    try:
        _service_name()
        return True
    except MediaError:
        return False


def _volume(service: str) -> float:
    out = _busctl("get-property", service, PLAYER_PATH, PLAYER_IFACE, "Volume")
    try:
        return max(0.0, min(1.0, float(out.split()[-1])))
    except (IndexError, ValueError) as exc:
        raise MediaError("读不到播放器音量", "操作失败") from exc


def _set_volume(service: str, value: float) -> None:
    value = max(0.0, min(1.0, round(float(value), 3)))
    _busctl("set-property", service, PLAYER_PATH, PLAYER_IFACE,
            "Volume", "d", f"{value:.3f}")


def _playback_status(service: str) -> str:
    out = _busctl("get-property", service, PLAYER_PATH, PLAYER_IFACE,
                  "PlaybackStatus")
    return out.split('"')[1] if '"' in out else out.strip()


def execute(action: str) -> None:
    """执行一个固定动作。不在白名单里直接拒绝，不拼任何命令。"""
    if action not in _METHODS and action not in VOLUME_ACTIONS:
        raise MediaError(f"不支持的媒体动作：{action}", "操作失败")
    with _lock:
        service = _service_name()
        if action in _METHODS:
            _busctl("call", service, PLAYER_PATH, PLAYER_IFACE, _METHODS[action])
            return
        global _muted_prev
        if action == "volume_up":
            _set_volume(service, _volume(service) + VOLUME_STEP)
            _muted_prev = None
        elif action == "volume_down":
            _set_volume(service, _volume(service) - VOLUME_STEP)
            _muted_prev = None
        elif action == "mute":
            # 已经是 0 就记一个默认恢复音量，免得「取消静音」没有可回的值
            level = _volume(service)
            _muted_prev = level if level > 0.01 else 1.0
            _set_volume(service, 0.0)
        elif action == "unmute":
            _set_volume(service, _muted_prev if _muted_prev is not None else 1.0)
            _muted_prev = None


def duck() -> bool:
    """正在播放时把音乐压到 25%。没播放器/没在播/失败都返回 False，不抛异常。"""
    global _duck_prev, _duck_value
    with _lock:
        if _duck_prev is not None:
            return True
        try:
            service = _service_name()
            if _playback_status(service) != "Playing":
                return False
            volume = _volume(service)
        except MediaError:
            return False
        target = round(volume * DUCK_RATIO, 3)
        if target >= volume - 0.001:
            return False
        try:
            _set_volume(service, target)
        except MediaError:
            return False
        _duck_prev, _duck_value = volume, target
        return True


def restore() -> None:
    """恢复 duck 前的音量。

    只回写「当前值仍是我们压低后的值」的情况：用户中途自己调了音量（或用了
    「音量调大」口令），就保持用户的新值，不被恢复动作覆盖。
    """
    global _duck_prev, _duck_value
    with _lock:
        prev, ducked = _duck_prev, _duck_value
        _duck_prev = _duck_value = None
        if prev is None:
            return
        try:
            service = _service_name()
            current = _volume(service)
        except MediaError:
            return
        if ducked is None or abs(current - ducked) > 0.02:
            return
        try:
            _set_volume(service, prev)
        except MediaError:
            pass


def match_intent(text: str) -> tuple[str, str] | None:
    """本地口令匹配：命中返回 (动作, 反馈词)，否则 None。

    只做关键词包含，不调模型；整句过长直接不认，交给对话链路。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    norm = _PUNCT_RE.sub("", raw).lower()
    if not norm or len(norm) > MAX_INTENT_CHARS:
        return None
    if _QUESTION_RE.search(norm):
        return None
    for action, words in _RULES:
        if any(word in norm for word in words):
            return action, FEEDBACK[action]
    return None


def _reset() -> None:
    """测试用：清掉 duck/静音状态，避免用例之间互相影响。"""
    global _duck_prev, _duck_value, _muted_prev
    with _lock:
        _duck_prev = _duck_value = _muted_prev = None
