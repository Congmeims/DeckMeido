#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描发布产物，确认里面没有账号密码、密钥这类不该外发的东西。

用法（仓库根目录）：
    python tools/audit_release.py            # 扫 dist/ 里现有的 tar.gz 和插件 zip
    python tools/audit_release.py --src      # 同时扫工作区里会被打进包的文件

命中即列出 文件:行号 和打码后的内容，有命中时退出码为 1。
"""
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 真实凭据的特征：SSH 密码、私钥、常见服务商的 key、网站令牌
PATTERNS = [
    # 形如 password = "xxx" / PASS = 'xxx'，要求值里不像环境变量名或占位符
    ("SSH 密码", re.compile(
        r"(?i)\b(password|passwd|pass)\b\s*[=:]\s*['\"]([^'\"\s]{4,})['\"]"
    )),
    ("私钥", re.compile(r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("API Key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # 服务器地址：公开仓库里不该出现具体 IP 和 SSH 端口
    ("服务器地址", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?![.\d])")),
    ("密码字段", re.compile(r"(api_key|secret|token)\s*=\s*\"(?![\"\s]*\")[^\"]{16,}\"")),
]
# 允许出现的占位值
ALLOW = {"", "meido", "your-token", "<token>", "changeme", "xxx"}
TEXT_EXT = {
    ".py", ".sh", ".ts", ".tsx", ".js", ".json", ".toml", ".md", ".txt",
    ".html", ".css", ".cfg", ".ini", ".yml", ".yaml",
}


def mask(line: str) -> str:
    line = line.strip()
    return line[:60] + ("…" if len(line) > 60 else "")


def looks_like_secret(val: str) -> bool:
    """排除「值其实是环境变量名 / 占位符」的情况，比如 PASS = "DEPLOY_PASS" """
    if val in ALLOW:
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", val):  # 全大写下划线 = 环境变量名
        return False
    if val.lower() in {"none", "null", "true", "false", "password", "passwd"}:
        return False
    return True


def is_doc_ip(val: str) -> bool:
    """示例/回环/私有网段保留地址不算泄露（文档里常写 127.0.0.1、192.168.x.x）"""
    try:
        a, b, _c, _d = (int(x) for x in val.split("."))
    except ValueError:
        return False
    return (
        a in (0, 127, 10) or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)
        or a >= 224 or val in ("255.255.255.255",)
    )


def noise(line: str) -> bool:
    """明显不是服务器地址的行：图标 SVG 的坐标、公开 DNS、代理 fake-ip 说明"""
    return any(k in line for k in ('"attr"', "viewBox", "8.8.8.8", "198.18.", "fake-ip"))


def scan_text(name: str, text: str) -> list:
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for label, pat in PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            val = m.group(2) if label == "SSH 密码" and m.lastindex and m.lastindex >= 2 else m.group(0)
            if label == "SSH 密码" and not looks_like_secret(val or ""):
                continue
            if label == "服务器地址" and (is_doc_ip(m.group(0)) or noise(line)):
                continue
            hits.append((name, i, label, mask(line)))
    return hits


def scan_tar(path: Path) -> list:
    hits = []
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if not m.isfile() or Path(m.name).suffix.lower() not in TEXT_EXT:
                continue
            f = tf.extractfile(m)
            if not f:
                continue
            try:
                text = f.read().decode("utf-8", "ignore")
            except Exception:
                continue
            hits += scan_text(f"{path.name}:{m.name}", text)
    return hits


def scan_zip(path: Path) -> list:
    hits = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if Path(n).suffix.lower() not in TEXT_EXT:
                continue
            try:
                text = z.read(n).decode("utf-8", "ignore")
            except Exception:
                continue
            hits += scan_text(f"{path.name}:{n}", text)
    return hits


def scan_src() -> list:
    """扫工作区：只看会进包的东西（tools/ 会进包，所以要一起查）"""
    hits = []
    skip = {".git", "node_modules", "__pycache__", "dist", ".venv", ".uploads"}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in TEXT_EXT:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            hits += scan_text(str(p.relative_to(ROOT)), text)
    return hits


def main():
    # 指定版本号时只查这个版本的产物；否则查 dist 里最新的两组
    ver = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
    if ver:
        ver = ver.lstrip("v")
        targets = [DIST / f"deckmeido-{ver}.tar.gz", DIST / f"deckmeido-plugin-{ver}.zip"]
        targets = [p for p in targets if p.exists()]
    else:
        targets = sorted(DIST.glob("deckmeido-*.tar.gz"))[-2:] + sorted(
            DIST.glob("deckmeido-plugin-*.zip"))[-2:]
    hits = []
    for p in targets:
        print(f"扫描 {p.name}")
        hits += scan_tar(p) if p.suffix == ".gz" else scan_zip(p)
    if "--src" in sys.argv:
        print("扫描工作区源文件")
        hits += scan_src()

    if not hits:
        print("\n未发现凭据。")
        return 0
    print(f"\n发现 {len(hits)} 处可疑内容：")
    for name, line, label, snippet in hits:
        print(f"  [{label}] {name}:{line}\n      {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
