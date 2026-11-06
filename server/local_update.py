"""本地离线更新包：扫描工作区、校验、应用。

包格式（由 tools/make_update_pkg.py 生成）：
    deckmeido-update-<ver>.zip
        manifest.json          版本、平台、文件清单+哈希、包体积
        SHA256SUMS             每个成员的 sha256
        files/                 发布产物（tar.gz / 插件 zip / install.sh / latest.json / ...）

包本身的 sha256 写在同名 `.sha256` 文件里；缺了它一律拒绝安装。
应用链路与网页手动上传共用 `apply_archive`，装完沿用 updater 的 install.sh upgrade 重启。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import zipfile
from pathlib import Path

from . import updater

NAME_RE = re.compile(r"^deckmeido-update-([0-9][0-9A-Za-z._-]*)\.zip$")
MANIFEST = "manifest.json"
SUMS = "SHA256SUMS"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_PLATFORM_OK = {"linux-x86_64", "linux-amd64", "linux", "any", ""}
_MAX_MANIFEST_BYTES = 1 * 1024 * 1024
_MAX_SUMS_BYTES = 2 * 1024 * 1024
_SHA_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")

# 扫描缓存：(路径, mtime_ns, 大小) -> (inspect 结果, 读取时间)
_cache: dict[tuple[str, int, int], tuple[float, dict]] = {}
_CACHE_TTL = 10.0


def local_dir() -> Path:
    cfg = updater.update_cfg()
    return Path(str(cfg.get("local_dir") or (Path.home() / "MeidoSpace"))).expanduser()


def enabled() -> bool:
    return bool(updater.update_cfg().get("local_enable", True))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sidecar_sha(path: Path) -> str:
    for suffix in (".sha256", ".sha256sum"):
        p = path.with_name(path.name + suffix)
        if not p.is_file():
            continue
        try:
            m = _SHA_RE.search(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            return m.group(1).lower()
    return ""


def _unsafe_name(name: str) -> str:
    """zip 成员名是否越界或不可接受；返回空串表示安全。"""
    if not name or "\x00" in name:
        return "成员名为空或含 NUL"
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return f"绝对路径：{name}"
    if "\\" in name:
        return f"路径含反斜杠（可能绕过检查）：{name}"
    parts = Path(name).parts
    if any(p in ("..", ".") for p in parts):
        return f"路径穿越：{name}"
    return ""


def _is_symlink(zi: zipfile.ZipInfo) -> bool:
    mode = (zi.external_attr >> 16) & 0o170000
    return mode == stat.S_IFLNK


def _safe_zip_members(z: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    """校验所有成员并返回 manifest 成员；有问题抛 RuntimeError。"""
    names = set()
    manifest_zi = None
    for zi in z.infolist():
        reason = _unsafe_name(zi.filename)
        if reason:
            raise RuntimeError(f"更新包里有不安全成员：{reason}")
        if _is_symlink(zi):
            raise RuntimeError(f"更新包不允许符号链接：{zi.filename}")
        if zi.is_dir():
            continue
        if zi.filename in names:
            raise RuntimeError(f"更新包里有重复成员：{zi.filename}")
        names.add(zi.filename)
        if zi.filename == MANIFEST:
            manifest_zi = zi
        if zi.file_size > MAX_UPLOAD_BYTES:
            raise RuntimeError(f"更新包成员过大：{zi.filename}")
    return manifest_zi


def _read_member(z: zipfile.ZipFile, name: str, limit: int) -> bytes:
    zi = z.getinfo(name)
    if zi.file_size > limit:
        raise RuntimeError(f"更新包里的 {name} 过大")
    return z.read(zi)


def _parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+[* ]?([^\s]+)\s*$", line)
        if m:
            out[m.group(2).lstrip("./")] = m.group(1).lower()
    return out


def inspect(path: Path) -> dict:
    """校验一个更新包，返回完整信息（ok=False 时 error 说明原因）。"""
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        return {"name": path.name, "path": str(path), "ok": False,
                "error": f"读取失败：{exc}"}
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return dict(hit[1])

    out: dict = {
        "name": path.name, "path": str(path), "size": st.st_size,
        "ok": False, "error": "", "version": "", "platform": "",
        "package_sha256": "", "payload": "", "files": 0,
    }

    def done():
        for k in list(_cache):
            if k[0] == str(path):
                _cache.pop(k, None)
        _cache[key] = (time.time(), dict(out))
        return out

    m = NAME_RE.match(path.name)
    if not m:
        out["error"] = "文件名不是 deckmeido-update-<版本>.zip"
        return done()
    out["version"] = m.group(1)

    expect = _sidecar_sha(path)
    if not expect:
        out["error"] = "缺少包级校验文件（需要同名的 .sha256）"
        return done()
    try:
        got = _sha256_file(path)
    except OSError as exc:
        out["error"] = f"读取失败：{exc}"
        return done()
    if got != expect:
        out["error"] = f"包级 sha256 不匹配（期望 {expect[:12]}…，实际 {got[:12]}…）"
        return done()
    out["package_sha256"] = got

    try:
        with zipfile.ZipFile(path) as z:
            manifest_zi = _safe_zip_members(z)
            if manifest_zi is None:
                raise RuntimeError("更新包里缺 manifest.json")
            if SUMS not in z.namelist():
                raise RuntimeError("更新包里缺 SHA256SUMS")
            manifest = json.loads(_read_member(z, MANIFEST, _MAX_MANIFEST_BYTES)
                                  .decode("utf-8"))
            if not isinstance(manifest, dict):
                raise RuntimeError("manifest.json 不是对象")
            ver = str(manifest.get("version") or "").lstrip("v")
            if ver != out["version"]:
                raise RuntimeError(
                    f"manifest 版本 {ver or '空'} 与文件名版本 {out['version']} 不一致")
            plat = str(manifest.get("platform") or "")
            if plat not in _PLATFORM_OK:
                raise RuntimeError(f"更新包平台不匹配：{plat}")
            out["platform"] = plat or "any"

            pkg_size = manifest.get("package_size")
            if pkg_size is not None:
                try:
                    declared = int(pkg_size)
                except (TypeError, ValueError):
                    raise RuntimeError("manifest.json 的 package_size 格式不对")
                if declared != st.st_size:
                    raise RuntimeError(
                        f"manifest 包体积 {declared} 与实际 {st.st_size} 不一致")

            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries:
                raise RuntimeError("manifest.json 缺 files 文件清单")
            listed: dict[str, dict] = {}
            for e in entries:
                if not isinstance(e, dict):
                    raise RuntimeError("manifest.json 的 files 项格式不对")
                p = str(e.get("path") or "")
                if not p or _unsafe_name(p):
                    raise RuntimeError(f"manifest 文件路径非法：{p}")
                listed[p] = e
            members = {n for n in z.namelist() if not z.getinfo(n).is_dir()}
            extra = members - {MANIFEST, SUMS} - set(listed)
            if extra:
                raise RuntimeError("更新包里有清单外文件：" + "、".join(sorted(extra)))
            missing = set(listed) - members
            if missing:
                raise RuntimeError("更新包缺清单里的文件：" + "、".join(sorted(missing)))

            sums = _parse_sums(_read_member(z, SUMS, _MAX_SUMS_BYTES).decode(
                "utf-8", "replace"))
            for p, e in listed.items():
                want = str(e.get("sha256") or "").lower()
                if not want:
                    raise RuntimeError(f"清单里 {p} 缺校验值")
                if p not in sums:
                    raise RuntimeError(f"SHA256SUMS 缺 {p}")
                if sums[p] != want:
                    raise RuntimeError(f"SHA256SUMS 与清单不一致：{p}")
                zi = z.getinfo(p)
                if e.get("size") is not None and int(e["size"]) != zi.file_size:
                    raise RuntimeError(f"文件大小与清单不一致：{p}")
                got_file = hashlib.sha256(z.read(zi)).hexdigest()
                if got_file != want:
                    raise RuntimeError(f"文件 sha256 不匹配：{p}")
            out["files"] = len(listed)

            payload = manifest.get("payload") or {}
            pfile = str((payload or {}).get("file") or "") if isinstance(payload, dict) else ""
            if not pfile or pfile not in listed:
                raise RuntimeError("manifest.json 没给出可安装的 payload 文件")
            psha = str((payload or {}).get("sha256") or "").lower()
            if not psha or psha != str(listed[pfile].get("sha256") or "").lower():
                raise RuntimeError("payload 的 sha256 与文件清单不一致")
            out["payload"] = pfile
            out["payload_sha256"] = psha
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        out["error"] = f"更新包格式不对：{exc}"
        return done()
    except RuntimeError as exc:
        out["error"] = str(exc)
        return done()

    out["ok"] = True
    return done()


def _candidates(root: Path) -> list[Path]:
    try:
        items = [p for p in root.iterdir()
                 if p.is_file() and NAME_RE.match(p.name)]
    except OSError:
        return []
    return sorted(items, key=lambda p: p.stat().st_mtime, reverse=True)


def status() -> dict:
    """给 /api/status 用的轻量结果；目录不存在/关掉时安静返回空。"""
    cfg = updater.update_cfg()
    root = local_dir()
    out = {
        "enabled": bool(cfg.get("local_enable", True)),
        "dir": str(root),
        "available": False,
        "candidate": None,
        "packages": [],
        "error": "",
    }
    if not out["enabled"]:
        return out
    if not root.is_dir():
        return out
    try:
        files = _candidates(root)
    except OSError as exc:
        out["error"] = f"读取目录失败：{exc}"
        return out
    current = updater.current_version()
    newer: list[dict] = []
    for p in files[:20]:
        info = inspect(p)
        out["packages"].append({
            "name": info["name"], "version": info["version"],
            "size": info["size"], "ok": info["ok"], "error": info["error"],
            "sha256": info["package_sha256"],
        })
        if info["ok"] and updater.ver_tuple(info["version"]) > updater.ver_tuple(current):
            newer.append(info)
    if newer:
        newer.sort(key=lambda x: updater.ver_tuple(x["version"]), reverse=True)
        best = dict(newer[0])
        out["candidate"] = {
            "name": best["name"], "version": best["version"],
            "size": best["size"], "sha256": best["package_sha256"],
            "payload": best.get("payload", ""),
        }
        out["available"] = True
    return out


def apply_archive(path: Path, *, expected_sha: str = "", allow_downgrade: bool = False,
                  restart: bool = True) -> dict:
    """校验并应用一个更新包；失败时抛 RuntimeError（目录已回滚）。"""
    info = inspect(path)
    if not info["ok"]:
        raise RuntimeError(info["error"] or "更新包校验不通过")
    if expected_sha:
        want = expected_sha.strip().lower()
        got = info["package_sha256"]
        if want != got:
            raise RuntimeError(f"上传的包 sha256 与内容不一致（期望 {want[:12]}…）")

    current = updater.current_version()
    ver = info["version"]
    cur_t = updater.ver_tuple(current)
    new_t = updater.ver_tuple(ver)
    if new_t < cur_t and not allow_downgrade:
        raise RuntimeError(f"本地包版本 {ver} 低于当前 {current}，已拒绝降级"
                           "（确需降级请显式确认）")
    mode = "repair" if new_t == cur_t else "upgrade"

    log_path = updater.INSTALL_DIR / "update.log"
    updater._append_log(log_path, f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                                  f"本地更新包 {ver}（{mode}）====\n")
    # 覆盖前先整包备份；_install_tree 自己也会逆序回滚，双保险
    backup = updater._backup(log_path)
    try:
        with zipfile.ZipFile(path) as z:
            payload = z.read(info["payload"])
        updater.apply_tarball_bytes(payload, ver,
                                    expected_sha=info["payload_sha256"],
                                    log_path=log_path)
    except Exception:
        if backup is not None:
            updater._restore(backup, log_path)
        raise

    if backup is not None and backup.exists():
        try:
            backup.unlink()
        except OSError:
            pass
    if not restart:
        return {"ok": True, "restarted": False, "version": ver, "mode": mode,
                "source": "local", "message": f"代码已更新到 {ver}，重启程序后生效"}
    return {**updater.restart_after_apply(ver, "local", log_path), "mode": mode}


def apply_package(name: str, *, allow_downgrade: bool = False,
                  expected_sha: str = "", restart: bool = True) -> dict:
    """按文件名应用工作区里的包（拒绝带路径的名字）。"""
    name = os.path.basename(name or "")
    if not name or not NAME_RE.match(name):
        raise RuntimeError("更新包文件名不合法")
    root = local_dir()
    path = root / name
    if not path.is_file():
        raise RuntimeError(f"工作区里找不到更新包：{name}")
    return apply_archive(path, expected_sha=expected_sha,
                         allow_downgrade=allow_downgrade, restart=restart)


def save_upload(data: bytes, expected_sha: str) -> Path:
    """把网页上传的包写到临时文件；校验值必须由客户端提供。"""
    want = (expected_sha or "").strip().lower()
    if not want:
        raise RuntimeError("缺少包级 sha256 校验值，已拒绝上传")
    if len(data) > MAX_UPLOAD_BYTES:
        raise RuntimeError(f"更新包超过上限（{MAX_UPLOAD_BYTES // (1024 * 1024)}MB）")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError(f"上传的包 sha256 不匹配（客户端 {want[:12]}…，"
                           f"服务端 {got[:12]}…）")
    fd, name = tempfile.mkstemp(prefix="deckmeido-upload-", suffix=".zip")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return Path(name)
