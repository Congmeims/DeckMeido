"""多组 API 配置（接口地址 + Key）的读写、迁移与切换。

配置形态（config.toml 的 [llm] 段）::

    current_api = "default"          # 当前使用的那一组（稳定 id，不是名称）
    apis = [
      { id = "default", name = "默认", base_url = "https://api.deepseek.com", api_key = "sk-..." },
      { id = "api-1a2b3c4d", name = "备用中转", base_url = "https://...", api_key = "sk-..." },
    ]

为什么用 id 而不是名称：用量统计按 id 归属，改名不会把历史拆成两段。
id 一旦生成不再变化；名称只是给人看的标签。

旧版只有单组 `base_url` / `api_key`：读取时自动折成一条 id="default"
的记录，并把旧键清空写回，保证同一份 Key 只存在一个来源（见 load）。
如果没有任何配置，返回空列表 —— 界面显示「未配置」，对话给出明确提示。

Key 只在服务端明文保存；对外（接口响应 / 日志 / 界面）一律走 mask。
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from . import devlog
from .tomlcfg import write_config

LEGACY_ID = "default"
DEFAULT_NAME = "默认"
MAX_PROFILES = 20
_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
# 前端回传的展示值（sk-7sw…54D5）：出现省略号就说明不是真 Key
_MASK_MARKS = ("…", "...")


def mask_key(key: str) -> str:
    """Key → sk-7sw…54D5；空串保持空串"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


# 用户常把完整端点粘进「接口地址」：https://api.deepseek.com/chat/completions。
# 保存后代码还会再拼一段 /chat/completions，变成 .../chat/completions/chat/completions
# → 404。这里剥掉误粘的端点后缀；/v1 之类正常路径段保留（OpenAI 兼容接口需要它）。
_ENDPOINT_SUFFIX_RE = re.compile(r"/(?:chat/completions|completions)$", re.I)


def norm_base(url: str) -> str:
    """接口地址去空白/尾斜杠，并剥掉误粘的端点后缀（可重复出现）"""
    out = (url or "").strip().rstrip("/")
    while True:
        trimmed = _ENDPOINT_SUFFIX_RE.sub("", out).rstrip("/")
        if trimmed == out:
            break
        out = trimmed
    return out


def _new_id(profiles: list[dict]) -> str:
    used = {p["id"] for p in profiles}
    for _ in range(64):
        candidate = "api-" + secrets.token_hex(4)
        if candidate not in used:
            return candidate
    # 理论上到不了这里；真撞满了就退化成序号，至少还是唯一的
    n = len(used) + 1
    while f"api-{n}" in used:
        n += 1
    return f"api-{n}"


def _clean(raw: dict, id_hint: str = "") -> dict:
    """一条配置 → 固定字段；缺名称给默认，地址去尾斜杠"""
    pid = str(raw.get("id") or id_hint or "").strip()
    if not _ID_RE.match(pid):
        pid = ""
    name = str(raw.get("name") or "").strip()[:40] or DEFAULT_NAME
    return {
        "id": pid,
        "name": name,
        "base_url": norm_base(str(raw.get("base_url") or "")),
        "api_key": str(raw.get("api_key") or "").strip(),
    }


def normalize(raw) -> list[dict]:
    """配置文件里的 apis 数组 → 干净的列表（丢弃坏条目、补 id、去重）"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    used: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        p = _clean(item)
        if not p["id"] or p["id"] in used:
            p["id"] = _new_id(out)
        used.add(p["id"])
        out.append(p)
        if len(out) >= MAX_PROFILES:
            break
    return out


def from_config(cfg: dict) -> dict:
    """只读解析：[llm] → {profiles, current, migrated}

    migrated=True 表示命中了旧版单组配置，需要调用方决定是否落盘（load 会写）。
    """
    llm = (cfg or {}).get("llm") or {}
    profiles = normalize(llm.get("apis"))
    migrated = False
    if not profiles:
        base = norm_base(str(llm.get("base_url") or ""))
        key = str(llm.get("api_key") or "").strip()
        if base or key:
            profiles = [{"id": LEGACY_ID, "name": DEFAULT_NAME,
                         "base_url": base, "api_key": key}]
            migrated = True
    ids = [p["id"] for p in profiles]
    current = str(llm.get("current_api") or "").strip()
    if not ids:
        current = ""
    elif current not in ids:
        current = ids[0]
    return {"profiles": profiles, "current": current, "migrated": migrated}


def active(state: dict) -> dict | None:
    """当前使用的那条；没有任何配置时返回 None"""
    profiles = (state or {}).get("profiles") or []
    current = (state or {}).get("current") or ""
    for p in profiles:
        if p["id"] == current:
            return p
    return profiles[0] if profiles else None


def select(state: dict, profile_id: str) -> dict:
    """切换当前使用；id 不存在时保持原值"""
    ids = {p["id"] for p in state.get("profiles") or []}
    if profile_id in ids:
        return {"profiles": state["profiles"], "current": profile_id}
    return {"profiles": state.get("profiles") or [], "current": state.get("current") or ""}


def to_updates(state: dict) -> dict:
    """状态 → 写回 [llm] 的键值

    同时把旧版 base_url / api_key 写成空串：迁移之后 Key 只保留在 apis 里，
    否则同一把 Key 会以两个来源存在，读的时候又得猜哪个为准。
    """
    return {
        "apis": list(state.get("profiles") or []),
        "current_api": state.get("current") or "",
        "base_url": "",
        "api_key": "",
    }


def _persist(path: Path, state: dict) -> bool:
    """把迁移/更新后的状态写回 config.toml；失败只记日志，不挡住启动"""
    try:
        write_config(path, {"llm": to_updates(state)})
        return True
    except Exception as exc:  # 配置只读 / 磁盘满 / 并发被改坏
        devlog.log("apicfg.write_failed", error=repr(exc))
        return False


def load(cfg: dict, path: Path | None = None) -> dict:
    """读取配置并在需要时完成旧版迁移（会原子写回 config.toml）

    迁移是幂等的：写回后 apis 存在、旧键为空，下次读取不会再造第二条。
    写盘失败不影响本次运行，内存里仍然只有一条记录（不会重复计费/重复显示）。
    """
    state = from_config(cfg)
    if state["migrated"] and path is not None:
        if _persist(path, state):
            llm = cfg.setdefault("llm", {})
            llm["apis"] = state["profiles"]
            llm["current_api"] = state["current"]
            llm["base_url"] = ""
            llm["api_key"] = ""
            devlog.log("apicfg.migrated", profiles=len(state["profiles"]),
                       current=state["current"])
    return state


def merge_update(current: dict, incoming: list[dict],
                 selected: str | None = None) -> dict:
    """把界面提交的完整列表合并进现有状态（新增 / 重命名 / 删除 / 改地址）

    Key 的三条「不覆盖」规则与单组时代的保存接口一致：
    - 新条目留空 → 不填 Key（界面随后显示「未配置」）
    - 已有条目留空 或 回传打码串 → 保持原 Key
    删除当前使用的条目时自动回退到剩余第一条；全删光则 current 为空。
    """
    old = {p["id"]: p for p in (current.get("profiles") or [])}
    out: list[dict] = []
    used: set[str] = set()
    for raw in incoming or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "").strip()
        prev = old.get(pid)
        p = _clean(raw, id_hint=pid if prev else "")
        if not p["id"] or p["id"] in used:
            # 新条目由服务端发 id：前端拿到后刷新即可，之后的改名/改地址都靠它
            p["id"] = _new_id(out)
        incoming_key = str(raw.get("api_key") or "").strip()
        if prev and (not incoming_key
                     or any(mark in incoming_key for mark in _MASK_MARKS)):
            p["api_key"] = prev["api_key"]
        used.add(p["id"])
        out.append(p)
        if len(out) >= MAX_PROFILES:
            break
    ids = [p["id"] for p in out]
    if selected is None:
        selected = current.get("current") or ""
    if not ids:
        selected = ""
    elif selected not in ids:
        selected = ids[0]
    return {"profiles": out, "current": selected}


def public(state: dict) -> list[dict]:
    """给界面的列表：Key 只给掩码与「是否已配置」，绝不下发明文"""
    current = (state or {}).get("current") or ""
    rows = []
    for p in (state or {}).get("profiles") or []:
        masked = mask_key(p.get("api_key") or "")
        rows.append({
            "id": p["id"],
            "name": p["name"],
            "base_url": p["base_url"],
            "api_key": masked,
            "api_key_masked": masked,
            "api_key_set": bool(p.get("api_key")),
            "current": p["id"] == current,
        })
    return rows
