"""TTS 语音播报：piper-plus（つくよみちゃん 6 语模型）为主，espeak-ng 兜底。

对外契约保持不变：`speak(text)` 在后台念、`stop()` 立刻掐断，新句子打断旧句子。
区别在于主引擎换成了本地神经网络模型：

- 模型不随发布包分发，首次使用时从 hf-mirror.com / huggingface.co 下载到
  `<数据根>/models/tts/tsukuyomi/`；
- 懒加载：第一次真正播报才 import piper_plus 并加载 ONNX，空闲一段时间后释放；
- ONNX 推理不是线程安全的，所有合成调用用同一把锁串行，并放到线程里跑，
  不阻塞事件循环；
- 运行时未装、模型缺失或加载失败时自动退回 espeak-ng，`status()` 里
  能看出当前实际用哪个引擎、为什么降级。

音声：つくよみちゃんコーパス（夢前黎）。合成产物只在本机播放，不再分发。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from . import paths

# ---------- 模型与音色 ----------

VOICE_ID = "tsukuyomi"
VOICE_LABEL = "つくよみちゃん（ja/zh/en/es/fr/pt）"
MODEL_DIR_NAME = "tsukuyomi"
ONNX_NAME = "tsukuyomi-chan-6lang-fp16.onnx"
CONFIG_NAME = "config.json"
MODEL_FILES = (ONNX_NAME, CONFIG_NAME)
# 完整模型约 38MB；少于 30MB 基本就是没下全
MIN_ONNX_BYTES = 30 * 1024 * 1024
MODEL_REPO = "ayousanz/piper-plus-tsukuyomi-chan"
MIRROR_BASE = f"https://hf-mirror.com/{MODEL_REPO}/resolve/main"
HF_BASE = f"https://huggingface.co/{MODEL_REPO}/resolve/main"

ATTRIBUTION = "音声：つくよみちゃんコーパス（夢前黎） / Tsukuyomi-chan Corpus"
ATTRIBUTION_URL = "https://tyc.rei-yumesaki.net/material/corpus/"

LANGUAGES = ("auto", "ja", "zh", "en", "es", "fr", "pt")
ESPEAK_VOICES = (
    ("cmn", "普通话（espeak-ng）"),
    ("en-us", "英语（espeak-ng）"),
    ("ja", "日语（espeak-ng）"),
)

# 模型下载与探测
UA = "DeckMeido-TTS/1.0"
PROBE_TIMEOUT = 6
RETRIES_PER_SOURCE = 3
MIN_FREE_BYTES = 80 * 1024 * 1024

# espeak-ng 的默认语速/上限
ESPEAK_SPEED = 170
MAX_CHARS = 300

# 说话文本清洗：代码块、URL、markdown 标记、常见表情符号都不适合念出来
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MD_CHARS_RE = re.compile(r"[*_`#>\[\]|]")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
_SPACE_RE = re.compile(r"[ \t\u3000]+")

# 分句与语种判定
_SENT_RE = re.compile(r"[^。！？!?；;：:\n]+[。！？!?；;：:\n]?")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_MIXED_SPLIT_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9'’\-]*|[^\x00-\x7f]+|\s+|[^\sA-Za-z]+")


class TTSError(RuntimeError):
    """带原因分类的错误：disk / network / permission / model / runtime"""

    def __init__(self, message: str, kind: str = "general"):
        super().__init__(message)
        self.kind = kind


def normalize_engine(raw: str | None) -> str:
    """把配置里的引擎写成两种之一。

    旧配置可能写的是命令名（espeak-ng / espeak / spd-say），一律按 espeak 兜底；
    认不出来的值也走 espeak，避免一次笔误让播报彻底没声。
    """
    value = str(raw or "").strip().lower()
    if value in ("piper-plus", "piper_plus", "piper", "auto"):
        return "piper-plus"
    if value.startswith(("espeak", "spd-say", "spd_say")):
        return "espeak-ng"
    return "espeak-ng"


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


def route_segments(text: str, language: str = "auto",
                   limit: int = MAX_CHARS) -> list[tuple[str, str]]:
    """按句子/语种切段，返回 [(语种, 文本)]。

    多语模型每个字母脚本按字符自动判定，所以中英混排不必逐字切换；
    这里只在「汉字 + 拉丁字母」同句时按脚本段切开，避免整句被当成一种语言。
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
        has_cjk = bool(_CJK_RE.search(sentence) or _KANA_RE.search(sentence))
        has_latin = bool(_LATIN_RE.search(sentence))
        if has_cjk and has_latin:
            for piece in _MIXED_SPLIT_RE.findall(sentence):
                if not piece.strip():
                    continue
                # 纯标点/空白不成段，避免把「，」当成一句送去合成
                if not (_LATIN_RE.search(piece) or _CJK_RE.search(piece)
                        or _KANA_RE.search(piece)):
                    continue
                out.append((detect_language(piece), piece.strip()))
        else:
            out.append((detect_language(sentence), sentence))
    # 超长段落再按长度硬切，保证每次合成不会一口气吃进一大段
    trimmed: list[tuple[str, str]] = []
    for lang, segment in out:
        while len(segment) > limit:
            trimmed.append((lang, segment[:limit]))
            segment = segment[limit:]
        if segment:
            trimmed.append((lang, segment))
    return trimmed


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{exc.__class__.__name__}: {text}" if text != exc.__class__.__name__ else text


class TTS:
    """语音播报器：piper-plus 懒加载 + espeak-ng 兜底，可下载模型。"""

    def __init__(
        self,
        enable: bool = False,
        engine: str = "piper-plus",
        voice: str = VOICE_ID,
        speed: int = ESPEAK_SPEED,
        max_chars: int = MAX_CHARS,
        speed_scale: float = 1.0,
        length_scale: float = 1.0,
        noise_scale: float = 0.667,
        language: str = "auto",
        model_source: str = "auto",
        model_root: Path | str | None = None,
        idle_unload_sec: float = 600.0,
    ):
        self.enable = bool(enable)
        self.engine = normalize_engine(engine)
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
        self._last_engine = ""
        self._last_error = ""

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
            found = shutil.which(name)
            if found:
                return found
        return ""

    @staticmethod
    def _player() -> str:
        for name in ("pw-play", "paplay", "aplay"):
            found = shutil.which(name)
            if found:
                return found
        return ""

    # ---------- 状态 ----------

    def status(self) -> dict:
        self._maybe_unload()
        runtime = self._has_runtime()
        model = self.model_ready()
        load_error = self._load_error
        if self.engine == "piper-plus":
            if not runtime:
                reason = "未安装 piper-plus 运行时（pip install -r requirements-tts.txt）"
            elif not model:
                reason = "语音模型未下载（约 38 MB）"
            elif load_error:
                reason = load_error
            else:
                reason = ""
            reason = reason or self._root_error
            effective = "piper-plus" if (runtime and model and not load_error) else "espeak-ng"
        else:
            effective = "espeak-ng"
            reason = "" if self._espeak_binary() else "找不到 espeak-ng / espeak / spd-say"
        return {
            "enable": self.enable,
            "engine": self.engine,
            # 当前真正会用的引擎：piper-plus 不可用时这里显示 espeak-ng
            "active_engine": effective,
            "runtime": runtime,
            "model": model,
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
            "ready": effective == "piper-plus" or bool(self._espeak_binary()),
            "reason": reason,
            "loaded": self._voice is not None,
            "player": self._player(),
            "last_engine": self._last_engine,
            "last_error": self._last_error,
            "attribution": ATTRIBUTION,
            "attribution_url": ATTRIBUTION_URL,
            "download": self.download_status(),
        }

    def summary(self) -> dict:
        """给 /api/status 的精简版：只带引擎可用性与原因，字段名前缀 tts_。"""
        state = self.status()
        return {
            "tts_engine": state["active_engine"],
            "tts_runtime": state["runtime"],
            "tts_model": state["model"],
            "tts_ready": state["ready"],
            "tts_error": state["reason"] or state["last_error"],
            "tts_attribution": ATTRIBUTION,
        }

    # ---------- 配置热更新 ----------

    def apply(self, cfg: dict | None):
        """从 [tts] 段热更新；引擎/音色/模型源变化时释放已加载的模型。"""
        cfg = cfg or {}
        engine = normalize_engine(cfg.get("engine", self.engine))
        voice = str(cfg.get("voice", self.voice) or VOICE_ID).strip() or VOICE_ID
        source = str(cfg.get("model_source", self.model_source) or "auto").strip() or "auto"
        if (
            engine != self.engine
            or voice != self.voice
            or source != self.model_source
        ):
            self.unload()
        self.engine = engine
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
        return voice

    # ---------- 播报 ----------

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
        try:
            segments = route_segments(cleaned, self.language, self.max_chars)
            if not segments:
                return
            if self._piper_ready():
                try:
                    await self._speak_piper(segments, event)
                    return
                except Exception as exc:
                    # 合成/播放失败：整段退回 espeak（已经念过的句子可能重复一遍，
                    # 但保证不会因为模型问题彻底没声）
                    self._last_error = f"piper-plus 播报失败：{_short_error(exc)}"
                    if not self.model_ready():
                        self.unload()
            await self._speak_espeak(cleaned, event)
        finally:
            if self._cancel is event:
                self._cancel = None

    async def preview(self, text: str = ""):
        """试听：不受 enable 开关限制，用当前引擎念一句短的。"""
        await self.speak(text or "你好，我是女仆。这样说话好听吗？", force=True)
        return {"engine": self._last_engine, "error": self._last_error}

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

    @staticmethod
    def _language_id(voice, language: str) -> int | None:
        config = getattr(voice, "config", None)
        mapping = getattr(config, "language_id_map", None) or {}
        try:
            value = mapping.get(language)
        except AttributeError:
            return None
        return value if isinstance(value, int) else None

    async def _speak_piper(self, segments: list[tuple[str, str]], event: threading.Event):
        self._last_engine = "piper-plus"
        try:
            for language, segment in segments:
                if event.is_set():
                    return
                wav = await asyncio.to_thread(self._synth_wav, segment, language)
                if event.is_set():
                    wav.unlink(missing_ok=True)
                    return
                try:
                    await self._play(wav, event)
                finally:
                    wav.unlink(missing_ok=True)
        finally:
            self._maybe_unload()

    async def _play(self, wav: Path, event: threading.Event):
        player = self._player()
        if not player:
            raise TTSError("没有可用的播放器（pw-play / paplay / aplay 都找不到）", kind="player")
        proc = await asyncio.create_subprocess_exec(
            player, str(wav),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._proc = proc
        try:
            code = await proc.wait()
        finally:
            if self._proc is proc:
                self._proc = None
        if code not in (0, -15) and not event.is_set():
            raise TTSError(f"播放器退出码 {code}", kind="player")

    async def _speak_espeak(self, text: str, event: threading.Event):
        binary = self._espeak_binary()
        if not binary:
            self._last_error = "找不到 espeak-ng / espeak / spd-say"
            return
        args = [binary]
        name = os.path.basename(binary)
        if name.startswith("espeak"):
            args += ["-v", self.voice, "-s", str(self.speed)]
        elif name.startswith("spd-say"):
            args += ["-l", self.voice, "-r", str(self.speed - 170)]
        args.append(text)
        self._last_engine = "espeak-ng"
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self._last_error = f"找不到语音引擎：{binary}"
            return
        self._proc = proc
        try:
            await proc.wait()
        finally:
            if self._proc is proc:
                self._proc = None
        if proc.returncode not in (0, -15) and not event.is_set():
            self._last_error = f"{name} 退出码 {proc.returncode}"

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
                except Exception:
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
        raise TTSError(f"下载中断（已重试 {RETRIES_PER_SOURCE} 次）：{last}", kind="network")

    def _download_files(self, src: dict, lengths: dict, progress=None, cancelled=None):
        root = self._prepare_root()
        total = sum(v for v in lengths.values() if v and v > 0) or -1
        base = 0
        for name in MODEL_FILES:
            dest = root / name
            part = dest.with_name(dest.name + ".part")
            minimum = MIN_ONNX_BYTES if name == ONNX_NAME else 1
            if dest.is_file() and dest.stat().st_size >= minimum:
                base += dest.stat().st_size
                continue

            def on_bytes(done, _file_total, _base=base, _label=name):
                if progress:
                    progress(_base + done, total, "downloading", _label)

            self._fetch_resumable(src["files"][name], part, cancelled, on_bytes)
            size = part.stat().st_size
            if size < minimum:
                part.unlink(missing_ok=True)
                raise TTSError(f"下载的 {name} 不完整（{size} 字节）", kind="network")
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
