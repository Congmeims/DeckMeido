"""DeckMeido 长期记忆：跨会话保留的「要记住的事」

和会话历史是两回事：history 是「这次聊了什么」，这里存的是「以后都要知道的」。
按用途分三类，写进同一个 JSON（memory.json，容易被用户直接打开看/备份）：

  profile  用户画像与偏好   「回答控制在三行以内」「别用表情」
  env      环境与操作经验   「启动这个游戏要先开兼容层」
  note     项目与知识条目   「d:/proj 用的是 uv 而不是 pip」

条目还带 scope，决定它属于谁、在哪种对话里被注入：
  private       主人私聊 / 本机网页（旧数据没有 scope 时一律归到这里）
  group:<群号>  某个群的记忆，只在该群注入，不带到私聊或别的群
  person:<QQ>   关于某个人的记忆，在私聊或他发言的群里与主 scope 一起注入

和 groupmemo 的分界：groupmemo 是群聊里最近消息的只读短缓存（TTL、随时过期），
本模块是明确写入、长期保留、可增删改查的条目；两者不互相写入。

注入方式见 main.py 里的 _memory_block()：拼在 system prompt 的**最末尾**。
记忆会随使用变化，放在开头会让每次改动把后面整段前缀缓存打掉；放在末尾
时受影响的最多只是记忆这一小段。
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
from . import paths


def _path() -> Path:
    """记忆文件：数据根下的 memory.json（惰性解析）"""
    return paths.memory_path()

# 三类记忆的中文名，注入提示词和前端展示共用
KINDS = {"profile": "用户画像", "env": "环境经验", "note": "知识条目"}
# 单条长度上限：记忆是给模型看的一句话，写成长文会挤占上下文
MAX_ITEM_CHARS = 300
# 总条数上限：超过就按「最近使用的靠前」截断，避免无限膨胀
MAX_ITEMS = 120
# 单轮注入的字数预算：条目再多也不能把上下文吃光。超预算时按
# 「知识条目 → 环境经验 → 用户画像」的顺序丢，同类里先丢最久没用的；
# 只是不注入这一轮，磁盘上的条目一条都不删。
BLOCK_MAX_CHARS = 2500

# scope：三个前缀。private 是旧数据的默认归属，group/person 后面跟稳定 id。
SCOPE_PRIVATE = "private"
SCOPE_GROUP_PREFIX = "group:"
SCOPE_PERSON_PREFIX = "person:"

# 当前回合的注入范围与写入归属：由 onebot 在创建回合子任务前设置，
# 网页 / 插件等本地回合没有设置时按私聊处理。
_CURRENT_SCOPES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "deckmeido_memory_scopes", default=(SCOPE_PRIVATE,))
_CURRENT_WRITE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "deckmeido_memory_write_scope", default=SCOPE_PRIVATE)


def norm_scope(value: str | None) -> str:
    """把 scope 归一成可存的键；空 / 认不出的一律回落到 private

    这样旧 memory.json（没有 scope 字段）读进来仍然全部算私聊记忆，
    升级后既不会丢条目，也不会把主人的私聊内容误注入到群里。
    """
    s = str(value or "").strip()
    if s == SCOPE_PRIVATE:
        return s
    for prefix in (SCOPE_GROUP_PREFIX, SCOPE_PERSON_PREFIX):
        if s.startswith(prefix) and s[len(prefix):].strip():
            return prefix + s[len(prefix):].strip()
    return SCOPE_PRIVATE


def scope_group(group_id: str | int | None) -> str:
    gid = str(group_id or "").strip()
    return SCOPE_GROUP_PREFIX + gid if gid else SCOPE_PRIVATE


def scope_person(uin: str | int | None) -> str:
    u = str(uin or "").strip()
    return SCOPE_PERSON_PREFIX + u if u else SCOPE_PRIVATE


def scopes_for(group_id: str | int | None = None,
               uin: str | int | None = None) -> tuple[str, ...]:
    """当前会话该注入哪些 scope：群 + 发言人，或私聊 + 对方"""
    out = [scope_group(group_id) if str(group_id or "").strip() else SCOPE_PRIVATE]
    person = scope_person(uin)
    if person != SCOPE_PRIVATE:
        out.append(person)
    return tuple(out)


def write_scope(group_id: str | int | None = None) -> str:
    """自动写入的归属：群聊进群记忆，私聊进私聊记忆（person 走显式指令）"""
    return scope_group(group_id)


def set_context(scopes=None, write: str | None = None):
    """设置当前回合的注入范围与写入归属，返回供 reset_context 用的令牌"""
    tok_scopes = _CURRENT_SCOPES.set(tuple(scopes or (SCOPE_PRIVATE,)))
    tok_write = _CURRENT_WRITE.set(norm_scope(write))
    return tok_scopes, tok_write


def reset_context(tokens) -> None:
    tok_scopes, tok_write = tokens
    _CURRENT_WRITE.reset(tok_write)
    _CURRENT_SCOPES.reset(tok_scopes)


def current_scopes() -> tuple[str, ...]:
    return tuple(_CURRENT_SCOPES.get() or (SCOPE_PRIVATE,))


def current_write_scope() -> str:
    return norm_scope(_CURRENT_WRITE.get())

_lock = threading.Lock()
# 内存里的「最近注入时间」：只为淘汰时留个依据，不参与每轮写盘
_used_lock = threading.Lock()
_USED: dict[str, float] = {}


def _used_of(it: dict) -> float:
    return max(it.get("used") or 0.0, _USED.get(it.get("id") or "", 0.0))


def _persist_used(data: dict) -> bool:
    """把内存里的「最近注入时间」合进要写盘的 data（调用方持有 _lock）

    返回是否有变化。以前 mark_used 只写 _USED，而 _save 从不把它合进 data，
    于是淘汰时「按最近使用保留」形同虚设：磁盘上的 used 永远是 0，
    120 条一满就按创建时间砍，常用的老条目照样被丢掉。
    """
    changed = False
    with _used_lock:
        if not _USED:
            return False
        for it in data.get("items") or []:
            u = _USED.get(it.get("id") or "")
            if u and u > (it.get("used") or 0):
                it["used"] = u
                changed = True
    return changed


def _empty() -> dict:
    return {"items": [], "updated": 0.0}


def load() -> dict:
    """读取全部记忆；文件坏了就当没有，不要因为一份记忆文件把服务拖挂"""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return _empty()
    items = []
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        items.append({
            "id": str(it.get("id") or ""),
            "kind": it.get("kind") if it.get("kind") in KINDS else "note",
            "text": text[:MAX_ITEM_CHARS],
            # 旧数据没有 scope：归到 private，保证升级后还能照常注入私聊
            "scope": norm_scope(it.get("scope")),
            "created": float(it.get("created") or 0),
            "used": float(it.get("used") or 0),
        })
    data["items"] = items
    return data


def _save(data: dict) -> None:
    """原子写：先写临时文件再替换，断电/杀进程不会留下半截 JSON

    写入前一律把内存里的 used 合进来：删除/编辑/清空这些路径也顺带把
    「最近用过哪些」落到磁盘，不必为它单独多写一次文件。
    """
    _persist_used(data)
    data["updated"] = time.time()
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def norm(text: str) -> str:
    """去重的比较键：忽略大小写、空白和常见标点差异

    「回答简短一点」和「回答简短一点。」是同一件事，不该存两遍。
    """
    return re.sub(r"[\s。，,.!！?？；;：:、]+", "", str(text)).lower()


def _new_id() -> str:
    return f"m{int(time.time() * 1000):x}{os.getpid() % 1000:03d}"


_CQ_AT_RE = re.compile(r"\[CQ:at,[^\]]*\]")
_LEAD_PUNCT_RE = re.compile(r"^[，,、：:；; \t]+")


def clean_text(text: str, names=()) -> str:
    """写入记忆前清洗：去掉 @提及、发送者昵称标签与多余空白

    names 传机器人昵称和发送者昵称：只删这两个人的提及，不碰「@别人」
    这类正文内容，也避开邮箱里的 @。群聊里 @ 机器人是触发方式、转发消息
    常带「昵称：」前缀，这些原样存进记忆就会变成「@meido 帮我记住…」
    这种没人看得懂的条目。
    """
    out = _CQ_AT_RE.sub("", str(text or ""))
    for name in sorted({str(n).strip() for n in names if str(n).strip()},
                       key=len, reverse=True):
        esc = re.escape(name)
        # @昵称：@ 前面必须是行首/空白/标点，避免把 a@b.com 或 xx@yy 拆坏
        out = re.sub(rf"(?<![\w.@-])@\s*{esc}", "", out)
        # 行首的「昵称：」「昵称:」「昵称说：」是转发/合并消息带进来的标签
        out = re.sub(rf"(?m)^\s*{esc}\s*(?:[：:]\s*|说[：:]?\s*)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = _LEAD_PUNCT_RE.sub("", out)
    return out.strip()


def add(kind: str, text: str, names=(), scope: str | None = None) -> dict:
    """新增一条记忆；同 scope 内内容重复时直接返回已有的那条"""
    kind = kind if kind in KINDS else "note"
    scope = norm_scope(scope) if scope is not None else current_write_scope()
    text = clean_text(text, names)[:MAX_ITEM_CHARS]
    if not text:
        return {}
    with _lock:
        data = load()
        key = norm(text)
        for it in data["items"]:
            # 去重只看同一 scope：私聊和群里各记一条同样的话是合理的，
            # 不该因为「别的 scope 里有」就把这一条吞掉。
            if it["scope"] == scope and norm(it["text"]) == key:
                return it
        item = {"id": _new_id(), "kind": kind, "text": text, "scope": scope,
                "created": time.time(), "used": 0.0}
        data["items"].append(item)
        # 先合入内存里记的「最近注入时间」，再按它淘汰 —— 否则排序键全是 0，
        # 常用的老条目和从没被用过的条目排在一起，被砍掉的可能正是最该留的
        _persist_used(data)
        # 超上限时丢最久没用过的那些（之前会话里注入过的优先保留）
        if len(data["items"]) > MAX_ITEMS:
            data["items"].sort(key=lambda x: (_used_of(x), x["created"]), reverse=True)
            data["items"] = data["items"][:MAX_ITEMS]
        _save(data)
        return item


def update(mid: str, text: str | None = None, kind: str | None = None) -> bool:
    with _lock:
        data = load()
        for it in data["items"]:
            if it["id"] != mid:
                continue
            if text is not None:
                t = str(text).strip()[:MAX_ITEM_CHARS]
                # 清空正文等于删除这条，前端编辑框清空后保存就是这个意思
                if not t:
                    data["items"] = [x for x in data["items"] if x["id"] != mid]
                    _save(data)
                    return True
                it["text"] = t
            if kind in KINDS:
                it["kind"] = kind
            _save(data)
            return True
        return False


def remove(mid: str) -> bool:
    with _lock:
        data = load()
        left = [x for x in data["items"] if x["id"] != mid]
        if len(left) == len(data["items"]):
            return False
        data["items"] = left
        _save(data)
        return True


def clear() -> None:
    with _lock:
        _save(_empty())
    # _USED 里的旧 id 跟着一起清掉：否则「删光记忆」后进程里还捏着一批
    # 永不匹配任何条目的 used 记录（量很小，纯粹是干净起见）
    with _used_lock:
        _USED.clear()


def list_scope(scope: str, limit: int | None = None) -> list[dict]:
    """某个 scope 的条目，按注入顺序（类别 + 创建时间）排好"""
    scope = norm_scope(scope)
    items = [x for x in load()["items"] if x["scope"] == scope]
    items.sort(key=lambda x: (list(KINDS).index(x["kind"]), x["created"]))
    return items[:limit] if limit else items


def clear_scopes(scopes) -> int:
    """清空给定 scope 的条目，返回删除条数（单次加锁、单次写盘）"""
    wanted = {norm_scope(s) for s in (scopes or ())}
    if not wanted:
        return 0
    with _lock:
        data = load()
        keep = [x for x in data["items"] if x["scope"] not in wanted]
        removed = [x for x in data["items"] if x["scope"] in wanted]
        if removed:
            data["items"] = keep
            _save(data)
    if removed:
        with _used_lock:
            for x in removed:
                _USED.pop(x.get("id") or "", None)
    return len(removed)


def clear_scope(scope: str) -> int:
    """清空一个 scope（/memory clear 与网页批量删除共用）"""
    return clear_scopes((scope,))


def mark_used(ids: list[str]) -> None:
    """记一次「被注入过」，用于上限淘汰时保留常用的那些

    注入了哪些 id 每个会话都不一样，直接写会每轮都改磁盘上的文件、还要
    和用户编辑记忆抢锁，所以这里只在内存里记一笔，随下一次真正的增删改一起落盘。
    """
    if not ids:
        return
    with _used_lock:
        _USED.update((i, time.time()) for i in ids)


def _render_block(items: list[dict], wanted: set[str]) -> tuple[str, list[str]]:
    """把条目展开成注入文本：同一个 items 每次结果完全一致"""
    if not items:
        return "", []
    # 私聊沿用原来的开头（老用户的提示词前缀不变）；群 / 人记忆用中性说法，
    # 避免把群里的内容说成「关于主人」。
    if wanted == {SCOPE_PRIVATE}:
        head = ("【关于主人的长期记忆】以下是之前明确要求过要记住的内容，"
                "在相关时遵守，无关时不要强行提起：")
    else:
        head = ("【已记住的内容】以下是当前会话相关的长期记忆，"
                "在相关时遵守，无关时不要强行提起：")
    lines = [head]
    for kind in KINDS:
        group = [x for x in items if x["kind"] == kind]
        if not group:
            continue
        lines.append(f"{KINDS[kind]}：")
        lines += [f"- {x['text']}" for x in group]
    return "\n".join(lines), [x["id"] for x in items]


def block(limit: int = 60, scopes=None,
          max_chars: int | None = BLOCK_MAX_CHARS) -> tuple[str, list[str]]:
    """拼成要注入 system prompt 的文本，返回 (文本, 用到的 id 列表)

    排序固定为「先按类别、再按创建时间」，同一份记忆每次展开结果都一样 —— 
    随机顺序会让每轮前缀都变，缓存命中率直接归零。
    scopes 不传时跟随当前回合（网页 / 插件默认 private，QQ 群回合由 onebot 设置）。
    max_chars：单轮注入的字数上限；超出时优先丢知识条目、再环境经验，
    最后才动用户画像，同类里先丢「最久没被注入 / 最早创建」的条目。
    """
    wanted = tuple(scopes) if scopes is not None else current_scopes()
    wanted = {norm_scope(s) for s in wanted} or {SCOPE_PRIVATE}
    data = load()
    items = [x for x in data["items"] if x["scope"] in wanted]
    items.sort(key=lambda x: (list(KINDS).index(x["kind"]), x["created"]))
    items = items[:limit]
    text, ids = _render_block(items, wanted)
    if not max_chars or len(text) <= max_chars:
        return text, ids
    # 参考分层记忆的「注入预算」思路，但不做摘要/召回：只是按重要性
    # 从低到高裁剪本次注入，磁盘上的记忆保持完整，下一轮条件变了还能再进来。
    for kind in ("note", "env", "profile"):
        while len(text) > max_chars:
            group = [x for x in items if x["kind"] == kind]
            if not group:
                break
            victim = min(group, key=lambda x: (_used_of(x), x["created"]))
            items.remove(victim)
            text, ids = _render_block(items, wanted)
    return text, ids


def stats() -> dict:
    data = load()
    return {"total": len(data["items"]),
            "profile": sum(1 for x in data["items"] if x["kind"] == "profile"),
            "env": sum(1 for x in data["items"] if x["kind"] == "env"),
            "note": sum(1 for x in data["items"] if x["kind"] == "note")}
