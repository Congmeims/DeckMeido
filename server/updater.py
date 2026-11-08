"""DeckMeido 自升级：查最新版本、拉取代码并重启服务

网页设置页的「检查更新 / 立即升级」和 AI 的 self_update 工具共用这里的逻辑。

默认同时探测两套更新源，取版本更高者（版本相同时优先镜像）：
- 国内镜像（纯静态站）：latest.json + deckmeido-<ver>.tar.gz，不依赖 git
- GitHub：api.github.com + Release 资产（git fetch/checkout 安装）
在 config.toml 的 [update] 段里可以关掉 GitHub（github_enable = false），
关掉后只用镜像。

升级过程会把正在运行的服务换掉，所以分两种走法：
- restart_service=True（用户点了升级、或 systemd 托管）：交给 install.sh upgrade，
  它会同步代码、装依赖、重启服务，等于在终端手动跑一遍
- restart_service=False（进程是被客户端临时拉起的）：只同步文件，不动进程，
  界面会自动重连，新代码下次启动生效
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import netmode, paths

INSTALL_DIR = Path(__file__).resolve().parent.parent

# 默认更新源。镜像站是纯静态目录：放 latest.json、deckmeido-<ver>.tar.gz、SHA256SUMS
DEFAULT_MIRROR = "https://meido.meagames.cn/deckmeido"
REPO = "Congmeims/DeckMeido"
GITHUB_API = "https://api.github.com"

# 这些结果文件不随 tarball 覆盖：升级不该把用户的配置、会话、日志一起换掉。
# 注意 VERSION 不在这个名单里：它记录的正是「现在装的是哪个版本」，
# 排除掉的话升级后 VERSION 还停在旧版本，设置页永远显示旧版本号、检查更新也会
# 一直提示有新版，于是每次都重复下载同一个包
_KEEP_REL = (
    "server/config.toml",
    "server/memory.json",
    "server/meidosoul.json",
    "update.log",
    "RELEASE-NOTES.md",
)
_KEEP_PREFIX = ("server/sessions/", "server/uploads/", "server/static/shots/")


# ---------- TLS ----------
#
# 默认强制校验证书。设备上常挂着代理（DeckyClash 之类）的 TUN 模式，DNS 被
# 劫持到 198.18.0.0/15 的 fake-ip 段，规则没匹配上的域名会被送进错误节点，
# 回一张自签名的假证书，校验必然失败。此时不再自动放宽，只给出明确提示，
# 由用户显式设置环境变量 DECKMEIDO_TLS_INSECURE=1 后才跳过证书校验。
#
# GitHub 那条路同样不放宽：它走 git 操作，没有 sha256 兜底，
# 放宽了等于把代码来源的可靠性交出去。

INSECURE_ENV = "DECKMEIDO_TLS_INSECURE"


def _insecure_tls_allowed() -> bool:
    """只有显式环境变量才允许放宽证书校验（1/true/yes/on）。"""
    return str(os.environ.get(INSECURE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _is_fake_ip(host: str) -> bool:
    """解析结果落在 198.18.0.0/15：几乎可以确定是本机代理的 fake-ip 段"""
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, OSError):
        return False
    return any(a.startswith(("198.18.", "198.19.")) for a in addrs)


# 回退用的系统 CA 包（Arch/SteamOS、Debian/Ubuntu、RHEL 各要一份）
_CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


def _first_existing(paths) -> str:
    """按顺序取第一个存在的文件路径；都不存在时返回空串"""
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isfile(p):
                return str(p)
        except OSError:
            continue
    return ""


def _certifi_bundle() -> str:
    try:
        import certifi
        return certifi.where()
    except (ImportError, OSError):
        return ""


@functools.lru_cache(maxsize=1)
def _ca_bundle() -> str:
    """更新源使用的 CA 包：优先 certifi，回退系统自带的证书束

    Decky 插件由 PyInstaller 打包，它拉起的后端会继承插件解包目录的
    LD_LIBRARY_PATH，_ssl 因此加载了插件自带的 OpenSSL——那套库的默认
    证书路径是 /usr/lib/ssl，SteamOS 上并不存在，于是所有 HTTPS 都报
    `unable to get local issuer certificate`。显式指定 CA 包可以绕开
    「OpenSSL 编译期默认路径不可用」这一类环境问题，而不是靠跳过校验。
    """
    return _first_existing((_certifi_bundle(), *_CA_FILES))


def _source_ssl_context(host: str) -> ssl.SSLContext | None:
    """更新源用的 SSL 上下文：返回 None 表示走系统默认（严格校验）

    默认一律严格校验，只是 CA 包由我们自己指定（见 _ca_bundle）；
    只有显式设置 DECKMEIDO_TLS_INSECURE=1 才放宽。
    这样代理链路出问题时不会悄悄退化成「不验证书」。
    """
    if _insecure_tls_allowed():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    cafile = _ca_bundle()
    if not cafile:
        return None
    try:
        return ssl.create_default_context(cafile=cafile)
    except (OSError, ssl.SSLError):
        return None


def _brief(exc: Exception) -> str:
    """异常压成一行，别把整串 traceback 塞进界面"""
    text = str(exc) or exc.__class__.__name__
    return text.replace("\n", " ")[:200]


def _special_cert_note(exc: Exception, host: str) -> str:
    """证书校验失败时给一句能看懂的说明，别把 OpenSSL 原文抛给用户"""
    raw = _brief(exc)
    if not isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" not in raw:
        return raw
    if "self-signed" in raw or "self signed" in raw:
        hint = f"在代理里给 {host} 加一条直连规则即可"
        if _is_fake_ip(host):
            hint += f"；确认是代理 fake-ip 时也可临时设置 {INSECURE_ENV}=1"
        return (f"{host} 返回了自签名证书，多半是本机代理（DeckyClash 之类）"
                f"把这个域名送进了错误节点。{hint}")
    return f"{host} 证书校验失败（{raw}）"


# ---------- 更新源配置 ----------

def update_cfg() -> dict:
    """读 [update] 段；没有这一段（老配置）时按默认值走"""
    defaults = {
        "mirror": DEFAULT_MIRROR,
        "github_repo": REPO,
        "github_enable": True,       # 双源模式下是否探测 GitHub
    }
    try:
        import tomllib
        cfg_file = paths.config_path()
        if not cfg_file.exists():
            return defaults
        cfg = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    u = cfg.get("update") or {}
    out = dict(defaults)
    if str(u.get("mirror") or "").strip():
        out["mirror"] = str(u["mirror"]).strip().rstrip("/")
    if str(u.get("github_repo") or "").strip():
        out["github_repo"] = str(u["github_repo"]).strip()
    if isinstance(u.get("github_enable"), bool):
        out["github_enable"] = u["github_enable"]
    return out


def sources_enabled(cfg: dict | None = None) -> list[str]:
    """本次实际探测/可用的源：默认两源都开，github_enable = false 时只留镜像"""
    cfg = cfg or update_cfg()
    return ["mirror", "github"] if cfg.get("github_enable", True) else ["mirror"]


def source_label(cfg: dict | None = None) -> str:
    cfg = cfg or update_cfg()
    enabled = sources_enabled(cfg)
    if enabled == ["mirror"]:
        return "仅国内镜像"
    if enabled == ["github"]:
        return "仅 GitHub"
    return "镜像 + GitHub"



# ---------- 版本 ----------

def current_version() -> str:
    """当前版本：优先读安装目录的 VERSION（install.sh 生成），退回 git 描述"""
    try:
        ver = (INSTALL_DIR / "VERSION").read_text(encoding="utf-8").strip()
        if ver:
            return ver.lstrip("v")
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["git", "-C", str(INSTALL_DIR), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if out:
            return out.lstrip("v")
    except (OSError, subprocess.SubprocessError):
        pass
    return "0.0.0"


def ver_tuple(ver: str) -> tuple:
    """"0.1.0" → (0, 1, 0)；非数字段落按 0 处理，方便直接比大小"""
    parts = re.findall(r"\d+", ver or "")
    return tuple(int(p) for p in parts[:4]) or (0,)


_COMMIT_RE = re.compile(r"[0-9a-fA-F]{4,40}")


def _short_commit(raw: str) -> str:
    """从任意文本里取出第一个十六进制提交号，截到 7 位；没有就空串"""
    match = _COMMIT_RE.search(str(raw or ""))
    return match.group(0).lower()[:7] if match else ""


def current_commit() -> str:
    """安装目录对应的短提交号。

    优先读安装目录的 COMMIT（部署脚本 / 发布包写入，git archive 部署没有 .git
    时只能靠它）；没有就退回本仓库的 git；都拿不到返回空串，界面不显示后缀。
    """
    try:
        raw = (INSTALL_DIR / "COMMIT").read_text(encoding="utf-8").strip()
        commit = _short_commit(raw)
        if commit:
            return commit
    except OSError:
        pass
    if (INSTALL_DIR / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(INSTALL_DIR), "rev-parse", "--short=7", "HEAD"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            return _short_commit(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def version_display(version: str | None = None, commit: str | None = None) -> str:
    """界面展示用的版本：`0.7.5-f2931ae`；没有提交号就只是 `0.7.5`。

    纯显示用途。升级判断只读 VERSION 的语义版本（current_version / ver_tuple），
    这里的提交后缀不参与 has_update。
    """
    ver = (version if version is not None else current_version()).strip().lstrip("v")
    suffix = _short_commit(commit if commit is not None else current_commit())
    return f"{ver}-{suffix}" if suffix else ver


# ---------- 查最新版本 ----------

def _http_json(url: str, timeout: float = 10.0,
               context: ssl.SSLContext | None = None) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "DeckMeido",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_text(url: str, timeout: float = 10.0,
               context: ssl.SSLContext | None = None) -> str:
    """拉取纯文本资产（Release 的 .sha256 文件）"""
    req = urllib.request.Request(url, headers={"User-Agent": "DeckMeido"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.read().decode("utf-8", "replace")


_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def _sha_from_text(text: str) -> str:
    m = _SHA256_RE.search(text or "")
    return m.group(1).lower() if m else ""


def mirror_release(mirror: str | None = None) -> dict:
    """查镜像站上的 latest.json

    格式（发布脚本生成）：
      {"tag": "0.2.0", "name": "DeckMeido 0.2.0", "notes": "...",
       "published_at": "2026-10-01T12:00:00Z",
       "files": {"tarball": "deckmeido-0.2.0.tar.gz", "sha256": "..."}}
    """
    base = (mirror or update_cfg()["mirror"]).rstrip("/")
    # 离线模式：镜像站一律快捷拒绝，不发起连接、也不等超时
    netmode.check(base, "检查更新")
    host = urllib.parse.urlsplit(base).hostname or ""
    ctx = _source_ssl_context(host)
    try:
        data = _http_json(f"{base}/latest.json", context=ctx)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"tag": "", "name": "", "notes": "", "url": "", "published_at": ""}
        raise RuntimeError(f"镜像站查询失败（HTTP {exc.code}）")
    except ssl.SSLError as exc:
        raise RuntimeError(f"连不上镜像站：{_special_cert_note(exc, host)}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raise RuntimeError(f"连不上镜像站：{_special_cert_note(reason, host)}")
        raise RuntimeError(f"连不上镜像站：{_brief(exc)}")
    except ValueError as exc:
        raise RuntimeError(f"镜像站返回的 latest.json 格式不对：{exc}")
    tag = str(data.get("tag") or "").lstrip("v")
    files = data.get("files") or {}
    tarball = str(files.get("tarball") or "").strip()
    if not tarball:
        tarball = f"deckmeido-{tag}.tar.gz"
    return {
        "tag": tag,
        "name": str(data.get("name") or ""),
        "notes": str(data.get("notes") or "")[:4000],
        "url": str(data.get("url") or ""),
        "published_at": str(data.get("published_at") or ""),
        "source": "mirror",
        "tarball_url": f"{base}/{tarball}",
        "sha256": str(files.get("sha256") or "").strip(),
    }


def github_release(repo: str | None = None) -> dict:
    """查 GitHub 最新 release（同步 HTTP，调用方放线程里跑）

    公开 API，不带任何认证头。发布包里优先读 latest.json 资产；没有时
    用 tag_name + 资产列表拼下载地址。独立 .sha256 也尽量拿，
    拿不到时留空，由下载环节改验包内 SHA256SUMS。
    """
    repo = repo or update_cfg()["github_repo"]
    netmode.check(GITHUB_API, "检查更新")
    host = urllib.parse.urlsplit(GITHUB_API).hostname or ""
    ctx = _source_ssl_context(host)
    try:
        data = _http_json(f"{GITHUB_API}/repos/{repo}/releases/latest", context=ctx)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 还没有发布过 release，就不算有更新
            return {"tag": "", "name": "", "notes": "", "url": "", "published_at": ""}
        raise RuntimeError(f"GitHub 查询失败（HTTP {exc.code}）")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"连不上 GitHub：{_brief(exc)}")
    except ValueError as exc:
        raise RuntimeError(f"GitHub 返回内容无法解析：{exc}")
    raw_tag = str(data.get("tag_name") or "")
    tag = raw_tag.lstrip("v")
    assets = {}
    for a in data.get("assets") or []:
        if isinstance(a, dict) and a.get("name"):
            assets[str(a["name"])] = str(a.get("browser_download_url") or "")
    payload: dict = {}
    latest_url = assets.get("latest.json") or ""
    if latest_url:
        try:
            payload = _http_json(latest_url, context=ctx)
        except Exception:
            payload = {}
    files = (payload.get("files") or {}) if isinstance(payload, dict) else {}
    tarball = str(files.get("tarball") or "").strip() or f"deckmeido-{tag}.tar.gz"
    tarball_url = assets.get(tarball) or (
        f"https://github.com/{repo}/releases/download/{raw_tag}/{tarball}")
    sha256 = str(files.get("sha256") or "").strip().lower()
    if not sha256:
        sha_url = assets.get(f"{tarball}.sha256") or assets.get("SHA256SUMS")
        if sha_url:
            try:
                sha256 = _sha_from_text(_http_text(sha_url, context=ctx))
            except Exception:
                sha256 = ""
    return {
        "tag": tag,
        "name": data.get("name") or "",
        "notes": (data.get("body") or "")[:4000],
        "url": data.get("html_url") or "",
        "published_at": data.get("published_at") or "",
        "source": "github",
        "tarball_url": tarball_url,
        "sha256": sha256,
    }


def _pick_higher(mirror: dict | None, github: dict | None) -> dict | None:
    """两源各查一次后选版本更高的；相等时优先镜像"""
    if not mirror:
        return github
    if not github:
        return mirror
    return github if ver_tuple(github.get("tag", "")) > ver_tuple(mirror.get("tag", "")) \
        else mirror


def probe_sources(cfg: dict | None = None) -> dict:
    """同时探测镜像与 GitHub，单源失败不影响另一源

    返回 {"mirror": info|None, "github": info|None, "mirror_error": str, "github_error": str}
    """
    cfg = cfg or update_cfg()
    allowed = sources_enabled(cfg)
    out: dict = {"mirror": None, "github": None,
                 "mirror_error": "", "github_error": ""}

    def _probe(src: str) -> tuple[str, dict | None, str]:
        try:
            info = mirror_release(cfg["mirror"]) if src == "mirror" \
                else github_release(cfg["github_repo"])
            return src, info, ""
        except RuntimeError as exc:
            return src, None, str(exc)

    with ThreadPoolExecutor(max_workers=len(allowed)) as pool:
        for src, info, err in pool.map(_probe, allowed):
            out[src] = info
            out[f"{src}_error"] = err
    return out


def latest_release() -> dict:
    """两源同时探测后取更高版本；两源都失败才抛 RuntimeError"""
    cfg = update_cfg()
    probe = probe_sources(cfg)
    mirror, github = probe["mirror"], probe["github"]
    chosen = _pick_higher(mirror, github)
    if not chosen:
        errors = [e for e in (probe["mirror_error"], probe["github_error"]) if e]
        raise RuntimeError("；".join(errors) or "查询更新失败")
    info = dict(chosen)
    info.update({
        "source_used": chosen.get("source", ""),
        "mirror_version": (mirror or {}).get("tag", ""),
        "github_version": (github or {}).get("tag", ""),
        "github_reachable": bool(github),
        "mirror_reachable": bool(mirror),
    })
    errors = [e for e in (probe["mirror_error"], probe["github_error"]) if e]
    if errors:
        info["fallback_from"] = errors
    return info


def notes_summary(text) -> str:
    """取发布说明里第一句非空内容，给「版本」旁边那句更新总结用"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Release notes 的常见写法：先去掉 markdown 标题/列表符号
        line = line.lstrip("#*- ").strip()
        if line:
            return line[:120]
    return ""


async def check() -> dict:
    """比对当前版本与最新 release"""
    netmode.check(feature="检查更新")
    latest = await asyncio.to_thread(latest_release)
    current = current_version()
    cfg = update_cfg()
    return {
        "current": current,
        "has_update": bool(latest["tag"]) and ver_tuple(latest["tag"]) > ver_tuple(current),
        "source_label": source_label(cfg),
        "notes_summary": notes_summary(latest.get("notes")),
        **latest,
    }


# ---------- 应用升级 ----------

def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(INSTALL_DIR), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _append_log(path: Path, text: str) -> None:
    try:
        with open(path, "ab") as f:
            f.write(text.encode())
    except OSError:
        pass


def _sums_verified(tf: tarfile.TarFile, got: str) -> bool | None:
    """包内如果带 SHA256SUMS，就用它校验；没有返回 None

    拿不到独立 .sha256 资产时（GitHub Release 常见），这是最后一道校验。
    """
    for m in tf.getmembers():
        if not m.isfile() or m.name.rsplit("/", 1)[-1] != "SHA256SUMS":
            continue
        fh = tf.extractfile(m)
        if fh is None:
            continue
        text = fh.read().decode("utf-8", "replace")
        hashes = [h.lower() for h in re.findall(r"[0-9a-fA-F]{64}", text)]
        if not hashes:
            return None
        if got not in hashes:
            raise RuntimeError("安装包 SHA256SUMS 校验不通过")
        return True
    return None


def _apply_mirror(info: dict, log_path: Path) -> str:
    """下载安装包覆盖到安装目录（不动配置/会话），返回版本号"""
    url = info.get("tarball_url") or ""
    ver = (info.get("tag") or "").lstrip("v")
    if not url:
        raise RuntimeError("更新源没有给出安装包地址（latest.json 缺 files.tarball）")
    _append_log(log_path, f"下载安装包：{url}\n")
    # 离线模式：apply() 已经拦过一次，这里按实际下载地址再兜一次（本地镜像放行）
    netmode.check(url, "下载安装包")
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DeckMeido"})
        with urllib.request.urlopen(
            req, timeout=180, context=_source_ssl_context(host)
        ) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"下载安装包失败（HTTP {exc.code}）：{url}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raise RuntimeError(f"下载安装包失败：{_special_cert_note(reason, host)}")
        raise RuntimeError(f"下载安装包失败：{_brief(exc)}")

    expect = (info.get("sha256") or "").strip().lower()
    got = hashlib.sha256(blob).hexdigest()
    if expect:
        if got != expect:
            raise RuntimeError(f"安装包校验不通过（期望 {expect[:12]}…，实际 {got[:12]}…）")
        _append_log(log_path, "sha256 校验通过\n")

    with tempfile.TemporaryDirectory(prefix="deckmeido-upd-") as tmp:
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                if not expect:
                    checked = _sums_verified(tf, got)
                    if checked is None:
                        # 没有任何可用的校验值就不能装：否则被篡改的包会被直接覆盖进代码目录
                        raise RuntimeError(
                            "拿不到校验值（没有独立 .sha256，包内也没有 SHA256SUMS），已拒绝安装")
                    _append_log(log_path,
                                "未拿到独立 .sha256，已按包内 SHA256SUMS 校验通过\n")
                _safe_extract(tf, tmp)
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(f"安装包解压失败：{exc}")
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("安装包结构不对：里面没有目录")
        _install_tree(roots[0], log_path)
    # 发布包里的 VERSION 会被 overlay 带过来；万一包里没有（老包/手工打的包），
    # 就用 latest.json 的 tag 补上，否则升级完了版本号还是旧的
    ver_file = INSTALL_DIR / "VERSION"
    try:
        got = ver_file.read_text(encoding="utf-8").strip().lstrip("v")
    except OSError:
        got = ""
    if got != ver and ver:
        ver_file.write_text(ver + "\n", encoding="utf-8")
        _append_log(log_path, f"写入 VERSION={ver}\n")
    _append_log(log_path, f"安装包已覆盖到 {INSTALL_DIR}\n")
    return ver


def _overlay_tree(src: Path, dst: Path) -> None:
    """把发布包里的文件盖到安装目录，用户数据一律跳过"""
    for item in src.iterdir():
        name = item.name
        if name in (".git", ".venv"):
            continue
        if item.is_dir():
            if name == "server":
                _overlay_tree(item, dst / "server")
            else:
                _merge(item, dst / name)
        else:
            rel = name
            if rel in _KEEP_REL:
                continue
            shutil.copy2(item, dst / name)


def _merge(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src).as_posix()
        if rel in _KEEP_REL or rel.startswith(_KEEP_PREFIX):
            continue
        if ".venv" in item.parts or "__pycache__" in item.parts:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _ensure_exec_bits(root: Path) -> None:
    """给脚本补上可执行位，避免覆盖安装后「缺少运行权限」。

    发布包可能是在 Windows/NTFS 上打的（权限位是 0666），git 导出也可能把
    `client/launch.sh` 这类脚本记成 0644。用 `bash install.sh` 调用不受影响，
    但桌面快捷方式是直接执行 `client/launch.sh` 的，缺可执行位时 Steam 会弹
    「缺少运行权限」，用户看到的就是热更新之后启动不了。这里在替换前统一补位，
    单个文件失败不影响整体升级。
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith((".sh", ".desktop")):
                continue
            p = Path(dirpath) / name
            try:
                p.chmod(p.stat().st_mode | 0o111)
            except OSError:
                pass


def _install_tree(src: Path, log_path: Path) -> None:
    """把解压好的新版本装进安装目录：先在本机临时目录建完整副本，再逐个原子替换。

    和旧实现（直接往安装目录里覆盖）相比，失败时不会留下「一半新一半旧」的目录：
    每一项都是 os.replace 完成的，中途出错按逆序把旧目录搬回来。
    server/ 下的配置、会话、上传、截图属于用户数据，替换前先复制进新副本。
    """
    stamp = f"{os.getpid()}-{int(time.time())}"
    stage_root = INSTALL_DIR / f".update-stage-{stamp}"
    if stage_root.exists():
        shutil.rmtree(stage_root, ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[str, Path | None]] = []
    try:
        staged: list[str] = []
        for item in src.iterdir():
            name = item.name
            if name in (".git", ".venv", "userdata", "update.log"):
                continue
            dst = stage_root / name
            if item.is_dir():
                shutil.copytree(item, dst, symlinks=True)
            else:
                shutil.copy2(item, dst)
            staged.append(name)

        # 统一补可执行位：包里的权限位不可信（Windows 打包常见 0666/0644）
        _ensure_exec_bits(stage_root)

        # server/ 里混着运行期数据：整棵换掉之前先把它们搬进新副本
        server_src = INSTALL_DIR / "server"
        server_dst = stage_root / "server"
        if server_dst.is_dir():
            for rel in ("config.toml", "memory.json", "meidosoul.json"):
                p = server_src / rel
                if p.is_file():
                    shutil.copy2(p, server_dst / rel)
            for rel in ("sessions", "uploads", "static/shots"):
                p = server_src / rel
                if p.is_dir():
                    shutil.copytree(p, server_dst / rel, symlinks=True, dirs_exist_ok=True)

        try:
            for name in staged:
                target = INSTALL_DIR / name
                old = INSTALL_DIR / f".update-old-{name}-{stamp}"
                if os.path.lexists(target):
                    os.replace(target, old)
                    moved.append((name, old))
                else:
                    moved.append((name, None))
                os.replace(stage_root / name, target)
        except Exception:
            # 逆序回滚：先删掉已经换上去的新项，再把旧项搬回原位
            for name, old in reversed(moved):
                target = INSTALL_DIR / name
                if os.path.lexists(target):
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        try:
                            target.unlink()
                        except OSError:
                            pass
                if old is not None and os.path.lexists(old):
                    os.replace(old, target)
            raise

        for _name, old in moved:
            if old is None or not os.path.lexists(old):
                continue
            try:
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    old.unlink()
            except OSError:
                pass
        _append_log(log_path, "新版本已原子替换到安装目录\n")
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def apply_tarball_bytes(blob: bytes, ver: str, *, expected_sha: str,
                        log_path: Path | None = None) -> str:
    """把已下载的发布包字节装进安装目录（远端升级用）。

    校验值必须由调用方提供：没有校验值的包一律拒绝，避免被篡改的包直接覆盖代码。
    """
    log_path = log_path or (INSTALL_DIR / "update.log")
    got = hashlib.sha256(blob).hexdigest()
    expect = (expected_sha or "").strip().lower()
    if not expect:
        raise RuntimeError("缺少安装包校验值，已拒绝安装")
    if got != expect:
        raise RuntimeError(f"安装包校验不通过（期望 {expect[:12]}…，实际 {got[:12]}…）")
    _append_log(log_path, f"安装包 sha256 校验通过：{got}\n")
    with tempfile.TemporaryDirectory(prefix="deckmeido-upd-") as tmp:
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                _safe_extract(tf, tmp)
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(f"安装包解压失败：{exc}")
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("安装包结构不对：里面没有目录")
        _install_tree(roots[0], log_path)
    ver_file = INSTALL_DIR / "VERSION"
    try:
        now_ver = ver_file.read_text(encoding="utf-8").strip().lstrip("v")
    except OSError:
        now_ver = ""
    if ver and now_ver != ver:
        ver_file.write_text(ver + "\n", encoding="utf-8")
        _append_log(log_path, f"写入 VERSION={ver}\n")
    return ver


def _unsafe_member(root: Path, m: tarfile.TarInfo) -> str:
    """成员路径或链接目标是否越界；返回空串表示安全。"""
    p = (root / m.name).resolve()
    if p != root and root not in p.parents:
        return f"成员路径越界：{m.name}"
    if m.issym() or m.islnk():
        # 符号链接相对所在目录，硬链接相对包根；绝对目标会被 Path 拼接直接带出 root
        base = (root / m.name).parent if m.issym() else root
        target = (base / m.linkname).resolve()
        if target != root and root not in target.parents:
            return f"链接指向越界：{m.name} -> {m.linkname}"
    return ""


def _safe_extract(tf: tarfile.TarFile, dest: str) -> None:
    """解压 tarball 到 dest，拒绝路径穿越与越界符号链接。

    Python 3.12 的 filter="data" 已经做这些事；更老版本没有 filter 参数，
    这里先手工校验成员路径与链接目标再走 extractall 兜底（镜像包若未提供
    sha256 且站点被攻破，恶意成员可能把文件写进任意目录，或先放一个
    指向 /etc 的符号链接再借后续成员写出去）。
    """
    root = Path(dest).resolve()
    for m in tf.getmembers():
        reason = _unsafe_member(root, m)
        if reason:
            raise RuntimeError(f"安装包里有不安全成员：{reason}")
    try:
        tf.extractall(dest, filter="data")
    except TypeError:  # Python < 3.12 没有 filter 参数
        tf.extractall(dest)


def _backup(log_path: Path) -> Path | None:
    """覆盖前把代码目录打包一份，镜像升级失败时能整个盖回去

    只备份代码目录，不含 .venv / 用户数据；出问题属于罕见情况，
    备份失败也不拦着升级（记一条日志，继续走）。
    """
    dest = INSTALL_DIR / f"update-backup-{time.strftime('%Y%m%d%H%M%S')}.tar.gz"

    def _skip(ti: tarfile.TarInfo):
        parts = ti.name.split("/")
        if ".venv" in parts or "__pycache__" in parts:
            return None
        if "sessions" in parts or "uploads" in parts or "shots" in parts:
            return None
        if ti.name.endswith("config.toml") or ti.name.endswith(".log"):
            return None
        return ti

    try:
        with tarfile.open(dest, "w:gz") as tf:
            for name in ("server", "client", "assets", "deploy", "tools", "plugin",
                         "requirements.txt", "install.sh", "VERSION"):
                item = INSTALL_DIR / name
                if item.exists():
                    tf.add(item, arcname=name, filter=_skip)
        _append_log(log_path, f"已备份当前代码：{dest}\n")
        return dest
    except (OSError, tarfile.TarError) as exc:
        _append_log(log_path, f"备份失败（继续升级）：{exc}\n")
        return None


def _restore(backup: Path | None, log_path: Path) -> None:
    if backup is None or not backup.exists():
        return
    try:
        with tarfile.open(backup, "r:gz") as tf:
            try:
                tf.extractall(INSTALL_DIR, filter="data")
            except TypeError:  # Python < 3.12
                tf.extractall(INSTALL_DIR)
        _append_log(log_path, f"已从备份恢复：{backup}\n")
    except (OSError, tarfile.TarError) as exc:
        _append_log(log_path, f"从备份恢复失败：{exc}；手动解压 {backup} 到 {INSTALL_DIR}\n")


def _fix_owners(log_path: Path) -> None:
    """把插件目录目录树还给安装目录的属主。

    Decky 以 root 加载插件（plugin/ 是软链接到本仓库），会在里面写
    __pycache__ 与票据，属主变成 root 之后 deck 用户再 git checkout 就会报
    `unable to unlink old 'plugin/main.py': Permission denied`。
    升级前先纠一次；自己不是 root 时无从修，静默跳过。
    """
    # Windows 上没有 geteuid / chown，整段跳过（开发机跑预览时不该因此报错）
    geteuid = getattr(os, "geteuid", None)
    chown = getattr(os, "chown", None)
    if geteuid is None or chown is None or geteuid() != 0:
        return
    root = INSTALL_DIR / "plugin"
    if not root.exists():
        return
    try:
        uid = INSTALL_DIR.stat().st_uid
        for child in [root, *root.rglob("*")]:
            if child.lstat().st_uid != uid:
                chown(child, uid, -1, follow_symlinks=False)
        _append_log(log_path, "已把 plugin/ 的属主改回安装用户\n")
    except OSError as exc:
        _append_log(log_path, f"修正 plugin/ 属主失败：{exc}\n")


def _apply_git(target: str, log_path: Path) -> str:
    """git 安装：stash → fetch → checkout <tag>，失败回滚到原 commit"""
    before = _git("rev-parse", "HEAD", timeout=10)
    prev = (before.stdout or "").strip()
    if before.returncode != 0:
        raise RuntimeError(f"读取当前版本失败：{(before.stderr or '').strip()[:300]}")
    _append_log(log_path, f"git 升级 {current_version()} → {target}（原 {prev[:8]}）\n")

    _fix_owners(log_path)

    # 有本地改动先 stash：直接 checkout 会报「local changes would be overwritten」
    dirty = _git("status", "--porcelain", timeout=30).stdout.strip()
    stashed = False
    if dirty:
        st = _git("stash", "push", "--include-untracked", "-m",
                  f"deckmeido-autoupdate-{time.strftime('%Y%m%d%H%M%S')}")
        if st.returncode != 0:
            raise RuntimeError(f"暂存本地改动失败：{(st.stderr or '').strip()[:300]}")
        stashed = True
        _append_log(log_path, "已暂存本地改动（git stash pop 可恢复）\n")

    def _rollback(reason: str) -> None:
        _git("checkout", prev, timeout=60)
        msg = (f"{reason}\n已回滚到升级前的版本（{prev[:8]}）。"
               f"手动回滚命令：git -C {INSTALL_DIR} checkout {prev}")
        if stashed:
            pop = _git("stash", "pop", timeout=60)
            if pop.returncode == 0:
                msg += "\n本地改动已恢复。"
            else:
                msg += "\n本地改动仍保存在 git stash 里（git stash pop 手动恢复）。"
        _append_log(log_path, msg + "\n")
        raise RuntimeError(msg)

    fetch = _git("fetch", "--tags", "--force", "origin", timeout=180)
    if fetch.returncode != 0:
        _rollback(f"git fetch 失败：{(fetch.stderr or fetch.stdout).strip()[:300]}")

    # 优先按 tag 切换；tag 不存在时退回对应的 origin/main
    co = _git("checkout", "--force", f"v{target}", timeout=120)
    if co.returncode != 0:
        co = _git("checkout", "--force", f"{target}", timeout=120)
    if co.returncode != 0:
        _rollback(f"切换到 {target} 失败：{(co.stderr or co.stdout).strip()[:300]}")
    after = (_git("rev-parse", "--short", "HEAD", timeout=10).stdout or "").strip()
    _append_log(log_path, f"已切换到 {target}（{after}）\n")
    return target


def apply_order(primary: str, probe: dict, target: str,
                cfg: dict | None = None) -> list[str]:
    """主源失败后能否回退到另一源。

    只有另一源的版本 ≥ 目标版本才允许回退，避免「主源包坏了就降级安装」。
    """
    cfg = cfg or update_cfg()
    allowed = sources_enabled(cfg)
    order = [primary] if primary in allowed else []
    other = "github" if primary == "mirror" else "mirror"
    info = probe.get(other) or {}
    if other in allowed and info and \
            ver_tuple((info.get("tag") or "").lstrip("v")) >= ver_tuple(target):
        order.append(other)
    return order


def apply(restart_service: bool = True, tag: str = "") -> dict:
    """切换到指定版本并（可选）重启服务；返回给调用方的说明文字

    tag 为空时同时探测两源并取更高版本。下载/校验失败时只在版本不降级
    的前提下回退另一源：
    - 镜像：下 tarball 覆盖文件，装成 git 仓库的也能用（不动 .git）
    - GitHub：stash 本地改动 → fetch --tags → checkout <tag>，
      失败一律回滚到原 commit，并把回滚命令写进日志
    """
    log_path = INSTALL_DIR / "update.log"
    # 离线模式：升级必须联网，直接给可读原因，不先跑一轮探测
    netmode.check(feature="检查更新")
    cfg = update_cfg()
    probe = probe_sources(cfg)
    target = (tag or "").lstrip("v")
    if not target:
        chosen = _pick_higher(probe["mirror"], probe["github"])
        if not chosen:
            errors = [e for e in (probe["mirror_error"], probe["github_error"]) if e]
            raise RuntimeError("；".join(errors) or "没有可用的更新源")
        target = (chosen.get("tag") or "").lstrip("v")
        primary = chosen.get("source") or "mirror"
    else:
        # 点名版本时镜像可以直接拼 tarball 地址；GitHub 要已有 Release 信息
        primary = "mirror" if "mirror" in sources_enabled(cfg) else "github"
    if not target:
        raise RuntimeError("没有可用的版本号：更新源上还没有发布任何版本")
    order = apply_order(primary, probe, target, cfg)
    if not order:
        raise RuntimeError("没有可用的更新源")
    _append_log(log_path, f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"升级到 {target} ====\n")

    is_git = (INSTALL_DIR / ".git").exists()
    used = ""
    errors: list[str] = []
    backup: Path | None = None
    touched = False          # 镜像包是否已经开始覆盖文件（决定要不要回滚）

    for src in order:
        info: dict = {}
        try:
            if src == "mirror":
                info = dict(probe.get("mirror") or {})
                if (info.get("tag") or "").lstrip("v") != target:
                    # 用户点名了版本：镜像站 latest.json 只描述最新版，
                    # 旧版本按约定文件名拼出来
                    info = {"tag": target, "sha256": "",
                            "tarball_url": f"{cfg['mirror']}/deckmeido-{target}.tar.gz"}
                # 覆盖式升级没有 git 兜底：先备份。备份本身失败（返回 None）时
                # 就不能再覆盖了——盖到一半出错没有任何东西可以盖回去
                backup = backup or _backup(log_path)
                if backup is None or not backup.exists():
                    raise RuntimeError("升级前备份失败，为免目录被改坏已中止本次覆盖")
                # 从这一步起文件才可能被改动，回滚判断从这里开始算
                touched = True
                _apply_mirror(info, log_path)
                used = "mirror"
            else:
                if not is_git:
                    raise RuntimeError("当前不是 git 安装，GitHub 源需要 git 仓库")
                _apply_git(target, log_path)
                used = "github"
            break
        except Exception as exc:  # 解压/校验/权限等意外也要能走到下一个源
            errors.append(str(exc))
            _append_log(log_path, f"{src} 升级失败：{exc}\n")
            # 镜像覆盖到一半失败：先还原，免得目录里新旧文件混在一起
            if src == "mirror" and touched:
                _restore(backup, log_path)
                touched = False
    else:
        raise RuntimeError("；".join(errors) or "升级失败")

    if errors:
        _append_log(log_path, "已回退到备用源：" + errors[0] + "\n")
    _append_log(log_path, f"升级来源：{used}\n")

    # 升级成功就不再需要这份备份，留着白占几十兆空间
    if backup is not None and backup.exists():
        try:
            backup.unlink()
        except OSError:
            pass

    if not restart_service:
        return {"ok": True, "restarted": False, "version": target,
                "source": used, "message": f"代码已更新到 {target}，重启程序后生效"}
    return restart_after_apply(target, used, log_path)


_SOURCE_NOTE = {
    "mirror": "（镜像站）",
    "github": "（GitHub）",
}


def restart_after_apply(target: str, used: str, log_path: Path) -> dict:
    """装完代码后的统一重启：镜像、GitHub 都走这里。

    日志已由调用方落盘，这里只负责拉起 install.sh upgrade。
    用 /bin/sh 拉起会踩到 bash 专有语法（数组、进程替换），必须显式指定 bash；
    系统里没有 bash 时退回 /bin/sh 只会得到一堆语法错误，所以找不到就不重启，
    直接把「代码已更新，重启生效」告诉用户。
    """
    bash = shutil.which("bash")
    script = INSTALL_DIR / "install.sh"
    if not bash:
        return {"ok": True, "restarted": False, "version": target, "source": used,
                "message": f"代码已更新到 {target}，但找不到 bash 来跑升级脚本，请手动重启程序"}
    if not script.is_file():
        return {"ok": True, "restarted": False, "version": target, "source": used,
                "log": str(log_path),
                "message": f"代码已更新到 {target}，但安装目录里没有 install.sh，"
                           f"无法自动重启；请手动执行：bash {script} upgrade"}
    try:
        # 可执行位不可信（Windows 打包 / git 导出都可能是 0644）：先补再跑，
        # 补不了也用 bash 调用，不会因为权限位卡住升级。
        script.chmod(script.stat().st_mode | 0o111)
    except OSError:
        pass
    with open(log_path, "ab") as log:
        # 内联升级没有终端：显式声明非交互，install.sh 才会自动更新 QAM 插件。
        # 否则 ask_yes 读不到 /dev/tty 会回答「不」，插件就静默停在旧版本。
        env = dict(os.environ, DECKMEIDO_NONINTERACTIVE="1")
        try:
            subprocess.Popen(
                [bash, str(script), "upgrade"],
                cwd=str(INSTALL_DIR), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env,
            )
        except OSError as exc:
            return {"ok": True, "restarted": False, "version": target, "source": used,
                    "log": str(log_path),
                    "message": f"代码已更新到 {target}，但升级脚本启动失败：{exc}；"
                               f"请手动执行：bash {script} upgrade"}
    src_note = _SOURCE_NOTE.get(used, "")
    return {"ok": True, "restarted": True, "version": target, "source": used,
            "log": str(log_path),
            "message": f"已更新到 {target}{src_note}，服务重启后生效"}
