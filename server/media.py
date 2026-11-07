"""媒体播控：Decky Music 的两个官方接口。

有两个面，都只用公开、稳定的那一层：

1. **MPRIS2**（`server/media.py` 原有部分）：Decky Music 的 `bin/player` 自己注册
   MPRIS2（插件 README「系统媒体控制」一节：蓝牙耳机按键、桌面媒体控件都走它）。
   切歌、暂停、停止、音量、duck 都走这里，不碰插件私有的 `player.sock` 帧协议。
2. **Decky Loader 的本地 WS RPC**：点播要搜曲库、要读队列，MPRIS 不提供这些。
   Decky Loader 在 `127.0.0.1:1337` 暴露 `loader/call_plugin_method`，可以按
   `search_songs / get_playback / get_queue / play_queue / queue_insert_next /
   queue_play` 调 Decky Music 自己声明的方法（插件 `main.py` 的 `__getattr__`
   白名单只转发这几个）。注意：每建一条新 WS，Loader 会 close 掉前端原有的那条
   （前端随后自动重连），所以点播时 QAM 可能闪一下。

安全边界（不允许变成任意 shell）：

* 动作只有一张固定白名单（`_METHODS` + `VOLUME_ACTIONS`），未知动作直接拒绝；
* 动作不带任何外部参数：音量步进、duck 比例都是模块内常量；
* RPC 方法名同样来自固定白名单（`_RPC_METHODS`），用户说的话永远不会被拼进
  shell 命令或任意方法名；
* 曲库搜索的关键词只作为 RPC 数据参数传递，且有长度上限。

降级：没有 busctl / 没有播放器 / 插件没登录 / 命令失败都抛 `MediaError`，
`reason` 是给人看的中文原因，`speech` 是一句可以直接念出来的短提示。
`duck()` / `restore()` 永远不抛异常：压不下音量就当作没压，不能影响唤醒主流程。
"""
from __future__ import annotations

import difflib
import itertools
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from . import devlog

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
CMD_TIMEOUT = 3.0

# 音量加减步进；duck 压到原来的这个比例
VOLUME_STEP = 0.1
DUCK_RATIO = 0.25

# Decky Loader 本地 RPC
LOADER_HOST = "127.0.0.1"
LOADER_PORT = 1337
RPC_TIMEOUT = 6.0
# Decky Loader 会在同一个 id 上回放上一次的响应：复用 id 会拿到旧结果，
# 必须每次请求用唯一 id，并在读取循环里校验 id 与本次请求一致。
_rpc_ids = itertools.count(1)
PLUGIN_NAME = "Decky Music"
# 只允许调这几个方法：点播链路需要的最小集合，避免变成任意插件调用
_RPC_METHODS = {
    "search_songs",
    "get_playback",
    "get_queue",
    "play_queue",
    "queue_insert_next",
    "queue_play",
}

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
# 「播放 / 继续」放在最后：它同时是点播的动词（「播放稻香」要走点播），
# 只有当整句抽不出歌名时才落到这里。
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

# 随机播放的口头说法
_RANDOM_WORDS = ("随机放", "随机播放", "随机来", "随便放", "随便来", "随便点",
                 "随便听", "随便播", "随便挑")

# 点播动词 + 可选量词
_SONG_LEAD = r"(?:放|播放|播|来|听|点歌|点)"
_SONG_QTY = r"(?:一首|一曲|一首歌|首歌|首|曲|个|一个|点|点个|一下)?"
# 抽出来的「关键词」若只是这些泛称，说明用户没点具体的歌，交给对话链路
_SONG_STOP = {"歌", "歌曲", "歌儿", "音乐", "歌吧", "首歌", "首", "曲", "点", "一点",
              "点歌", "一首", "一下", "那个", "这个", "什么", "什么歌", "啥",
              "这首歌", "那首歌", "这首", "那首", "了", "的", "个",
              "放", "播", "来", "听", "播放", "继续"}

# 太长的句子不当作口令：避免「下一首歌唱的是什么」这类问题被误执行
MAX_INTENT_CHARS = 20
MAX_QUERY_CHARS = 24
# 带疑问词的句子一律交给模型：口令是祈使句，问句不该被当成指令
_QUESTION_RE = re.compile(r"什么|为什么|怎么|哪|吗|多少|谁|是不是|好不好")
_PUNCT_RE = re.compile(r"[\s，。！？、,.!?；;：:~“”\"'（）()<>《》…\-]+")
# 抽歌名时保留《》，只去空白与常见标点
_SONG_STRIP_RE = re.compile(r"[\s，。！？、,.!?；;：:~“”\"'（）()<>…\-]+")
_TRAIL_RE = re.compile(r"(?:吧|啊|呀|呢|呗|啦|哦|喔|嘛)$")

# 匹配阈值：0.62 约等于「歌名相似度 78%」或「歌手完全一致」；
# 前两名差距小于 0.06 视为歧义，先确认一次再放。
MATCH_THRESHOLD = 0.62
AMBIGUITY_GAP = 0.06

_FIELD_WEIGHTS = {"name": 0.6, "singer": 0.25, "album": 0.15}

_SEARCH_ERRORS = {
    "not_login": "Decky Music 没登录",
    "upstream_timeout": "曲库查询超时",
}

_YES_RE = re.compile(r"^(对|是|是的|嗯|好|可以|没错|就这个|就它|行|ok|yes)")
_NO_RE = re.compile(r"^(不是|不对|不|错了|换|不要|no)")

_lock = threading.RLock()
_duck_prev: float | None = None
_duck_value: float | None = None
_muted_prev: float | None = None
_py_cache: dict[str, str] = {}


class MediaError(RuntimeError):
    """播控失败：reason 给人看，speech 直接念。"""

    def __init__(self, reason: str, speech: str = "操作失败"):
        super().__init__(reason)
        self.reason = reason
        self.speech = speech


@dataclass(frozen=True)
class Intent:
    """本地口令的分流结果。

    kind：
    * ``transport`` —— 固定播控动作，`action` 是白名单里的动作名；
    * ``song`` —— 点播，`query` 是抽出来的歌名/歌手/专辑关键词；
    * ``random`` —— 随机放一首。
    """

    kind: str
    action: str = ""
    feedback: str = ""
    query: str = ""


@dataclass(frozen=True)
class SongPlan:
    """点播计划：搜到的歌 + 该怎么说。

    status：
    * ``ok`` —— 唯一高分候选，直接放；
    * ``ask`` —— 前两名太接近，先问一次「是 XXX 那首吗」；
    * ``miss`` —— 没找到，`speech` 直接念。
    """

    status: str
    speech: str = ""
    item: dict | None = field(default=None, compare=False)
    name: str = ""
    singer: str = ""

    @property
    def label(self) -> str:
        return _label(self.name, self.singer)


# ---------- MPRIS（原有播控面） ----------


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


# ---------- Decky Loader 本地 RPC（点播面） ----------


def _loader_token() -> str:
    """`GET /auth/token` 返回纯文本 token；本地回环、无鉴权。"""
    url = f"http://{LOADER_HOST}:{LOADER_PORT}/auth/token"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            token = resp.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        raise MediaError(f"连不上 Decky Loader：{exc}", "点播没准备好") from exc
    if not token:
        raise MediaError("Decky Loader 没给鉴权 token", "点播没准备好")
    return token


def _next_rpc_id() -> int:
    """每次请求取唯一 id（Loader 复用 id 会把上一次的响应当成这一次的）。"""
    return next(_rpc_ids)


def _brief(value) -> str:
    """RPC 结果的日志摘要：列表只记条数，字典只记链路相关字段。"""
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        parts = []
        if "ok" in value:
            parts.append(f"ok={value.get('ok')}")
        if isinstance(value.get("songs"), list):
            parts.append(f"songs={len(value['songs'])}")
        for key in ("index", "playing", "queue_mode", "player_failed"):
            if key in value:
                parts.append(f"{key}={value.get(key)}")
        if not parts:
            parts.append(f"keys={len(value)}")
        return ",".join(parts)
    return str(value)[:120]


def _rpc_exchange(token: str, req_id: int, payload: str, method: str):
    """在一条新 WS 上发请求并等**同一个 id** 的应答。"""
    from websockets.sync.client import connect

    with connect(f"ws://{LOADER_HOST}:{LOADER_PORT}/ws?auth={token}",
                 open_timeout=RPC_TIMEOUT, close_timeout=1) as ws:
        ws.send(payload)
        deadline = time.monotonic() + RPC_TIMEOUT
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise MediaError(f"Decky Music 没有响应（{method} 超时）", "没反应")
            raw = ws.recv(timeout=remain)
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get("id") != req_id:
                # id 不是本次请求的：Loader 串出来的旧响应，丢掉继续等
                continue
            kind = msg.get("type")
            if kind == 1:
                return msg.get("result")
            if kind == 2:
                raise MediaError("Decky Music 插件已停止", "点播没准备好")
            err = msg.get("error") or {}
            text = (err.get("message") if isinstance(err, dict) else "") \
                or (err.get("name") if isinstance(err, dict) else "") \
                or "未知错误"
            raise MediaError(f"Decky Music 调用失败（{method}）：{text}",
                             "查不到" if method == "search_songs" else "操作失败")


def _rpc(method: str, *args):
    """调 Decky Music 的插件方法，返回它的原样结果。

    只允许 `_RPC_METHODS` 里的方法；`args` 只能是字符串/数字/列表/字典这类
    JSON 数据，永远不会进 shell。
    """
    method = str(method or "")
    if method not in _RPC_METHODS:
        raise MediaError(f"不允许的插件方法：{method}", "操作失败")
    token = _loader_token()
    try:
        from websockets.sync.client import connect  # noqa: F401
    except Exception as exc:
        raise MediaError("缺少 websockets 库，无法调用 Decky Music",
                         "点播没准备好") from exc
    req_id = _next_rpc_id()
    payload = json.dumps(
        {"type": 0, "id": req_id, "route": "loader/call_plugin_method",
         "args": [PLUGIN_NAME, method, *args]},
        ensure_ascii=False)
    started = time.monotonic()
    try:
        result = _rpc_exchange(token, req_id, payload, method)
    except MediaError as exc:
        devlog.log("media.rpc", method=method, id=req_id,
                   elapsed_ms=int((time.monotonic() - started) * 1000),
                   error=getattr(exc, "reason", "") or str(exc))
        raise
    except TimeoutError as exc:
        devlog.log("media.rpc", method=method, id=req_id,
                   elapsed_ms=int((time.monotonic() - started) * 1000),
                   error="timeout")
        raise MediaError(f"Decky Music 没有响应（{method} 超时）", "没反应") from exc
    except Exception as exc:
        devlog.log("media.rpc", method=method, id=req_id,
                   elapsed_ms=int((time.monotonic() - started) * 1000),
                   error=str(exc))
        raise MediaError(f"调用 Decky Music 失败：{exc}", "点播没准备好") from exc
    devlog.log("media.rpc", method=method, id=req_id,
               elapsed_ms=int((time.monotonic() - started) * 1000),
               result=_brief(result))
    return result


def search_songs(keyword: str, page: int = 0) -> list[dict]:
    """用插件自带的 provider 搜歌（没有本地曲库，Deck 上也没有音乐文件）。"""
    keyword = (keyword or "").strip()
    if len(keyword) < 2 or len(keyword) > 30:
        raise MediaError("搜索关键词长度不合适", "没听清")
    result = _rpc("search_songs", keyword, int(page))
    if not isinstance(result, dict):
        raise MediaError("曲库返回格式不对", "查不到")
    if result.get("ok") is False:
        code = str(result.get("error") or "")
        raise MediaError(
            f"曲库查询失败：{_SEARCH_ERRORS.get(code, code or '未知原因')}",
            "查不到")
    songs = result.get("songs") or []
    return [s for s in songs if isinstance(s, dict)]


# ---------- 打分：歌名 / 歌手 / 专辑 ----------


def _norm(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or "")).lower()


def _pinyin(text: str) -> str:
    """懒加载 pypinyin；没装就只做字形比较，不抛异常。"""
    key = text
    if key in _py_cache:
        return _py_cache[key]
    try:
        from pypinyin import lazy_pinyin
        value = "".join(lazy_pinyin(text))
    except Exception:
        value = ""
    _py_cache[key] = value
    return value


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    # 包含关系直接算全中（单字太容易误伤，要求两边都不短于 2）
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _field_sim(query: str, value: str) -> float:
    """同一段关键词与一个字段的相似度：字形 + 拼音取较大值。

    拼音比较就是中文错别字/同音字的容错（「稻香 / 道香」「周杰伦 / 周杰论」），
    英文因为统一转小写、去空格，也天然不敏感。
    """
    qn = _norm(query)
    vn = _norm(value)
    if not qn or not vn:
        return 0.0
    return max(_sim(qn, vn), _sim(_pinyin(qn), _pinyin(vn)))


def score_song(query: str, song: dict) -> float:
    """0–1 的匹配分：标题 0.6 / 歌手 0.25 / 专辑 0.15。

    只报歌手名（「来点周杰伦」）时加权只有 0.25，永远够不到阈值，所以再加
    一条「单字段置信度 × 0.8」的下限：任一个字段几乎完全命中就算 0.8。
    """
    sims = {
        "name": _field_sim(query, song.get("name") or ""),
        "singer": _field_sim(query, song.get("singer") or ""),
        "album": _field_sim(query, song.get("album") or ""),
    }
    weighted = sum(_FIELD_WEIGHTS[k] * v for k, v in sims.items())
    single = max(sims.values()) * 0.8
    return round(max(weighted, single), 4)


def _same_title(a: str, b: str) -> bool:
    """同名/包含关系视为同一首歌的不同版本（翻唱、女声版、童声版）。"""
    if not a or not b:
        return False
    return a == b or (len(a) >= 2 and len(b) >= 2 and (a in b or b in a))


def _queue_item(song: dict) -> dict:
    """搜索项 → 队列项。队列用 `id`，搜索用 `mid`，两者同源（数值字符串）。"""
    sid = str(song.get("mid") or song.get("id") or "").strip()
    if not sid:
        raise MediaError("这首歌缺 id，放不了", "播放失败")
    return {
        "id": sid,
        "media_mid": str(song.get("media_mid") or sid),
        "name": str(song.get("name") or ""),
        "singer": str(song.get("singer") or ""),
        "cover": str(song.get("cover") or ""),
        "duration": float(song.get("duration") or 0),
        "max_quality": str(song.get("max_quality") or "none"),
    }


def _label(name: str, singer: str, limit: int = 18) -> str:
    text = f"{singer}的{name}" if singer and name else (name or singer or "")
    text = text.strip()
    return text[:limit] if text else "这首歌"


def _search_candidates(query: str) -> list[dict]:
    """先整句搜；整句没结果再按「的」/空格拆开搜，合并去重。"""
    songs = search_songs(query)
    if songs:
        return songs
    merged: list[dict] = []
    seen = set()
    for token in re.split(r"[\s的]+", query):
        token = token.strip()
        if len(token) < 2 or token == query:
            continue
        try:
            part = search_songs(token)
        except MediaError:
            continue
        for song in part:
            key = (str(song.get("name") or ""), str(song.get("singer") or ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(song)
    return merged


def resolve_song(query: str) -> SongPlan:
    """把一句点播关键词解析成可执行的计划（只搜不播）。"""
    query = (query or "").strip()
    if not query:
        return SongPlan("miss", "没听清要放哪首")
    candidates = _search_candidates(query)
    devlog.log("media.song.search", query=query, count=len(candidates),
               threshold=MATCH_THRESHOLD)
    if not candidates:
        devlog.log("media.song.resolve", query=query, status="miss", count=0,
                   top_score=0.0, name="", singer="")
        return SongPlan("miss", f"没找到{query[:12]}")
    ranked: list[tuple[float, dict]] = []
    seen = set()
    for song in candidates:
        key = (str(song.get("name") or ""), str(song.get("singer") or ""))
        if key in seen:
            continue
        seen.add(key)
        ranked.append((score_song(query, song), song))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top = ranked[0]
    if top_score < MATCH_THRESHOLD:
        devlog.log("media.song.resolve", query=query, status="miss",
                   count=len(ranked), top_score=top_score,
                   name=str(top.get("name") or ""),
                   singer=str(top.get("singer") or ""))
        return SongPlan("miss", f"没找到{query[:12]}")
    name = str(top.get("name") or "")
    singer = str(top.get("singer") or "")
    item = _queue_item(top)
    # 只有「不同歌名」才算真歧义；同名翻唱（多个「稻香」）直接取最高分，
    # 反馈里带歌手名让用户听出来放的是哪一版，不反复追问。
    top_key = _norm(name)
    rival = 0.0
    for score, song in ranked[1:]:
        if not _same_title(top_key, _norm(str(song.get("name") or ""))):
            rival = score
            break
    status = "ok"
    if rival >= MATCH_THRESHOLD and top_score - rival < AMBIGUITY_GAP:
        status = "ask"
        speech = f"是{_label(name, singer)}吗"
    else:
        speech = f"放{_label(name, singer)}"
    devlog.log("media.song.resolve", query=query, status=status,
               count=len(ranked), top_score=top_score, rival_score=rival,
               threshold=MATCH_THRESHOLD, name=name, singer=singer)
    return SongPlan(status, speech, item, name, singer)


def resolve_random() -> SongPlan:
    """从当前队列里随机挑一首（排除正在放的那首）。"""
    playback = _rpc("get_playback")
    if not isinstance(playback, dict):
        playback = {}
    index = playback.get("index")
    queue = _rpc("get_queue")
    items = (queue.get("items") if isinstance(queue, dict) else None) or []
    pool = [it for i, it in enumerate(items)
            if i != index and isinstance(it, dict) and str(it.get("id") or "")]
    if not pool:
        raise MediaError("曲库是空的（Decky Music 队列里没有可放的歌）", "曲库空的")
    import random
    chosen = random.choice(pool)
    name = str(chosen.get("name") or "")
    singer = str(chosen.get("singer") or "")
    devlog.log("media.song.random", pool=len(pool), id=str(chosen.get("id")),
               name=name, singer=singer)
    item = {
        "id": str(chosen.get("id")),
        "media_mid": str(chosen.get("media_mid") or chosen.get("id")),
        "name": name,
        "singer": singer,
        "cover": str(chosen.get("cover") or ""),
        "duration": float(chosen.get("duration") or 0),
        "max_quality": str(chosen.get("max_quality") or "none"),
    }
    return SongPlan("ok", f"放{_label(name, singer)}", item, name, singer)


def _verify_playing(item: dict) -> None:
    """回读播放态：必须是目标曲目且在播放，否则算「没放出来」。

    Loader 的 RPC 之前会串响应，导致「调用成功」变成假成功；这里用插件自己
    的真相源（`get_playback` 的 current.id + playing）确认真的开播了。
    """
    try:
        playback = _rpc("get_playback")
    except MediaError as exc:
        raise MediaError(f"放歌后读不到播放状态：{exc.reason}", "没放出来") from exc
    if not isinstance(playback, dict):
        raise MediaError("放歌后读不到播放状态", "没放出来")
    current = playback.get("current")
    current_id = str((current or {}).get("id") or "") if isinstance(current, dict) else ""
    ok = current_id == str(item.get("id")) and playback.get("playing") is True
    devlog.log("media.song.verify", want=str(item.get("id")), current=current_id,
               playing=playback.get("playing"),
               player_failed=playback.get("player_failed"), ok=ok)
    if not ok:
        raise MediaError(
            f"歌曲没放出来（current={current_id or '无'} playing={playback.get('playing')}）",
            "没放出来")


def play_plan(item: dict | None) -> None:
    """放一首歌，并回读确认真的开始播放。

    队列是普通模式且当前有位置：插到当前下一首再跳过去，用户原来的队列保留；
    电台模式（插入会被插件静默忽略）或空队列：直接用它开一个新队列。
    """
    if not isinstance(item, dict) or not str(item.get("id") or "").strip():
        raise MediaError("点播计划不完整", "播放失败")
    with _lock:
        try:
            playback = _rpc("get_playback")
        except MediaError:
            playback = {}
        index = playback.get("index") if isinstance(playback, dict) else None
        mode = str(playback.get("queue_mode") or "") if isinstance(playback, dict) else ""
        route = "rebuild"
        if mode == "normal" and isinstance(index, int) and index >= 0:
            try:
                _rpc("queue_insert_next", item)
                _rpc("queue_play", index + 1)
                route = "queue"
            except MediaError as exc:
                devlog.log("media.song.queue.fallback", error=str(exc))
        if route != "queue":
            _rpc("play_queue", [item], 0)
        devlog.log("media.song.play", id=str(item.get("id")),
                   name=str(item.get("name") or ""), route=route)
        _verify_playing(item)


# ---------- 口令匹配 ----------


def _song_intent(text: str) -> Intent | None:
    """从「放一首 X / 放 X 的《Y》/ 来点 X」里抽关键词。"""
    raw = _TRAIL_RE.sub("", _SONG_STRIP_RE.sub("", text or ""))
    if not raw:
        return None

    def build(*parts: str) -> Intent | None:
        words = [p.strip() for p in parts if p and p.strip()]
        words = [w for w in words if w not in _SONG_STOP]
        query = " ".join(words).strip()
        if not query or query in _SONG_STOP:
            return None
        if len(query) > MAX_QUERY_CHARS:
            return None
        return Intent("song", feedback="好", query=query)

    # 1) 放 X 的《Y》
    m = re.search(_SONG_LEAD + r"[^《]+?的《([^》]+)》", raw)
    if m:
        got = build(m.group(1))
        if got:
            return got
    # 2) 《Y》
    m = re.search(r"《([^》]+)》", raw)
    if m:
        got = build(m.group(1))
        if got:
            return got
    # 3) 放 X 的 Y
    m = re.search(_SONG_LEAD + _SONG_QTY + r"([^的]+?)的(.+)$", raw)
    if m:
        got = build(m.group(1), m.group(2))
        if got:
            return got
    # 4) 放 X
    m = re.search(_SONG_LEAD + _SONG_QTY + r"(.+)$", raw)
    if m:
        got = build(m.group(1))
        if got:
            return got
    # 5) X 的歌
    m = re.search(r"(.+?)的歌$", raw)
    if m:
        got = build(m.group(1))
        if got:
            return got
    return None


def match_intent(text: str) -> Intent | None:
    """本地口令匹配：命中返回 Intent，否则 None。

    只做正则/关键词匹配，不调模型；整句过长或带疑问词直接不认，交给对话链路。
    顺序：随机播放 → 点播 → 固定播控（「播放/继续」排最后，避免抢走「播放稻香」）。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    norm = _PUNCT_RE.sub("", raw).lower()
    if not norm or len(norm) > MAX_INTENT_CHARS:
        return None
    if _QUESTION_RE.search(norm):
        return None
    if any(word in norm for word in _RANDOM_WORDS):
        return Intent("random", feedback="好")
    # 先过「停止 / 暂停 / 切歌 / 音量」这类强口令，它们不会被点播动词抢走
    for action, words in _RULES:
        if action == "play":
            continue
        if any(word in norm for word in words):
            return Intent("transport", action=action, feedback=FEEDBACK[action])
    song = _song_intent(raw)
    if song is not None:
        return song
    # 「播放 / 继续」最后：整句抽不出歌名才算播控
    for action, words in _RULES:
        if action != "play":
            continue
        if any(word in norm for word in words):
            return Intent("transport", action=action, feedback=FEEDBACK[action])
    return None


def is_yes(text: str) -> bool | None:
    """歧义确认的回答：True=对，False=不是，None=没听出来（按普通输入继续）。"""
    norm = _PUNCT_RE.sub("", text or "").lower()
    if not norm:
        return None
    if _NO_RE.match(norm):
        return False
    if _YES_RE.match(norm):
        return True
    return None


def _reset() -> None:
    """测试用：清掉 duck/静音状态，避免用例之间互相影响。"""
    global _duck_prev, _duck_value, _muted_prev
    with _lock:
        _duck_prev = _duck_value = _muted_prev = None
    _py_cache.clear()
