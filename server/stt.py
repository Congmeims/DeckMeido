"""语音输入（STT）：sherpa-onnx + SenseVoice-Small int8，纯 CPU 离线识别

为什么选它（见交接纪要 §5）：用户没有云端 STT 的 key；SenseVoice-Small int8
压缩包约 1GB（只解出 int8 模型，约 250MB）、10 秒语音推算 1–1.5 秒、
中文与中英混说都行、自带标点。
三者都跑在 sherpa-onnx 上，将来换模型不用改这里的调用代码。

几个硬约束：

* **模型不进插件 zip**，也不是安装时的必需依赖。sherpa-onnx 和模型都在
  首次按键时按需下载并校验，装不上只影响语音，不影响其它功能（懒依赖）。
* **懒加载 + 空闲卸载**：第一次识别才建 recognizer，之后空闲
  `IDLE_UNLOAD_SEC` 秒释放，Deck 只有 15W 功耗预算，不能让模型常驻。
* **2 线程**：给推理留 2 个线程就够跑满实时率，线程开多了会和游戏抢 CPU。
* 模型许可 Apache-2.0，可商用但**必须署名并保留模型名**，见 ATTRIBUTION。

录音走 `pw-record`（PipeWire），需要 XDG_RUNTIME_DIR 与 /run/user/1000，
这和抓帧的要求一致。识别接口吃的是前端录完的 WAV，不是直接接麦克风，
所以这里只管「WAV 进、文字出」。
"""
from __future__ import annotations

import errno
import io
import os
import shutil
import tarfile
import threading
import time
import urllib.request
import wave
from pathlib import Path

# 模型署名：Apache-2.0 要求保留，界面上展示「语音识别」的地方一并显示
ATTRIBUTION = "SenseVoice-Small (FunAudioLLM) · sherpa-onnx · Apache-2.0"

# 模型与运行时都放在用户数据目录，不进插件包、也不进 venv
MODEL_DIR_NAME = "sensevoice-small-int8"
# 官方发布的 tar.bz2（内含 model.int8.onnx / tokens.txt）
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
)
MODEL_ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
# 解压后真正的模型文件在顶层同名目录里
MODEL_INNER = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
# 下载 + 解压至少需要的空闲空间：压缩包约 1GB，解出来的 int8 模型约 250MB
MIN_FREE_BYTES = int(1.5 * 1024**3)

# 推理线程数：2 线程已能跑到实时率的数倍，再多只会和游戏抢 CPU
NUM_THREADS = 2
# 空闲多久释放 recognizer（秒）。10 分钟：连续对话不用反复加载，
# 停下来之后又能尽快把内存还给系统
IDLE_UNLOAD_SEC = 600


class STTError(RuntimeError):
    """语音识别相关的可预期错误（没装依赖、模型没下载、音频坏了）

    kind 用来把下载失败分个类，前端按类别给「网络 / 磁盘 / 权限 / 解压」提示，
    而不是把所有失败都笼统说成「模型下载失败」。
    """

    def __init__(self, message: str, kind: str = "general"):
        super().__init__(message)
        self.kind = kind


class STTCancelled(STTError):
    """用户主动取消下载（不是错误，前端只把状态收回去）"""

    def __init__(self, message: str = "已取消下载"):
        super().__init__(message, kind="cancelled")


def _resolve_model_root(base: Path) -> Path:
    """找到含 model.int8.onnx 的那层目录

    发布包里可能多套一层同名目录，写死路径会在换包时失效，
    这里按实际结构找，找不到再报错。
    """
    for cand in (base / MODEL_INNER, base):
        if (cand / "model.int8.onnx").is_file():
            return cand
    found = next(base.rglob("model.int8.onnx"), None)
    if found is None:
        raise STTError(f"模型文件缺失：{base} 下没有 model.int8.onnx")
    return found.parent


class STT:
    """SenseVoice 语音识别器：懒加载、空闲卸载、线程安全"""

    def __init__(self, model_root: Path, enable: bool = True):
        self.model_root = Path(model_root)
        self.enable = enable
        self._rec = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._ready_path: Path | None = None
        # 模型下载任务：后台线程 + 可查询进度。前端不再被一次 600 秒的
        # 同步请求挂住，也能在下载中途取消。
        self._dl_lock = threading.Lock()
        self._dl_thread: threading.Thread | None = None
        self._dl_cancel = False
        self._dl = {
            "state": "idle",  # idle / downloading / extracting / done / error / cancelled
            "done": 0,
            "total": -1,
            "percent": 0.0,
            "file": "",
            "phase": "",
            "error": "",
            "error_kind": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }

    # ---------- 状态 ----------

    def model_ready(self) -> bool:
        try:
            self._ready_path = _resolve_model_root(self.model_root)
            return True
        except STTError:
            return False

    def installed(self) -> bool:
        """sherpa-onnx 装没装（没装时前端提示去装依赖，而不是报一堆栈）"""
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except Exception:
            return False

    def status(self) -> dict:
        return {
            "enable": self.enable,
            "runtime": self.installed(),
            "model": self.model_ready(),
            "model_root": str(self.model_root),
            "attribution": ATTRIBUTION,
            "download": self.download_status(),
        }

    # ---------- 下载任务 ----------

    def download_status(self) -> dict:
        with self._dl_lock:
            data = dict(self._dl)
            data["running"] = bool(self._dl_thread and self._dl_thread.is_alive())
            data["cancellable"] = data["state"] in ("downloading", "extracting")
        return data

    def _dl_update(self, **kw):
        with self._dl_lock:
            self._dl.update(kw)
            self._dl["updated_at"] = time.time()

    def _dl_should_cancel(self) -> bool:
        with self._dl_lock:
            return self._dl_cancel

    def _on_progress(self, done, total, phase):
        """下载/解压进度回调：done 为字节数或文件数，total 未知时 -1"""
        percent = 0.0
        if total and total > 0 and done is not None:
            percent = min(100.0, round(done * 100.0 / total, 1))
        self._dl_update(
            state=phase,
            done=int(done or 0),
            total=int(total if total is not None else -1),
            percent=percent,
            phase=phase,
        )

    def start_download(self) -> dict:
        """启动后台下载；已经在跑就直接复用，已完成则秒回

        返回的是下载状态快照，前端据此显示进度条，不再苦等一个长请求。
        """
        if self.model_ready():
            self._dl_update(
                state="done", percent=100.0, done=self._dl.get("total", -1),
                total=self._dl.get("total", -1), error="", error_kind="",
                phase="done",
            )
            return self.download_status()

        with self._dl_lock:
            if self._dl_thread and self._dl_thread.is_alive():
                return self.download_status_locked()
            self._dl_cancel = False
            self._dl.update(
                state="downloading", done=0, total=-1, percent=0.0,
                error="", error_kind="", phase="downloading",
                file=MODEL_ARCHIVE, started_at=time.time(), updated_at=time.time(),
            )
            self._dl_thread = threading.Thread(
                target=self._download_worker, name="stt-model-download", daemon=True
            )
            self._dl_thread.start()
        return self.download_status()

    def download_status_locked(self) -> dict:
        """调用方已持有 _dl_lock 时用，避免重复加锁"""
        data = dict(self._dl)
        data["running"] = bool(self._dl_thread and self._dl_thread.is_alive())
        data["cancellable"] = data["state"] in ("downloading", "extracting")
        return data

    def request_cancel(self) -> dict:
        with self._dl_lock:
            if self._dl_thread and self._dl_thread.is_alive():
                self._dl_cancel = True
                self._dl["phase"] = "cancelling"
        return self.download_status()

    def _download_worker(self):
        try:
            path = self.download_model(
                progress=self._on_progress, cancelled=self._dl_should_cancel
            )
            # 模型换了/刚下完：把旧的 recognizer 丢掉，下一次识别按新路径重建，
            # 这样下载完成不用重启后端就能直接说话
            self.unload()
            self._dl_update(
                state="done", percent=100.0, phase="done", file="",
                error="", error_kind="", done=self._dl.get("total", -1),
            )
            print(f"[DeckMeido] 语音模型就绪：{path}")
        except STTCancelled:
            self._dl_update(state="cancelled", phase="cancelled", error="", error_kind="")
        except STTError as exc:
            self._dl_update(
                state="error", phase="error", error=str(exc),
                error_kind=getattr(exc, "kind", "general"),
            )
        except Exception as exc:  # 兜底：线程里抛出去会静默消失
            self._dl_update(state="error", phase="error", error=str(exc), error_kind="general")

    # ---------- 模型下载 ----------

    def download_model(self, progress=None, cancelled=None) -> Path:
        """下载并解压模型（压缩包约 1GB，解出约 250MB），已存在则直接返回

        progress(done, total, phase) 用来报进度：phase 是 downloading / extracting，
        done 是已下载字节数或已解压文件数，total 未知时传 -1。
        cancelled() 返回 True 时中止并抛 STTCancelled，由后台任务把状态收回去。
        """
        if self.model_ready():
            return self._ready_path

        try:
            self.model_root.mkdir(parents=True, exist_ok=True)
            self.model_root.chmod(0o700)
        except OSError as exc:
            raise STTError(
                f"模型目录不可写：{self.model_root}（{exc}）；"
                "请检查数据根权限或 DECKMEIDO_DATA_DIR",
                kind="permission",
            ) from exc

        try:
            free = shutil.disk_usage(self.model_root).free
        except OSError:
            free = None
        if free is not None and free < MIN_FREE_BYTES:
            raise STTError(
                f"磁盘空间不足：{self.model_root.parent} 仅剩 {free / 1024**3:.1f} GB，"
                f"下载并解压语音模型至少需要 {MIN_FREE_BYTES / 1024**3:.1f} GB",
                kind="disk",
            )

        archive = self.model_root / MODEL_ARCHIVE
        tmp = archive.with_suffix(".part")
        try:
            if not archive.is_file():
                req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "DeckMeido"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        total = int(resp.headers.get("Content-Length") or -1)
                        done = 0
                        with open(tmp, "wb") as fh:
                            while True:
                                if cancelled and cancelled():
                                    raise STTCancelled()
                                chunk = resp.read(1 << 16)
                                if not chunk:
                                    break
                                fh.write(chunk)
                                done += len(chunk)
                                if progress:
                                    progress(done, total, "downloading")
                except (STTCancelled, STTError):
                    raise
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    raise STTError(f"语音模型下载失败（网络）：{exc}", kind="network") from exc
                if cancelled and cancelled():
                    raise STTCancelled()
                tmp.replace(archive)

            # 只解出要用的三个文件：包里还有一个 fp32 的 model.onnx（937MB），
            # 解出来纯属白占地方，int8 版识别效果够用
            want = {"model.int8.onnx", "tokens.txt", "LICENSE"}
            root = self.model_root / MODEL_INNER
            root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, "r:bz2") as tf:
                members = tf.getmembers()
                total_members = max(1, len(members))
                for idx, m in enumerate(members):
                    if cancelled and cancelled():
                        raise STTCancelled()
                    if not m.isfile() or Path(m.name).name not in want:
                        continue
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    with src, open(root / Path(m.name).name, "wb") as fh:
                        shutil.copyfileobj(src, fh)
                    if progress:
                        progress(idx + 1, total_members, "extracting")
            # 压缩包 1GB，解完就没用了
            archive.unlink(missing_ok=True)
            # 老版本解过整包的，把 fp32 那份和残留目录顺手清掉
            for stale in (root / "model.onnx", root / "export-onnx.py", root / "README.md"):
                stale.unlink(missing_ok=True)
            shutil.rmtree(root / "test_wavs", ignore_errors=True)
        except STTCancelled:
            tmp.unlink(missing_ok=True)
            raise
        except STTError:
            raise
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            if exc.errno == errno.ENOSPC:
                raise STTError(f"磁盘空间不足，语音模型写入失败：{exc}") from exc
            raise STTError(f"语音模型下载/解压失败：{exc}") from exc
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise STTError(f"语音模型下载/解压失败：{exc}") from exc

        if not self.model_ready():
            raise STTError("模型解压后仍找不到 model.int8.onnx")
        return self._ready_path

    # ---------- 识别 ----------

    def _ensure_recognizer(self):
        # 整个加载过程放在锁里：并发请求同时判定 self._rec 为空会各建一份，
        # 后一份覆盖前一份，前一份的线程/内存就漏掉了
        with self._lock:
            if self._rec is not None:
                return self._rec
            try:
                import sherpa_onnx
            except Exception as exc:
                raise STTError(
                    "没有装 sherpa-onnx，语音功能不可用（pip install sherpa-onnx）"
                ) from exc
            if not self.model_ready():
                raise STTError("语音模型还没下载")

            root = self._ready_path
            try:
                self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(root / "model.int8.onnx"),
                    tokens=str(root / "tokens.txt"),
                    # use_itn：数字、日期转成习惯写法（「三点」「3:00」），并补标点
                    use_itn=True,
                    num_threads=NUM_THREADS,
                    # 不在这里选语言：SenseVoice 自带中英日韩粤识别，让它自己判
                    language="auto",
                )
            except Exception as exc:
                raise STTError(f"识别器加载失败：{exc}") from exc
            return self._rec

    def _maybe_unload(self):
        """空闲超过 IDLE_UNLOAD_SEC 就释放 recognizer，把内存还给系统"""
        # 判定和卸载都放进锁里：只看锁外的旧 _last_used 可能刚有请求进来，
        # 结果把正在用的 recognizer 卸掉，下一次识别直接失败
        with self._lock:
            if self._rec is None:
                return
            if time.time() - self._last_used < IDLE_UNLOAD_SEC:
                return
            self._rec = None

    @staticmethod
    def _read_wav(wav_bytes: bytes) -> tuple[list[float], int]:
        """WAV → (float32 采样, 采样率)

        前端录的是单声道 16k 16bit，和模型输入一致；不是的话这里会报错，
        而不是悄悄喂进错采样率导致识别成乱码。
        """
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                ch = wf.getnchannels()
                width = wf.getsampwidth()
                rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
        except Exception as exc:
            raise STTError(f"音频不是合法的 WAV：{exc}") from exc
        if ch != 1 or width != 2:
            raise STTError(f"音频格式不对：需要单声道 16bit，收到 {ch} 声道 {width * 8}bit")
        import array
        samples = array.array("h")
        samples.frombytes(frames)
        # 归一化到 [-1, 1]，sherpa-onnx 要求 float
        scale = 1.0 / 32768.0
        return [s * scale for s in samples], rate

    def transcribe(self, wav_bytes: bytes) -> str:
        """整段 WAV → 文字。失败抛 STTError，调用方转成 HTTP 错误"""
        if not self.enable:
            raise STTError("语音输入已关闭")
        self._maybe_unload()
        samples, rate = self._read_wav(wav_bytes)
        if not samples:
            raise STTError("没有录到声音")

        rec = self._ensure_recognizer()
        # sherpa-onnx 的 recognizer 不是线程安全的，并发请求在这里排队
        try:
            with self._lock:
                stream = rec.create_stream()
                stream.accept_waveform(rate, samples)
                rec.decode_stream(stream)
                text = (stream.result.text or "").strip()
            self._last_used = time.time()
        except Exception as exc:
            # 识别器可能在长时间驻留后损坏；丢掉缓存，下一次按需重新加载，
            # 而不是把坏掉的实例一直留着让后面每一轮都失败
            with self._lock:
                self._rec = None
            raise STTError(f"识别失败：{exc}") from exc
        return text

    def unload(self):
        with self._lock:
            self._rec = None


def record_supported() -> dict:
    """录音设备可用性：给状态接口用，前端据此禁用按钮"""
    ok = bool(shutil.which("pw-record"))
    reason = ""
    if not ok:
        reason = "缺少 pw-record（PipeWire 录音工具）"
    elif not os.environ.get("XDG_RUNTIME_DIR"):
        ok = False
        reason = "缺少 XDG_RUNTIME_DIR，后端起不到桌面会话里"
    return {"ok": ok, "reason": reason}
