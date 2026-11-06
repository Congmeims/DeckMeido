#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 winres/deckmeido 里的发布产物打成一个本地离线更新包。

用法（仓库根目录）：
    python tools/make_update_pkg.py 0.7.1

产物：
    deckmeido-update-<ver>.zip
    deckmeido-update-<ver>.zip.sha256    包级校验值（后端缺它直接拒绝）

为什么用 zip 而不是 tar.gz：
  - 内层已经是压缩过的 tar.gz，zip 对 payload 用 STORE，不再压第二遍；
  - zipfile 是标准库，后端读清单/成员不需要额外解压器，网页上传也方便；
  - 能随机访问 manifest.json / SHA256SUMS，校验时不用先解整包。

包结构：
    manifest.json     版本、平台、文件清单+哈希、包体积、payload 指向
    SHA256SUMS        每个成员的 sha256（与 manifest 一一对应）
    files/            7 项发布产物（tar.gz、插件 zip、install.sh、latest.json 等）
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINRES = ROOT / "winres" / "deckmeido"
DIST = ROOT / "dist"
PLATFORM = "linux-x86_64"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _required(ver: str, src: Path = WINRES) -> list[Path]:
    names = [
        f"deckmeido-{ver}.tar.gz",
        f"deckmeido-{ver}.tar.gz.sha256",
        f"deckmeido-plugin-{ver}.zip",
        f"deckmeido-plugin-{ver}.zip.sha256",
        "install.sh",
        "latest.json",
        "SHA256SUMS",
    ]
    paths = [src / n for n in names]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        raise SystemExit("winres/deckmeido 缺文件，先跑 tools/release.py：\n  - "
                         + "\n  - ".join(missing))
    return paths


def _write_sums(target: Path, lines: list[str]) -> None:
    target.write_text("".join(lines), encoding="utf-8", newline="\n")


def _append_sums(path: Path, name: str, digest: str) -> None:
    """在现有 SHA256SUMS 末尾加一行；同名旧行先删掉，重复打包不会越积越多。"""
    old = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.rstrip().endswith("  " + name):
                old.append(line.rstrip() + "\n")
    old.append(f"{digest}  {name}\n")
    _write_sums(path, old)


def build(ver: str, *, src: Path = WINRES, out_dir: Path = DIST) -> Path:
    ver = ver.lstrip("v")
    files = _required(ver, src)
    payload = [p for p in files if p.name == f"deckmeido-{ver}.tar.gz"]
    if not payload:
        raise SystemExit("找不到 deckmeido-<ver>.tar.gz")

    entries = []
    for p in files:
        entries.append({
            "path": f"files/{p.name}",
            "size": p.stat().st_size,
            "sha256": sha256(p),
        })
    payload_entry = next(e for e in entries
                         if e["path"].endswith(f"deckmeido-{ver}.tar.gz"))
    manifest = {
        "format": "deckmeido-update",
        "version": ver,
        "platform": PLATFORM,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        # 包体积（zip 自身字节数）：先填 0，后面按实际大小回填到不动点
        "package_size": 0,
        "payload": {"file": payload_entry["path"], "sha256": payload_entry["sha256"]},
        "files": entries,
    }
    sums = "".join(f"{e['sha256']}  {e['path']}\n" for e in entries)

    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"deckmeido-update-{ver}.zip"
    out = out_dir / name
    stage = Path(tempfile.mkdtemp(prefix="deckmeido-update-pkg-"))
    try:
        (stage / "files").mkdir(parents=True, exist_ok=True)
        for p in files:
            shutil.copy2(p, stage / "files" / p.name)
        (stage / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")

        # 包体积就是 zip 自身的字节数，但 zip 大小又受 manifest 里这个数字影响。
        # manifest 用 STORED 写入（字节数只随内容长度变），「量大小 → 回填」两三轮
        # 必然收敛；DEFLATE 可能因数字不同压缩出 ±几字节，导致来回振荡。
        size = 0
        for _ in range(8):
            manifest["package_size"] = size
            (stage / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n")
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                p = stage / "manifest.json"
                zi = zipfile.ZipInfo.from_file(p, "manifest.json")
                zi.external_attr = (0o644 | 0o100000) << 16
                zi.compress_type = zipfile.ZIP_STORED
                z.writestr(zi, p.read_bytes())
                q = stage / "SHA256SUMS"
                zi = zipfile.ZipInfo.from_file(q, "SHA256SUMS")
                zi.external_attr = (0o644 | 0o100000) << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, q.read_bytes())
                for f in files:
                    q = stage / "files" / f.name
                    zi = zipfile.ZipInfo.from_file(q, f"files/{f.name}")
                    zi.external_attr = (0o644 | 0o100000) << 16
                    # 内层 tar.gz 已压缩：再压一遍只会多花 CPU，几乎不省体积
                    zi.compress_type = zipfile.ZIP_STORED
                    z.writestr(zi, q.read_bytes())
            actual = out.stat().st_size
            if actual == size:
                break
            size = actual
        else:
            raise SystemExit("更新包体积无法收敛，manifest 的 package_size 不准确")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    digest = sha256(out)
    sidecar = out.with_name(out.name + ".sha256")
    sidecar.write_text(f"{digest}  {out.name}\n", encoding="utf-8", newline="\n")

    # 同步到 winres（只保留当前版本），并把包级哈希并入 SHA256SUMS
    win = src / name
    shutil.copy2(out, win)
    shutil.copy2(sidecar, src / sidecar.name)
    for old in src.glob("deckmeido-update-*.zip*"):
        try:
            m = old.name[len("deckmeido-update-"):].split(".zip")[0]
        except Exception:
            m = ""
        if m and m.lstrip("v") != ver:
            old.unlink()
    _append_sums(src / "SHA256SUMS", name, digest)
    if (DIST / "SHA256SUMS").is_file():
        _append_sums(DIST / "SHA256SUMS", name, digest)
    return out


def main():
    ver = sys.argv[1] if len(sys.argv) > 1 else json.loads(
        (ROOT / "plugin" / "package.json").read_text(encoding="utf-8"))["version"]
    out = build(ver)
    side = out.with_name(out.name + ".sha256")
    print(f"    {out.name} {out.stat().st_size // 1024} KB")
    print(f"    {side.name} {side.read_text(encoding='utf-8').split()[0][:16]}…")


if __name__ == "__main__":
    main()
