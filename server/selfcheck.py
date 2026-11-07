"""桌面控件自检的「可补装组件」注册表与单任务安装器。

设计原则和 install.sh 保持一致：状态一律实测（which / 目录 / 子进程），
不信包数据库；安装动作按 install.sh 里对应的分支来，不另造一套源。

- tts_model  : 复用 tts.start_download()（hf-mirror / huggingface 双源 + 大小校验）
- nltk_data  : GitHub raw → jsDelivr → ghfast 三个源，zip 解压后按目录校验
- ydotoold   : depcheck.fix_ydotoold()（systemd 用户单元，不需要 root）
- 系统包     : pacman；只有探测到受限 sudoers（`sudo -n` 可用）才静默执行，
               否则只返回终端命令，绝不假装成功

进度用阶段权重法：下载 / 解包 / 校验各占一段，回调由 main 接到 WS 广播。
同一时间只允许一个安装任务；重复点击直接返回「进行中」。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

from . import depcheck, tts as tts_mod

_tts = None


def set_tts(engine) -> None:
    """由 main 注入语音引擎实例（模块本身不持有单例，避免循环依赖）"""
    global _tts
    _tts = engine


def _engine():
    if _tts is None:
        raise RuntimeError("语音引擎未初始化")
    return _tts

UA = "DeckMeido-Selfcheck/1.0"
NLTK_PACKAGES = (
    ("corpora", "cmudict"),
    ("taggers", "averaged_perceptron_tagger"),
    ("taggers", "averaged_perceptron_tagger_eng"),
)
NLTK_SOURCES = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/{sub}/{pkg}.zip",
    "https://cdn.jsdelivr.net/gh/nltk/nltk_data@gh-pages/packages/{sub}/{pkg}.zip",
    "https://ghfast.top/https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/{sub}/{pkg}.zip",
)

SUDO_PROBE_TTL = 60.0
_sudo_cache: tuple[float, bool] | None = None


def _run(args: list[str], timeout: float = 4.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ})
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, "调用超时"
    except OSError as exc:
        return 126, str(exc)


def sudo_silent() -> bool:
    """是否能在不弹终端的情况下执行 sudo（受限 sudoers 放行）。

    只做 `sudo -n true`：不允许读密码、不弹任何认证框；探测失败一律当不可用。
    """
    global _sudo_cache
    now = time.time()
    if _sudo_cache and now - _sudo_cache[0] < SUDO_PROBE_TTL:
        return _sudo_cache[1]
    rc, _ = _run(["sudo", "-n", "true"], timeout=3.0)
    ok = rc == 0
    _sudo_cache = (now, ok)
    return ok


# ---------- 组件定义 ----------
# action: python = 本模块内可静默完成；package = pacman（可能要 root）；
#         service = 用户级 systemd（不需要 root）
# command: 无法静默时要给用户复制的终端命令


def _which_status(binary: str, label: str) -> tuple[str, str]:
    if shutil.which(binary):
        return "ready", ""
    return "missing", f"未找到 {label}"


def _ydotoold_status() -> tuple[str, str]:
    if not shutil.which("ydotool"):
        return "missing", "ydotool 未安装"
    if not shutil.which("ydotoold"):
        return "missing", "ydotoold 缺失，需重装 ydotool"
    rc, _ = _run(["systemctl", "--user", "is-active", "ydotool.service"], timeout=2.0)
    if rc == 0:
        return "ready", "ydotoold 已由 systemd 托管"
    if shutil.which("pgrep") and _run(["pgrep", "-x", "ydotoold"], timeout=2.0)[0] == 0:
        return "mismatch", "进程在跑但没交给 systemd，重启后会失效"
    return "missing", "ydotoold 没在运行"


def _tts_status() -> tuple[str, str]:
    try:
        st = _engine().status()
    except RuntimeError:
        return "mismatch", "语音引擎未初始化"
    if not st.get("runtime"):
        return "mismatch", "piper-plus 运行时未安装（pip install -r requirements-tts.txt）"
    if not st.get("model"):
        return "missing", "语音模型未下载（约 38MB）"
    return "ready", ""


def _nltk_status() -> tuple[str, str]:
    return ("ready", "") if tts_mod.english_data_ready() else ("missing", "英文 G2P 数据缺失")


def _component(key: str, name: str, status_fn, size: str, action: str,
               command: str = "", privileged: bool = False) -> dict:
    return {
        "key": key, "name": name, "size": size, "action": action,
        "command": command, "privileged": privileged, "status_fn": status_fn,
    }


COMPONENTS = [
    _component("tts_model", "月读酱语音模型", _tts_status, "约 38MB", "python"),
    _component("nltk_data", "英文发音数据（nltk）", _nltk_status, "约 12MB", "python"),
    _component("ydotoold", "按键注入守护进程", _ydotoold_status, "无需下载", "service",
               "systemctl --user enable --now ydotool.service"),
    _component("ydotool", "ydotool（点击 / 输入）", lambda: _which_status("ydotool", "ydotool"),
               "约 2MB", "package", "bash install.sh check --fix", True),
    _component("wtype", "wtype（Wayland 打字）", lambda: _which_status("wtype", "wtype"),
               "约 1MB", "package", "bash install.sh check --fix", True),
    _component("kdotool", "kdotool（窗口聚焦）", lambda: _which_status("kdotool", "kdotool"),
               "约 1MB", "package", "bash install.sh check --fix", True),
    _component("spectacle", "spectacle（截图）", lambda: _which_status("spectacle", "spectacle"),
               "约 5MB", "package", "bash install.sh check --fix", True),
    _component("grim", "grim（截图备选）", lambda: _which_status("grim", "grim"),
               "约 1MB", "package", "bash install.sh check --fix", True),
    _component("espeak", "espeak-ng（机器人语音）",
               lambda: _which_status("espeak-ng", "espeak-ng"),
               "约 5MB", "package", "bash install.sh check --fix", True),
    _component("fakeroot", "fakeroot（构建依赖）",
               lambda: _which_status("fakeroot", "fakeroot"),
               "约 1MB", "package", "bash install.sh check --fix", True),
]

_BY_KEY = {c["key"]: c for c in COMPONENTS}
STATUS_LABELS = {"ready": "已就绪", "missing": "缺失", "mismatch": "版本不符"}


def _public(item: dict) -> dict:
    status, detail = item["status_fn"]()
    action = item["action"]
    can_install = action in ("python", "service")
    command = item["command"]
    if action == "package":
        can_install = sudo_silent()
        if not command:
            command = "bash install.sh check --fix"
    return {
        "key": item["key"], "name": item["name"], "size": item["size"],
        "status": status, "status_label": STATUS_LABELS.get(status, status),
        "detail": detail, "action": action,
        "can_install": bool(can_install), "command": command,
    }


def snapshot() -> dict:
    items = [_public(c) for c in COMPONENTS]
    ready = sum(1 for i in items if i["status"] == "ready")
    return {"components": items, "ready": ready, "total": len(items),
            "job": job_snapshot()}


# ---------- 单任务安装器 ----------
_job_lock = threading.Lock()
_job: dict | None = None
_job_thread: threading.Thread | None = None


def job_snapshot() -> dict | None:
    with _job_lock:
        return dict(_job) if _job else None


def _set(component: str, phase: str, percent: float, message: str = "",
         state: str = "running", error: str = "") -> None:
    global _job
    with _job_lock:
        _job = {
            "component": component, "phase": phase,
            "percent": max(0.0, min(100.0, float(percent))),
            "message": message, "state": state, "error": error,
            "updated_at": time.time(),
        }
        data = dict(_job)
    hook = _hook
    if hook:
        try:
            hook(data)
        except Exception:
            pass


_hook = None


def set_progress_hook(hook) -> None:
    """main 启动时注入：把进度转成 WS 广播；失败不影响安装本身"""
    global _hook
    _hook = hook


def start(component: str, hook=None) -> dict:
    global _job_thread
    item = _BY_KEY.get(component)
    if not item:
        return {"ok": False, "error": "未知组件", "reason": "unknown_component"}
    with _job_lock:
        if _job_thread and _job_thread.is_alive():
            return {"ok": False, "error": "已有安装任务在进行", "reason": "busy",
                    "job": dict(_job or {})}
        if hook:
            set_progress_hook(hook)
        _job_thread = threading.Thread(
            target=_run_job, args=(item,), name=f"selfcheck-{component}", daemon=True)
        _job_thread.start()
    return {"ok": True, "job": job_snapshot()}


def _run_job(item: dict) -> None:
    key = item["key"]
    try:
        _set(key, "prepare", 1.0, "准备中…")
        if key == "tts_model":
            _install_tts()
        elif key == "nltk_data":
            _install_nltk()
        elif key == "ydotoold":
            _install_ydotoold()
        elif item["action"] == "package":
            _install_package(item)
        else:
            raise RuntimeError("该组件不支持自动安装")
        _set(key, "done", 100.0, "已就绪", state="done")
    except _NeedTerminal as exc:
        _set(key, "done", 0.0, "需要在终端执行", state="need_terminal",
             error=str(exc))
    except Exception as exc:
        _set(key, "error", 0.0, "安装失败", state="error", error=str(exc) or "未知错误")


class _NeedTerminal(Exception):
    pass


def _poll_sleep(stop_check=None) -> None:
    time.sleep(0.3)


def _install_tts() -> None:
    """下载模型：0–90% 跟 tts 自己的进度，最后 10% 是加载校验"""
    engine = _engine()
    if not engine.status().get("runtime"):
        raise RuntimeError("piper-plus 运行时未安装，网页无法静默装；"
                           "请先在终端执行 pip install -r requirements-tts.txt")
    engine.start_download()
    deadline = time.time() + 1800
    while time.time() < deadline:
        dl = engine.download_status()
        state = dl.get("state")
        if state == "error":
            raise RuntimeError(dl.get("error") or "模型下载失败")
        if state == "cancelled":
            raise RuntimeError("模型下载已取消")
        if state == "done" or engine.model_ready():
            break
        _set("tts_model", "download", 2.0 + (dl.get("percent") or 0.0) * 0.86,
             "下载模型 " + ("%.0f%%" % (dl.get("percent") or 0.0)))
        _poll_sleep()
    else:
        raise RuntimeError("模型下载超时")
    _set("tts_model", "verify", 92.0, "校验模型文件…")
    if not engine.model_ready():
        raise RuntimeError("下载完成但模型校验没通过，请重试")
    _set("tts_model", "verify", 100.0, "模型已就绪")


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _install_nltk() -> None:
    """下载 3 个 nltk 包：下载 / 解包按包分段推进（2→92%），最后 94→100% 校验"""
    dest = tts_mod.nltk_data_dir()
    (dest / "corpora").mkdir(parents=True, exist_ok=True)
    (dest / "taggers").mkdir(parents=True, exist_ok=True)
    total = len(NLTK_PACKAGES)
    # 每个包分到一段固定的进度区间（下载占这段的 40%，解包占 80%），
    # 包与包之间不会出现「第二个包的下载进度比第一个包的解包进度还小」的回退
    span = 90.0 / max(1, total)
    for idx, (sub, pkg) in enumerate(NLTK_PACKAGES):
        base = 2.0 + idx * span
        blob = None
        errors = []
        for source in NLTK_SOURCES:
            url = source.format(sub=sub, pkg=pkg)
            for attempt in range(2):  # 失败重试 1 次
                try:
                    _set("nltk_data", "download", base + span * 0.4, f"下载 {pkg}…")
                    blob = _http_get(url)
                    break
                except Exception as exc:
                    errors.append(f"{url.split('/')[2]}: {exc}")
                    if attempt == 0:
                        _poll_sleep()
            if blob:
                break
        if not blob:
            raise RuntimeError(f"{pkg} 下载失败；" + "；".join(errors[-3:]))
        _set("nltk_data", "unpack", base + span * 0.8, f"解包 {pkg}…")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(dest / sub)
    _set("nltk_data", "verify", 94.0, "校验英文数据…")
    if not tts_mod.english_data_ready():
        raise RuntimeError("解包后校验失败，请重试")
    _set("nltk_data", "verify", 100.0, "英文数据已就绪")


def _install_ydotoold() -> None:
    if not shutil.which("ydotool"):
        raise _NeedTerminal("ydotool 未安装，请先在终端执行：bash install.sh check --fix")
    _set("ydotoold", "service", 30.0, "交给 systemd 用户单元托管…")
    ok, msg = depcheck.fix_ydotoold()
    _set("ydotoold", "service", 80.0, msg or "配置守护进程…")
    if not ok:
        raise RuntimeError(msg or "ydotoold 启动失败")
    _set("ydotoold", "verify", 100.0, msg or "ydotoold 已就绪")


def _install_package(item: dict) -> None:
    """系统包：只有受限 sudoers 放行才静默装，否则交给用户去终端"""
    if not sudo_silent():
        raise _NeedTerminal(item["command"] or "bash install.sh check --fix")
    pkg = item["key"]
    _set(item["key"], "install", 20.0, f"pacman 安装 {pkg}…")
    rc, out = _run(["sudo", "-n", "pacman", "-S", "--needed", "--noconfirm", pkg],
                   timeout=600.0)
    if rc != 0:
        raise RuntimeError((out or f"pacman 退出码 {rc}")[-300:])
    _set(item["key"], "verify", 90.0, "检查命令是否可用…")
    probe = "espeak-ng" if item["key"] == "espeak" else item["key"]
    if not shutil.which(probe):
        raise RuntimeError(f"装完仍找不到 {probe}")
    _set(item["key"], "verify", 100.0, "已就绪")
