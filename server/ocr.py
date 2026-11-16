"""DeckMeido 离线 OCR：中日英文字识别（可选组件）

用途只有两个，都是「模型看不到图时的兜底」：
  1. 纯文本模型（DeepSeek 等）收到图片：先把字认出来，当文本交给它翻译；
  2. 图片读取失败 / 上游不认格式：同样走这里，避免模型自己去翻磁盘。
看图本身仍然优先走多模态通道，OCR 不参与「描述画面」这类任务。

引擎是 RapidOCR（onnxruntime 后端），模型直接沿用 PaddleOCR / Umi-OCR 那一套
PP-OCR 权重（Apache-2.0），不需要 Docker、不需要联网推理。
依赖装在 requirements-ocr.txt；没装时 available() 返回 False，
调用方保持原来的行为，不会因为缺包把对话弄坏。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

# 版权与来源（设置页和日志里展示用）
ATTRIBUTION = "PP-OCRv6 (PaddleOCR) · RapidOCR · onnxruntime · Apache-2.0"

# 模型体积（pip 下载 + 磁盘占用口径，与 install.sh 的 DEP_TABLE 保持一致）。
# 外部调用方（安装包体提示、设置页自检）都用这两个数，不要各写各的。
MODEL_MB = 31
PACKAGE_MB = 300

# 空闲多久卸载模型：Steam Deck 内存紧，OCR 不是每轮都用，
# 五分钟没人找它就释放，下次用到再懒加载（0.5s 级）
IDLE_UNLOAD_SEC = 300


class OCRError(RuntimeError):
    """OCR 不可用或识别失败（消息直接给用户看）"""


def available() -> dict:
    """探一下依赖在不在，不加载模型、不下载任何东西"""
    try:
        import rapidocr  # noqa: F401
    except Exception as exc:  # ImportError 之外还有 cv2 动态库缺失等
        return {"ok": False, "reason": _short(exc)}
    return {"ok": True, "reason": ""}


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text.splitlines()[0][:200]


class OCR:
    """懒加载、空闲卸载、线程安全的识别器

    第一次调用时导入 rapidocr 并建引擎（会读 30MB 模型，约 0.3s），
    之后复用；后台线程每分钟查一次，闲置超过 IDLE_UNLOAD_SEC 就释放。
    """

    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._idle_thread: threading.Thread | None = None
        # 最近一次识别耗时，给设置页/日志展示
        self.last_elapsed = 0.0

    # ---------- 引擎 ----------

    def _build(self):
        from rapidocr import RapidOCR

        # Rec.lang_type 不指定：RapidOCR 3.10 起默认路由到 PP-OCRv6 多语种
        # 识别模型，中日英混排实测 0.99 置信度，不需要按语种切模型。
        # onnxruntime 线程数不调：Deck 上单张 1 秒左右，再压会拖慢别的服务
        return RapidOCR()

    def _ensure(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is None:
                probe = available()
                if not probe["ok"]:
                    raise OCRError(
                        "离线 OCR 组件没装上：" + probe["reason"]
                        + "。可在设置页「语音引擎」下方点安装，或运行 "
                        "pip install -r requirements-ocr.txt")
                try:
                    self._engine = self._build()
                except Exception as exc:
                    raise OCRError("离线 OCR 初始化失败：" + _short(exc)) from exc
                self._start_idle_watch()
            return self._engine

    def _start_idle_watch(self):
        if self._idle_thread and self._idle_thread.is_alive():
            return

        def loop():
            while True:
                time.sleep(30)
                with self._lock:
                    if self._engine is None:
                        return
                    if time.time() - self._last_used > IDLE_UNLOAD_SEC:
                        # 只丢引用，onnxruntime 的会话交给 GC；下次用到重建即可
                        self._engine = None
                        return

        self._idle_thread = threading.Thread(target=loop, name="ocr-idle",
                                             daemon=True)
        self._idle_thread.start()

    # ---------- 识别 ----------

    def recognize(self, path: Path | bytes) -> str:
        """识别一张图里的文字，按行返回（保持阅读顺序）

        path 可以是文件路径或原始字节；没有识别到文字返回空字符串，
        调用方据此决定要不要提示「图里没找到字」。
        """
        data = path if isinstance(path, (bytes, bytearray)) else None
        src = None
        if data is None:
            src = Path(path)
            if not src.is_file():
                raise OCRError(f"图片不存在：{src}")
        engine = self._ensure()
        t0 = time.time()
        try:
            res = engine(data if data is not None else str(src))
        except Exception as exc:
            raise OCRError("识别失败：" + _short(exc)) from exc
        finally:
            self._last_used = time.time()
        texts = list(getattr(res, "txts", None) or [])
        # PP-OCR 的输出顺序本来就按从上到下、从左到右的检测框顺序，
        # 这里只清理首尾空白和整行空串
        lines = [t.strip() for t in texts if t and t.strip()]
        self.last_elapsed = time.time() - t0
        return "\n".join(lines)

    def status(self) -> dict:
        return {
            "available": available()["ok"],
            "loaded": self._engine is not None,
            "attribution": ATTRIBUTION,
            "last_elapsed": round(self.last_elapsed, 2),
        }


# 全局单例：main.py 直接 ocr.OCR_ENGINE.recognize(...)
OCR_ENGINE = OCR()
