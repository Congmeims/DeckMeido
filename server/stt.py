"""语音输入（STT）：sherpa-onnx + SenseVoice-Small int8，纯 CPU 离线识别

为什么选它（见交接纪要 §5）：用户没有云端 STT 的 key；SenseVoice-Small int8
模型约 250MB（官方 GitHub 是 1GB 的 tar.bz2，只解出 int8 那份）、10 秒语音
推算 1–1.5 秒、中文与中英混说都行、自带标点。
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
import urllib.error
import urllib.request
import wave
from pathlib import Path

# 模型署名：Apache-2.0 要求保留，界面上展示「语音识别」的地方一并显示
ATTRIBUTION = "SenseVoice-Small (FunAudioLLM) · sherpa-onnx · Apache-2.0"

# 官方发布的 tar.bz2（内含 model.int8.onnx / tokens.txt）
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
)
MODEL_ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
# 解压后真正的模型文件在顶层同名目录里
MODEL_INNER = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

# 国内镜像（hf-mirror.com）：直接取 model.int8.onnx 与 tokens.txt，不用下 1GB 整包
MIRROR_BASE = (
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
)
MODEL_FILES = ("model.int8.onnx", "tokens.txt")
# int8 模型实际约 250MB；低于这个数说明没下全或下成了网页
MIN_MODEL_BYTES = 100 * 1024**2
# 探测每个源的超时（秒）：国内镜像正常 1~3 秒内就能回，超时算不可用
PROBE_TIMEOUT = 3.0
# 单个源内最多重试次数（不含第一次）；所有源最多扫两轮
RETRIES_PER_SOURCE = 2
MAX_SOURCE_ROUNDS = 2
# 下载 + 解压至少需要的空闲空间：压缩包约 1GB，解出来的 int8 模型约 250MB
MIN_FREE_BYTES = int(1.5 * 1024**3)
# 直取文件只需要模型本身 + 少量临时空间
MIN_FREE_FILES = int(0.5 * 1024**3)
# 下载/解压用的 UA：少数镜像会拦默认的 Python-urllib
UA = "DeckMeido"

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


def _short_error(exc: Exception) -> str:
    """把网络异常压成一句人话，避免把几十行 traceback 塞进状态接口"""
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{exc.__class__.__name__}: {text}" if text != exc.__class__.__name__ else text


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

    def __init__(self, model_root: Path, enable: bool = True, model_source: str = "auto"):
        self.model_root = Path(model_root)
        self.enable = enable
        # [stt] model_source：auto（默认，镜像→GitHub）/ mirror / github / 自定义 URL
        self.model_source = (model_source or "auto").strip() or "auto"
        self._rec = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._ready_path: Path | None = None
        # 最近一次实际选中/尝试的下载源，给状态接口展示
        self._source_name = ""
        self._source_url = ""
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
            "source": "",
            "source_url": "",
            "error": "",
            "error_kind": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }

    # ---------- 下载源 ----------

    def _source_defs(self) -> list[dict]:
        """按 [stt] model_source 给出候选源，顺序即优先级

        auto：国内镜像优先、GitHub 兜底 —— 普通用户没有代理时 GitHub 基本不可用，
        镜像反过来的顺序会让多数人第一次下载就失败。
        mirror / github：只试指定的那个；其他非空值当作自定义 tar.bz2 URL。
        """
        mirror = {
            "name": "mirror",
            "label": "hf-mirror.com 镜像",
            "kind": "files",
            "files": {name: f"{MIRROR_BASE}/{name}" for name in MODEL_FILES},
        }
        github = {
            "name": "github",
            "label": "GitHub Releases",
            "kind": "archive",
            "url": MODEL_URL,
        }
        mode = self.model_source
        if mode == "mirror":
            return [mirror]
        if mode == "github":
            return [github]
        if mode.startswith(("http://", "https://")):
            return [{
                "name": "custom",
                "label": "自定义源",
                "kind": "archive",
                "url": mode,
            }]
        # auto（以及拼错的配置值）都走默认顺序，不让一次笔误把语音彻底弄坏
        return [mirror, github]

    def _default_source_name(self) -> str:
        return self._source_defs()[0]["name"]

    def _default_source_url(self) -> str:
        src = self._source_defs()[0]
        if src["kind"] == "files":
            return src["files"][MODEL_FILES[0]]
        return src["url"]

    @staticmethod
    def _probe_url(url: str) -> tuple[bool, int, str]:
        """HEAD（跟随重定向）探测一个 URL 是否可用，返回 (可用, 文件大小, 原因)

        3 秒短超时：镜像正常一两秒就回；GitHub 没有代理时是直接超时。
        个别服务不支持 HEAD，再用 Range: bytes=0-0 的 GET 补一次探测。
        """
        def attempt(method: str) -> tuple[int, int]:
            headers = {"User-Agent": UA}
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
                code = getattr(resp, "status", 200)
                length = int(resp.headers.get("Content-Length") or -1)
                if method == "GET":
                    resp.read(64)
                return code, length

        try:
            code, length = attempt("HEAD")
        except urllib.error.HTTPError as exc:
            if exc.code not in (405, 501):
                return False, -1, f"HTTP {exc.code}"
            try:
                code, length = attempt("GET")
            except Exception as inner:
                return False, -1, _short_error(inner)
        except Exception as exc:
            return False, -1, _short_error(exc)
        if code not in (200, 206):
            return False, -1, f"HTTP {code}"
        if length == 0:
            return False, 0, "Content-Length 为 0"
        return True, length, ""

    def _probe_source(self, src: dict) -> tuple[bool, dict, str]:
        """探测一个下载源，返回 (可用, 各文件 Content-Length, 原因)"""
        if src["kind"] == "files":
            lengths = {}
            for name, url in src["files"].items():
                ok, length, err = self._probe_url(url)
                if not ok:
                    return False, {}, f"{name}: {err}"
                lengths[name] = length
            return True, lengths, ""
        ok, length, err = self._probe_url(src["url"])
        return ok, {MODEL_ARCHIVE: length}, err

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
            # 当前/最近使用的下载源：还没下载过时给出配置的首选源
            "source": self._source_name or self._default_source_name(),
            "source_url": self._source_url or self._default_source_url(),
            "model_source": self.model_source,
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

    def _on_progress(self, done, total, phase, file=None):
        """下载/解压进度回调：done 为字节数或文件数，total 未知时 -1"""
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
                error="", error_kind="", phase="downloading", file="",
                source=self._source_name, source_url=self._source_url,
                started_at=time.time(), updated_at=time.time(),
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

    def _prepare_root(self):
        try:
            self.model_root.mkdir(parents=True, exist_ok=True)
            self.model_root.chmod(0o700)
        except OSError as exc:
            raise STTError(
                f"模型目录不可写：{self.model_root}（{exc}）；"
                "请检查数据根权限或 DECKMEIDO_DATA_DIR",
                kind="permission",
            ) from exc

    def _require_free(self, need: int):
        try:
            free = shutil.disk_usage(self.model_root).free
        except OSError:
            return
        if free < need:
            raise STTError(
                f"磁盘空间不足：{self.model_root.parent} 仅剩 {free / 1024**3:.1f} GB，"
                f"语音模型至少需要 {need / 1024**3:.1f} GB",
                kind="disk",
            )

    def _ready_or_fail(self) -> Path:
        if not self.model_ready():
            raise STTError("下载完成但仍找不到 model.int8.onnx")
        return self._ready_path

    def _cleanup_parts(self):
        """失败/取消时清掉所有半截文件，不留怪文件给人猜"""
        root = self.model_root / MODEL_INNER
        candidates = [
            self.model_root / (MODEL_ARCHIVE + ".part"),
            # 旧版本用的是 with_suffix(".part")，名字少一段，一起清
            (self.model_root / MODEL_ARCHIVE).with_suffix(".part"),
            root / "model.int8.onnx.part",
            root / "tokens.txt.part",
        ]
        for path in candidates:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def download_model(self, progress=None, cancelled=None) -> Path:
        """下载模型，已存在则直接返回

        首选国内镜像直取 model.int8.onnx + tokens.txt（约 250MB），失败再回退
        GitHub 的 tar.bz2（约 1GB）。progress(done, total, phase, file) 报进度，
        cancelled() 返回 True 时抛 STTCancelled；.part 支持 Range 断点续传。
        """
        if self.model_ready():
            return self._ready_path

        self._prepare_root()

        # 兜底：模型目录里已经有 tar.bz2 就跳过联网，直接解压
        archive = self.model_root / MODEL_ARCHIVE
        if archive.is_file():
            self._extract_archive(archive, progress, cancelled)
            return self._ready_or_fail()

        # 探测：可用的按配置顺序排前面，不可用的排后面（探测误判时还能兜底）
        available: list[dict] = []
        unavailable: list[dict] = []
        for src in self._source_defs():
            if cancelled and cancelled():
                raise STTCancelled()
            ok, lengths, reason = self._probe_source(src)
            item = {**src, "lengths": lengths}
            if ok:
                available.append(item)
                print(f"[DeckMeido] STT 源探测：{src['name']} 可用")
            else:
                item["probe_error"] = reason
                unavailable.append(item)
                print(f"[DeckMeido] STT 源探测：{src['name']} 不可用（{reason}）")
        order = available + unavailable

        last_error = ""
        for round_no in range(1, MAX_SOURCE_ROUNDS + 1):
            for src in order:
                if cancelled and cancelled():
                    self._cleanup_parts()
                    raise STTCancelled()
                self._source_name = src["name"]
                self._source_url = (
                    src["files"][MODEL_FILES[0]] if src["kind"] == "files" else src["url"]
                )
                self._dl_update(source=self._source_name, source_url=self._source_url)
                print(
                    f"[DeckMeido] STT 第 {round_no} 轮尝试源：{src['name']} "
                    f"({self._source_url})"
                )
                try:
                    if src["kind"] == "files":
                        self._require_free(MIN_FREE_FILES)
                        self._download_files(src, progress, cancelled)
                    else:
                        self._require_free(MIN_FREE_BYTES)
                        self._download_archive(src, progress, cancelled)
                    # 前面失败的源可能留下半截 .part，成功后一并清掉
                    self._cleanup_parts()
                    return self._ready_or_fail()
                except STTCancelled:
                    self._cleanup_parts()
                    raise
                except STTError as exc:
                    if exc.kind in ("disk", "permission"):
                        self._cleanup_parts()
                        raise
                    last_error = str(exc)
                    print(f"[DeckMeido] STT 源 {src['name']} 失败：{exc}；切换下一个源")
                except Exception as exc:
                    last_error = _short_error(exc)
                    print(f"[DeckMeido] STT 源 {src['name']} 失败：{exc}；切换下一个源")

        self._cleanup_parts()
        detail = "；".join(
            f"{s['name']}：{s.get('probe_error') or '下载失败'}" for s in order
        )
        raise STTError(
            f"语音模型所有下载源都失败（{detail}）；最后一次错误：{last_error}",
            kind="network",
        )

    def _fetch(self, url: str, part: Path, cancelled, on_bytes):
        """把 url 写进 part，part 已有内容时用 Range 续传"""
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
                # 源不支持 Range（返回 200 整份）：只能从头写
                resume = 0
            length = int(resp.headers.get("Content-Length") or -1)
            total = resume + length if length >= 0 else -1
            done = resume
            on_bytes(done, total)
            with open(part, mode) as fh:
                while True:
                    if cancelled and cancelled():
                        raise STTCancelled()
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    on_bytes(done, total)
            if total > 0 and done < total:
                raise STTError(f"连接提前结束：{done}/{total} 字节", kind="network")

    def _fetch_resumable(self, url: str, part: Path, cancelled, on_bytes):
        """单个源内重试 RETRIES_PER_SOURCE 次（继续用 .part 续传），仍失败则抛给上层切源"""
        last = ""
        for attempt in range(RETRIES_PER_SOURCE + 1):
            if cancelled and cancelled():
                raise STTCancelled()
            try:
                self._fetch(url, part, cancelled, on_bytes)
                return
            except STTCancelled:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    # .part 已经超过远端长度（上次下到末尾但没改名），清掉重下
                    part.unlink(missing_ok=True)
                last = f"HTTP {exc.code}"
            except Exception as exc:
                last = _short_error(exc)
            if attempt < RETRIES_PER_SOURCE:
                # 留一点间隔，别在断网瞬间把重试额度烧光；期间仍响应取消
                for _ in range(10):
                    if cancelled and cancelled():
                        raise STTCancelled()
                    time.sleep(0.1)
        raise STTError(
            f"下载中断（已重试 {RETRIES_PER_SOURCE} 次）：{last}", kind="network"
        )

    def _download_files(self, src: dict, progress=None, cancelled=None):
        """镜像源：直接下 model.int8.onnx + tokens.txt 到模型目录"""
        root = self.model_root / MODEL_INNER
        root.mkdir(parents=True, exist_ok=True)
        lengths = src.get("lengths") or {}
        total = sum(v for v in lengths.values() if v and v > 0) or -1
        base = 0
        for name in MODEL_FILES:
            dest = root / name
            part = dest.with_name(dest.name + ".part")
            min_bytes = MIN_MODEL_BYTES if name == "model.int8.onnx" else 1
            if dest.is_file() and dest.stat().st_size >= min_bytes:
                base += dest.stat().st_size
                continue

            def on_bytes(done, _file_total, _base=base, _label=name):
                if progress:
                    progress(_base + done, total, "downloading", _label)

            self._fetch_resumable(src["files"][name], part, cancelled, on_bytes)
            size = part.stat().st_size
            if size < min_bytes:
                part.unlink(missing_ok=True)
                raise STTError(
                    f"下载的 {name} 不完整（{size} 字节）", kind="network"
                )
            part.replace(dest)
            base += size
        self._validate_files(root)

    def _download_archive(self, src: dict, progress=None, cancelled=None):
        """GitHub/自定义源：下 tar.bz2 再只解出需要的文件"""
        archive = self.model_root / MODEL_ARCHIVE
        part = self.model_root / (MODEL_ARCHIVE + ".part")
        legacy = archive.with_suffix(".part")
        if legacy.is_file() and not part.is_file():
            legacy.replace(part)
        total = (src.get("lengths") or {}).get(MODEL_ARCHIVE, -1)

        def on_bytes(done, _file_total):
            if progress:
                progress(done, total, "downloading", MODEL_ARCHIVE)

        self._fetch_resumable(src["url"], part, cancelled, on_bytes)
        size = part.stat().st_size
        if size <= 0:
            part.unlink(missing_ok=True)
            raise STTError("下载的模型压缩包为空", kind="network")
        if total > 0 and size < total:
            raise STTError(
                f"模型压缩包不完整：{size}/{total} 字节", kind="network"
            )
        part.replace(archive)
        self._extract_archive(archive, progress, cancelled)

    def _extract_archive(self, archive: Path, progress=None, cancelled=None):
        """只解出要用的三个文件：包里还有一个 fp32 的 model.onnx（937MB）"""
        want = {"model.int8.onnx", "tokens.txt", "LICENSE"}
        root = self.model_root / MODEL_INNER
        root.mkdir(parents=True, exist_ok=True)
        try:
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
                        progress(
                            idx + 1, total_members, "extracting", Path(m.name).name
                        )
        except STTCancelled:
            raise
        except STTError:
            raise
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise STTError(f"磁盘空间不足，语音模型写入失败：{exc}", kind="disk") from exc
            raise STTError(f"语音模型解压失败：{exc}") from exc
        except Exception as exc:
            raise STTError(f"语音模型解压失败：{exc}") from exc

        self._validate_files(root)
        # 压缩包 1GB，解完就没用了
        archive.unlink(missing_ok=True)
        # 老版本解过整包的，把 fp32 那份和残留目录顺手清掉
        for stale in (root / "model.onnx", root / "export-onnx.py", root / "README.md"):
            stale.unlink(missing_ok=True)
        shutil.rmtree(root / "test_wavs", ignore_errors=True)

    def _validate_files(self, root: Path):
        """下载/解压后的校验：存在、体积合理、tokens 能解析、装了就试加载"""
        model = root / "model.int8.onnx"
        tokens = root / "tokens.txt"
        for path in (model, tokens):
            if not path.is_file():
                raise STTError(f"语音模型文件缺失：{path}")
        size = model.stat().st_size
        if size < MIN_MODEL_BYTES:
            raise STTError(
                f"模型文件大小异常：{size / 1024**2:.1f} MB，"
                "int8 模型应大于 100MB（实际约 250MB），可能没下全",
                kind="network",
            )
        if tokens.stat().st_size <= 0:
            raise STTError("tokens.txt 为空", kind="network")
        lines = 0
        try:
            with open(tokens, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.strip():
                        lines += 1
                        if lines > 100:
                            break
        except OSError as exc:
            raise STTError(f"tokens.txt 读取失败：{exc}") from exc
        if lines <= 100:
            raise STTError(f"tokens.txt 解析失败（只有 {lines} 行）", kind="network")
        if not self.installed():
            return
        try:
            import sherpa_onnx
            # 只建一次就丢：验证文件能被 onnxruntime 读进去，不常驻内存
            sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model),
                tokens=str(tokens),
                use_itn=True,
                num_threads=1,
                language="auto",
            )
        except Exception as exc:
            raise STTError(f"模型加载校验失败：{exc}") from exc


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
