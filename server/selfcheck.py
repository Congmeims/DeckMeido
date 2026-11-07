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
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

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

# 允许网页自己卸载的组件：只限我们写入/启用过的东西。
# 系统包（pacman）一律不在这里出现——卸载包要用户自己决定，后端不代劳。
UNINSTALLABLE = {"tts_model", "nltk_data", "ydotoold"}


def _path_size(paths) -> int:
    total = 0
    for p in paths:
        try:
            if p.is_file():
                total += p.stat().st_size
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        total += f.stat().st_size
        except OSError:
            pass
    return total


def _fmt_size(n: int) -> str:
    if n <= 0:
        return "0"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _uninstall_spec(key: str) -> tuple[list[Path], str, str]:
    """(要删的精确路径, 额外动作, 说明)。

    只列我们自己写下的路径，绝不整目录删除用户可能放了别的东西的目录。
    """
    if key == "tts_model":
        root = Path(_engine().model_root)
        paths = [root / tts_mod.ONNX_NAME, root / tts_mod.CONFIG_NAME]
        return ([p for p in paths if p.exists()], "",
                f"删除模型文件（{root} 下的 {tts_mod.ONNX_NAME} 与 {tts_mod.CONFIG_NAME}）")
    if key == "nltk_data":
        root = tts_mod.nltk_data_dir()
        paths = [root / sub / pkg for sub, pkg in NLTK_PACKAGES]
        return ([p for p in paths if p.exists()], "",
                f"删除 {root} 下由本程序下载的 {len(NLTK_PACKAGES)} 个 nltk 包，"
                "目录里的其它文件不动")
    if key == "ydotoold":
        # unit 文件由系统包提供，删它等于改系统；只撤销我们做的 enable/start
        return ([], "service",
                "停用并关闭开机自启 systemd 用户单元 ydotool.service；"
                "包文件属于系统，不执行卸载")
    return ([], "", "")


def uninstall_plan(key: str) -> dict:
    """给前端确认框用的卸载预览：删什么、多大、是否允许"""
    if key not in UNINSTALLABLE:
        return {"allowed": False, "reason": "该组件不支持卸载"}
    try:
        paths, action, desc = _uninstall_spec(key)
    except Exception as exc:
        return {"allowed": False, "reason": str(exc) or "无法读取卸载目标"}
    size = 0 if action == "service" else _path_size(paths)
    return {"allowed": bool(paths or action), "paths": [str(p) for p in paths],
            "size": _fmt_size(size), "target": desc}


def _public(item: dict) -> dict:
    status, detail = item["status_fn"]()
    action = item["action"]
    can_install = action in ("python", "service")
    command = item["command"]
    if action == "package":
        can_install = sudo_silent()
        if not command:
            command = "bash install.sh check --fix"
    out = {
        "key": item["key"], "name": item["name"], "size": item["size"],
        "status": status, "status_label": STATUS_LABELS.get(status, status),
        "detail": detail, "action": action,
        "can_install": bool(can_install), "command": command,
        "can_uninstall": False, "uninstall": None,
    }
    if status == "ready" and item["key"] in UNINSTALLABLE:
        plan = uninstall_plan(item["key"])
        out["can_uninstall"] = bool(plan.get("allowed"))
        out["uninstall"] = plan if plan.get("allowed") else None
    return out


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


def start_uninstall(component: str, hook=None) -> dict:
    """后台卸载单个组件；与安装共用同一个单任务锁，杜绝并发重复点击"""
    global _job_thread
    if component not in UNINSTALLABLE:
        return {"ok": False, "error": "该组件不支持卸载", "reason": "not_allowed"}
    plan = uninstall_plan(component)
    if not plan.get("allowed"):
        return {"ok": False, "error": plan.get("reason") or "没有可卸载的内容",
                "reason": "not_allowed"}
    with _job_lock:
        if _job_thread and _job_thread.is_alive():
            return {"ok": False, "error": "已有任务在进行", "reason": "busy",
                    "job": dict(_job or {})}
        if hook:
            set_progress_hook(hook)
        _job_thread = threading.Thread(
            target=_run_uninstall, args=(component,),
            name=f"selfcheck-uninstall-{component}", daemon=True)
        _job_thread.start()
    return {"ok": True, "job": job_snapshot()}


def _run_uninstall(component: str) -> None:
    try:
        _set(component, "prepare", 1.0, "准备卸载…")
        paths, action, _desc = _uninstall_spec(component)
        _set(component, "remove", 30.0, "正在删除…")
        for p in paths:
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(f"删除 {p} 失败：{exc}") from exc
        if action == "service":
            _set(component, "remove", 60.0, "停用 systemd 用户单元…")
            rc, out = _run(["systemctl", "--user", "disable", "--now",
                            "ydotool.service"], timeout=15.0)
            # 未启用/未运行时 disable 会报 not loaded，这本来就是目标状态
            low = (out or "").lower()
            if rc != 0 and "not loaded" not in low and "not found" not in low:
                raise RuntimeError(f"停用失败：{out.splitlines()[-1][:80] if out else '无输出'}")
        _set(component, "done", 100.0, "已卸载，可在本页重新安装", state="done")
    except Exception as exc:
        _set(component, "error", 0.0, "卸载失败", state="error",
             error=str(exc) or "未知错误")


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


# ---------- 系统包下载 ----------
# 不能提权时不再让用户复制命令，而是把真实的安装包下载到浏览器。
# 下载地址只从本机 pacman 配置解析（`pacman -Sp`），请求里只接受组件 key，
# 不接受任何 URL —— 这样接口不会变成任意地址抓取（SSRF）的跳板。
PACKAGE_NAMES = {"espeak": "espeak-ng"}
_PKG_SCHEMES = ("http://", "https://")
_PKG_SAFE_RE = re.compile(r"[^A-Za-z0-9._+-]")
_DL_CHUNK = 64 * 1024
_download_lock = threading.Lock()


class PackageDownloadError(Exception):
    """下载失败：reason 给前端分类，message 直接展示给用户"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _pkg_url_ok(url: str) -> bool:
    """只允许 http(s)、无空白/换行的镜像地址；拒绝 file://、data: 等本地协议"""
    if not url or any(ch in url for ch in "\r\n\t "):
        return False
    low = url.lower()
    if not any(low.startswith(s) for s in _PKG_SCHEMES):
        return False
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return False
    # 不允许 user:pass@ 形式，避免把凭据拼进可下载地址
    netloc = parts.netloc.rsplit("@", 1)
    return len(netloc) == 1


def _pkg_filename(name: str, version: str, url: str) -> str:
    """优先用镜像给出的原始包文件名；否则按 包名-版本 重建，两种都可辨识"""
    base = urllib.parse.unquote(url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
    safe = _PKG_SAFE_RE.sub("_", base)
    if name in safe and version in safe and safe.endswith(
            (".pkg.tar.zst", ".pkg.tar.xz", ".pkg.tar.gz")):
        return safe
    return _PKG_SAFE_RE.sub("_", f"{name}-{version}.pkg.tar.zst")


def package_spec(component: str) -> dict:
    """解析某个组件对应的包名/版本/下载地址；只接受注册表里的系统包 key"""
    item = _BY_KEY.get(component)
    if not item:
        raise PackageDownloadError("unknown_component", "未知组件")
    if item["action"] != "package":
        raise PackageDownloadError("not_package", "该组件不是系统包，不能下载")
    status, _ = item["status_fn"]()
    if status == "ready":
        raise PackageDownloadError("already_ready", f"{item['name']} 已就绪，无需下载")
    pkg = PACKAGE_NAMES.get(component, component)
    try:
        proc = subprocess.run(
            ["pacman", "-Sp", "--print-format", "%n|%v|%l", pkg],
            capture_output=True, text=True, timeout=20.0, env={**os.environ})
    except FileNotFoundError:
        raise PackageDownloadError("pacman_missing", "本机没有 pacman，无法解析安装包")
    except subprocess.TimeoutExpired:
        raise PackageDownloadError("resolve_failed", "pacman 解析安装包超时")
    except OSError as exc:
        raise PackageDownloadError("resolve_failed", f"调用 pacman 失败：{exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-300:]
        raise PackageDownloadError(
            "resolve_failed", f"无法解析 {pkg} 的下载地址：{detail or 'pacman 退出码 %s' % proc.returncode}")
    # -Sp 可能连带打印依赖；只取目标包那一行，避免下错文件
    for line in proc.stdout.splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 3:
            continue
        name, version, url = parts[0], parts[1], parts[2]
        if name != pkg:
            continue
        if not _pkg_url_ok(url):
            raise PackageDownloadError("unsafe_url", f"pacman 给出的地址不受支持：{url[:120]}")
        return {"component": component, "package": name, "version": version,
                "url": url, "filename": _pkg_filename(name, version, url)}
    raise PackageDownloadError(
        "resolve_failed", f"pacman 没有给出 {pkg} 的下载地址（包数据库可能未同步）")


def download_package(component: str, hook=None) -> dict:
    """把安装包抓到临时文件，返回路径供接口用 FileResponse 发出去。

    进度沿用既有 selfcheck_progress 机制（download → verify），不另造一套。
    失败抛 PackageDownloadError，调用方把 reason/error 原样给前端。
    """
    if hook:
        set_progress_hook(hook)
    with _download_lock:
        with _job_lock:
            if _job_thread and _job_thread.is_alive():
                raise PackageDownloadError("busy", "已有安装任务在进行，请等它完成")
        spec = package_spec(component)
        name, version = spec["package"], spec["version"]
        _set(component, "download", 2.0, f"开始下载 {name} {version}…")
        tmpdir = tempfile.mkdtemp(prefix="deckmeido-pkg-")
        path = os.path.join(tmpdir, spec["filename"])
        try:
            req = urllib.request.Request(spec["url"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                last_pct = 2.0
                with open(path, "wb") as fh:
                    while True:
                        chunk = resp.read(_DL_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = 4.0 + 92.0 * got / total
                            if pct - last_pct >= 1.0:
                                last_pct = pct
                                _set(component, "download", pct,
                                     f"下载 {name} {pct:.0f}%")
                if total and got < total:
                    raise PackageDownloadError(
                        "download_failed",
                        f"下载不完整：{got}/{total} 字节，请重试")
            _set(component, "verify", 97.0, "校验安装包…")
            size = os.path.getsize(path)
            if size <= 0:
                raise PackageDownloadError("download_failed", "下载到的文件是空的，请重试")
            _set(component, "done", 100.0, f"已下载 {spec['filename']}",
                 state="done")
            return {"ok": True, "path": path, "filename": spec["filename"],
                    "size": size, "package": name, "version": version,
                    "url_host": urllib.parse.urlsplit(spec["url"]).hostname or ""}
        except PackageDownloadError as exc:
            _set(component, "error", 0.0, "下载失败", state="error", error=str(exc))
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        except Exception as exc:
            _set(component, "error", 0.0, "下载失败", state="error", error=str(exc))
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise PackageDownloadError(
                "download_failed", f"下载失败：{exc or '未知错误'}") from exc


def cleanup_package(path: str) -> None:
    """响应发完就删临时目录（FileResponse 的 background 回调）"""
    try:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    except Exception:
        pass
