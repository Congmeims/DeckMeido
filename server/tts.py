"""TTS 语音播报：两个发音人（机器人 = espeak-ng，女仆 = piper-plus つくよみちゃん）。

对外契约保持不变：`speak(text)` 在后台念、`stop()` 立刻掐断，新句子打断旧句子。
发音人由 `[tts] speaker` 单一决定，本机播报、试听、QQ 语音条都读同一个值：

- `robot`：Deck 自带的 espeak-ng；
- `maid`：piper-plus 本地神经网络模型。

女仆模型相关的实现说明：

- 模型不随发布包分发，首次使用时从 hf-mirror.com / huggingface.co 下载到
  `<数据根>/models/tts/tsukuyomi/`；
- 懒加载：第一次真正播报才 import piper_plus 并加载 ONNX，空闲一段时间后释放；
- ONNX 推理不是线程安全的，所有合成调用用同一把锁串行，并放到线程里跑，
  不阻塞事件循环；
- 运行时未装、模型缺失或加载失败时自动退回 espeak-ng，`status()` 里
  能看出当前实际用哪个引擎、为什么降级。

音声：つくよみちゃんコーパス（CV.夢前黎）。合成产物只在本机播放，不再分发。
署名与使用条件见设置页「语音回复」卡片下方，条款：
https://tyc.rei-yumesaki.net/material/corpus/#terms3
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from . import devlog, netmode, paths

# ---------- 发音人 ----------
# 只保留两个发音人，界面与配置都以这两个 id 为准：
#   robot = Deck 自带的 espeak-ng；maid = piper-plus つくよみちゃん 本地模型
SPEAKER_ROBOT = "robot"
SPEAKER_MAID = "maid"
DEFAULT_SPEAKER = SPEAKER_MAID
SPEAKERS = (
    {"id": SPEAKER_ROBOT, "label": "机器人"},
    {"id": SPEAKER_MAID, "label": "月读酱"},
)
# 试听接口的硬超时：合成 + 播放整体超过这个时间就判定播放端挂起
PREVIEW_TIMEOUT = 20.0
SPEAKER_LABELS = {item["id"]: item["label"] for item in SPEAKERS}

# 试听默认句：按发音人选，各用单一语言，避免中英混排跨段
PREVIEW_TEXTS = {
    SPEAKER_ROBOT: "你好，我是机器人语音。",
    SPEAKER_MAID: "你好，我是月读酱。",
}

VOICE_ID = "tsukuyomi"
VOICE_LABEL = "つくよみちゃん（ja/zh/en/es/fr/pt）"
ONNX_NAME = "tsukuyomi-chan-6lang-fp16.onnx"
CONFIG_NAME = "config.json"
MODEL_FILES = (ONNX_NAME, CONFIG_NAME)
# 完整模型约 38MB；少于 30MB 基本就是没下全
MIN_ONNX_BYTES = 30 * 1024 * 1024
MODEL_REPO = "ayousanz/piper-plus-tsukuyomi-chan"
# 钉死到具体 commit，不用 resolve/main：上游换过文件（2026-11-04 重新上传），
# main 一挪，本地校验就会突然失败，而且用户完全无从判断。
MODEL_REVISION = "619ad26fcbd5635b3d70a27eac919f4c2ce892b1"
# 模型文件的真实 sha256，即 HF API 里该文件的 lfs.oid（响应头 x-linked-etag）。
# ⚠️ 不要用 xetHash（HF 的 Xet 存储哈希）：算法和粒度都不同，值也长得像 sha256，
#    之前就是把它当成了文件 sha256，导致每次下载都判「校验失败」。
# 只校验 onnx；config.json 很小且随上游更新，只做 JSON 解析校验。
ONNX_SHA256 = "5289e9b6eaf21080803b7fe1c4dc85b5491d4c216121207a41df18dd5f68e5d7"
ONNX_BYTES = 39652717
MIRROR_BASE = f"https://hf-mirror.com/{MODEL_REPO}/resolve/{MODEL_REVISION}"
HF_BASE = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}"

# 署名照抄官方模板与模型卡，不要翻译/改写：
#   模型卡「クレジット表記について（必須）」= ■Piper Plus:つくよみちゃん
#   つくよみちゃんコーパス利用規約第③类 = ■つくよみちゃんコーパス（CV.夢前黎）
ATTRIBUTION = "■つくよみちゃんコーパス（CV.夢前黎）"
ATTRIBUTION_URL = "https://tyc.rei-yumesaki.net/material/corpus/"

LANGUAGES = ("auto", "ja", "zh", "en", "es", "fr", "pt")
ESPEAK_VOICES = (
    ("cmn", "普通话（espeak-ng）"),
    ("en-us", "英语（espeak-ng）"),
    ("ja", "日语（espeak-ng）"),
)
# piper-plus 某一段合成失败时，用相应语种的 espeak 音色把这一段念出来，
# 而不是静默跳过（英文 G2P 数据缺失时以前就是整段没声）。
ESPEAK_LANG_VOICES = {
    "zh": "cmn", "ja": "ja", "en": "en-us",
    "es": "es", "fr": "fr", "pt": "pt",
}

# 模型下载与探测
UA = "DeckMeido-TTS/1.0"
PROBE_TIMEOUT = 6
RETRIES_PER_SOURCE = 3
MIN_FREE_BYTES = 80 * 1024 * 1024

# espeak-ng 的默认语速/上限
ESPEAK_SPEED = 170
MAX_CHARS = 300

# piper-plus 自带的英文 g2p_en 依赖 nltk 数据（cmudict / 词性标注器）。
# 安装阶段会下到 <数据根>/models/nltk_data，运行时按这个目录找；
# 数据缺失时 g2p_en 在 import 时会尝试 nltk.download，运行时联网会被
# SSRF 拦截，表现就是英文段整段合成失败。
NLTK_DATA_DIR_NAME = "nltk_data"


def nltk_data_dir() -> Path:
    return paths.data_dir() / "models" / NLTK_DATA_DIR_NAME


def english_data_ready() -> bool:
    """英文 G2P 需要的 nltk 数据是否齐全（只看目录，不 import nltk）"""
    try:
        root = nltk_data_dir()
        return (
            (root / "corpora" / "cmudict").exists()
            and (root / "taggers" / "averaged_perceptron_tagger_eng").exists()
        )
    except Exception:
        return False


def prepare_english_backend() -> None:
    """让 piper-plus 的英文前端只读本机数据，不在运行时联网。

    - NLTK_DATA 指到安装时下好的目录；
    - nltk.download 变成空操作：piper_plus 的 g2p_en 在 import 时无条件
      调用它，联网被拦就会 import 失败；
    - 数据目录仍缺失时，下面的空操作会让它继续走到 cmudict.dict()，
      再由 speak() 把「英文前端不可用」如实写进 last_error。
    """
    try:
        root = nltk_data_dir()
        if root.is_dir():
            os.environ["NLTK_DATA"] = str(root)
        import nltk

        if not getattr(nltk, "_deckmeido_offline", False):
            nltk.download = lambda *a, **k: True  # type: ignore[assignment]
            nltk._deckmeido_offline = True
    except Exception as exc:
        devlog.debug("tts.prepare_english_backend", error=repr(exc))
        pass


# 说话文本清洗：代码块、URL、markdown 标记、常见表情符号都不适合念出来
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MD_CHARS_RE = re.compile(r"[*_`#>\[\]|]")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
_SPACE_RE = re.compile(r"[ \t\u3000]+")
# 评分星号、对错勾叉这类「可见符号」先映射成可读词。它们大多落在表情符号
# 区间，如果直接删掉，播报里就会凭空少一个字符（例如 3★ 变成 3）。
_SPOKEN_SYMBOLS = {
    "★": "星", "☆": "星", "♥": "心", "♡": "心",
    "✓": "对", "✔": "对", "✗": "错", "✘": "错",
}
_SYMBOL_RE = re.compile("|".join(re.escape(ch) for ch in _SPOKEN_SYMBOLS))

# 分句与语种判定
_SENT_RE = re.compile(r"[^。！？!?；;：:\n]+[。！？!?；;：:\n]?")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# 拉丁「词」：英文单词、缩写、版本号、路径等（用于数词数，判断是否整句英文）
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-_.:/]*")
# 逐段切分：假名 / 汉字 / 数字 / 拉丁词（GPU、v0.7.0 这类整体保留）/ 其余单个字符。
# 「其余」包括标点、空白和其它脚本，只用于拼回原文，不单独送语音。
_SCRIPT_TOKEN_RE = re.compile(
    r"(?P<kana>[\u3040-\u30ff\u31f0-\u31ff]+)"
    r"|(?P<han>[\u3400-\u9fff\uf900-\ufaff]+)"
    r"|(?P<digit>\d+(?:[.,]\d+)*)"
    r"|(?P<latin>[A-Za-z][A-Za-z0-9'’\-_.:/]*)"
    r"|(?P<other>.)"
)

# 句子主语言的判定阈值（没有词典，只能按字符构成估）：
# 日语正常行文里假名（助词、送假名）通常占 CJK 的一半左右；中文句里嵌片假名
# 专名时，假名只占 5%～30%——实测「ミカ是崔尼蒂综合学园的学生会长…」7/27≈0.26、
# 「她的口头禅是「甘いものは正義です」…」7/26≈0.27。取 0.30 可以把这类中文
# 主语句判回 zh，同时给真日语句（通常 >0.5）留足余量。
KANA_JA_RATIO = 0.30
# 旧版按「拉丁字符占比」判整句英文（历史阈值 0.75）。实测「把 API 和 GPU 的配置改一下」
# 这种中文句会被判成英文，整句换语言。现在改成按词数 + 是否有完整 CJK 语句判断，
# 旧的占比常量已删除，不再参与判定。

_HIRAGANA_RE = re.compile(r"[\u3040-\u309f]")
# 引号/括号内的文本单独判定语言，外面的文本按外层主语言判定
_BRACKET_OPEN = {"「", "『", "（", "(", "《"}
_BRACKET_CLOSE = {"」", "』", "）", ")", "》"}


class TTSError(RuntimeError):
    """带原因分类的错误：disk / network / permission / model / runtime"""

    def __init__(self, message: str, kind: str = "general"):
        super().__init__(message)
        self.kind = kind


# ---------- 输出设备兜底 ----------
# 默认 sink 的端口可能变成 "not available"（例如 Headphones 没插），此时
# 播放会直接失败。下面只做「换一个可用 sink 播」，绝不改系统默认 sink /
# 端口 / mute，避免产生系统级副作用。
_SINK_CACHE_TTL = 10.0
# shutil.which 会把 PATH 整个扫一遍，而 status() 每 3 秒被 /api/status 调一次，
# 一次要查 5 个名字（pactl / wpctl / espeak-ng / espeak / spd-say / pw-play…）。
# PATH 在进程生命周期里不会变，结果缓存起来即可。
_WHICH_CACHE: dict[str, str] = {}


def _which(name: str) -> str:
    if name not in _WHICH_CACHE:
        _WHICH_CACHE[name] = shutil.which(name) or ""
    return _WHICH_CACHE[name]


def _pactl_output(*args: str) -> str:
    if not _which("pactl"):
        return ""
    try:
        proc = subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=3.0)
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def parse_sinks(text: str) -> list[dict]:
    """解析 `pactl list sinks`：每个 sink 的 Name / Description / Active Port
    以及各端口是否 available。解析不了就返回空列表，调用方保持默认行为。"""
    sinks: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Sink #"):
            cur = {"name": "", "desc": "", "active_port": "", "ports": {}}
            sinks.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("Name:"):
            cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            cur["desc"] = line.split(":", 1)[1].strip()
        elif line.startswith("Active Port:"):
            cur["active_port"] = line.split(":", 1)[1].strip()
        elif "available:" in line and ":" in line:
            port, rest = line.split(":", 1)
            avail = rest.rsplit("available:", 1)[-1].strip().lower()
            if port.strip() and avail.startswith(("yes", "no")):
                cur["ports"][port.strip()] = avail.startswith("yes")
    return sinks


def sink_available(sink: dict) -> bool:
    """活动端口明确不可用才算不可用；没解析到端口信息时按可用处理"""
    port = sink.get("active_port") or ""
    ports = sink.get("ports") or {}
    if port and port in ports:
        return bool(ports[port])
    return True


def pick_sink() -> str | None:
    """返回需要定向播放的 sink；None = 默认 sink 可用，按默认走。

    探测不到 pactl / 解析失败时返回 None，不改变既有行为。
    """
    if not _which("pactl"):
        return None
    default = _pactl_output("get-default-sink")
    sinks = parse_sinks(_pactl_output("list", "sinks"))
    if not sinks:
        return None
    by_name = {s["name"]: s for s in sinks if s.get("name")}
    if not default or default not in by_name or sink_available(by_name[default]):
        return None
    usable = [s for s in sinks if s.get("name") and sink_available(s)]
    if not usable:
        raise TTSError("默认音频输出不可用，也没有其它可用输出（no_sink）",
                       kind="no_sink")
    keys = ("speaker", "扬声器", "内置")
    prefer = [s for s in usable
              if any(k in (s.get("desc", "") + " " + s.get("active_port", "")).lower()
                     for k in keys)]
    return (prefer or usable)[0]["name"]


def normalize_engine(raw: str | None) -> str:
    """内部兼容：把旧配置里的引擎值归一成 piper-plus / espeak-ng。"""
    value = str(raw or "").strip().lower()
    if value in ("piper-plus", "piper_plus", "piper", "auto"):
        return "piper-plus"
    if value.startswith(("espeak", "spd-say", "spd_say")):
        return "espeak-ng"
    return "espeak-ng"


# ---------- 播报音量临时抬升 ----------
# 系统音量很低时（Deck 上实测默认 sink 可能只有 20%），合成正常也听不清。
# 播报前把默认 sink 临时抬到可听水平，播完无条件恢复；只读探测失败、找不到
# pactl/wpctl、或用户关掉 [tts] boost_volume 时全程不动系统音量。
# 只在「用户当前音量低于可听下限」时抬，恢复时也只回写仍等于临时值的音量：
# 用户中途自己调过音量就保留他的新值，不被恢复动作覆盖。
BOOST_BELOW = 0.40
BOOST_TARGET = 0.55

_boost_lock = threading.Lock()
_boost_clicks = 0          # 同时进行的播报数：只有降到 0 才恢复
_boost_prev: int | None = None   # 用户原来的音量（百分比）
_boost_set: int | None = None    # 临时抬到的音量（百分比）


def _volume_tool() -> str:
    """音量读写工具：pactl 百分比读写最直接，没有时用 wpctl。"""
    if _which("pactl"):
        return "pactl"
    if _which("wpctl"):
        return "wpctl"
    return ""


def _run_volume(args: list[str]):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3.0)
    except Exception:
        return None


def sink_state() -> tuple[int, bool] | None:
    """默认 sink 的 (音量百分比, 是否静音)；读不到返回 None，调用方保持原行为。"""
    tool = _volume_tool()
    if tool == "pactl":
        vol = _run_volume(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        mute = _run_volume(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        if vol is None or vol.returncode != 0:
            return None
        match = re.search(r"(\d+)%", vol.stdout or "")
        if not match:
            return None
        muted = bool(mute and mute.returncode == 0
                     and "yes" in (mute.stdout or "").lower())
        return int(match.group(1)), muted
    if tool == "wpctl":
        out = _run_volume(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        if out is None or out.returncode != 0:
            return None
        text = out.stdout or ""
        match = re.search(r"Volume:\s*([0-9.]+)", text)
        if not match:
            return None
        return int(round(float(match.group(1)) * 100)), "[MUTED]" in text
    return None


def _set_sink_volume(percent: int) -> bool:
    tool = _volume_tool()
    if tool == "pactl":
        out = _run_volume(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                           f"{int(percent)}%"])
    elif tool == "wpctl":
        out = _run_volume(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
                           f"{int(percent) / 100:.2f}"])
    else:
        return False
    return bool(out and out.returncode == 0)


def boost_enter(enabled: bool) -> dict:
    """播报开始：音量低于下限时临时抬到 BOOST_TARGET，返回当时的 sink 快照。

    无论是否真的抬升都要计数：`boost_exit` 一定会在播报结束（含异常/取消）
    时配对调用，只有最后一个退出才恢复，避免并发播报互相把音量改回去。
    """
    global _boost_clicks, _boost_prev, _boost_set
    state = sink_state()
    snapshot = {
        "volume": state[0] if state else None,
        "muted": state[1] if state else None,
        "boosted": False,
    }
    with _boost_lock:
        _boost_clicks += 1
        if _boost_clicks > 1:
            snapshot["boosted"] = _boost_set is not None
            return snapshot
        if state is None or not enabled:
            return snapshot
        level = state[0]
        if level >= int(BOOST_BELOW * 100):
            return snapshot
        target = int(BOOST_TARGET * 100)
        if not _set_sink_volume(target):
            return snapshot
        _boost_prev, _boost_set = level, target
        snapshot["boosted"] = True
        return snapshot


def boost_exit() -> None:
    """播报结束（成功/失败/取消/异常退出都一样）：恢复临时抬高的音量。"""
    global _boost_clicks, _boost_prev, _boost_set
    with _boost_lock:
        if _boost_clicks <= 0:
            return
        _boost_clicks -= 1
        if _boost_clicks > 0:
            return
        prev, applied = _boost_prev, _boost_set
        _boost_prev = _boost_set = None
        if prev is None or applied is None:
            return
        state = sink_state()
        if state is None or abs(state[0] - applied) > 1:
            # 读不到状态，或用户中途自己调过音量：保留现状，不覆盖用户的值
            return
        _set_sink_volume(prev)


def boost_reset() -> None:
    """测试用：清掉计数与记录，避免用例之间互相影响。"""
    global _boost_clicks, _boost_prev, _boost_set
    with _boost_lock:
        _boost_clicks = 0
        _boost_prev = _boost_set = None


def normalize_speaker(raw: str | None) -> str:
    """把配置里的发音人写成 robot / maid 两种之一。

    认不出来的值退回默认（女仆），避免一次笔误让播报彻底没声；
    同时接受中文别名与旧版引擎/音色名，老配置不用改也能继续用。
    """
    value = str(raw or "").strip().lower()
    if value in (SPEAKER_ROBOT, "机器人", "espeak-ng", "espeak", "spd-say", "spd_say"):
        return SPEAKER_ROBOT
    if value in (SPEAKER_MAID, "女仆", "piper-plus", "piper_plus", "piper",
                 VOICE_ID, "auto"):
        return SPEAKER_MAID
    # 旧 espeak 音色名（cmn / en-us / ja）视为机器人
    if value in ("cmn", "en-us", "zh", "ja", "en", "es", "fr", "pt"):
        return SPEAKER_ROBOT
    return DEFAULT_SPEAKER


def speaker_from_legacy(engine: str | None = None, voice: str | None = None) -> str:
    """旧 [tts] engine/voice 映射到新 speaker：piper 系 → 女仆，其余 → 机器人。"""
    raw_engine = str(engine or "").strip().lower()
    if raw_engine:
        return SPEAKER_MAID if normalize_engine(raw_engine) == "piper-plus" else SPEAKER_ROBOT
    raw_voice = str(voice or "").strip().lower()
    if raw_voice in (VOICE_ID, "piper-plus", "piper_plus", "piper"):
        return SPEAKER_MAID
    if raw_voice:
        return SPEAKER_ROBOT
    return DEFAULT_SPEAKER


def _clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def readify(text: str, limit: int = MAX_CHARS) -> str:
    """把模型回复清成适合朗读的纯文本。"""
    text = _FENCE_RE.sub(" ", text or "")
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _SYMBOL_RE.sub(lambda m: _SPOKEN_SYMBOLS[m.group(0)], text)
    text = _EMOJI_RE.sub(" ", text)
    text = _MD_CHARS_RE.sub(" ", text)
    text = text.replace("…", "。").replace("——", "，")
    text = re.sub(r"[·•]+", " ", text)
    text = _SPACE_RE.sub(" ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    text = re.sub(r"([，。！？!?、])\1+", r"\1", text)
    return text[: max(1, limit)].strip()


def detect_language(text: str) -> str:
    """一小段文本该用哪个语种读：有假名是日语，只有汉字是中文，其余按英文。"""
    if _KANA_RE.search(text):
        return "ja"
    if _CJK_RE.search(text):
        return "zh"
    if _LATIN_RE.search(text):
        return "en"
    return "en"


def _script_kind(kind: str) -> bool:
    """是否是需要发声的脚本片段（数字也发声，只是语言跟邻居走）"""
    return kind in ("kana", "han", "latin", "digit")


def _neighbor_script(tokens: list[tuple[str, str]], index: int, step: int) -> int:
    """跳过标点/空白，找前一个或后一个脚本片段的 token 下标（没有则 -1）"""
    j = index + step
    while 0 <= j < len(tokens):
        if _script_kind(tokens[j][0]):
            return j
        j += step
    return -1


def _sentence_language(text: str) -> str:
    """整句主语言：只有整句确属英文才判 en，否则按假名占 CJK 的比例分 ja/zh。

    判据从「拉丁字符占比」改成「英文词数 + 是否还构成 CJK 句子」：
    - 没有任何 CJK：有英文词就是 en；
    - 有 CJK 和几个技术词：留在 zh/ja（不因 API/GPU 这类词整句换语言）；
    - 「OK，thanks！」这种只剩 ≤2 个 CJK 字符、≥3 个英文词的，算整句英文。

    纯符号/空串没有可读内容，返回 zh 占位（不会产生语音片段）。
    """
    kana = len(_KANA_RE.findall(text))
    han = len(_CJK_RE.findall(text))
    words = _LATIN_WORD_RE.findall(text)
    cjk = kana + han
    if cjk == 0:
        return "en" if words else "zh"
    if len(words) >= 3 and cjk <= 2:
        return "en"
    # 只有片假名 + 汉字、完全没有平假名：日语行文几乎一定有助词/送假名，
    # 这种构成基本是中文句子里的外来专名（「ミカ的设定」「她喜欢ミカ」），
    # 不能按假名比例判成日语。
    if han and not _HIRAGANA_RE.search(text):
        return "zh"
    return "ja" if kana / (kana + han) >= KANA_JA_RATIO else "zh"


def _token_language(tokens: list[tuple[str, str]], index: int, main: str,
                    ja_terms: frozenset[str] | None = None) -> str:
    """单个脚本片段的语言：以整句主语言为准，再做局部修正。

    - 假名永远是 ja，拉丁永远是 en；
    - 主语言 ja：句内所有汉字都按日语读（不要用中文音读和语汉字）；
    - 主语言 zh/en：汉字只有紧贴「平假名」或命中 ja_terms 词表时才算日语。
      片假名多为外来专名，不把邻近汉字拖成日语。

    已知限制：没有词典时，中文主语句里纯汉字写的日文人名/地名（石川由依、
    東京大学）仍会判成 zh 并按中文读。可选词表 [tts].ja_terms 可以逐个补上，
    但只做整词匹配、不做分词，无法覆盖所有日文汉字词。
    """
    kind = tokens[index][0]
    if kind == "kana":
        return "ja"
    if kind == "latin":
        return "en"
    if kind == "han":
        if main == "ja":
            return "ja"
        if ja_terms and tokens[index][1] in ja_terms:
            return "ja"
        for j in (index - 1, index + 1):
            if 0 <= j < len(tokens):
                neighbor = tokens[j]
                if neighbor[0] == "kana" and _HIRAGANA_RE.search(neighbor[1]):
                    return "ja"
        return "zh"
    return ""


def _digit_language(tokens: list[tuple[str, str]], index: int, main: str,
                    ja_terms: frozenset[str] | None = None) -> str:
    """数字没有自己的语言：优先跟前一段，其次跟后一段，绝不丢"""
    for step in (-1, 1):
        j = _neighbor_script(tokens, index, step)
        while j >= 0 and tokens[j][0] == "digit":
            j = _neighbor_script(tokens, j, step)
        if j >= 0:
            lang = _token_language(tokens, j, main, ja_terms)
            if lang:
                return lang
    return "en"


def _context_spans(sentence: str) -> list[tuple[bool, str]]:
    """按引号/括号切成 (是否在括号内, 文本)，拼接后与原句逐字一致。

    嵌套括号整体留在同一片段里（只做单层语境）；未闭合的括号一直算到句尾；
    多出来的右括号按普通标点留在外面。两种情况都不会崩、不会丢字符。
    """
    spans: list[tuple[bool, str]] = []
    buf = ""
    depth = 0
    for ch in sentence:
        if ch in _BRACKET_OPEN:
            if depth == 0:
                if buf:
                    spans.append((False, buf))
                buf = ch
            else:
                buf += ch
            depth += 1
        elif ch in _BRACKET_CLOSE and depth > 0:
            buf += ch
            depth -= 1
            if depth == 0:
                spans.append((True, buf))
                buf = ""
        else:
            buf += ch
    if buf:
        spans.append((depth > 0, buf))
    return spans


def _route_span(text: str, main: str, ja_terms: frozenset[str] | None = None) -> list[list[str]]:
    """把一段连续上下文按脚本切成 [语种, 片段] 列表（未合并、未切长）。"""
    tokens = [(m.lastgroup, m.group(0)) for m in _SCRIPT_TOKEN_RE.finditer(text)]
    segs: list[list[str]] = []
    lead = ""  # 段首标点先攒着，等第一个有声片段一起送
    for i, (kind, piece) in enumerate(tokens):
        if kind == "other":
            if segs:
                segs[-1][1] += piece
            else:
                lead += piece
            continue
        if kind == "digit":
            lang = segs[-1][0] if segs else _digit_language(tokens, i, main, ja_terms)
        else:
            lang = _token_language(tokens, i, main, ja_terms)
        segs.append([lang, lead + piece])
        lead = ""
    if lead and segs:
        segs[-1][1] += lead
    return segs


def _route_sentence(sentence: str,
                    ja_terms: frozenset[str] | None = None) -> list[tuple[str, str]]:
    """一句之内先分括号语境，再各自按主语言切段，最后合并同语言相邻段。"""
    spans = _context_spans(sentence)
    outer_text = "".join(text for inside, text in spans if not inside)
    outer_lang = _sentence_language(outer_text)
    segs: list[list[str]] = []
    pending = ""  # 只有标点/符号的片段不能单独丢，攒着并给相邻片段
    for inside, span_text in spans:
        lang = _sentence_language(span_text) if inside else outer_lang
        rows = _route_span(span_text, lang, ja_terms)
        if not rows:
            pending += span_text
            continue
        if pending:
            rows[0][1] = pending + rows[0][1]
            pending = ""
        segs.extend(rows)
    if pending:
        if not segs:
            return []
        segs[-1][1] += pending
    merged: list[tuple[str, str]] = []
    for lang, text in segs:
        if merged and merged[-1][0] == lang:
            merged[-1] = (lang, merged[-1][1] + text)
        else:
            merged.append((lang, text))
    return merged


def route_segments(text: str, language: str = "auto",
                   limit: int = MAX_CHARS,
                   ja_terms: frozenset[str] | None = None) -> list[tuple[str, str]]:
    """按句子 + 脚本切段，返回 [(语种, 文本)]。

    auto 时先按标点和换行分句，再给每句判主语言（只有整句英文才判 en，
    否则按假名占比分 ja/zh）；引号/括号内的文本单独判定。句内按脚本切成
    假名 / 汉字 / 拉丁 / 数字片段，标点和空白并进相邻片段，保证把片段
    拼回去后与原句（去掉纯空白）逐字等价；纯符号句子没有可读内容，返回空列表。

    固定语言优先：显式选了 ja/zh/en/... 时整句不拆，直接用该语言。
    """
    forced = language if language in LANGUAGES and language != "auto" else ""
    out: list[tuple[str, str]] = []
    for sentence in _SENT_RE.findall(text or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        if forced:
            out.append((forced, sentence))
            continue
        out.extend(_route_sentence(sentence, ja_terms))
    # 超长段落再按长度硬切，保证每次合成不会一口气吃进一大段
    trimmed: list[tuple[str, str]] = []
    for lang, segment in out:
        while len(segment) > limit:
            trimmed.append((lang, segment[:limit]))
            segment = segment[limit:]
        if segment:
            trimmed.append((lang, segment))
    return trimmed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{exc.__class__.__name__}: {text}" if text != exc.__class__.__name__ else text


# 当前进程里最近构造的 TTS 实例。唤醒服务用它预合成「我在」并播放，
# 这样不必让 main.py 再多传一条依赖；测试里直接注入假实现，不碰这里。
_active: "TTS | None" = None


def active() -> "TTS | None":
    """返回当前 TTS 实例；没构造过（或纯单测）时返回 None。"""
    return _active


class TTS:
    """语音播报器：piper-plus 懒加载 + espeak-ng 兜底，可下载模型。"""

    def __init__(
        self,
        enable: bool = False,
        speaker: str | None = None,
        engine: str | None = None,
        voice: str | None = None,
        speed: int = ESPEAK_SPEED,
        max_chars: int = MAX_CHARS,
        speed_scale: float = 1.0,
        length_scale: float = 1.0,
        noise_scale: float = 0.667,
        language: str = "auto",
        model_source: str = "auto",
        model_root: Path | str | None = None,
        idle_unload_sec: float = 600.0,
        ja_terms: str | list[str] | None = None,
        boost_volume: bool = True,
    ):
        self.enable = bool(enable)
        # 发音人是唯一来源：显式 speaker 优先，老配置的 engine/voice 映射过来
        if str(speaker or "").strip():
            self.speaker = normalize_speaker(speaker)
        else:
            self.speaker = speaker_from_legacy(engine, voice)
        self.voice = str(voice or VOICE_ID).strip() or VOICE_ID
        self.speed = int(_clamp(speed, 80, 320, ESPEAK_SPEED))
        self.max_chars = int(_clamp(max_chars, 20, 2000, MAX_CHARS))
        # speed_scale：1.0 = 模型默认语速，>1 更快；内部换算成 length_scale
        self.speed_scale = _clamp(speed_scale, 0.5, 2.0, 1.0)
        self.length_scale = _clamp(length_scale, 0.4, 3.0, 1.0)
        self.noise_scale = _clamp(noise_scale, 0.0, 1.0, 0.667)
        self.language = language if language in LANGUAGES else "auto"
        self.model_source = str(model_source or "auto").strip() or "auto"
        self.idle_unload_sec = _clamp(idle_unload_sec, 30, 24 * 3600, 600)
        self._model_root = Path(model_root) if model_root else None
        # 可选日文汉字词表：中文句里纯汉字写的日文人名（石川由依）默认按中文读，
        # 把词写进 [tts].ja_terms 后，命中整词就按日语读。只做整词匹配，不做分词。
        if isinstance(ja_terms, str):
            terms = ja_terms.replace("，", ",").split(",")
        else:
            terms = list(ja_terms or [])
        self.ja_terms = frozenset(t.strip() for t in terms if t and t.strip())
        # 播报时临时抬升系统音量（音量过低时），播完恢复；可在设置里关掉
        self.boost_volume = bool(boost_volume)
        self._sink_snapshot: dict | None = None

        # 懒加载状态
        self._voice = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._load_error = ""
        self._root_error = ""
        self._runtime: bool | None = None

        # 播报状态
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel: threading.Event | None = None
        self._state_listener = None
        self._announced = False
        self._last_engine = ""
        self._last_error = ""
        self._last_failed_segments = 0
        # 最近一次播报里「piper 合成失败、改用 espeak-ng 念出来」的片段数
        self._last_degraded_segments = 0

        # 下载任务
        self._dl_lock = threading.Lock()
        self._dl_thread: threading.Thread | None = None
        self._dl_cancel = False
        self._source_name = ""
        self._source_url = ""
        self._dl = {
            "state": "idle",  # idle / downloading / done / error / cancelled
            "done": 0,
            "total": -1,
            "percent": 0.0,
            "file": "",
            "phase": "",
            "source": "",
            "source_url": "",
            "error": "",
            "error_kind": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        global _active
        _active = self

    @property
    def engine(self) -> str:
        """内部路由用的引擎名，由 speaker 派生（不再是可独立修改的配置项）。"""
        return "piper-plus" if self.speaker == SPEAKER_MAID else "espeak-ng"

    # ---------- 路径与能力 ----------

    @property
    def model_root(self) -> Path:
        if self._model_root is None:
            self._model_root = paths.tts_model_dir()
        return self._model_root

    def _safe_root(self) -> Path | None:
        try:
            return self.model_root
        except Exception as exc:  # 数据根不可用时状态接口不能 500
            self._root_error = f"数据根不可用：{exc}"
            return None

    def _has_runtime(self) -> bool:
        """piper_plus 装没装。缓存结果，状态接口每秒轮询不能反复扫模块。"""
        if self._runtime is None:
            try:
                self._runtime = importlib.util.find_spec("piper_plus") is not None
            except (ImportError, ValueError):
                self._runtime = False
        return bool(self._runtime)

    def model_ready(self) -> bool:
        root = self._safe_root()
        if root is None:
            return False
        onnx = root / ONNX_NAME
        config = root / CONFIG_NAME
        try:
            return (
                onnx.is_file() and config.is_file()
                and onnx.stat().st_size >= MIN_ONNX_BYTES
            )
        except OSError:
            return False

    def _piper_ready(self) -> bool:
        return self._has_runtime() and self.model_ready() and not self._load_error

    def _espeak_binary(self) -> str:
        for name in ("espeak-ng", "espeak", "spd-say"):
            found = _which(name)
            if found:
                return found
        return ""

    @staticmethod
    def _player() -> str:
        for name in ("pw-play", "paplay", "aplay"):
            found = _which(name)
            if found:
                return found
        return ""

    # ---------- 状态 ----------

    def status(self) -> dict:
        self._maybe_unload()
        runtime = self._has_runtime()
        model = self.model_ready()
        load_error = self._load_error
        # 女仆能不能用与当前选中哪个发音人无关：选「机器人」时 engine 就是
        # espeak-ng，此时 active_engine 也必然是 espeak-ng，不能拿它判断女仆
        # 是否就绪（设置页曾因此把已装好的女仆误报成不可用）。
        if not runtime:
            maid_reason = "未安装 piper-plus 运行时（pip install -r requirements-tts.txt）"
        elif not model:
            maid_reason = "语音模型未下载（约 38 MB）"
        else:
            maid_reason = load_error or ""
        maid_ready = bool(runtime and model and not load_error)
        if self.engine == "piper-plus":
            reason = maid_reason or self._root_error
            effective = "piper-plus" if maid_ready else "espeak-ng"
        else:
            effective = "espeak-ng"
            reason = "" if self._espeak_binary() else "找不到 espeak-ng / espeak / spd-say"
        return {
            "enable": self.enable,
            # 用户选择的发音人（唯一配置项）：robot / maid
            "speaker": self.speaker,
            "speaker_label": SPEAKER_LABELS.get(self.speaker, ""),
            "speakers": [dict(item) for item in SPEAKERS],
            # 内部引擎名，保留给旧客户端/状态页做诊断
            "engine": self.engine,
            # 当前真正会用的引擎：piper-plus 不可用时这里显示 espeak-ng
            "active_engine": effective,
            "runtime": runtime,
            "model": model,
            # 女仆是否就绪：只取决于运行时 + 模型 + 加载错误，与当前选中的
            # 发音人无关，界面的「女仆不可用」提示只该看这一组字段
            "maid_ready": maid_ready,
            "maid_reason": "" if maid_ready else maid_reason,
            # 英文 G2P 的 nltk 数据是否就绪（缺了英文段合成失败）
            "english_data": english_data_ready(),
            "model_root": str(self._safe_root() or ""),
            "model_source": self.model_source,
            "voice": self.voice,
            "voices": [{"id": VOICE_ID, "label": VOICE_LABEL}] + [
                {"id": name, "label": label} for name, label in ESPEAK_VOICES
            ],
            "language": self.language,
            "languages": list(LANGUAGES),
            "speed": self.speed,
            "speed_scale": self.speed_scale,
            "length_scale": self.length_scale,
            "noise_scale": self.noise_scale,
            # 播报音量过低时是否临时抬升（播完恢复）
            "boost_volume": self.boost_volume,
            "ready": effective == "piper-plus" or bool(self._espeak_binary()),
            "reason": reason,
            "loaded": self._voice is not None,
            "player": self._player(),
            "last_engine": self._last_engine,
            "last_error": self._last_error,
            # 最近一次播报里合成失败的片段数（0 = 全部念完）
            "failed_segments": self._last_failed_segments,
            # 合成失败后改用 espeak-ng 念出来的片段数（>0 表示这段被降级朗读）
            "degraded_segments": self._last_degraded_segments,
            "attribution": ATTRIBUTION,
            "attribution_url": ATTRIBUTION_URL,
            "download": self.download_status(),
        }

    def summary(self) -> dict:
        """给 /api/status 的精简版：只带发音人/引擎可用性与原因，字段名前缀 tts_。"""
        state = self.status()
        error = state["reason"] or state["last_error"]
        if not error and state["active_engine"] == "piper-plus" and not state["english_data"]:
            # 英文 G2P 数据缺失时英文段会逐段退回 espeak-ng：不算致命，但要让
            # 状态接口和设置页看得见，而不是等用户发现「英文没声」。
            error = ("英文语音数据缺失（nltk cmudict），英文段会用 espeak-ng 兜底；"
                     "补装：bash install.sh install --tts")
        return {
            "tts_speaker": state["speaker"],
            "tts_engine": state["active_engine"],
            "tts_runtime": state["runtime"],
            "tts_model": state["model"],
            "tts_ready": state["ready"],
            "tts_english_data": state["english_data"],
            "tts_error": error,
            "tts_attribution": ATTRIBUTION,
        }

    # ---------- 配置热更新 ----------

    def apply(self, cfg: dict | None):
        """从 [tts] 段热更新；发音人/模型源变化时释放已加载的模型。"""
        cfg = cfg or {}
        # 新键 speaker 优先；没有时用旧 engine/voice 映射，再没有就保持当前值
        if str(cfg.get("speaker") or "").strip():
            speaker = normalize_speaker(cfg.get("speaker"))
        elif cfg.get("engine") is not None or cfg.get("voice") is not None:
            speaker = speaker_from_legacy(cfg.get("engine"), cfg.get("voice"))
        else:
            speaker = self.speaker
        voice = str(cfg.get("voice", self.voice) or VOICE_ID).strip() or VOICE_ID
        source = str(cfg.get("model_source", self.model_source) or "auto").strip() or "auto"
        if (
            speaker != self.speaker
            or voice != self.voice
            or source != self.model_source
        ):
            self.unload()
        self.speaker = speaker
        self.voice = voice
        self.model_source = source
        self.enable = bool(cfg.get("enable", self.enable))
        self.speed = int(_clamp(cfg.get("speed", self.speed), 80, 320, ESPEAK_SPEED))
        self.max_chars = int(_clamp(cfg.get("max_chars", self.max_chars), 20, 2000, MAX_CHARS))
        self.speed_scale = _clamp(cfg.get("speed_scale", self.speed_scale), 0.5, 2.0, 1.0)
        self.length_scale = _clamp(cfg.get("length_scale", self.length_scale), 0.4, 3.0, 1.0)
        self.noise_scale = _clamp(cfg.get("noise_scale", self.noise_scale), 0.0, 1.0, 0.667)
        language = str(cfg.get("language", self.language) or "auto").strip().lower()
        self.language = language if language in LANGUAGES else "auto"
        self.idle_unload_sec = _clamp(
            cfg.get("idle_unload_sec", self.idle_unload_sec), 30, 24 * 3600, 600
        )
        self.boost_volume = bool(cfg.get("boost_volume", self.boost_volume))

    # ---------- 懒加载 ----------

    def unload(self):
        with self._lock:
            self._voice = None
            self._load_error = ""

    def _maybe_unload(self):
        if self._voice is None:
            return
        if self.idle_unload_sec <= 0:
            return
        if time.time() - self._last_used < self.idle_unload_sec:
            return
        with self._lock:
            if self._voice is not None and time.time() - self._last_used >= self.idle_unload_sec:
                self._voice = None

    def _ensure_voice_locked(self):
        """调用方必须持有 self._lock。加载失败会把原因留在 _load_error。"""
        if self._voice is not None:
            self._last_used = time.time()
            return self._voice
        if not self._has_runtime():
            raise TTSError(
                "未安装 piper-plus 运行时（pip install -r requirements-tts.txt）",
                kind="runtime",
            )
        root = self._safe_root()
        if root is None:
            raise TTSError(self._root_error or "数据根不可用", kind="permission")
        onnx = root / ONNX_NAME
        config = root / CONFIG_NAME
        if not (onnx.is_file() and config.is_file()):
            raise TTSError("语音模型未下载（约 38 MB）", kind="model")
        prepare_english_backend()
        try:
            from piper_plus import PiperVoice

            try:
                voice = PiperVoice.load(str(onnx), config_path=str(config))
            except TypeError:
                # 老版本没有 config_path 关键字
                voice = PiperVoice.load(str(onnx), str(config))
        except Exception as exc:
            self._load_error = f"模型加载失败：{_short_error(exc)}"
            raise TTSError(self._load_error, kind="model") from exc
        self._voice = voice
        self._load_error = ""
        self._last_used = time.time()
        devlog.log("tts.model.load", model=onnx.name, chars_limit=self.max_chars)
        return voice

    # ---------- 播报 ----------

    def set_state_listener(self, fn) -> None:
        """注册播放状态回调（start / end）；智能音箱模式用它驱动眼睛状态。"""
        self._state_listener = fn

    def _announce_state(self, kind: str) -> None:
        cb = self._state_listener
        if not cb:
            return
        try:
            cb(kind)
        except Exception as exc:
            devlog.debug("tts._announce_state", error=repr(exc))
            pass

    async def speak(self, text: str, force: bool = False):
        """异步播报；新的一次调用会先掐断上一次。失败只降级，不抛给对话流程。"""
        if not self.enable and not force:
            return
        cleaned = readify(text, self.max_chars)
        if not cleaned:
            return
        await self.stop()
        event = threading.Event()
        self._cancel = event
        self._last_error = ""
        self._last_failed_segments = 0
        self._last_degraded_segments = 0
        self._announced = False
        try:
            self._sink_snapshot = boost_enter(self.boost_volume)
            devlog.log("tts.boost", enabled=self.boost_volume, **self._sink_log_fields())
            segments = route_segments(cleaned, self.language, self.max_chars,
                                      self.ja_terms)
            if not segments:
                return
            self._announced = True
            self._announce_state("start")
            devlog.log("tts.speak", chars=len(cleaned), segments=len(segments),
                       engine=self.engine, language=self.language,
                       max_chars=self.max_chars,
                       piper_ready=self._piper_ready())
            if self.speaker == SPEAKER_ROBOT:
                # 机器人发音固定走 Deck 自带的 espeak-ng
                voice = self.voice
                if voice == VOICE_ID:
                    # 从 piper 继承下来的音色名 espeak-ng 不认识，按语言映射
                    voice = ESPEAK_LANG_VOICES.get(self.language, "cmn")
                await self._speak_espeak(cleaned, event, voice=voice,
                                         language=self.language)
                return
            if self._piper_ready():
                try:
                    failures, degraded, first_error = await self._speak_piper(
                        segments, event)
                except Exception as exc:
                    # 整体不可用（模型加载失败、没有播放器）：整段退回 espeak
                    self._last_error = f"piper-plus 不可用：{_short_error(exc)}"
                    if not self.model_ready():
                        self.unload()
                else:
                    self._last_failed_segments = failures
                    self._last_degraded_segments = degraded
                    if failures >= len(segments):
                        # 一段都没成功：当整体不可用处理，允许整段重念一遍
                        self._last_error = (
                            f"piper-plus 全部 {len(segments)} 段合成失败：{first_error}")
                        await self._speak_espeak(cleaned, event, language=self.language)
                    elif failures:
                        # 个别片段连 espeak 兜底都失败：如实报出失败的段数
                        self._last_error = (
                            f"piper-plus {failures}/{len(segments)} 段合成失败：{first_error}")
                    elif degraded:
                        # 这些片段已经用 espeak-ng 念了，不算丢段，但要让状态可见
                        self._last_error = (
                            f"piper-plus {degraded}/{len(segments)} 段改用 espeak-ng 兜底："
                            f"{first_error}")
                    return
            await self._speak_espeak(cleaned, event, language=self.language)
        finally:
            if self._cancel is event:
                self._cancel = None
            boost_exit()
            self._sink_snapshot = None
            if self._announced:
                self._announced = False
                self._announce_state("end")

    async def preview(self, text: str = ""):
        """试听：不受 enable 开关限制；传了文本就用传入的，否则按发音人取默认句。

        播放端（paplay/aplay）在音频输出不可用时可能一直不返回。这里给整段
        试听加硬超时，超时后主动掐掉播放进程并把原因带回接口，避免 HTTP
        请求和界面按钮一起卡死。
        """
        spoken = text or PREVIEW_TEXTS.get(self.speaker, PREVIEW_TEXTS[DEFAULT_SPEAKER])
        timed_out = False
        try:
            await asyncio.wait_for(self.speak(spoken, force=True),
                                   timeout=PREVIEW_TIMEOUT)
        except asyncio.TimeoutError:
            timed_out = True
            self._last_error = (
                f"试听超时：{int(PREVIEW_TIMEOUT)} 秒内播放端没有返回"
                "（音频输出可能不可用）")
            await self.stop()
        return {"engine": self._last_engine, "error": self._last_error,
                "timed_out": timed_out}

    async def stop(self):
        event = self._cancel
        if event is not None:
            event.set()
        proc, self._proc = self._proc, None
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            # kill 之后必须再 wait 一次，否则会留下僵尸进程
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    def _synth_wav(self, text: str, language: str) -> Path:
        """在线程里跑：加载/合成都在锁内串行。返回临时 wav 路径。"""
        with self._lock:
            voice = self._ensure_voice_locked()
            self._last_used = time.time()
            prepare_english_backend()
            length_scale = self.length_scale / max(0.3, self.speed_scale)
            fd, name = tempfile.mkstemp(prefix="deckmeido-tts-", suffix=".wav")
            os.close(fd)
            path = Path(name)
            try:
                with wave.open(str(path), "wb") as wav:
                    kwargs = {
                        "length_scale": round(length_scale, 3),
                        "noise_scale": round(self.noise_scale, 3),
                    }
                    language_id = self._language_id(voice, language)
                    if language_id is not None:
                        kwargs["language_id"] = language_id
                    try:
                        voice.synthesize(text, wav, **kwargs)
                    except TypeError:
                        # 极老版本不支持 language_id / 噪声参数时退回最简调用
                        voice.synthesize(text, wav)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return path

    def synth_wav(self, text: str, language: str = "") -> Path:
        """按当前发音人生成 wav（QQ 语音条用）；返回临时文件，调用方负责删。

        robot 直接调用 espeak-ng 导出 wav，maid 走 piper-plus；失败抛 TTSError，
        由调用方按现有逻辑降级为只发文字。
        """
        cleaned = readify(text, self.max_chars)
        if not cleaned:
            raise ValueError("没有可朗读的文本")
        lang = language or (self.language if self.language != "auto"
                            else detect_language(cleaned))
        devlog.log("tts.synth", speaker=self.speaker, engine=self.engine,
                   language=lang, chars=len(cleaned))
        if self.speaker == SPEAKER_ROBOT:
            return self._synth_wav_espeak(cleaned, lang)
        return self._synth_wav(cleaned, lang)

    def synth_bytes(self, text: str, language: str = "") -> bytes:
        """合成到内存并返回 wav 字节。

        唤醒回应必须在命中瞬间就能播，所以由唤醒服务在启动时预合成缓存。
        合成过程仍会落一个临时文件（espeak-ng 的接口只收路径），这里读完后
        立刻删除，磁盘上不留音频。
        """
        path = self.synth_wav(text, language)
        try:
            data = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        # 预热 sink 探测：命中唤醒词时不能再花几十毫秒跑一次 pactl
        try:
            self._pick_sink_cached()
        except Exception as exc:
            devlog.debug("tts.synth_bytes", error=repr(exc))
            pass
        return data

    def _synth_wav_espeak(self, text: str, language: str = "") -> Path:
        """espeak-ng 直接写 wav（机器人发音的语音条）。"""
        binary = self._espeak_binary()
        name = os.path.basename(binary) if binary else ""
        if not binary or not name.startswith("espeak"):
            raise TTSError("找不到 espeak-ng，无法生成语音条", kind="runtime")
        spoken = ESPEAK_LANG_VOICES.get(language or detect_language(text), "cmn")
        fd, tmp = tempfile.mkstemp(prefix="deckmeido-tts-", suffix=".wav")
        os.close(fd)
        path = Path(tmp)
        try:
            proc = subprocess.run(
                [binary, "-v", spoken, "-s", str(self.speed), "-w", str(path), text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60, check=False)
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise TTSError(f"espeak-ng 合成失败：{_short_error(exc)}", kind="synth") from exc
        if proc.returncode != 0 or not path.is_file() or path.stat().st_size <= 0:
            path.unlink(missing_ok=True)
            raise TTSError(f"espeak-ng 退出码 {proc.returncode}，未生成语音",
                           kind="synth")
        return path

    @staticmethod
    def _language_id(voice, language: str) -> int | None:
        config = getattr(voice, "config", None)
        mapping = getattr(config, "language_id_map", None) or {}
        try:
            value = mapping.get(language)
        except AttributeError:
            return None
        return value if isinstance(value, int) else None

    async def _speak_piper(
        self, segments: list[tuple[str, str]], event: threading.Event
    ) -> tuple[int, int, str]:
        """逐段合成播放，返回 (失败段数, 降级段数, 首个错误)。

        单段合成失败时不再直接跳过：先用该语种的 espeak-ng 把这一段念出来
        （降级段数 +1），只有连 espeak 都不行才算失败段，避免「英文整段静默
        消失」。模型加载失败 / 没有播放器这类整体性错误仍然抛出，由 speak()
        整段兜底。
        """
        self._last_engine = "piper-plus"
        failures = 0
        degraded = 0
        first_error = ""
        try:
            for language, segment in segments:
                if event.is_set():
                    break
                seg_start = time.perf_counter()
                try:
                    wav = await asyncio.to_thread(self._synth_wav, segment, language)
                except Exception as exc:
                    if isinstance(exc, TTSError) and exc.kind in ("model", "runtime"):
                        raise
                    first_error = first_error or _short_error(exc)
                    if await self._speak_segment_fallback(segment, language, event, exc):
                        degraded += 1
                    else:
                        failures += 1
                    continue
                devlog.log("tts.segment", engine="piper-plus", language=language,
                           chars=len(segment),
                           synth_ms=round((time.perf_counter() - seg_start) * 1000, 1),
                           wav_bytes=(wav.stat().st_size if wav.exists() else 0))
                if event.is_set():
                    wav.unlink(missing_ok=True)
                    break
                try:
                    await self._play(wav, event)
                except TTSError as exc:
                    if exc.kind == "player":
                        raise
                    failures += 1
                    first_error = first_error or _short_error(exc)
                except Exception as exc:
                    failures += 1
                    first_error = first_error or _short_error(exc)
                finally:
                    wav.unlink(missing_ok=True)
        finally:
            self._maybe_unload()
        return failures, degraded, first_error

    async def _speak_segment_fallback(self, text: str, language: str,
                                      event: threading.Event, exc: Exception) -> bool:
        """piper 某段合成失败时，用该语种的 espeak-ng 把这一段念出来。

        以前这里只 `continue`，英文 G2P 数据缺失时英文段就整段没声、日志里
        也看不出少了什么。现在至少保证原文被念出来，并且把失败原因写进日志
        （tts.segment.error / tts.segment.fallback），状态接口里也能看到降级段数。
        """
        if event.is_set():
            return True
        voice = ESPEAK_LANG_VOICES.get(language) or self.voice
        devlog.log("tts.segment.error", engine="piper-plus", language=language,
                   chars=len(text), voice=voice, error=_short_error(exc))
        ok = await self._speak_espeak(text, event, voice=voice, language=language)
        if ok:
            devlog.log("tts.segment.fallback", engine="espeak-ng", language=language,
                       voice=voice, chars=len(text), error=_short_error(exc))
        return ok

    async def _play(self, wav: Path, event: threading.Event):
        player = self._player()
        if not player:
            raise TTSError("没有可用的播放器（pw-play / paplay / aplay 都找不到）", kind="player")
        # 默认 sink 的活动端口可能不可用；换一个可用 sink 播（只读探测，不改系统设置）
        sink = await asyncio.to_thread(self._pick_sink_cached)
        args, env = self._player_args(player, sink)
        # 三个播放器（pw-play / paplay / aplay）都要把音频文件作为位置参数传进去，
        # 少了它只会打印用法后退出，piper 这一路会被误判成"播放器失败"而降级
        args = [*args, str(wav)]
        devlog.log("tts.play", player=os.path.basename(player),
                   sink=sink or "default", **self._sink_log_fields())
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **({"env": env} if env else {}),
        )
        self._proc = proc
        try:
            code = await proc.wait()
        finally:
            if self._proc is proc:
                self._proc = None
        if code not in (0, -15) and not event.is_set():
            raise TTSError(f"播放器退出码 {code}", kind="player")

    async def play_bytes(self, data: bytes, force: bool = True) -> bool:
        """播放内存里的 wav 字节（唤醒回应「我在」）。

        与 speak() 共用同一个播放进程句柄，所以 stop() 能立刻掐断它。
        播放器只认文件路径，这里临时落一份、播完（含取消/失败）立即删除。
        """
        if not data:
            return False
        if not self.enable and not force:
            return False
        await self.stop()
        event = threading.Event()
        self._cancel = event
        fd, name = tempfile.mkstemp(prefix="deckmeido-ack-", suffix=".wav")
        os.close(fd)
        path = Path(name)
        try:
            self._sink_snapshot = boost_enter(self.boost_volume)
            devlog.log("tts.boost", enabled=self.boost_volume, **self._sink_log_fields())
            path.write_bytes(data)
            await self._play(path, event)
            return True
        except asyncio.CancelledError:
            # 被唤醒服务中止时 _play 还停在 proc.wait()，必须主动掐掉播放进程
            await self.stop()
            raise
        finally:
            path.unlink(missing_ok=True)
            boost_exit()
            self._sink_snapshot = None
            if self._cancel is event:
                self._cancel = None

    def _pick_sink_cached(self) -> str | None:
        """探测默认 sink 是否可用；10 秒内复用结果，避免逐句都跑 pactl"""
        now = time.monotonic()
        cache = getattr(self, "_sink_cache", None)
        if cache and now - cache[0] < _SINK_CACHE_TTL:
            return cache[1]
        sink = pick_sink()
        self._sink_cache = (now, sink)
        return sink

    def _sink_log_fields(self) -> dict:
        """日志用：播报开始时的 sink 音量/静音/是否临时抬升。"""
        snap = self._sink_snapshot or {}
        return {
            "sink_volume": snap.get("volume"),
            "sink_mute": snap.get("muted"),
            "boosted": snap.get("boosted"),
        }

    @staticmethod
    def _player_args(player: str, sink: str | None) -> tuple[list[str], dict | None]:
        """按播放器能力把目标 sink 传下去；aplay 只能靠 PULSE_SINK 环境变量"""
        if not sink:
            return [player], None
        name = os.path.basename(player)
        env = dict(os.environ)
        env["PULSE_SINK"] = sink
        if name.startswith("paplay"):
            return [player, "--device=" + sink], env
        if name.startswith("pw-play"):
            return [player, "--target=" + sink], env
        return [player], env


    async def _speak_espeak(self, text: str, event: threading.Event,
                            voice: str | None = None,
                            language: str = "") -> bool:
        """用 espeak-ng 念一段文本；返回是否真的念了（失败原因写进 _last_error）。

        voice/language 用于逐段兜底：英文段用 en-us，日文段用 ja，中文段用 cmn。
        整句兜底（voice 为 None）时沿用配置里的音色。
        """
        binary = self._espeak_binary()
        if not binary:
            self._last_error = "找不到 espeak-ng / espeak / spd-say"
            return False
        spoken = voice or self.voice
        if not voice and language not in ("", "auto"):
            spoken = ESPEAK_LANG_VOICES.get(language, self.voice)
        args = [binary]
        name = os.path.basename(binary)
        if name.startswith("espeak"):
            args += ["-v", spoken, "-s", str(self.speed)]
        elif name.startswith("spd-say"):
            args += ["-l", spoken, "-r", str(self.speed - 170)]
        args.append(text)
        self._last_engine = "espeak-ng"
        devlog.log("tts.segment", engine="espeak-ng", binary=name, chars=len(text),
                   voice=spoken, language=language or spoken)
        devlog.log("tts.play", player=name, sink="default",
                   **self._sink_log_fields())
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self._last_error = f"找不到语音引擎：{binary}"
            return False
        self._proc = proc
        try:
            await proc.wait()
        finally:
            if self._proc is proc:
                self._proc = None
        if proc.returncode not in (0, -15) and not event.is_set():
            self._last_error = f"{name} 退出码 {proc.returncode}"
            devlog.log("tts.segment.error", engine="espeak-ng",
                       code=proc.returncode,
                       ms=round((time.perf_counter() - started) * 1000, 1))
            return False
        return True

    # ---------- 模型下载 ----------

    def _source_defs(self) -> list[dict]:
        """按 [tts] model_source 给出候选源，顺序即优先级（镜像优先）。"""
        mirror = {
            "name": "mirror",
            "label": "hf-mirror.com 镜像",
            "files": {name: f"{MIRROR_BASE}/{name}" for name in MODEL_FILES},
        }
        hf = {
            "name": "hf",
            "label": "huggingface.co",
            "files": {name: f"{HF_BASE}/{name}" for name in MODEL_FILES},
        }
        mode = self.model_source
        if mode == "mirror":
            return [mirror]
        if mode == "hf":
            return [hf]
        if mode.startswith(("http://", "https://")):
            base = mode.rstrip("/")
            return [{
                "name": "custom",
                "label": "自定义源",
                "files": {name: f"{base}/{name}" for name in MODEL_FILES},
            }]
        return [mirror, hf]

    @staticmethod
    def _probe_url(url: str) -> tuple[bool, int, str]:
        # 离线模式：外网源直接判失败，连 HEAD 都不发（本地自定义源仍放行）
        try:
            netmode.check(url, "语音模型下载")
        except netmode.OfflineError as exc:
            return False, -1, str(exc)
        headers = {"User-Agent": UA}
        try:
            req = urllib.request.Request(url, headers=headers, method="HEAD")
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
                length = int(resp.headers.get("Content-Length") or -1)
                return True, length, ""
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 405, 501):
                return False, -1, f"HTTP {exc.code}"
            # 有些 CDN 不支持 HEAD，用 Range 探测一小段
            try:
                req = urllib.request.Request(
                    url, headers={**headers, "Range": "bytes=0-0"}, method="GET")
                with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
                    content_range = resp.headers.get("Content-Range") or ""
                    total = content_range.rsplit("/", 1)[-1]
                    length = int(total) if total.isdigit() else -1
                    resp.read(64)
                    return True, length, ""
            except Exception as inner:
                return False, -1, _short_error(inner)
        except Exception as exc:
            return False, -1, _short_error(exc)

    def _probe_source(self, src: dict) -> tuple[bool, dict, str]:
        lengths = {}
        for name, url in src["files"].items():
            ok, length, why = self._probe_url(url)
            if not ok:
                return False, {}, f"{name}：{why}"
            lengths[name] = length
        return True, lengths, ""

    def download_status(self) -> dict:
        with self._dl_lock:
            data = dict(self._dl)
            data["running"] = bool(self._dl_thread and self._dl_thread.is_alive())
            data["cancellable"] = data["state"] == "downloading"
            data["model_ready"] = self.model_ready()
            data["source_name"] = self._source_name
        return data

    def _dl_update(self, **kw):
        with self._dl_lock:
            self._dl.update(kw)
            self._dl["updated_at"] = time.time()

    def _dl_should_cancel(self) -> bool:
        with self._dl_lock:
            return self._dl_cancel

    def _on_progress(self, done, total, phase, file=None):
        percent = 0.0
        if total and total > 0 and done is not None:
            percent = min(100.0, round(done * 100.0 / total, 1))
        update = {
            "state": phase,
            "done": int(done or 0),
            "total": int(total if total is not None else -1),
            "percent": percent,
            "phase": phase,
        }
        if file:
            update["file"] = file
        self._dl_update(**update)

    def start_download(self) -> dict:
        if self.model_ready():
            self._dl_update(state="done", percent=100.0, phase="done",
                            error="", error_kind="")
            return self.download_status()
        with self._dl_lock:
            if self._dl_thread and self._dl_thread.is_alive():
                return self._download_status_locked()
            self._dl_cancel = False
            self._dl.update(
                state="downloading", done=0, total=-1, percent=0.0,
                error="", error_kind="", phase="downloading", file="",
                source=self._source_name, source_url=self._source_url,
                started_at=time.time(), updated_at=time.time(),
            )
            self._dl_thread = threading.Thread(
                target=self._download_worker, name="tts-model-download", daemon=True)
            self._dl_thread.start()
        return self.download_status()

    def _download_status_locked(self) -> dict:
        data = dict(self._dl)
        data["running"] = bool(self._dl_thread and self._dl_thread.is_alive())
        data["cancellable"] = data["state"] == "downloading"
        data["model_ready"] = self.model_ready()
        data["source_name"] = self._source_name
        return data

    def request_cancel(self) -> dict:
        with self._dl_lock:
            if self._dl_thread and self._dl_thread.is_alive():
                self._dl_cancel = True
                self._dl["phase"] = "cancelling"
        return self.download_status()

    def _download_worker(self):
        try:
            self.download_model(progress=self._on_progress, cancelled=self._dl_should_cancel)
            self.unload()
            # 下载完试加载一次：能加载就保持常驻，下一次播报不用再等几十秒
            if self._has_runtime():
                try:
                    with self._lock:
                        self._ensure_voice_locked()
                except Exception as exc:
                    devlog.debug("tts._download_worker", error=repr(exc))
                    pass
            self._dl_update(state="done", percent=100.0, phase="done", file="",
                            error="", error_kind="", done=self._dl.get("total", -1))
            print(f"[DeckMeido] 语音合成模型就绪：{self.model_root}")
        except TTSError as exc:
            state = "cancelled" if exc.kind == "cancelled" else "error"
            self._dl_update(state=state, phase=state, error=str(exc),
                            error_kind=exc.kind)
        except Exception as exc:  # 线程里抛出去会静默消失
            self._dl_update(state="error", phase="error", error=_short_error(exc),
                            error_kind="general")

    def _prepare_root(self) -> Path:
        root = self._safe_root()
        if root is None:
            raise TTSError(self._root_error or "数据根不可用", kind="permission")
        try:
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(0o700)
        except OSError as exc:
            raise TTSError(f"模型目录不可写：{root}（{exc}）", kind="permission") from exc
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = -1
        if 0 <= free < MIN_FREE_BYTES:
            raise TTSError(
                f"磁盘空间不足：仅剩 {free / 1024**2:.0f} MB，语音模型至少需要 80 MB",
                kind="disk")
        return root

    def _fetch(self, url: str, part: Path, cancelled, on_bytes):
        # 离线模式：探测被放过（例如本地自定义源）时，真正下载这一层再兜一次
        netmode.check(url, "语音模型下载")
        resume = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": UA}
        if resume:
            headers["Range"] = f"bytes={resume}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = getattr(resp, "status", 200)
            mode = "wb"
            if resume and code == 206:
                mode = "ab"
            elif resume:
                resume = 0  # 源不支持续传，只能重下
            length = int(resp.headers.get("Content-Length") or -1)
            total = resume + length if length >= 0 else -1
            done = resume
            on_bytes(done, total)
            with open(part, mode) as fh:
                while True:
                    if cancelled and cancelled():
                        raise TTSError("已取消", kind="cancelled")
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    on_bytes(done, total)
            if total > 0 and done < total:
                raise TTSError(f"连接提前结束：{done}/{total} 字节", kind="network")

    def _fetch_resumable(self, url: str, part: Path, cancelled, on_bytes):
        last = ""
        for attempt in range(RETRIES_PER_SOURCE + 1):
            if cancelled and cancelled():
                raise TTSError("已取消", kind="cancelled")
            try:
                self._fetch(url, part, cancelled, on_bytes)
                return
            except TTSError:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    part.unlink(missing_ok=True)
                last = f"HTTP {exc.code}"
            except Exception as exc:
                last = _short_error(exc)
            if attempt < RETRIES_PER_SOURCE:
                for _ in range(10):
                    if cancelled and cancelled():
                        raise TTSError("已取消", kind="cancelled")
                    time.sleep(0.1)
        # 重试次数用完：删掉半成品，让下一次尝试从头开始而不是接着坏文件续传
        part.unlink(missing_ok=True)
        raise TTSError(f"下载中断（已重试 {RETRIES_PER_SOURCE} 次）：{last}", kind="network")

    def _download_files(self, src: dict, lengths: dict, progress=None, cancelled=None):
        root = self._prepare_root()
        total = sum(v for v in lengths.values() if v and v > 0) or -1
        base = 0
        for name in MODEL_FILES:
            dest = root / name
            part = dest.with_name(dest.name + ".part")
            minimum = MIN_ONNX_BYTES if name == ONNX_NAME else 1
            # 已有文件必须「大小对且能过校验」才算数：半成品/旧版本一律重下
            if dest.is_file() and dest.stat().st_size >= minimum:
                base += dest.stat().st_size
                continue

            def on_bytes(done, _file_total, _base=base, _label=name):
                if progress:
                    progress(_base + done, total, "downloading", _label)

            self._fetch_resumable(src["files"][name], part, cancelled, on_bytes)
            size = part.stat().st_size
            expected = ONNX_BYTES if name == ONNX_NAME else -1
            if size < minimum or (expected > 0 and size != expected):
                part.unlink(missing_ok=True)
                raise TTSError(
                    f"下载的 {name} 不完整（{size} 字节"
                    + (f"，应为 {expected}" if expected > 0 else "")
                    + "）", kind="network")
            part.replace(dest)
            base += size
        self._validate_files(root)

    def _validate_files(self, root: Path):
        onnx = root / ONNX_NAME
        config = root / CONFIG_NAME
        for path in (onnx, config):
            if not path.is_file():
                raise TTSError(f"语音模型文件缺失：{path}", kind="model")
        size = onnx.stat().st_size
        if size < MIN_ONNX_BYTES:
            raise TTSError(
                f"模型文件大小异常：{size / 1024**2:.1f} MB（完整模型约 38 MB），可能没下全",
                kind="network")
        digest = _sha256_file(onnx)
        if digest != ONNX_SHA256:
            # 坏文件留着只会每次都加载失败，删掉让下次下载能重来
            onnx.unlink(missing_ok=True)
            raise TTSError(
                f"模型文件校验失败（sha256 {digest[:12]}…，应为 {ONNX_SHA256[:12]}…），"
                "已删除损坏文件，重新下载即可", kind="model")
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TTSError(f"config.json 无法解析：{exc}", kind="model") from exc
        if not isinstance(data, dict) or not data:
            raise TTSError("config.json 内容为空或格式不对", kind="model")

    def download_model(self, progress=None, cancelled=None) -> Path:
        root = self._prepare_root()
        sources = self._source_defs()
        errors = []
        chosen = None
        for src in sources:
            ok, lengths, why = self._probe_source(src)
            if ok:
                chosen = (src, lengths)
                self._source_name = src["name"]
                self._source_url = src["files"][MODEL_FILES[0]]
                self._dl_update(source=src["name"], source_url=self._source_url)
                break
            errors.append(f"{src['label']}：{why}")
        if chosen is None:
            raise TTSError("所有下载源都不可用：" + "；".join(errors), kind="network")
        self._download_files(chosen[0], chosen[1], progress=progress, cancelled=cancelled)
        if not self.model_ready():
            raise TTSError("下载完成但模型校验仍未通过", kind="model")
        return root
