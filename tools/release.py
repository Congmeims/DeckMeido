#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键发版：打 tar.gz + 插件 zip + latest.json，并上传到镜像站。

用法（仓库根目录）：
    python tools/release.py 0.4.1
不带版本号时读 plugin/package.json 的 version。

产物写到 dist/，只打包不上传；
上传镜像站用 tools/deploy_all.py。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

ITEMS = [
    "server", "client", "assets", "deploy", "plugin",
    "requirements.txt", "requirements-gui.txt", "README.md", "LICENSE", "install.sh",
]
# tools/ 是仓库自用的运维脚本（连服务器的、打包的），不进发布包。
# cli/ 里要是有给用户用的工具，单独列在这里
EXTRA_DIRS = ["cli"]
# 不进发布包的运行产物
SKIP_ANY = {
    "server/config.toml", "server/sessions", "server/uploads",
    "server/static/shots", "server/overlay.log", "plugin/node_modules",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_tarball(ver: str) -> Path:
    name = f"deckmeido-{ver}"
    stage_root = Path(tempfile.mkdtemp(prefix="deckmeido-rel-"))
    stage = stage_root / name
    stage.mkdir(parents=True)
    for item in ITEMS + [d for d in EXTRA_DIRS if (ROOT / d).is_dir()]:
        src = ROOT / item
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(
                src, stage / item,
                ignore=shutil.ignore_patterns("__pycache__", "node_modules"),
            )
        else:
            shutil.copy2(src, stage / item)
    for junk in SKIP_ANY:
        p = stage / junk
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    # 离线依赖包：有就带上，没有不影响发版
    deps = ROOT / "deploy" / "deps"
    if deps.is_dir() and any(deps.glob("*.pkg.tar.zst")):
        (stage / "deploy" / "deps").mkdir(parents=True, exist_ok=True)
        for f in deps.glob("*.pkg.tar.zst"):
            shutil.copy2(f, stage / "deploy" / "deps" / f.name)

    (stage / "VERSION").write_text(ver + "\n", encoding="utf-8", newline="\n")
    notes = subprocess.run(
        ["git", "log", "-n", "20", "--no-merges", "--pretty=- %s"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    (stage / "RELEASE-NOTES.md").write_text(
        f"# DeckMeido {ver}\n\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M}\n\n"
        f"## 最近提交\n\n{notes}", encoding="utf-8", newline="\n",
    )

    out = DIST / f"{name}.tar.gz"
    DIST.mkdir(exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        tf.add(stage, arcname=name)
    shutil.rmtree(stage_root, ignore_errors=True)
    return out


def build_plugin_zip(ver: str) -> Path:
    plugin = ROOT / "plugin"
    out = DIST / f"deckmeido-plugin-{ver}.zip"
    # Decky 的安装器只认「顶层同名文件夹」，文件摆在 zip 根目录它会报
    # No plugin.json found in plugin ZIP
    top = "DeckMeido/"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(plugin / "main.py", top + "main.py")
        z.write(plugin / "plugin.json", top + "plugin.json")
        # package.json 是 Decky 加载插件时读的第一个文件（取 name / version），
        # 漏了它插件不会报错，只是**整个不出来**，面板里什么都看不到。
        # 但 "type": "module" 必须去掉：带着它，Decky 前端把 dist/index.js
        # 当 CJS 解析，顶层 export 直接抛 Unexpected token 'export'。
        # deploy.sh 复制部署时目录里没有 package.json，所以从没踩到过
        pkg = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
        pkg.pop("type", None)
        z.writestr(top + "package.json",
                   json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")
        for name in ("README.md", "LICENSE"):
            p = plugin / name
            if p.is_file():
                z.write(p, top + name)
        # py_modules 是插件后端的 Python 包（已安装的插件在这里放自身模块，
        # Decky 会把插件目录加进 sys.path），目录要连内容一起打进 zip
        pym = plugin / "py_modules"
        if pym.is_dir():
            for q in sorted(pym.rglob("*")):
                if q.is_file() and "__pycache__" not in q.parts:
                    z.write(q, top + q.relative_to(plugin).as_posix())
        for p in sorted((plugin / "dist").rglob("*")):
            if p.is_file():
                z.write(p, top + "dist/" + p.relative_to(plugin / "dist").as_posix())
    return out


def write_meta(ver: str, tarball: Path):
    name = tarball.name
    s = sha256(tarball)
    (DIST / f"{name}.sha256").write_text(f"{s}  {name}\n", encoding="utf-8", newline="\n")
    (DIST / "SHA256SUMS").write_text(f"{s}  {name}\n", encoding="utf-8", newline="\n")
    notes = subprocess.run(
        ["git", "log", "-n", "20", "--no-merges", "--pretty=- %s"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    data = {
        "tag": ver,
        "name": f"DeckMeido {ver}",
        "notes": f"# DeckMeido {ver}\n\n" + notes,
        "published_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": {"tarball": name, "sha256": s},
    }
    (DIST / "latest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def main():
    ver = sys.argv[1] if len(sys.argv) > 1 else json.loads(
        (ROOT / "plugin" / "package.json").read_text(encoding="utf-8")
    )["version"]
    ver = ver.lstrip("v")
    if not (ROOT / "plugin" / "dist" / "index.js").exists():
        sys.exit("plugin/dist/index.js 不存在，先 cd plugin && npm run build")

    print(f"==> 打包 {ver}")
    tar = build_tarball(ver)
    z = build_plugin_zip(ver)
    write_meta(ver, tar)
    (DIST / f"{z.name}.sha256").write_text(
        f"{sha256(z)}  {z.name}\n", encoding="utf-8", newline="\n"
    )
    shutil.copy2(ROOT / "install.sh", DIST / "install.sh")
    print(f"    {tar.name} {tar.stat().st_size // 1024} KB")
    print(f"    {z.name} {z.stat().st_size // 1024} KB")

    # 发之前先查一遍包里有没有账号密码，命中就不上传
    print("==> 凭据检查")
    audit = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "audit_release.py"), ver],
        cwd=ROOT, text=True, encoding="utf-8",
    )
    if audit.returncode != 0:
        sys.exit("发布包里有疑似凭据，已中止上传（见上面的列表）")

    print("==> 打包完成")
    print(f"    {tar}")
    print(f"    {z}")
    print("    产物在 dist/，官网镜像站走 tools/deploy_all.py 上传")

    print("==> 核对线上版本")
    try:
        import urllib.request
        with urllib.request.urlopen(
            "https://meido.meagames.cn/deckmeido/latest.json?t=" + str(int(datetime.now().timestamp())),
            timeout=20,
        ) as r:
            tag = json.loads(r.read().decode("utf-8")).get("tag")
        print(f"    线上 tag = {tag}" + ("  OK" if tag == ver else "  ← 不一致，检查 CDN 缓存"))
    except Exception as exc:
        print(f"    核对失败（不影响发布）：{exc}")


if __name__ == "__main__":
    main()
