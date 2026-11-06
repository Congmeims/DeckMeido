"""文件管理器三级分区（唯一事实来源）。

前端不自己写正则，只认 /api/browse 返回的 zone / readonly 字段；所有写接口
也走这里的同一套判定，避免「前端禁了按钮、后端还能写」的割裂。

分区规则：
  工作区  : 配置的工作区 + 安装目录（Deck 上默认 /home/deck/DeckMeido）
            → 无标识，正常读写
  谨慎区  : /home/deck/**（工作区除外）、/run/media/deck/**（SD/U 盘）
            → 黄色「谨慎操作」，默认可读写；挂载点本身是 ro（写保护卡）时按只读
  危险区  : 其余全部路径（/、/etc、/usr、/var、/boot、/opt、/srv、/lib*、
            /home 下其他用户目录…）→ 红色「危险区域 · 只读」，写操作一律 403
  禁浏览  : /proc、/sys、/dev → 直接拒绝进入（这不等于只读，是根本不给看）

判定前统一走 realpath：/home/deck/link -> /etc 会按 /etc（危险区）算，
`..`、重复斜杠、尾部斜杠、`.` 也在同一步规范化掉。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ZONE_WORKSPACE = "workspace"
ZONE_CAUTION = "caution"
ZONE_DANGER = "danger"
ZONE_FORBIDDEN = "forbidden"

ZONE_LABELS = {
    ZONE_WORKSPACE: "",
    ZONE_CAUTION: "谨慎操作",
    ZONE_DANGER: "危险区域 · 只读",
    ZONE_FORBIDDEN: "禁止浏览",
}

# 机器可读的拒绝原因，前端/日志按这个分支，不要去匹配中文文案
REASON_READ_ONLY_ZONE = "read_only_zone"     # 危险区
REASON_READ_ONLY_MOUNT = "read_only_mount"   # 谨慎区但挂载点 ro
REASON_FORBIDDEN_ZONE = "forbidden_zone"     # /proc、/sys、/dev

DEFAULT_WORKSPACE_ROOTS = ("/home/deck/DeckMeido",)
CAUTION_ROOTS = ("/home/deck", "/run/media/deck")
FORBIDDEN_ROOTS = ("/proc", "/sys", "/dev")

# 测试/特殊平台可以替换掉：canonical 只经这一个入口读 realpath
_realpath = os.path.realpath

_OCTAL_ESC = re.compile(r"\\([0-7]{3})")


def _is_windows() -> bool:
    return os.name == "nt"


def _squeeze(p: str) -> str:
    """压掉重复斜杠 / `.` / 尾部斜杠；`..` 在 realpath 之后理论上不该再出现，
    这里仍按栈回退一次，防止调用方传进来没走过 realpath 的路径。"""
    parts = [s for s in p.split("/") if s and s != "."]
    out: list[str] = []
    for s in parts:
        if s == "..":
            if out and out[-1] != "..":
                out.pop()
            continue
        out.append(s)
    lead = "/" if p.startswith("/") else ""
    body = "/".join(out)
    if lead:
        return lead + body if body else "/"
    return body or "."


def canonical(raw) -> str:
    """展开 ~、解析软链接、规范化成绝对路径（统一用 / 分隔，不带尾部斜杠）。

    realpath 同时把 `.`、`..`、重复斜杠处理掉；Windows 上返回的 `M:\\x`
    统一转成 `M:/x`，让同一份规则在开发机和 Deck 上都能跑。
    """
    text = os.fspath(raw) if not isinstance(raw, str) else raw
    text = os.path.expanduser(text or "")
    try:
        real = _realpath(text)
    except (OSError, ValueError):
        real = os.path.normpath(text)
    if _is_windows():
        real = real.replace("\\", "/")
    return _squeeze(real)


def _key(path: str) -> str:
    """比较用的键：Linux 区分大小写，Windows 盘符/路径大小写不敏感。"""
    return path.lower() if _is_windows() else path


def _under(path: str, root: str) -> bool:
    """路径是否在 root 之下（含 root 本身），不会把 /home/deckx 当成 /home/deck。"""
    pk, rk = _key(path), _key(root)
    if rk in ("", ".", "/"):
        return True
    if pk == rk:
        return True
    return pk.startswith(rk.rstrip("/") + "/")


def _unescape_mount(s: str) -> str:
    """mountinfo 里的空格/制表符等写成 \\040 这类八进制转义。"""
    return _OCTAL_ESC.sub(lambda m: chr(int(m.group(1), 8)), s)


def _linux_mount_readonly(path: str) -> bool:
    """按 /proc/self/mountinfo 判最内层挂载是否是 ro（写保护 SD/U 盘）。

    非 Linux（开发机）直接返回 False；拿不到 mountinfo 也按可写处理。
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    target = _key(path)
    best = ""
    best_ro = False
    for line in text.splitlines():
        left = line.split(" - ", 1)[0]
        fields = left.split()
        if len(fields) < 6:
            continue
        mount_point = _key(canonical(_unescape_mount(fields[4])))
        if not _under(target, mount_point):
            continue
        # 取最长的挂载点：/run/media/deck 和它下面某张卡各自的挂载项要分开算
        if len(mount_point) > len(best):
            best = mount_point
            best_ro = "ro" in fields[5].split(",")
    return bool(best) and best_ro


def zone_info(path, workspace_roots=None) -> dict:
    """返回该路径的分区信息（前端胶囊、写接口拦截都用这一份）。

    workspace_roots 传配置的工作区（可多个）；不传用 Deck 默认安装目录。
    """
    canon = canonical(path)
    ws_roots = [canonical(p) for p in (workspace_roots or DEFAULT_WORKSPACE_ROOTS)]

    for root in FORBIDDEN_ROOTS:
        if _under(canon, canonical(root)):
            return {"path": canon, "zone": ZONE_FORBIDDEN, "readonly": True,
                    "reason": REASON_FORBIDDEN_ZONE,
                    "label": ZONE_LABELS[ZONE_FORBIDDEN]}

    if any(_under(canon, root) for root in ws_roots):
        return {"path": canon, "zone": ZONE_WORKSPACE, "readonly": False,
                "reason": "", "label": ZONE_LABELS[ZONE_WORKSPACE]}

    for root in CAUTION_ROOTS:
        if _under(canon, canonical(root)):
            ro = _linux_mount_readonly(canon)
            return {"path": canon, "zone": ZONE_CAUTION, "readonly": ro,
                    "reason": REASON_READ_ONLY_MOUNT if ro else "",
                    "label": ZONE_LABELS[ZONE_CAUTION]}

    # 没被工作区/谨慎区认领的一律按危险区：宁可多锁，不能漏放
    return {"path": canon, "zone": ZONE_DANGER, "readonly": True,
            "reason": REASON_READ_ONLY_ZONE,
            "label": ZONE_LABELS[ZONE_DANGER]}


def write_block(path, workspace_roots=None) -> str:
    """写操作是否被拒；返回机器可读原因字符串，可写返回空串。"""
    info = zone_info(path, workspace_roots)
    if info["zone"] == ZONE_FORBIDDEN:
        return REASON_FORBIDDEN_ZONE
    if info["readonly"]:
        return info["reason"] or REASON_READ_ONLY_ZONE
    return ""


def browse_denied(path, workspace_roots=None) -> bool:
    """/proc、/sys、/dev 拒绝浏览（不是只读）。"""
    return zone_info(path, workspace_roots)["zone"] == ZONE_FORBIDDEN
