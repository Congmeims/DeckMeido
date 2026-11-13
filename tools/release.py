#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地发版构建：打 tar.gz + 插件 zip + latest.json。

用法（仓库根目录）：
    python tools/release.py            # 版本号读 plugin/package.json
    python tools/release.py 0.8.6      # 显式指定版本
    python tools/release.py --no-check 跳过安装器自检（不建议）
    python tools/release.py --no-push  出包后只提交、不推送（不建议）

产物写到 dist/，并自动同步到 winres/deckmeido/（该目录只保留当前版本产物）。
打包完成后脚本自己把产物提交并推送到远端：出包必须和云端一致，
否则别人按 tag 拉下来的仍是旧代码，装机日志里的 COMMIT 也对不上。
推送失败会直接报错退出，不会留下「本地有包、云端没有」的状态。
最后一步仍是人工的：用 gh release 把 tar.gz / 插件 zip / sha256 /
latest.json / install.sh 作为资产传到 GitHub。
"""
import hashlib
import json
import re
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
WINRES = ROOT / "winres" / "deckmeido"

ITEMS = [
    "server", "client", "assets", "deploy", "plugin", "vendor",
    "requirements.txt", "requirements-gui.txt", "requirements-stt.txt",
    "requirements-tts.txt",
    "README.md", "LICENSE", "install.sh",
]
# tools/ 是仓库自用的运维脚本（连服务器的、打包的），不进发布包。
# cli/ 里要是有给用户用的工具，单独列在这里
EXTRA_DIRS = ["cli"]
# 不进发布包的运行产物
SKIP_ANY = {
    "server/config.toml", "server/sessions", "server/uploads",
    "server/static/shots", "plugin/node_modules",
}
# 出包产物：打包前要放过（它们会被本次出包覆盖），打包后要自动提交。
# dist/ 在 .gitignore 里不参与，真正入库的是 winres/deckmeido（只留当前版本）
RELEASE_PATHS = ["winres/deckmeido", "plugin/package.json",
                 "plugin/package-lock.json", "deploy/deckmeido-plugin.zip"]


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

    (stage / "VERSION").write_text(ver + "\n", encoding="utf-8", newline="\n")
    # 构建标记：发布包也带上当时的短提交号，解包安装（没有 .git）时
    # 后端才能把 version_display 显示成 0.7.5-xxxxxxx
    try:
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    if commit:
        (stage / "COMMIT").write_text(commit + "\n", encoding="utf-8", newline="\n")
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

    def _norm_mode(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        # 源码在 Windows/NTFS 上时权限位是 0666/0644，tar 会原样带进包里；
        # 解到 Deck 上后 client/launch.sh 这类脚本就没了可执行位，桌面快捷方式
        # 点开报「缺少运行权限」。打包时统一：目录 0755、脚本 0755、其余 0644。
        if ti.isdir():
            ti.mode = 0o755
        elif ti.name.endswith((".sh", ".desktop")):
            ti.mode = 0o755
        else:
            ti.mode = 0o644
        return ti

    with tarfile.open(out, "w:gz") as tf:
        tf.add(stage, arcname=name, filter=_norm_mode)
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


def sync_winres(ver: str, tarball: Path, zip_path: Path) -> None:
    """把当前版本产物同步到 winres/deckmeido，并清掉其它版本的旧包。

    约定：这个目录只放最新版本，用户直接从里面取包装机。清理按文件名里的
    版本号判断，绝不按修改时间（避免误删刚复制进来的当前版本）；与版本号
    无关的 install.sh / latest.json / SHA256SUMS 永远保留并覆盖。
    """
    WINRES.mkdir(parents=True, exist_ok=True)
    current = [
        tarball.name, f"{tarball.name}.sha256",
        zip_path.name, f"{zip_path.name}.sha256",
    ]
    pat = re.compile(r"^deckmeido(?:-plugin)?-(\d+(?:\.\d+)*)\.(?:tar\.gz|zip)(?:\.sha256)?$")
    removed = []
    for p in sorted(WINRES.iterdir()):
        if not p.is_file() or p.name in current:
            continue
        m = pat.match(p.name)
        if m and m.group(1) != ver:
            p.unlink()
            removed.append(p.name)
    for name in removed:
        print(f"    清理旧产物 {name}")
    for name in current + ["install.sh", "latest.json", "SHA256SUMS"]:
        src = DIST / name
        if src.exists():
            shutil.copy2(src, WINRES / name)
    print(f"    已同步到 winres/deckmeido（只保留 {ver} 版本产物）")


def check_installer():
    """出包前检查安装器：交互式档位菜单已经在安装器里删掉，只留完整安装。

    以前这份源码里又长回「请选择安装档位」的菜单，打出来的包对谁都弹菜单；
    管道安装时 /dev/tty 不存在，安装直接读失败。这里命中就中止，
    避免带档位菜单的包再次发出去。
    """
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    bad = [
        (r"请选择安装档位", "交互式档位菜单"),
        (r"choose_install_profile", "档位选择函数"),
        (r"DECKMEIDO_PROFILE", "档位环境变量"),
        (r"--minimal|--standard|--custom", "档位命令行参数"),
    ]
    hits = []
    for pattern, label in bad:
        for m in re.finditer(pattern, text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"    install.sh:{line}  {label}：{m.group(0)}")
    if hits:
        sys.exit("安装器里仍有档位相关代码，出包会带着它一起发出去。"
                 "先删干净再出包（或用 --no-check 强行跳过）：\n" + "\n".join(hits))


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在仓库根目录跑一条 git；失败时把 git 的原始输出带出来，不然排障只能靠猜"""
    res = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        sys.exit(f"git {' '.join(args)} 失败：\n{detail}")
    return res


def commit_and_push(ver: str, push: bool = True) -> None:
    """把本次出包的产物提交并推送到远端

    出包和云端必须一致：只提交不推送的话，别人按 tag 拉到的还是旧代码，
    装机日志里的 COMMIT 也对不上。推送失败直接退出，让问题当场暴露。
    """
    print("==> 提交出包产物")
    # 只加产物目录与版本文件，不用 add -A：出包时工作区里可能还有别的手改
    paths = RELEASE_PATHS
    git("add", "-A", *paths)
    staged = git("diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        print("    产物无变化，跳过提交")
    else:
        git("commit", "-m", f"chore(release): {ver}")
        print("    已提交：" + "、".join(staged.splitlines()[:6]))

    if not push:
        print("    已跳过推送（--no-push）；注意云端仍是旧代码")
        return
    print("==> 推送到远端")
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    res = git("push", "origin", branch, check=False)
    if res.returncode != 0:
        print((res.stderr or res.stdout or "").strip())
        sys.exit("推送失败，本次出包中止。修好远端后再推送，"
                 "确认 `git status` 与远端一致后才能打 tag 发版。")
    print(f"    已推送 {branch}")


def main():
    argv = [a for a in sys.argv[1:] if a not in ("--no-check", "--no-push")]
    skip_check = "--no-check" in sys.argv[1:]
    no_push = "--no-push" in sys.argv[1:]
    ver = argv[0] if argv else json.loads(
        (ROOT / "plugin" / "package.json").read_text(encoding="utf-8")
    )["version"]
    ver = ver.lstrip("v")
    # 构建号取自 HEAD：如果版本号是在 HEAD 之后才改的（还没提交），包里就会出现
    # 「VERSION 是新版本、COMMIT 是旧提交」的错位，装机日志里两者对不上，排障时
    # 没法判断装的到底是不是这个版本。这里提前拦下。
    pkg_ver = json.loads((ROOT / "plugin" / "package.json").read_text(encoding="utf-8"))["version"]
    if pkg_ver != ver:
        sys.exit(f"版本不一致：参数 {ver} ≠ plugin/package.json {pkg_ver}")
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    # 产物目录不算「未提交改动」：它们是上一次出包留下的，本来就要被这次
    # 出包覆盖掉再统一提交。把它们算进去的话，第二次出包永远过不了这一关。
    dirt = [
        line for line in dirty.splitlines()
        if not any(p in line for p in RELEASE_PATHS)
    ]
    if dirt:
        sys.exit("工作区有未提交改动，构建号（COMMIT）会和包内容对不上。先提交再出包：\n"
                 + "\n".join("    " + line for line in dirt[:10]))
    if not (ROOT / "plugin" / "dist" / "index.js").exists():
        sys.exit("plugin/dist/index.js 不存在，先 cd plugin && npm run build")

    # 安装器自检放在最前面：不通过就别浪费一次完整打包
    if skip_check:
        print("==> 安装器自检：已用 --no-check 跳过")
    else:
        check_installer()
        print("==> 安装器自检通过（无档位菜单，完整安装）")

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

    print("==> 同步 winres")
    sync_winres(ver, tar, z)

    print("==> 打包完成")
    print(f"    {tar}")
    print(f"    {z}")

    # 产物落盘后再提交推送：云端代码与装机包里的 COMMIT 必须是同一个提交
    commit_and_push(ver, push=not no_push)

    print("    产物已写入 dist/ 与 winres/deckmeido/")
    print("    下一步：打 tag 并用 gh release 上传资产")


if __name__ == "__main__":
    main()
