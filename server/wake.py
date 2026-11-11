"""智能音箱模式：常驻唤醒检测 + 半双工语音轮次。

设计要点（完整方案见 docs/voice-wake-plan.md）：

* **全部在本地**：KWS（sherpa-onnx）+ silero VAD（同运行时）在设备内做唤醒，
  不联网、不调用云 API。识别出的文字才会进入现有对话链路。
* **三道闸**：KWS 置信度阈值、VAD 确认真人声、最短语音时长。任一不过就不
  转写、不发送。
* **半双工**：唤醒后先静音外放（复用 audiomute.duck），说完立刻恢复；
  TTS 播放期间丢弃所有采集帧，避免自激。
* **音频不落盘**：唤醒前后都只在内存里保留帧；转写用的 WAV 也是内存字节，
  交给 stt.transcribe，用完即丢。
* **状态机**：disarmed → armed → waking（播「我在」）→ listening → thinking →
  speaking → listening（追问窗口）→ armed；任何异常都会回到 armed，并把原因
  放进 last_error。录音阶段如果一直没人说话，wake_timeout_sec 后自动回 armed，
  眼睛状态同步退回。

这个模块不导入 main.py，避免循环依赖；对话入口、广播和电源保持由 main.py
在启动时通过 set_handlers() 注入。测试可以注入假的采集/检测/转写/对话实现。
"""
from __future__ import annotations

import asyncio
import io
import os
import queue
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import wave
from array import array
from pathlib import Path

from . import devlog, media, netmode, paths

SAMPLE_RATE = 16000
FRAME_MS = 160
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 16k 单声道 16bit = 5120
_MIN_FREE_BYTES = 200 * 1024**2
# 没有可播的「我在」时，屏幕上的字幕至少停留这么久，避免状态一闪而过
ACK_FALLBACK_HOLD_SEC = 0.6

# KWS 模型（sherpa-onnx 官方 release）
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2"
)
MODEL_ARCHIVE = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2"
MODEL_INNER = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
VAD_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "silero_vad.onnx"
)
VAD_NAME = "silero_vad.onnx"

DEFAULTS = {
    "enabled": False,
    "wake_words": ["你好梅朵"],
    "keywords_threshold": 0.25,
    "chunk_size": 8,
    "input_device": "default",
    "min_speech_ms": 600,
    "max_utterance_sec": 15,
    "followup_sec": 4,
    # 唤醒后如果一直没人说话，多久退回待机。取 8 秒：比「我在」播完后的
    # 自然反应时间宽裕一点，又不至于长时间占着麦克风。
    "wake_timeout_sec": 8.0,
    "silence_ms": 700,
    "eyes_display": "auto",
    # 唤醒回应：必须在服务启动时预合成并缓存，命中瞬间只做播放。
    "ack_text": "我在",
    # 口令直连：命中本地口令（放歌/切歌/音量）就直接执行，不调模型。
    # 关掉后全部都走对话链路，长句点播交给模型的 music_play 工具。
    "local_intent": False,
}

STATES = ("disarmed", "armed", "waking", "listening", "thinking", "speaking",
          "error")

_MESSAGES = {
    "no_capture": "缺少 parec / pw-record（PipeWire 录音工具），无法常驻监听",
    "no_runtime": "未安装 sherpa-onnx，无法做本地唤醒（pip install -r requirements-stt.txt）",
    "no_model": "唤醒模型未下载，请保持联网后重试（约 38MB）",
    "no_keywords": "唤醒词文件生成失败，请检查 sherpa-onnx-cli 是否可用",
}


class WakeError(RuntimeError):
    """可预期的唤醒服务错误（缺工具/缺模型/模型加载失败）"""


def _merge_cfg(raw: dict | None) -> dict:
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        # 配置段沿用项目习惯写 enable；内部统一成 enabled
        if "enabled" not in raw and raw.get("enable") is not None:
            raw = {**raw, "enabled": bool(raw.get("enable"))}
        for key, value in raw.items():
            if key in cfg and value is not None:
                cfg[key] = value
    # 唤醒词统一成字符串列表；空列表回落到默认词
    words = cfg.get("wake_words")
    if isinstance(words, str):
        words = [words]
    if not isinstance(words, list):
        words = []
    words = [str(w).strip() for w in words if str(w).strip()]
    cfg["wake_words"] = words or list(DEFAULTS["wake_words"])
    return cfg


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """PCM 字节 → 内存 WAV；不写磁盘。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def pcm_level(frame: bytes) -> float:
    """归一化电平（0–1），给眼睛界面做音量反馈；样本很少，直接遍历即可。"""
    if not frame:
        return 0.0
    samples = array("h")
    try:
        samples.frombytes(frame[: len(frame) // 2 * 2])
    except Exception:
        return 0.0
    if not samples:
        return 0.0
    return min(1.0, sum(abs(s) for s in samples) / (len(samples) * 32768.0))


# ---------- 模型准备 ----------


def model_dir(root: Path) -> Path:
    """返回真正含 encoder/decoder/joiner 的那层目录；找不到返回 root。"""
    if not root.exists():
        return root
    for cand in (root / MODEL_INNER, root):
        if list(cand.glob("encoder*.onnx")):
            return cand
    found = next(root.rglob("encoder*.onnx"), None)
    return found.parent if found else root


def _download(url: str, dest: Path, timeout: float = 60.0) -> None:
    # 离线模式：唤醒模型首次启用要联网下载，直接给出原因，不发起连接
    netmode.check(url, "唤醒模型下载")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "DeckMeido"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)


def ensure_model(root: Path, model_url: str = "") -> Path:
    """确保 KWS 模型与 VAD 模型都在本地；缺了就下载并解包。"""
    root.mkdir(parents=True, exist_ok=True)
    target = model_dir(root)
    if not list(target.glob("encoder*.onnx")):
        archive = root / MODEL_ARCHIVE
        if not archive.is_file():
            try:
                _download(model_url or MODEL_URL, archive, timeout=120)
            except Exception as exc:
                raise WakeError(f"{_MESSAGES['no_model']}（{exc}）") from exc
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                # 只解包普通文件/目录，拒绝路径穿越
                members = [m for m in tar.getmembers()
                           if m.isdir() or (m.isfile() and ".." not in Path(m.name).parts
                                            and not Path(m.name).is_absolute())]
                tar.extractall(root, members=members, filter="data")
        except TypeError:
            # Python 3.11 没有 filter 参数
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(root)
        except Exception as exc:
            raise WakeError(f"唤醒模型解包失败：{exc}") from exc
        target = model_dir(root)
    if not list(target.glob("encoder*.onnx")):
        raise WakeError(_MESSAGES["no_model"])
    vad = target / VAD_NAME
    if not vad.is_file():
        try:
            _download(VAD_URL, vad)
        except Exception as exc:
            raise WakeError(f"VAD 模型下载失败：{exc}") from exc
    return target


def _cli_path() -> str:
    """定位 sherpa-onnx-cli：PATH、当前解释器目录、脚本目录、用户级安装目录。

    Deck 上可能装在 venv（和解释器同目录），也可能 pip --user（~/.local/bin，
    由启动它的客户端/插件决定 PATH），不能只靠 shutil.which。
    """
    names = ("sherpa-onnx-cli", "sherpa-onnx-cli.exe") if os.name == "nt" \
        else ("sherpa-onnx-cli",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    dirs: list[Path] = []
    exe = Path(sys.executable)
    dirs.append(exe.parent)
    try:
        import sysconfig

        scripts = sysconfig.get_path("scripts")
        if scripts:
            dirs.append(Path(scripts))
    except Exception:
        pass
    try:
        import site

        if site.USER_BASE:
            dirs.append(Path(site.USER_BASE) / ("Scripts" if os.name == "nt" else "bin"))
        # Windows 商店版 Python 把用户脚本放在 site-packages 同级 Scripts，
        # 既不在 USER_BASE/Scripts 也不在 sysconfig 的 scripts
        if os.name == "nt" and site.USER_SITE:
            dirs.append(Path(site.USER_SITE).parent / "Scripts")
    except Exception:
        pass
    dirs.append(Path.home() / ".local" / "bin")
    for d in dirs:
        for name in names:
            cand = d / name
            if cand.is_file():
                return str(cand)
    return ""


def ensure_keywords(target: Path, words: list[str]) -> Path:
    """用 text2token 生成 keywords.txt；自定义中文词不需要训练。"""
    raw = target / "keywords_raw.txt"
    out = target / "keywords.txt"
    raw.write_text("\n".join(f"{w} @{w}" for w in words) + "\n", encoding="utf-8")
    tokens = target / "tokens.txt"
    if not tokens.is_file():
        raise WakeError(_MESSAGES["no_keywords"])
    cli = _cli_path()
    if not cli:
        raise WakeError("找不到 sherpa-onnx-cli，无法生成唤醒词文件")
    last = ""
    # 这个模型用 phone+ppinyin；ppinyin 是它的简化版。text2token 只认下面这几种，
    # 传别的值 CLI 会直接报 Invalid value。
    for tokens_type in ("phone+ppinyin", "ppinyin"):
        try:
            proc = subprocess.run(
                [cli, "text2token", "--tokens", str(tokens),
                 "--tokens-type", tokens_type, str(raw), str(out)],
                capture_output=True, text=True, timeout=30)
        except Exception as exc:
            last = str(exc)
            continue
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return out
        last = (proc.stderr or proc.stdout or "").strip()[-200:]
    if "sentencepiece" in last:
        last = "缺少 sentencepiece（pip install sentencepiece）"
    raise WakeError(f"{_MESSAGES['no_keywords']}（{last or '未知原因'}）")


# ---------- 检测器：KWS + silero VAD ----------


class SherpaDetector:
    """sherpa-onnx 的唤醒词检测 + 人声检测；加载失败抛 WakeError。"""

    def __init__(self, root: Path, words: list[str], threshold: float,
                 chunk_size: int = 8):
        self._root = root
        self._words = words
        self._threshold = float(threshold)
        self._chunk_size = int(chunk_size)
        self._spotter = None
        self._vad = None
        self._stream = None
        self._np = None

    def load(self) -> None:
        target = ensure_model(self._root)
        keywords = ensure_keywords(target, self._words)
        try:
            import numpy as np
            import sherpa_onnx
        except Exception as exc:
            raise WakeError(_MESSAGES["no_runtime"]) from exc
        encoders = sorted(target.glob("encoder*.onnx"))
        decoders = sorted(target.glob("decoder*.onnx"))
        joiners = sorted(target.glob("joiner*.onnx"))
        if not (encoders and decoders and joiners):
            raise WakeError("唤醒模型文件不完整（缺 encoder/decoder/joiner）")
        try:
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(target / "tokens.txt"),
                encoder=str(encoders[0]),
                decoder=str(decoders[0]),
                joiner=str(joiners[0]),
                num_threads=2,
                max_active_paths=4,
                keywords_file=str(keywords),
                keywords_score=1.0,
                keywords_threshold=self._threshold,
                num_trailing_blanks=1,
            )
            self._stream = self._spotter.create_stream()
            cfg = sherpa_onnx.VadModelConfig()
            cfg.silero_vad.model = str(target / VAD_NAME)
            cfg.silero_vad.threshold = 0.5
            cfg.silero_vad.min_silence_duration = 0.25
            cfg.silero_vad.min_speech_duration = 0.1
            cfg.sample_rate = SAMPLE_RATE
            self._vad = sherpa_onnx.VoiceActivityDetector(
                cfg, buffer_size_in_seconds=30)
        except Exception as exc:
            raise WakeError(f"唤醒模型加载失败：{exc}") from exc
        self._np = np

    def _samples(self, frame: bytes):
        data = self._np.frombuffer(frame, dtype=self._np.int16)
        return data.astype(self._np.float32) / 32768.0

    def accept(self, frame: bytes) -> str | None:
        """喂一帧，命中唤醒词时返回词名；否则 None。"""
        if self._spotter is None:
            return None
        try:
            self._stream.accept_waveform(SAMPLE_RATE, self._samples(frame))
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                # 命中后必须立刻 reset_stream，否则这条流会一直停在结果态
                self._spotter.reset_stream(self._stream)
                return str(result)
        except Exception as exc:
            devlog.log("wake.kws.error", error=str(exc))
        return None

    def speech(self, frame: bytes) -> bool:
        if self._vad is None:
            return False
        try:
            self._vad.accept_waveform(self._samples(frame))
            return bool(self._vad.is_speech_detected())
        except Exception as exc:
            devlog.log("wake.vad.error", error=str(exc))
            return False

    def reset(self) -> None:
        try:
            if self._vad is not None:
                self._vad.reset()
        except Exception:
            pass


# ---------- 采集：parec 优先，pw-record 兜底 ----------


class Capture:
    """常驻 PCM 采集进程；只走内存队列，不落盘。"""

    def __init__(self, device: str = ""):
        self._device = device if device and device != "default" else ""
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._tool = ""

    def _cmd(self) -> list[str]:
        parec = shutil.which("parec")
        if parec:
            self._tool = "parec"
            cmd = [parec, "--format=s16le", "--rate=16000", "--channels=1",
                   "--latency-msec=160"]
            if self._device:
                cmd.append(f"--device={self._device}")
            return cmd
        pw = shutil.which("pw-record")
        if pw:
            self._tool = "pw-record"
            cmd = [pw, "--rate=16000", "--channels=1", "--format=s16"]
            if self._device:
                cmd.append(f"--target={self._device}")
            cmd.append("-")
            return cmd
        raise WakeError(_MESSAGES["no_capture"])

    def start(self) -> None:
        try:
            cmd = self._cmd()
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except WakeError:
            raise
        except Exception as exc:
            raise WakeError(f"启动录音进程失败：{exc}") from exc
        self._thread = threading.Thread(target=self._reader, name="wake-capture",
                                        daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buf = bytearray()
        header = self._tool == "pw-record"
        while not self._stop.is_set():
            chunk = self._proc.stdout.read(FRAME_BYTES * 2)
            if not chunk:
                break
            if header:
                # pw-record 输出 WAV：跳过头部（可能带 LIST 块），从 data 块之后读
                buf.extend(chunk)
                idx = buf.find(b"data")
                if idx < 0:
                    if len(buf) > 4096:
                        buf.clear()
                    continue
                chunk = bytes(buf[idx + 8:])
                header = False
                buf.clear()
            if not chunk:
                continue
            if self._paused.is_set():
                # 半双工：TTS 期间继续读走管道，但不入队，避免积压和自激
                continue
            # 补齐成固定帧，保证 KWS/VAD 的输入长度稳定
            pending = chunk
            while pending:
                try:
                    self._queue.put(pending[:FRAME_BYTES], timeout=0.5)
                except queue.Full:
                    pass
                pending = pending[FRAME_BYTES:]
        self._drain()

    def read(self, timeout: float = 0.2) -> bytes:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return b""

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
            self._drain()
        else:
            self._paused.clear()

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        t = self._thread
        if t is not None:
            t.join(timeout=2)
        self._thread = None
        self._drain()


# ---------- 服务本体 ----------


def _default_ack_synth(text: str) -> bytes:
    """默认用进程里的 TTS 合成「我在」；没有 TTS 就抛错，由服务降级为屏幕显示。"""
    from . import tts as tts_mod

    engine = tts_mod.active()
    if engine is None:
        raise RuntimeError("TTS 未初始化")
    return engine.synth_bytes(text)


def _default_ack_play(data: bytes):
    """默认用进程里的 TTS 播放缓存字节；返回协程，由调用方 await。"""
    from . import tts as tts_mod

    engine = tts_mod.active()
    if engine is None:
        raise RuntimeError("TTS 未初始化")
    return engine.play_bytes(data)


class WakeService:
    """状态机 + 三道闸 + 半双工。所有硬件交互都通过可注入的回调完成。"""

    def __init__(self, model_root: Path | None = None, *,
                 detector_factory=None, capture_factory=None,
                 transcribe=None, chat=None, duck=None, restore=None,
                 power_hold=None, power_release=None,
                 ack_synth=None, ack_play=None,
                 media_duck=None, media_restore=None, media_action=None,
                 media_resolve=None, media_random=None, media_play=None):
        self._root = model_root or paths.wake_model_dir()
        self._detector_factory = detector_factory or (
            lambda root, cfg: SherpaDetector(
                root, cfg["wake_words"], cfg["keywords_threshold"],
                int(cfg["chunk_size"])))
        self._capture_factory = capture_factory
        self._transcribe = transcribe
        self._chat = chat
        self._duck = duck
        self._restore = restore
        self._power_hold = power_hold
        self._power_release = power_release
        # 唤醒回应：合成只在启动/开关打开时做一次，命中时只播放缓存字节。
        self._ack_synth = ack_synth or _default_ack_synth
        self._ack_play = ack_play or _default_ack_play
        self._ack_wav: bytes | None = None
        self._ack_text = ""
        self._ack_error = ""
        self._ack_preparing = False
        # 音乐 duck 与「录音时静音系统输出」（audiomute）是两套：前者只压
        # Decky Music 自己的 MPRIS 音量，不影响 TTS；没有播放器时什么都不做。
        self._media_duck = media_duck
        self._media_restore = media_restore
        self._media_action = media_action
        # 点播：搜（纯查询）→ 随机挑 → 真正开播，三段分开注入，测试可各段造假
        self._media_resolve = media_resolve
        self._media_random = media_random
        self._media_play = media_play
        # 歧义确认只问一次：这里存待确认的计划，下一轮的第一句就回答它
        self._pending_confirm = None
        self._music_ducked = False
        # 音乐是否在放：给眼睛页的闲置判定用。busctl 是子进程，不能每 250ms
        # 探一次（状态接口被高频轮询），缓存 5 秒；探测失败按「没在放」处理。
        self._playing = False
        self._playing_at = 0.0
        # 短反馈词的合成缓存：同一句「切了」只合成一次
        self._fb_wav: dict[str, bytes] = {}
        self._cancel_chat = None
        self._state_cb = None
        self._cfg = _merge_cfg(None)
        self._enabled = False
        self._state = "disarmed"
        self._detector = None
        self._capture = None
        self._reader_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._arm_lock = asyncio.Lock()
        self._ducked = False
        # 转写/分流期间仍显示「倾听」，但不再接受新的录音帧
        self._busy = False
        self._pcm = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0
        self._wait_ms = 0
        self._speech_started = False
        self._followup_until = 0.0
        self._followup = False
        self._last_error = ""
        self._wake_count = 0
        self._level = 0.0
        self._turns = 0
        self._error_timer = None
        self._round_cancelled = False
        # 组件是否真的起来了：只有它为 True，error 态才允许 3 秒后自称「待机」。
        # 否则（模型缺失、没装 sherpa、麦克风被占）眼睛会显示「监听中」但实际没在听。
        self._ready = False
        self._clock = time.monotonic

    # ---- 配置与状态 ----

    def apply(self, raw: dict | None) -> None:
        """热更新 [wake] 段；关掉开关时立刻退出监听。"""
        old = dict(self._cfg)
        self._cfg = _merge_cfg(raw)
        was = self._enabled
        self._enabled = bool(self._cfg.get("enabled", False))
        ack_changed = old.get("ack_text") != self._cfg.get("ack_text")
        if ack_changed:
            # 回应文本改了：旧缓存作废，重新预合成
            self._ack_wav = None
            self._ack_text = ""
        if was and not self._enabled:
            try:
                asyncio.get_running_loop().create_task(self.disarm())
            except RuntimeError:
                pass
        elif not was and self._enabled:
            # 设置页刚打开总开关：自动进入待机监听（模型准备失败会进 error）
            try:
                asyncio.get_running_loop().create_task(self._arm_quiet())
            except RuntimeError:
                pass
        elif was and self._enabled and old.get("wake_words") != self._cfg["wake_words"]:
            # 唤醒词/阈值改了：下一轮重新加载检测器
            self._detector = None
            try:
                asyncio.get_running_loop().create_task(self._rebuild())
            except RuntimeError:
                pass
        if ack_changed and was and self._enabled and self._state != "disarmed":
            self._start_ack_prepare()

    def _start_ack_prepare(self) -> asyncio.Task | None:
        """后台预合成「我在」；不阻塞 arm，也不允许失败冒到事件循环。"""
        if self._ack_preparing or self._ack_wav:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        return asyncio.create_task(self._prepare_ack())

    async def _warm_ack(self, timeout: float = 2.5) -> None:
        """arm/重建时尽量让「我在」就绪，但不要卡住整个开关。

        正常热启动合成通常不到 1 秒；如果模型需要冷加载或下载，就放后台继续，
        这期间命中唤醒只显示屏幕字幕。
        """
        task = self._start_ack_prepare()
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _prepare_ack(self) -> None:
        """合成唤醒回应并缓存到内存。失败只记原因，唤醒流程照常可用。"""
        text = str(self._cfg.get("ack_text") or "").strip() or "我在"
        self._ack_text = text
        self._ack_error = ""
        self._ack_wav = None
        self._ack_preparing = True
        try:
            data = await asyncio.to_thread(self._ack_synth, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ack_error = f"回应合成失败：{exc}"
            devlog.log("wake.ack.synth.error", text=text, error=str(exc))
        else:
            if data:
                self._ack_wav = bytes(data)
                devlog.log("wake.ack.ready", text=text, bytes=len(self._ack_wav))
            else:
                self._ack_error = "回应合成结果为空"
                devlog.log("wake.ack.synth.empty", text=text)
        finally:
            self._ack_preparing = False

    async def _arm_quiet(self) -> None:
        """后台自动进入待机：失败只落到 error + 日志，不让任务异常冒到事件循环"""
        try:
            await self.arm()
        except WakeError as exc:
            devlog.log("wake.arm.failed", reason=str(exc))

    async def _rebuild(self) -> None:
        await self._stop_capture()
        self._detector = None
        self._ready = False
        if self._enabled and self._state != "disarmed":
            try:
                await self._start_detector()
                await self._start_capture()
            except WakeError as exc:
                self._fail(str(exc))
                return
            self._ready = True
            await self._warm_ack()
            self._set_state("armed")

    def set_handlers(self, *, transcribe=None, chat=None, duck=None, restore=None,
                     power_hold=None, power_release=None, cancel_chat=None,
                     media_duck=None, media_restore=None, media_action=None,
                     media_resolve=None, media_random=None, media_play=None,
                     on_state=None) -> None:
        if transcribe is not None:
            self._transcribe = transcribe
        if chat is not None:
            self._chat = chat
        if duck is not None:
            self._duck = duck
        if restore is not None:
            self._restore = restore
        if power_hold is not None:
            self._power_hold = power_hold
        if power_release is not None:
            self._power_release = power_release
        if cancel_chat is not None:
            self._cancel_chat = cancel_chat
        if media_duck is not None:
            self._media_duck = media_duck
        if media_restore is not None:
            self._media_restore = media_restore
        if media_action is not None:
            self._media_action = media_action
        if media_resolve is not None:
            self._media_resolve = media_resolve
        if media_random is not None:
            self._media_random = media_random
        if media_play is not None:
            self._media_play = media_play
        if on_state is not None:
            self._state_cb = on_state

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        return {
            "state": self._state,
            "enabled": self._enabled,
            "armed": self._state in ("armed", "listening"),
            "level": round(self._level, 3),
            "wake_count": self._wake_count,
            "turns": self._turns,
            # 音乐是否正被压低（只反映状态，不在轮询里跑 busctl）；
            # playing 带 5 秒缓存，供眼睛页判断「还在放音乐就别当成闲置」
            "media": {"ducked": self._music_ducked,
                      "playing": self._playing_now()},
            "last_error": self._last_error,
            "input_device": self._cfg.get("input_device", "default"),
            "wake_words": list(self._cfg.get("wake_words") or []),
            "eyes_display": self._cfg.get("eyes_display", "auto"),
            # 唤醒回应缓存是否就绪：眼睛页在合成失败时只显示「我在」，不出声
            "ack": {
                "text": self._ack_text or str(self._cfg.get("ack_text") or "我在"),
                "ready": bool(self._ack_wav),
                "error": self._ack_error,
            },
            "gates": {
                "kws_threshold": float(self._cfg.get("keywords_threshold", 0.25)),
                "vad": self._detector is not None,
                "min_speech_ms": int(self._cfg.get("min_speech_ms", 600)),
            },
            "runtime": {
                "detector": self._detector is not None,
                "capture": self._capture is not None,
                "transcribe": self._transcribe is not None,
                "chat": self._chat is not None,
            },
        }

    # ---- 开关 ----

    async def arm(self) -> dict:
        async with self._arm_lock:
            if not self._enabled:
                raise WakeError("智能音箱模式总开关未打开")
            if self._state in ("waking", "listening", "thinking", "speaking"):
                return self.status()
            self._last_error = ""
            try:
                if self._detector is None:
                    await self._start_detector()
                if self._capture is None:
                    await self._start_capture()
            except WakeError as exc:
                self._ready = False
                self._fail(str(exc))
                raise
            self._ready = True
            await self._warm_ack()
            self._set_state("armed")
            if self._power_hold:
                try:
                    self._power_hold()
                except Exception:
                    pass
        return self.status()

    async def disarm(self) -> dict:
        self._cancel_round()
        self._ready = False
        await self._stop_capture()
        self._release_duck()
        self._release_music_duck()
        if self._power_release:
            try:
                self._power_release()
            except Exception:
                pass
        self._set_state("disarmed")
        return self.status()

    async def toggle(self, on: bool | None = None) -> dict:
        if on is None:
            on = not self._enabled
        self._enabled = bool(on)
        self._cfg["enabled"] = bool(on)
        if on:
            return await self.arm()
        return await self.disarm()

    async def stop(self) -> dict:
        """停止当前一轮（TTS / 对话），回到待机；不退出模式。"""
        self._cancel_round()
        if self._cancel_chat:
            try:
                result = self._cancel_chat()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        tts = getattr(self, "_tts_stop", None)
        if callable(tts):
            try:
                await tts()
            except Exception:
                pass
        self._release_duck()
        self._release_music_duck()
        self._reset_audio()
        self._set_state("armed")
        return self.status()

    def set_tts_stop(self, fn) -> None:
        self._tts_stop = fn

    # ---- 硬件/检测器 ----

    async def _start_detector(self) -> None:
        cfg = dict(self._cfg)
        try:
            det = await asyncio.to_thread(self._detector_factory, self._root, cfg)
            if hasattr(det, "load"):
                await asyncio.to_thread(det.load)
        except WakeError:
            raise
        except Exception as exc:
            raise WakeError(f"唤醒检测器初始化失败：{exc}") from exc
        self._detector = det

    async def _start_capture(self) -> None:
        factory = self._capture_factory
        if factory is None:
            cap = Capture(str(self._cfg.get("input_device") or ""))
        else:
            cap = factory()
        if cap is None:
            return  # 测试注入：不加采集，靠 feed_frame 喂数据
        try:
            await asyncio.to_thread(cap.start)
        except WakeError:
            raise
        except Exception as exc:
            raise WakeError(f"常驻采集启动失败：{exc}") from exc
        self._capture = cap
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _stop_capture(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        cap, self._capture = self._capture, None
        if cap is not None:
            try:
                await asyncio.to_thread(cap.stop)
            except Exception:
                pass

    async def _reader_loop(self) -> None:
        cap = self._capture
        if cap is None:
            return
        while cap is self._capture:
            frame = await asyncio.to_thread(cap.read, 0.2)
            if not frame:
                await asyncio.sleep(0.02)
                continue
            self.feed_frame(frame)

    # ---- 状态机 ----

    def feed_frame(self, frame: bytes, now: float | None = None) -> None:
        """处理一帧 PCM（16k/单声道/16bit）。测试直接调用它。"""
        # 电平给眼睛做反馈：TTS 期间虽然不入队，页面仍能拿到最近的音量
        self._level = pcm_level(frame)
        # 转写/分流期间沿用最后一段录音，不再接收新帧（避免同一轮被二次触发）
        if self._busy:
            return
        if not self._enabled or self._state in ("disarmed", "error", "waking",
                                                "thinking", "speaking"):
            return
        now = self._clock() if now is None else now
        if self._state == "armed":
            hit = self._detector.accept(frame) if self._detector else None
            if hit:
                self._begin_wake(hit, now)
            return
        if self._state == "listening":
            self._handle_listening(frame, now)

    def _begin_wake(self, word: str, now: float) -> None:
        """命中唤醒词：先切「被叫醒」表情并播放预缓存的「我在」，播完再录音。"""
        self._wake_count += 1
        self._reset_audio()
        self._followup = False
        if self._detector is not None:
            self._detector.reset()
        self._set_state("waking")
        # 命中先把音乐压低：接下来要放「我在」，别让音乐和回应抢声音
        self._duck_music()
        devlog.log("wake.hit", word=word, count=self._wake_count,
                   ack_ready=bool(self._ack_wav))
        self._task = asyncio.create_task(self._run_ack())

    async def _run_ack(self) -> None:
        """播放缓存音频。没有缓存/合成失败/播放失败都静默降级，直接进录音。"""
        self._round_cancelled = False
        played = False
        if self._ack_wav and self._ack_play:
            try:
                result = self._ack_play(self._ack_wav)
                if asyncio.iscoroutine(result):
                    await result
                played = True
                self._ack_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 播放失败不影响这一轮：屏幕上仍然显示「我在」
                self._ack_error = f"回应播放失败：{exc}"
                devlog.log("wake.ack.play.error", error=str(exc))
        if self._round_cancelled or self._state != "waking":
            return
        if not played:
            # 没有声音：让屏幕上的「我在」至少完整显示一次，再进录音
            try:
                await asyncio.sleep(ACK_FALLBACK_HOLD_SEC)
            except asyncio.CancelledError:
                raise
            if self._round_cancelled or self._state != "waking":
                return
        self._enter_listen()

    def _enter_listen(self) -> None:
        """回应播完：开始录音，并重新计时「一直没人说话」的超时。"""
        self._reset_audio()
        self._wait_until = self._clock() + float(
            self._cfg.get("wake_timeout_sec", 8.0))
        self._set_state("listening")

    def _handle_listening(self, frame: bytes, now: float) -> None:
        speech = self._detector.speech(frame) if self._detector else False
        if not self._speech_started:
            if speech:
                self._speech_started = True
                self._speech_ms = FRAME_MS
                self._silence_ms = 0
                self._pcm.extend(frame)
                self._duck_now()
                # 追问窗口里再次开口：音乐同样要压低
                self._duck_music()
                return
            # 唤醒后没人说话：超时直接退回待机，不转写、不发送
            if not self._followup and now >= getattr(self, "_wait_until", now):
                self._abort("没听到说话")
            elif self._followup and now >= self._followup_until:
                self._to_armed()
            return
        self._pcm.extend(frame)
        if speech:
            self._speech_ms += FRAME_MS
            self._silence_ms = 0
        else:
            self._silence_ms += FRAME_MS
        if self._silence_ms >= int(self._cfg.get("silence_ms", 700)):
            self._finish_utterance()
        elif self._speech_ms >= int(self._cfg.get("max_utterance_sec", 15)) * 1000:
            self._finish_utterance()

    def _finish_utterance(self) -> None:
        min_ms = int(self._cfg.get("min_speech_ms", 600))
        if self._speech_ms < min_ms:
            self._abort("说话太短")
            return
        pcm = bytes(self._pcm)
        self._reset_audio()
        # 录音结束就还原系统静音（audiomute）；音乐会一直压到本轮说完再恢复
        self._release_duck()
        if not pcm:
            self._to_armed()
            return
        # 从这一刻到分流结束都不再收新帧：转写期间状态仍是 listening
        self._busy = True
        self._task = asyncio.create_task(self._run_round(pcm_to_wav(pcm)))

    async def _run_round(self, wav: bytes) -> None:
        self._round_cancelled = False
        try:
            text = ""
            if self._transcribe:
                text = await asyncio.to_thread(self._transcribe, wav)
            text = (text or "").strip()
            if not text:
                self._abort("没有识别到文字")
                return
            devlog.log("wake.transcribe", chars=len(text), text=text)
            # 上一轮问过「是 XXX 那首吗」：这一句先当作回答，最多问一次
            pending, self._pending_confirm = self._pending_confirm, None
            if pending is not None:
                answer = media.is_yes(text)
                if answer is True:
                    await self._play_and_ack(pending)
                    return
                if answer is False:
                    await self._speak_short("那算了")
                    if not self._round_cancelled:
                        self._to_armed()
                    return
                # 既不是「对」也不是「不是」：按普通输入继续，绝不循环追问
            # 本地快速通道：命中口令就直接执行，不进对话、不进思考态
            # （配置 local_intent=false 时保持旧行为，全都交给对话链路）
            intent = media.match_intent(text)
            if intent is not None and self._cfg.get("local_intent", False):
                devlog.log("wake.intent", kind=intent.kind, action=intent.action,
                           query=intent.query)
                if intent.kind == "random":
                    await self._run_random()
                    return
                if intent.kind == "song":
                    await self._run_song(intent.query)
                    return
                if self._media_action:
                    await self._run_media(intent.action, intent.feedback)
                    return
            if intent is not None:
                devlog.log("wake.intent.agent", kind=intent.kind,
                           action=intent.action, query=intent.query)
            self._set_state("thinking")
            if not self._chat:
                raise WakeError("对话入口未就绪")
            # 本地没抓住口令的句子（含长句点播）交给模型，模型自己带 music_play 工具
            # 本轮强制 TTS：handle_chat 在 force_tts=True 时会 await 播放结束
            await self._chat(text, force_tts=True, client="wake")
            # start_chat 会吞掉 CancelledError 返回 cancelled=True，所以这里必须
            # 看自己的取消标记，不能只看有没有抛异常。
            if self._round_cancelled:
                self._set_state("armed")
                return
            self._turns += 1
            self._enter_followup()
        except asyncio.CancelledError:
            self._release_duck()
            self._set_state("armed")
            raise
        except Exception as exc:
            self._fail(f"对话失败：{exc}")
        finally:
            self._busy = False
            # 「我在」、回答、口令反馈都结束：恢复音乐音量
            self._release_music_duck()

    async def _run_media(self, action: str, feedback: str) -> None:
        """本地意图快通道：固定动作 + 一句短反馈，不经过对话链路。"""
        speech = feedback
        if action in media.VOLUME_ACTIONS:
            # 用户要改音量：先把 duck 恢复成原值，否则「恢复」会把他要求的值冲掉
            self._release_music_duck()
        try:
            await asyncio.to_thread(self._media_action, action)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 播控失败只影响这一句提示：主流程照常回待机，不卡在非待机态
            speech = getattr(exc, "speech", "") or "操作失败"
            self._last_error = getattr(exc, "reason", "") or str(exc) or "媒体控制失败"
            devlog.log("wake.media.error", action=action, error=str(exc))
        if self._round_cancelled:
            return
        await self._speak_short(speech)
        if self._round_cancelled:
            return
        self._turns += 1
        self._to_armed()

    async def _run_song(self, query: str) -> None:
        """点播：搜曲库 → 唯一高分直接放；歧义问一次；没找到就直说。"""
        if not self._media_resolve:
            await self._speak_short("点播没准备好")
            if not self._round_cancelled:
                self._to_armed()
            return
        try:
            plan = await asyncio.to_thread(self._media_resolve, query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            speech = getattr(exc, "speech", "") or "没找到"
            self._last_error = getattr(exc, "reason", "") or str(exc) or "点播失败"
            devlog.log("wake.song.resolve.error", query=query, error=str(exc))
            if not self._round_cancelled:
                await self._speak_short(speech)
            if not self._round_cancelled:
                self._to_armed()
            return
        if self._round_cancelled:
            return
        devlog.log("wake.song.plan", query=query, status=plan.status,
                   name=plan.name, singer=plan.singer)
        if plan.status == "ask":
            # 只问一次：下一轮的第一句话由 _run_round 当作回答处理
            self._pending_confirm = plan
            await self._speak_short(plan.speech)
            if not self._round_cancelled:
                self._turns += 1
                self._enter_listen()
            return
        if plan.status != "ok":
            await self._speak_short(plan.speech or "没找到")
            if not self._round_cancelled:
                self._to_armed()
            return
        await self._play_and_ack(plan)

    async def _run_random(self) -> None:
        """随机放一首：从当前队列里挑，排除正在放的那首。"""
        if not self._media_random:
            await self._speak_short("点播没准备好")
            if not self._round_cancelled:
                self._to_armed()
            return
        try:
            plan = await asyncio.to_thread(self._media_random)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            speech = getattr(exc, "speech", "") or "播放失败"
            self._last_error = getattr(exc, "reason", "") or str(exc) or "随机播放失败"
            devlog.log("wake.song.random.error", error=str(exc))
            if not self._round_cancelled:
                await self._speak_short(speech)
            if not self._round_cancelled:
                self._to_armed()
            return
        if self._round_cancelled:
            return
        devlog.log("wake.song.plan", query="<random>", status=plan.status,
                   name=plan.name, singer=plan.singer)
        if plan.status != "ok":
            await self._speak_short(plan.speech or "曲库空的")
            if not self._round_cancelled:
                self._to_armed()
            return
        await self._play_and_ack(plan)

    async def _play_and_ack(self, plan) -> None:
        """真正开播 + 一句短反馈。播放失败也念原因，然后照常回待机。"""
        speech = plan.speech or f"放{plan.label}"
        played = False
        try:
            if not self._media_play:
                raise media.MediaError("点播入口未就绪", "播放失败")
            await asyncio.to_thread(self._media_play, plan.item)
            played = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            speech = getattr(exc, "speech", "") or "播放失败"
            self._last_error = getattr(exc, "reason", "") or str(exc) or "播放失败"
            devlog.log("wake.song.play.error", name=plan.name, error=str(exc))
        devlog.log("wake.song.play", name=plan.name, ok=played,
                   id=str(plan.item.get("id") if plan.item else ""))
        if self._round_cancelled:
            return
        await self._speak_short(speech)
        if self._round_cancelled:
            return
        self._turns += 1
        self._to_armed()

    async def _speak_short(self, text: str) -> None:
        """念一句极短反馈；合成或播放失败都只记日志，不拦回待机。"""
        if not text:
            return
        data = self._fb_wav.get(text)
        if data is None:
            try:
                data = await asyncio.to_thread(self._ack_synth, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                devlog.log("wake.media.feedback.synth.error", text=text,
                           error=str(exc))
                return
            if not data:
                return
            data = bytes(data)
            self._fb_wav[text] = data
        self._set_state("speaking")
        try:
            result = self._ack_play(data)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            devlog.log("wake.media.feedback.play.error", error=str(exc))

    def _enter_followup(self) -> None:
        self._reset_audio()
        self._followup = True
        self._followup_until = self._clock() + float(
            self._cfg.get("followup_sec", 4))
        self._set_state("listening")

    # ---- TTS 状态 ----

    def note_tts(self, kind: str) -> None:
        """由 TTS 播放器通知：start / end。只影响眼睛显示，不改变轮次收尾。"""
        if kind == "start" and self._state == "thinking":
            self._set_state("speaking")
        elif kind == "end" and self._state == "speaking":
            # 真正的「回监听」由 _run_round 在 await 返回后做，避免两处抢状态
            pass

    # ---- 内部工具 ----

    def _set_state(self, state: str) -> None:
        if state not in STATES:
            return
        if self._state == state:
            return
        self._state = state
        # 半双工：唤醒回应/思考/播报期间暂停采集入队；回到待机/聆听时恢复
        if self._capture is not None:
            try:
                self._capture.set_paused(state in ("waking", "thinking", "speaking"))
            except Exception:
                pass
        cb = self._state_cb
        if cb:
            try:
                result = cb(self.status())
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(result)
            except Exception:
                pass

    def _abort(self, reason: str) -> None:
        self._pending_confirm = None
        self._release_duck()
        self._release_music_duck()
        self._reset_audio()
        if reason:
            self._last_error = reason
            devlog.log("wake.abort", reason=reason)
        self._set_state("armed")

    def _fail(self, reason: str) -> None:
        self._pending_confirm = None
        self._release_duck()
        self._release_music_duck()
        self._reset_audio()
        self._last_error = reason
        devlog.log("wake.error", reason=reason)
        self._set_state("error")
        # 3 秒后自动回到待机，避免眼睛界面永久停在错误态。
        # 但只有组件确实起来了才允许回待机：启动失败（缺模型/缺 sherpa/麦克风被占）
        # 必须一直停在 error，否则界面会显示「监听中」而实际什么都没在听。
        if not self._ready:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._error_timer is not None:
            self._error_timer.cancel()
        self._error_timer = loop.call_later(
            3.0, lambda: self._set_state("armed")
            if self._state == "error" else None)

    def _to_armed(self) -> None:
        self._pending_confirm = None
        self._release_duck()
        self._release_music_duck()
        self._reset_audio()
        self._set_state("armed")

    def _reset_audio(self) -> None:
        self._busy = False
        self._pcm.clear()
        self._speech_ms = 0
        self._silence_ms = 0
        self._wait_ms = 0
        self._speech_started = False
        self._followup = False

    def _duck_now(self) -> None:
        if self._ducked:
            return
        if self._duck:
            try:
                self._duck()
                self._ducked = True
            except Exception:
                pass

    def _release_duck(self) -> None:
        if not self._ducked:
            return
        self._ducked = False
        if self._restore:
            try:
                self._restore()
            except Exception:
                pass

    def _playing_now(self) -> bool:
        """音乐是否在放，5 秒缓存。探测异常一律当「没在放」。

        缓存放宽是必要的：眼睛页 250ms 轮询一次状态，每次都起 busctl
        子进程会把 Deck 拖得很重；而「有没有在放音乐」5 秒的粒度足够。
        """
        now = time.monotonic()
        if now - self._playing_at >= 5.0:
            try:
                self._playing = media.playing()
            except Exception as exc:
                devlog.log("wake.media.playing.error", error=str(exc))
                self._playing = False
            self._playing_at = now
        return self._playing

    def _duck_music(self) -> None:
        """压低正在播放的音乐。handler 返回 True 才记状态；异常一律吞掉。"""
        if self._music_ducked or not self._media_duck:
            return
        try:
            ducked = self._media_duck()
        except Exception as exc:
            devlog.log("wake.media.duck.error", error=str(exc))
            return
        if ducked:
            self._music_ducked = True

    def _release_music_duck(self) -> None:
        """恢复音乐音量。无论哪条收尾路径都要走到，异常也不能抛出去。"""
        if not self._music_ducked:
            return
        self._music_ducked = False
        if self._media_restore:
            try:
                self._media_restore()
            except Exception as exc:
                devlog.log("wake.media.restore.error", error=str(exc))

    def _cancel_round(self) -> None:
        self._round_cancelled = True
        self._pending_confirm = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        self._release_duck()
        self._release_music_duck()
        self._reset_audio()
