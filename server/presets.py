"""按游戏保存的提示词预设。

存储是 <数据根>/presets/presets.json 一个 JSON 文件，带 schema 版本号，
以后要迁移字段时只看 version。文件读不到、JSON 坏了、单条记录字段不对，
一律忽略并使用默认提示词，绝不让一份预设把后端启动拖挂。

绑定游戏用两级标识：
  1. Steam appid（首选，最稳）
  2. 游戏名（appid 拿不到的非 Steam 游戏、或老数据只有名字时用）

预设内容和 [agent] status_prompt 是两份数据：套用预设只改运行时生效值，
不会写回 config.toml，退出游戏后自动回到用户原来的全局配置。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1
MAX_PROMPT = 2000
MAX_TITLE = 60
MAX_GAME_NAME = 100

_lock = threading.RLock()

# 当前游戏与命中的预设（进程内状态，不落盘；重启后由插件重新上报）
_active: dict = {
    "appid": "",
    "game_name": "",
    "preset_id": "",
    "title": "",
    "applied": False,
}


def _path() -> Path:
    return paths.data_dir() / "presets" / "presets.json"


def _log(msg: str) -> None:
    print(f"[DeckMeido] {msg}", flush=True)


def _clean_text(value, limit: int) -> str:
    """去掉首尾空白并截断；非字符串一律当空串"""
    text = str(value or "").strip()
    return text[:limit]


def appid_text(value) -> str:
    """appid 统一成字符串；数字对象也接受，空/非法值返回空串"""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    text = str(value or "").strip()
    return text if text.isdigit() and text != "0" else ""


def normalize_name(value) -> str:
    """游戏名归一：全半角统一、大小写不敏感、连续空白压成一个"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"\s+", " ", text)


def _clean_item(raw) -> dict | None:
    """整理一条预设记录；关键字段全空/类型不对就丢掉"""
    if not isinstance(raw, dict):
        return None
    prompt = _clean_text(raw.get("prompt"), MAX_PROMPT)
    game_name = _clean_text(raw.get("game_name"), MAX_GAME_NAME)
    appid = appid_text(raw.get("appid"))
    if not prompt or not (appid or game_name):
        return None
    title = _clean_text(raw.get("title"), MAX_TITLE) or game_name or f"appid {appid}"
    created = raw.get("created")
    updated = raw.get("updated")
    return {
        "id": _clean_text(raw.get("id"), 40) or _new_id(),
        "appid": appid,
        "game_name": game_name,
        "title": title,
        "prompt": prompt,
        "created": float(created) if isinstance(created, (int, float)) else time.time(),
        "updated": float(updated) if isinstance(updated, (int, float)) else time.time(),
    }


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def load() -> list[dict]:
    """读全部预设；文件不存在/损坏时返回空列表并留一条日志"""
    path = _path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        _log(f"提示词预设文件损坏，已忽略并回退默认：{path}（{exc}）")
        return []
    if not isinstance(data, dict):
        _log(f"提示词预设文件格式不对，已忽略并回退默认：{path}")
        return []
    version = data.get("version")
    if version is not None and str(version) != str(SCHEMA_VERSION):
        # 未来版本的字段可能不兼容：能读几条读几条，读不懂的跳过
        _log(f"提示词预设 schema 版本 {version} 与当前 {SCHEMA_VERSION} 不同，按兼容模式读取")
    raw_items = data.get("presets")
    if not isinstance(raw_items, list):
        return []
    out: list[dict] = []
    for raw in raw_items:
        item = _clean_item(raw)
        if item:
            out.append(item)
        else:
            _log(f"提示词预设里有一条记录内容为空或字段不全，已跳过：{path}")
    return out


def _write(items: list[dict]) -> None:
    """原子写整份预设文件"""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SCHEMA_VERSION, "presets": items}
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def find(items: list[dict], appid: str = "", game_name: str = "") -> dict | None:
    """按 appid 优先、游戏名兜底的顺序找预设

    当前游戏没有 appid 时只能按名字找；有 appid 但这条是老数据（只有名字）
    时也允许按名字命中，否则升级后旧的预设会突然失效。已经绑定了别的 appid
    的同名预设不会被抢过来——同名的两款游戏各算各的。
    """
    aid = appid_text(appid)
    name = normalize_name(game_name)
    if aid:
        for item in items:
            if item.get("appid") == aid:
                return item
    if name:
        for item in items:
            if item.get("appid"):
                continue
            if normalize_name(item.get("game_name")) == name:
                return item
    return None


def list_presets() -> list[dict]:
    """给界面的列表：不含运行时状态，按更新时间倒序"""
    items = load()
    items.sort(key=lambda x: float(x.get("updated") or 0), reverse=True)
    return items


def _maybe_bind(item: dict, appid: str, game_name: str) -> bool:
    """命中老数据时把新拿到的 appid/游戏名补上，返回是否改动"""
    changed = False
    aid = appid_text(appid)
    if aid and not item.get("appid"):
        item["appid"] = aid
        changed = True
    name = _clean_text(game_name, MAX_GAME_NAME)
    if name and not item.get("game_name"):
        item["game_name"] = name
        changed = True
    return changed


def upsert(appid, game_name, prompt, title=None) -> dict:
    """按游戏保存（已有就更新内容），返回落盘后的记录

    prompt 为空视为无效输入，抛 ValueError，由接口层转成 400。
    """
    aid = appid_text(appid)
    name = _clean_text(game_name, MAX_GAME_NAME)
    text = _clean_text(prompt, MAX_PROMPT)
    if not (aid or name):
        raise ValueError("缺少游戏标识（appid 或游戏名）")
    if not text:
        raise ValueError("提示词不能为空")
    with _lock:
        items = load()
        item = find(items, aid, name)
        now = time.time()
        if item:
            item["prompt"] = text
            _maybe_bind(item, aid, name)
            if title is not None:
                item["title"] = _clean_text(title, MAX_TITLE) or item["title"]
            item["updated"] = now
        else:
            item = {
                "id": _new_id(),
                "appid": aid,
                "game_name": name,
                "title": _clean_text(title, MAX_TITLE) or name or f"appid {aid}",
                "prompt": text,
                "created": now,
                "updated": now,
            }
            items.append(item)
        _write(items)
    return dict(item)


def update(preset_id: str, prompt=None, title=None) -> dict:
    """改内容 / 改名；改名不改变绑定的游戏，只影响列表显示"""
    pid = _clean_text(preset_id, 40)
    if not pid:
        raise ValueError("缺少预设 id")
    with _lock:
        items = load()
        item = next((x for x in items if x.get("id") == pid), None)
        if not item:
            raise KeyError("预设不存在")
        if prompt is not None:
            text = _clean_text(prompt, MAX_PROMPT)
            if not text:
                raise ValueError("提示词不能为空")
            item["prompt"] = text
        if title is not None:
            item["title"] = _clean_text(title, MAX_TITLE) or item["title"]
        item["updated"] = time.time()
        _write(items)
        if _active.get("preset_id") == pid:
            # 正在使用的预设改了内容：运行时立刻跟上，不用等下次切游戏
            _apply_active(item)
    return dict(item)


def delete(preset_id: str) -> bool:
    """删除预设；如果删的正是在用的那条，回到默认提示词"""
    pid = _clean_text(preset_id, 40)
    with _lock:
        items = load()
        kept = [x for x in items if x.get("id") != pid]
        if len(kept) == len(items):
            return False
        _write(kept)
        if _active.get("preset_id") == pid:
            _clear_active("预设已删除")
        return True


def _apply_active(item: dict) -> None:
    """把命中的预设设为当前生效值（只改运行时，不碰 config.toml）"""
    prompt = _clean_text(item.get("prompt"), MAX_PROMPT)
    if not prompt:
        _clear_active("预设内容为空")
        return
    _active.update({
        "preset_id": item.get("id", ""),
        "title": item.get("title", ""),
        "applied": True,
    })
    _log(f"提示词预设已套用：{_active.get('game_name') or '当前游戏'} → {_active['title']}")


def _clear_active(reason: str = "") -> None:
    was = bool(_active.get("applied"))
    _active.update({"preset_id": "", "title": "", "applied": False})
    if was:
        _log(f"提示词预设已取消（{reason or '游戏已退出'}），恢复默认提示词")


def active_view() -> dict:
    """当前游戏 / 当前预设状态（给 /api/status、/api/config 与插件展示）"""
    return {
        "game_appid": _active.get("appid", ""),
        "game_name": _active.get("game_name", ""),
        "preset_id": _active.get("preset_id", ""),
        "title": _active.get("title", ""),
        "applied": bool(_active.get("applied")),
    }


def set_active_game(appid="", game_name="") -> dict:
    """插件上报「当前在跑哪款游戏」时调用：命中预设就套用，没有就回默认

    游戏退出（appid 与名字都为空）会清空运行时状态，回到全局提示词。
    """
    aid = appid_text(appid)
    name = _clean_text(game_name, MAX_GAME_NAME)
    with _lock:
        prev = (_active.get("appid"), _active.get("game_name"))
        if not aid and not name:
            _active.update({"appid": "", "game_name": ""})
            _clear_active("游戏已退出")
            return active_view()
        _active.update({"appid": aid, "game_name": name})
        item = find(load(), aid, name)
        if item:
            _apply_active(item)
        else:
            _clear_active("该游戏没有预设")
            if prev != (aid, name):
                _log(f"当前游戏《{name or aid}》没有提示词预设，使用默认提示词")
        return active_view()


def effective_prompt(default: str) -> str:
    """当前生效的提示词：有套用的预设用预设，否则用传入的全局值"""
    with _lock:
        if _active.get("applied"):
            prompt = _clean_text(_active_preset_prompt(), MAX_PROMPT)
            if prompt:
                return prompt
            _clear_active("预设内容为空")
    return default


def _active_preset_prompt() -> str:
    pid = _active.get("preset_id", "")
    if not pid:
        return ""
    item = next((x for x in load() if x.get("id") == pid), None)
    return _clean_text(item.get("prompt"), MAX_PROMPT) if item else ""


def reset_runtime() -> None:
    """清空进程内状态（单测/重载用）"""
    with _lock:
        _active.update({"appid": "", "game_name": "", "preset_id": "",
                        "title": "", "applied": False})
