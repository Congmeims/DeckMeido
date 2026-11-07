"""桌面控件自检的「可补装组件」注册表与单任务安装器。

设计原则和 install.sh 保持一致：状态一律实测（which / 目录 / 子进程），
不信包数据库；安装动作按 install.sh 里对应的分支来，不另造一套源。

- tts_model  : 复用 tts.start_download()（hf-mirror / huggingface 双源 + 大小校验）
- nltk_data  : GitHub raw → jsDelivr → ghfast 三个源，zip 解压后按目录校验
- ydotoold   : depcheck.fix_ydotoold()（systemd 用户单元，不需要 root）
- 系统包     : 软件内下载到工作区临时目录 → 校验 → 提权安装 → 重新探测；
               提权顺序为免密 sudo → polkit → 界面密码（sudo -S），全程不落浏览器下载

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

from . import depcheck, tools, tts as tts_mod

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
PHASE_LABELS = {
    "prepare": "准备", "download": "下载", "verify": "校验",
    "auth": "授权", "install": "安装", "remove": "卸载",
    "done": "完成", "error": "失败",
}

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
    # 系统包也走软件内自动安装：没有免密 sudo 时由界面收密码，
    # 所以「能不能装」不再取决于 sudo 探测，只影响是否需要额外授权
    can_install = action in ("python", "service", "package")
    command = item["command"]
    needs_auth = False
    if action == "package":
        needs_auth = not sudo_silent()
        if not command:
            command = "bash install.sh check --fix"
    out = {
        "key": item["key"], "name": item["name"], "size": item["size"],
        "status": status, "status_label": STATUS_LABELS.get(status, status),
        "detail": detail, "action": action,
        "can_install": bool(can_install), "needs_auth": needs_auth,
        "command": command,
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
            "phase_label": PHASE_LABELS.get(phase, phase),
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
        msg = "已就绪"
        if key == "tts_model":
            _install_tts()
        elif key == "nltk_data":
            _install_nltk()
        elif key == "ydotoold":
            _install_ydotoold()
        elif item["action"] == "package":
            msg = _install_package(item) or msg
        else:
            raise RuntimeError("该组件不支持自动安装")
        _set(key, "done", 100.0, msg, state="done")
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


def _install_package(item: dict) -> str:
    """系统包：软件内下载 → 校验 → 提权安装 → 重新探测，全程不用浏览器下载。

    提权顺序：免密 sudo → polkit（有图形会话时）→ 界面收密码走 sudo -S。
    安装包装在工作区临时目录，无论成败都会清理。
    """
    key = item["key"]
    spec = package_spec(key)
    tmpdir = _workspace_tmp()
    try:
        path = Path(tmpdir) / spec["filename"]
        _set(key, "download", 3.0,
             f"下载 {spec['package']} {spec['version']}…")
        _download_to(spec, path, key)
        _set(key, "verify", 45.0, "校验安装包…")
        _verify_package(path, spec)

        rc = 0
        out = ""
        if sudo_silent():
            _set(key, "install", 85.0, f"pacman 安装 {spec['package']}…")
            rc, out = _exec_install("sudo", None, path, spec["package"])
            if rc != 0 and _needs_password(out):
                # 免密缓存过期（例如服务重启后）：不直接报错，改走界面密码
                password = _request_password(key, "免密 sudo 失效，需要管理员密码")
                _set(key, "install", 85.0, f"pacman 安装 {spec['package']}…")
                rc, out = _exec_install("password", password, path, spec["package"])
        elif _pkexec_ready():
            _set(key, "auth", 50.0, "等待系统授权（请在桌面确认弹窗）…")
            try:
                rc, out = _exec_install("pkexec", None, path, spec["package"], 90.0)
            except RuntimeError as exc:
                rc, out = 126, str(exc)
            if _pkexec_denied(rc, out):
                _set(key, "auth", 50.0, "没有拿到 polkit 授权，改用管理员密码…")
                password = _request_password(key, "需要管理员密码来安装系统包")
                _set(key, "install", 85.0, f"pacman 安装 {spec['package']}…")
                rc, out = _exec_install("password", password, path, spec["package"])
        else:
            password = _request_password(key, "需要管理员密码来安装系统包")
            _set(key, "install", 85.0, f"pacman 安装 {spec['package']}…")
            rc, out = _exec_install("password", password, path, spec["package"])

        if rc != 0:
            raise RuntimeError(_install_error_text(out, rc))

        _set(key, "verify", 92.0, "重新检测组件状态…")
        status, status_detail = item["status_fn"]()
        if status != "ready":
            raise RuntimeError(
                f"安装完成但 {item['name']} 仍不可用：{status_detail or status}")
        warn = "DECKMEIDO_WARN restore_failed" in (out or "")
        if warn:
            _set(key, "verify", 99.0,
                 "安装完成，但只读保护未恢复，请手动执行 steamos-readonly enable")
            return "已就绪（只读保护未恢复，请手动执行 steamos-readonly enable）"
        _set(key, "verify", 99.0, "已就绪")
        return ""
    finally:
        _clear_password()
        _cleanup_tmp(tmpdir)


# ---------- 系统包：软件内下载并安装 ----------
# 下载地址只从本机 pacman 配置解析（`pacman -Sp`），请求里只接受组件 key，
# 不接受任何 URL —— 这样接口不会变成任意地址抓取（SSRF）的跳板。
# 安装包下到工作区临时目录，校验后直接交给 pacman，最后清理临时目录。
PACKAGE_NAMES = {"espeak": "espeak-ng"}
_PKG_SCHEMES = ("http://", "https://")
_PKG_SAFE_RE = re.compile(r"[^A-Za-z0-9._+-]")
_DL_CHUNK = 64 * 1024

PASSWORD_WAIT_SECONDS = 300.0
PASSWORD_MAX_ATTEMPTS = 3


class PackageError(Exception):
    """安装包相关的失败：reason 给前端分类，message 直接展示给用户"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# 提权三选一：免密 sudo → polkit 弹窗 → 界面收密码走 sudo -S。
# 密码只存在进程内存的本任务里，用完立即清空，不写日志、不写配置、不回显。
_pw_lock = threading.Lock()
_pw_event = threading.Event()
_pw_value: str | None = None
_pw_fail = ""
_pw_attempts = 0
_pw_waiting = False

# 一次提权会话里把「关只读 → 安装 → 恢复只读」串成一条命令：
# 既减少授权次数，也保证无论 pacman 成败都会把 SteamOS 恢复成只读。
_INSTALL_SCRIPT = r'''pkg="$1"
name="$2"
ro=0
restore() {
  if [ "$ro" = 1 ]; then
    if ! steamos-readonly enable >/dev/null 2>&1; then
      echo "DECKMEIDO_WARN restore_failed"
    fi
  fi
}
trap restore EXIT
trap 'exit 130' INT TERM
if command -v steamos-readonly >/dev/null 2>&1; then
  if steamos-readonly disable >/dev/null 2>&1; then
    ro=1
  else
    echo "DECKMEIDO_ERR readonly_disable"
    exit 11
  fi
fi
if pacman -U --needed --noconfirm "$pkg" 2>&1; then
  exit 0
fi
echo "DECKMEIDO_INFO local_install_failed"
# SteamOS 换镜像后 keyring 常与仓库对不上，签名校验会直接失败；先修 keyring
pacman-key --init >/dev/null 2>&1 || true
pacman-key --populate archlinux holo >/dev/null 2>&1 || true
# 本地包缺依赖时回退到仓库安装（会补下依赖，但目标包仍在本地）
pacman -Sy --needed --noconfirm "$name" 2>&1
exit $?
'''


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
        raise PackageError("unknown_component", "未知组件")
    if item["action"] != "package":
        raise PackageError("not_package", "该组件不是系统包，不能下载")
    status, _ = item["status_fn"]()
    if status == "ready":
        raise PackageError("already_ready", f"{item['name']} 已就绪，无需下载")
    pkg = PACKAGE_NAMES.get(component, component)
    try:
        proc = subprocess.run(
            ["pacman", "-Sp", "--print-format", "%n|%v|%l", pkg],
            capture_output=True, text=True, timeout=20.0, env={**os.environ})
    except FileNotFoundError:
        raise PackageError("pacman_missing", "本机没有 pacman，无法解析安装包")
    except subprocess.TimeoutExpired:
        raise PackageError("resolve_failed", "pacman 解析安装包超时")
    except OSError as exc:
        raise PackageError("resolve_failed", f"调用 pacman 失败：{exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-300:]
        raise PackageError(
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
            raise PackageError("unsafe_url", f"pacman 给出的地址不受支持：{url[:120]}")
        return {"component": component, "package": name, "version": version,
                "url": url, "filename": _pkg_filename(name, version, url)}
    raise PackageError(
        "resolve_failed", f"pacman 没有给出 {pkg} 的下载地址（包数据库可能未同步）")


def _workspace_tmp() -> Path:
    """工作区里的临时目录；工作区不可写时退回系统临时目录"""
    try:
        base = Path(tools.WORKSPACE) / ".deckmeido-tmp"
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="pkg-", dir=str(base)))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="deckmeido-pkg-"))


def _cleanup_tmp(tmpdir: Path) -> None:
    """删安装包；顺手把空的 .deckmeido-tmp 也收掉，不在工作区留痕"""
    shutil.rmtree(str(tmpdir), ignore_errors=True)
    parent = Path(tmpdir).parent
    if parent.name == ".deckmeido-tmp":
        try:
            parent.rmdir()
        except OSError:
            pass


def _download_to(spec: dict, path: Path, component: str) -> None:
    """下载安装包到临时文件；进度 3%→43%，不完整直接判失败"""
    req = urllib.request.Request(spec["url"], headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last_pct = 3.0
            with open(path, "wb") as fh:
                while True:
                    chunk = resp.read(_DL_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = 3.0 + 40.0 * got / total
                        if pct - last_pct >= 1.0:
                            last_pct = pct
                            _set(component, "download", pct,
                                 f"下载 {spec['package']} {got * 100 // total}%")
            if total and got < total:
                raise PackageError(
                    "download_failed", f"下载不完整：{got}/{total} 字节，请重试")
            if not total:
                _set(component, "download", 43.0, f"下载 {spec['package']} 完成")
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError("download_failed", f"下载失败：{exc or '未知错误'}") from exc


def _verify_package(path: Path, spec: dict) -> None:
    """校验下载到的是目标包：非空、扩展名正常、pacman 读出的包名一致"""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size <= 0:
        raise PackageError("download_failed", "下载到的安装包是空的，请重试")
    if not path.name.lower().endswith((".pkg.tar.zst", ".pkg.tar.xz", ".pkg.tar.gz")):
        raise PackageError("verify_failed", f"安装包扩展名异常：{path.name}")
    rc, out = _run(["pacman", "-Qp", str(path)], timeout=15.0)
    if rc != 0:
        raise PackageError(
            "verify_failed", f"安装包校验失败：{(out or 'pacman -Qp 失败')[-200:]}")
    head = (out or "").splitlines()[0].split() if out else []
    if head and head[0] != spec["package"]:
        raise PackageError(
            "verify_failed", f"安装包名称不符：期望 {spec['package']}，实际 {head[0]}")


# SteamOS 桌面常见的 polkit 认证代理；检测到才去弹窗，
# 避免无代理时 pkexec 空等一次没人看的授权
_POLKIT_AGENTS = (
    "polkit-kde-authentication-agent-1",
    "polkit-gnome-authentication-agent-1",
    "polkit-mate-authentication-agent-1",
    "lxpolkit",
)


def _pkexec_ready() -> bool:
    """有 pkexec 且有图形认证代理时才尝试 polkit"""
    if not shutil.which("pkexec"):
        return False
    if not shutil.which("pgrep"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    for name in _POLKIT_AGENTS:
        rc, _ = _run(["pgrep", "-f", name], timeout=3.0)
        if rc == 0:
            return True
    return False


def _pkexec_denied(rc: int, out: str) -> bool:
    """pkexec 授权失败（未授权 / 用户取消 / 没有认证代理）"""
    if rc in (126, 127):
        return True
    low = (out or "").lower()
    return any(s in low for s in (
        "no authentication agent", "not authorized", "not permitted"))


def _needs_password(out: str) -> bool:
    """免密 sudo 缓存过期时会报这些字样，需要回退到界面密码"""
    low = (out or "").lower()
    return any(s in low for s in ("password is required", "a terminal is required",
                                  "askpass", "sudo: a password"))


def _exec_install(mode: str, password: str | None, path: Path, name: str,
                  timeout: float = 630.0) -> tuple[int, str]:
    """按授权方式执行「关只读 → pacman → 恢复只读」脚本。

    密码通过 stdin 传给 `sudo -S`，不进 argv，也不会出现在任何日志/界面里。
    """
    base = ["bash", "-c", _INSTALL_SCRIPT, "deckmeido-install", str(path), name]
    if mode == "sudo":
        argv = ["sudo", "-n"] + base
    elif mode == "pkexec":
        argv = ["pkexec"] + base
    else:
        argv = ["sudo", "-S", "-p", ""] + base
    kwargs = dict(capture_output=True, text=True, timeout=timeout,
                  stderr=subprocess.STDOUT, env={**os.environ})
    if mode == "password":
        kwargs["input"] = (password or "") + "\n"
    try:
        proc = subprocess.run(argv, **kwargs)
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired:
        raise RuntimeError("安装超时（约 10 分钟），可能卡在依赖下载；请重试")
    except FileNotFoundError:
        raise RuntimeError("找不到 sudo / polkit，无法提权安装")
    except OSError as exc:
        raise RuntimeError(f"调用安装命令失败：{exc}")


def _sudo_check(password: str) -> tuple[bool, str]:
    """用 `sudo -S -v` 验证密码（只验证，不执行任何命令）"""
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", "-v"], input=password + "\n",
            capture_output=True, text=True, timeout=15.0, env={**os.environ})
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return False, "本机没有 sudo"
    except subprocess.TimeoutExpired:
        return False, "sudo 校验超时"
    except OSError as exc:
        return False, str(exc)


def _request_password(component: str, message: str) -> str:
    """把任务挂起等界面输入密码；超时或连续错误都由这里统一抛错"""
    global _pw_value, _pw_fail, _pw_attempts, _pw_waiting
    with _pw_lock:
        _pw_value = None
        _pw_fail = ""
        _pw_attempts = 0
        _pw_event.clear()
        _pw_waiting = True
    _set(component, "auth", 50.0, message, state="need_password")
    try:
        if not _pw_event.wait(PASSWORD_WAIT_SECONDS):
            raise RuntimeError("等待管理员密码超时，请重新点击安装")
    finally:
        _pw_waiting = False
    with _pw_lock:
        password, _pw_value = _pw_value, None
        fail = _pw_fail
    if fail:
        raise RuntimeError(fail)
    if not password:
        raise RuntimeError("未收到管理员密码")
    return password


def provide_password(component: str, password: str) -> dict:
    """界面提交管理员密码：先验证，通过后交给正在等待的安装任务"""
    global _pw_value, _pw_fail, _pw_attempts
    if not isinstance(password, str) or not password.strip():
        return {"ok": False, "reason": "empty", "error": "请输入管理员密码"}
    with _job_lock:
        job = dict(_job or {})
    if (job.get("component") != component or job.get("state") != "need_password"
            or not _pw_waiting):
        return {"ok": False, "reason": "not_waiting", "error": "当前没有等待授权的安装任务"}
    ok, _out = _sudo_check(password)
    if ok:
        with _pw_lock:
            _pw_value = password
            _pw_attempts = 0
            _pw_event.set()
        return {"ok": True}
    with _pw_lock:
        _pw_attempts += 1
        left = PASSWORD_MAX_ATTEMPTS - _pw_attempts
        if left <= 0:
            _pw_fail = "管理员密码连续错误，安装已终止"
            _pw_event.set()
    if left <= 0:
        return {"ok": False, "reason": "too_many_attempts",
                "error": "密码连续错误，安装已终止"}
    _set(component, "auth", 50.0, f"管理员密码不正确，还可尝试 {left} 次",
         state="need_password", error="密码不正确")
    return {"ok": False, "reason": "bad_password",
            "error": f"密码不正确，还可再试 {left} 次"}


def _clear_password() -> None:
    global _pw_value, _pw_fail, _pw_attempts, _pw_waiting
    with _pw_lock:
        _pw_value = None
        _pw_fail = ""
        _pw_attempts = 0
        _pw_waiting = False
        _pw_event.clear()


def _install_error_text(out: str, rc: int) -> str:
    """从 pacman / 脚本输出里挑出真正的失败原因，而不是只报「失败」"""
    if "DECKMEIDO_ERR readonly_disable" in (out or ""):
        return ("无法关闭 SteamOS 只读保护，系统包需要可写根分区；"
                "请先在终端执行 steamos-readonly disable 后重试")
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    lines = [l for l in lines if not l.startswith("DECKMEIDO_")]
    keys = ("error", "failed", "invalid", "could not", "unable", "conflict",
            "exists in filesystem", "not found", "signature", "denied",
            "permission", "只读", "无法", "失败")
    hits = [l for l in lines if any(k in l.lower() for k in keys)]
    picked = hits[:3] if hits else lines[-3:]
    detail = "；".join(picked)[-400:]
    return f"安装失败：{detail or f'安装命令退出码 {rc}'}"
