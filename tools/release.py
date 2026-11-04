#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地发版构建：打 tar.gz + 插件 zip + latest.json，不在任何地方上传。

用法（仓库根目录）：
    python tools/release.py 0.6.6
不带版本号时读 plugin/package.json 的 version。

产物只写到 dist/。发版剩下的步骤是人工的：
  1. 把 dist/ 里的产物同步进 winres/deckmeido/ 并提交；
  2. 用 gh release 把 tar.gz / 插件 zip / sha256 / latest.json / install.sh
     作为资产传到 GitHub。
"""
import hashlib
import json
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
    "server/static/shots", "plugin/node_modules",
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
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
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
        def put(src: Path, arc: str):
            # 工作区在 Windows/NTFS 上时源文件可能是 0666 之类，z.write 会把这份权限
            # 一起带进 zip，解到 Deck 上就成了 world-writable。统一按 0644 写
            zi = zipfile.ZipInfo.from_file(src, arc)
            zi.external_attr = (0o644 | 0o100000) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, src.read_bytes())

        put(plugin / "main.py", top + "main.py")
        put(plugin / "plugin.json", top + "plugin.json")
        # package.json 是 Decky 加载插件时读的第一个文件（取 name / version），
        # 漏了它插件不会报错，只是**整个不出来**，面板里什么都看不到。
        # 里面必须原样保留 "type": "module" —— Decky 读这个字段决定用哪种方式
        # 加载前端产物。删掉它，Decky 会按 CJS 去 eval 产物，顶层的
        # `export { ... }` 立刻变成 SyntaxError: Unexpected token 'export'。
        # 别处的插件包都是 type=module，这是唯一正确的形态。
        pkg = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
        pkg["main"] = "dist/index.js"
        zi = zipfile.ZipInfo(top + "package.json")
        zi.external_attr = (0o644 | 0o100000) << 16
        zi.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(zi, json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")
        for name in ("README.md", "LICENSE"):
            p = plugin / name
            if p.is_file():
                put(p, top + name)
        # py_modules 是插件后端的 Python 包（已安装的插件在这里放自身模块，
        # Decky 会把插件目录加进 sys.path），目录要连内容一起打进 zip
        pym = plugin / "py_modules"
        if pym.is_dir():
            for q in sorted(pym.rglob("*")):
                if q.is_file() and "__pycache__" not in q.parts:
                    put(q, top + q.relative_to(plugin).as_posix())
        for p in sorted((plugin / "dist").rglob("*")):
            if p.is_file():
                put(p, top + "dist/" + p.relative_to(plugin / "dist").as_posix())
    return out


def verify_plugin_zip(zip_path: Path) -> None:
    """发版前逐条校验插件包，任何一条不合规就直接中止。

    ⚠️ 这里校验的每一条都是真实踩过的坑，别删、别放宽：
    - package.json 必须带 "type": "module"。Decky 读它决定前端产物按 ESM
      还是 CJS 处理。曾经为了「避开顶层 export」把它删掉，结果插件加载报
      `SyntaxError: Unexpected token 'export'`，改 dist 产物和 main 都没用。
    - main 必须指向 dist/index.js。
    - dist/index.js 必须存在且带 default export，否则 Decky 报「插件没有导出默认组件」。
    - 包里不能残留 dist/legacy-iife.js 之类的历史产物。
    """
    problems = []
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        # 1. 顶层必须是 DeckMeido/，否则 Decky 装不上
        if "DeckMeido/plugin.json" not in names:
            problems.append("zip 顶层缺少 DeckMeido/plugin.json（Decky 要求顶层同名文件夹）")
        raw = z.read("DeckMeido/package.json") if "DeckMeido/package.json" in names else None
        if raw is None:
            problems.append("zip 里缺 DeckMeido/package.json（Decky 靠它取 name/version）")
        else:
            pkg = json.loads(raw.decode("utf-8"))
            if pkg.get("type") != "module":
                problems.append(
                    'package.json 的 "type" 不是 "module"'
                    "（Decky 会按 CJS 加载 → Unexpected token 'export'）"
                )
            if pkg.get("main") != "dist/index.js":
                problems.append(f'package.json 的 main 是 {pkg.get("main")!r}，应为 "dist/index.js"')
        # 2. 前端产物
        entry = "DeckMeido/dist/index.js"
        if entry not in names:
            problems.append("zip 里缺 DeckMeido/dist/index.js")
        else:
            code = z.read(entry).decode("utf-8", "replace")
            if " as default" not in code:
                problems.append("dist/index.js 没有 default export（Decky 会拿不到组件）")
        # 3. 历史遗留产物不该再出现
        for n in sorted(names):
            if "legacy-iife" in n:
                problems.append(f"zip 里残留历史产物 {n}（应删除）")
    if problems:
        raise SystemExit(
            "插件包校验未通过，已中止打包：\n" + "\n".join("  - " + p for p in problems)
        )


def write_meta(ver: str, tarball: Path):
    name = tarball.name
    s = sha256(tarball)
    (DIST / f"{name}.sha256").write_text(f"{s}  {name}\n", encoding="utf-8", newline="\n")
    (DIST / "SHA256SUMS").write_text(f"{s}  {name}\n", encoding="utf-8", newline="\n")
    notes = subprocess.run(
        ["git", "log", "-n", "20", "--no-merges", "--pretty=- %s"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
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
    z = build_plugin_zip(ver)
    # 先校验再往下走：校验没过就退出，错误的包不会落到 deploy/ 和 tarball 里
    verify_plugin_zip(z)
    print("    插件包参数校验通过（type=module / main=dist/index.js / default export）")
    # 插件 zip 也要放进 install.sh 找的那个位置（deploy/deckmeido-plugin.zip）：
    # 以前只落在 dist/ 根下，install.sh 永远找不到它，「装插件」整段被跳过，
    # 用户只能自己去 Decky 里装 —— 这是插件装不上/装成半截的主要来源。
    # 必须在 build_tarball 之前放好，发布包才会带上它
    shutil.copy2(z, ROOT / "deploy" / "deckmeido-plugin.zip")
    tar = build_tarball(ver)
    write_meta(ver, tar)
    (DIST / f"{z.name}.sha256").write_text(
        f"{sha256(z)}  {z.name}\n", encoding="utf-8", newline="\n"
    )
    shutil.copy2(ROOT / "install.sh", DIST / "install.sh")
    print(f"    {tar.name} {tar.stat().st_size // 1024} KB")
    print(f"    {z.name} {z.stat().st_size // 1024} KB")

    # 出包前先查一遍包里有没有账号密码，命中就不出包
    print("==> 凭据检查")
    audit = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "audit_release.py"), ver],
        cwd=ROOT, text=True, encoding="utf-8", errors="replace",
    )
    if audit.returncode != 0:
        sys.exit("发布包里有疑似凭据，已中止本次发布（见上面的列表）")

    print("==> 打包完成")
    print(f"    {tar}")
    print(f"    {z}")
    print("    产物只在 dist/，没有上传步骤")
    print("    下一步：同步 dist/ 到 winres/deckmeido/ 并提交，再用 gh release 上传资产")


if __name__ == "__main__":
    main()
