"""API 用量与余额（DeepSeek 官方口径）

官方事实（核对日期 2026-11-06）：

1. 余额查询：GET https://api.deepseek.com/user/balance
   - 鉴权：Authorization: Bearer <API Key>
   - 返回：is_available（余额是否够用）、balance_infos[]（可多币种，每项含
     currency / total_balance / granted_balance / topped_up_balance，金额为字符串）
   - 文档未说明该接口计费或有独立频率限制；它不属于 token 计费端点。
   - 来源：https://api-docs.deepseek.com/zh-cn/api/get-user-balance

2. 单次调用用量（chat completion 的 usage 字段）：
   - prompt_tokens（= prompt_cache_hit_tokens + prompt_cache_miss_tokens）
   - prompt_cache_hit_tokens / prompt_cache_miss_tokens
   - completion_tokens、total_tokens
   - 来源：https://api-docs.deepseek.com/api/create-chat-completion

3. 计价（人民币 / 百万 tokens，2026-09-10 12:00 起生效）：
   - deepseek-flash（含旧名 deepseek-chat / deepseek-reasoner / deepseek-v4-flash）：
     缓存命中 闲时 0.02 / 高峰 0.04；缓存未命中 1 / 2；输出 4 / 8
   - deepseek-v4-pro：缓存命中 0.15 / 0.30；缓存未命中 4.5 / 9.0；输出 13.5 / 27.0
   - 高峰时段：北京时间（UTC+8）周一至周五 09:00-12:00、14:00-18:00，
     不含中国法定节假日；其余时段（含周末与法定节假日全天）为空闲时段，价格为高峰的一半。
   - 来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
   - 公告：https://api-docs.deepseek.com/zh-cn/news/news260910/

4. 官方没有 token 用量 / 账单明细查询接口，只有网页控制台的「Billing」页；
   因此本地统计只能按 usage 字段自行累计，费用为估算，实际以官方账单为准。

存储：<数据根>/usage/<YYYY-MM-DD>.json，按北京时间自然日聚合、按模型分档；
每天再按 peak / offpeak 分桶，事后可按下单时的时段精确折算，重启不丢。
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx

from . import devlog, netmode, paths

CN_TZ = timezone(timedelta(hours=8))

# 高峰时段（北京时间）。与官方文档一致：周一至周五 09:00-12:00、14:00-18:00
PEAK_WINDOWS = ((time(9, 0), time(12, 0)), (time(14, 0), time(18, 0)))

# 2026 年中国法定节假日（国务院办公厅国办发明电〔2025〕7号，2025-11-04 发布）。
# 只用于判断「当天全天是否属于闲时」；跨年后按国务院新通知补充即可。
HOLIDAYS_2026 = frozenset({
    # 元旦 1/1-1/3
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节 2/15-2/23
    *{f"2026-02-{d:02d}" for d in range(15, 24)},
    # 清明 4/4-4/6
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节 5/1-5/5
    *{f"2026-05-{d:02d}" for d in range(1, 6)},
    # 端午 6/19-6/21
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋 9/25-9/27
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆 10/1-10/7
    *{f"2026-10-{d:02d}" for d in range(1, 8)},
})

HOLIDAYS = HOLIDAYS_2026

# 单价（人民币 / 百万 tokens），(空闲时段, 高峰时段)。价格只在这里维护一份，
# 接口层与界面都从这里取，改价只改这张表并更新下面的生效日期。
PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICING_EFFECTIVE = "2026-09-10 12:00（北京时间）"
PRICING = {
    "flash": {
        "label": "deepseek-flash",
        "hit": (0.02, 0.04),
        "miss": (1.0, 2.0),
        "out": (4.0, 8.0),
    },
    "pro": {
        "label": "deepseek-v4-pro",
        "hit": (0.15, 0.30),
        "miss": (4.5, 9.0),
        "out": (13.5, 27.0),
    },
}

# 旧模型名到档位的映射：官方已把它们路由到 V4.1 Flash，并按 Flash 单价计费
_FLASH_KEYS = ("flash", "deepseek-chat", "deepseek-reasoner",
               "deepseek-v4-flash", "deepseek-v4.1")
_PRO_KEYS = ("pro",)

_LOCK = threading.Lock()
_SCHEMA_VERSION = 2

# 会话级用量：每次模型调用追加一条明细，便于回答「这个会话每轮平均烧多少 token」
_SESSION_SCHEMA = 1
SESSION_MAX_ROUNDS = 500       # 单个会话保留最近多少次模型调用
SESSION_MAX_FILES = 200        # 保留最近多少个会话的明细文件

# 后台调用（压缩 / 起标题）：不是用户那句提问直接引起的，但仍然真实花钱，
# 所以合计与「每轮平均」里都算上，只在界面上单列次数让人知道占比
BACKGROUND_KINDS = ("compact", "title", "status")

# 当前这次模型调用属于哪个会话 / 哪一轮对话 / 这一轮的第几次调用 / 什么用途。
# 由 agent_engine（对话）与 main（压缩、起标题）在发请求前用 call_scope 设置，
# record() 写日聚合时顺手读出来落一份会话明细；没有上下文（探活、群友轮次、
# 单测）就只记日聚合，不影响原有统计。
#
# run 用「这一轮提问的时间戳」而不是轮次序号：序号在每次 run_agent 里都从 1 重来，
# 多个用户提问会互相撞号；时间戳天然唯一，聚合时按它去重就是「用户问了几轮」。
_CALL_CTX: contextvars.ContextVar = contextvars.ContextVar("usage_call_ctx", default=None)


@contextlib.contextmanager
def call_scope(sid: str = "", run: float = 0.0, call: int = 0, kind: str = "chat"):
    """标记接下来的模型调用归属哪个会话；异常退出时自动还原"""
    token = _CALL_CTX.set({"sid": str(sid or ""), "run": float(run or 0.0),
                           "call": int(call or 0), "kind": str(kind or "chat")})
    try:
        yield
    finally:
        _CALL_CTX.reset(token)


# ---------- 时间与时段 ----------

def _cn(dt: datetime | None = None) -> datetime:
    """统一到北京时间；无时区信息的时间按 UTC 处理，避免服务器本地时区干扰"""
    if dt is None:
        return datetime.now(CN_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CN_TZ)


def is_peak(dt: datetime | None = None,
            holidays: frozenset[str] = HOLIDAYS) -> bool:
    """这一时刻是否处于官方高峰时段（决定用标准价还是闲时半价）"""
    local = _cn(dt)
    if local.weekday() >= 5:          # 周六 / 周日全天闲时
        return False
    if local.date().isoformat() in holidays:
        return False
    t = local.time()
    return any(start <= t < end for start, end in PEAK_WINDOWS)


def tier_label(peak: bool) -> str:
    return "高峰时段（标准价）" if peak else "空闲时段（闲时半价）"


def model_tier(model: str) -> str | None:
    """模型名 → 价目档位；未收录返回 None（只统计 token，不估算费用）"""
    name = (model or "").strip().lower()
    if not name:
        return None
    if any(k in name for k in _PRO_KEYS):
        return "pro"
    if any(k in name for k in _FLASH_KEYS):
        return "flash"
    return None


def _price(key: str, peak: bool) -> tuple[float, float, float] | None:
    item = PRICING.get(key)
    if not item:
        return None
    idx = 1 if peak else 0
    return item["hit"][idx], item["miss"][idx], item["out"][idx]


def cost_for(model: str, hit: int, miss: int, out: int, peak: bool) -> float | None:
    """按官方单价折算一次（或一天里同一时段）的费用，单位：元"""
    key = model_tier(model)
    price = _price(key, peak) if key else None
    if not price:
        return None
    hit_price, miss_price, out_price = price
    return (max(hit, 0) / 1e6 * hit_price
            + max(miss, 0) / 1e6 * miss_price
            + max(out, 0) / 1e6 * out_price)


# ---------- usage 字段解析 ----------

def parse_usage(usage: dict | None) -> dict:
    """把服务商返回的 usage 归一成固定字段；兼容 OpenAI 与 DeepSeek 两种写法"""
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "cache_hit_tokens": 0, "cache_miss_tokens": 0}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (prompt + completion)
    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens")
              or cached or 0)
    hit = min(max(hit, 0), prompt) if prompt else max(hit, 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if not miss:
        miss = max(prompt - hit, 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
    }


# ---------- 存储 ----------

def usage_dir() -> Path:
    path = paths.data_dir() / "usage"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _day_file(day: date) -> Path:
    return usage_dir() / f"{day.isoformat()}.json"


def _empty_bucket() -> dict:
    return {"requests": 0, "hit": 0, "miss": 0, "out": 0}


def _new_day(day: date) -> dict:
    # models 是 v1（单接口时代）的全局聚合，保留读取兼容；新记录只写 apis，
    # 每个 api id 一份独立桶，改名不会打散历史（id 不变）
    return {"version": _SCHEMA_VERSION, "date": day.isoformat(),
            "updated_at": "", "models": {}, "apis": {}}


def _read_day(day: date) -> dict:
    path = _day_file(day)
    if not path.exists():
        return _new_day(day)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        devlog.log("usage.read_failed", path=str(path), error=repr(exc))
        return _new_day(day)
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return _new_day(day)
    if not isinstance(data.get("apis"), dict):
        data["apis"] = {}
    return data


def _write_day(day: date, data: dict) -> None:
    path = _day_file(day)
    data["updated_at"] = _cn().isoformat(timespec="seconds")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _add(bucket: dict, hit: int, miss: int, out: int) -> None:
    bucket["requests"] += 1
    bucket["hit"] += hit
    bucket["miss"] += miss
    bucket["out"] += out


def record(usage: dict | None, model: str, base_url: str = "",
           api_id: str = "", api_name: str = "",
           now: datetime | None = None) -> bool:
    """把一次成功请求的 usage 记到当天（北京时间）聚合文件；失败不抛给调用方

    api_id 是多组 API 配置的稳定 id（不是名称）；改名只更新标签，旧记录仍归在同一个
    桶里。api_id 为空表示调用方没有传（升级前的调用 / 单测），记在全局空桶。
    """
    if not isinstance(usage, dict):
        return False
    parsed = parse_usage(usage)
    if not parsed["prompt_tokens"] and not parsed["completion_tokens"]:
        return False
    local = _cn(now)
    peak = is_peak(local)
    day = local.date()
    key = model or "unknown"
    bucket_id = str(api_id or "")
    with _LOCK:
        data = _read_day(day)
        bucket = data["apis"].setdefault(bucket_id,
                                         {"name": "", "models": {}})
        if api_name:
            # 名称只是展示标签：改名后新记录覆盖旧标签，历史数值留在同一桶
            bucket["name"] = str(api_name)[:40]
        m = bucket["models"].setdefault(key, {
            "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "peak": _empty_bucket(), "offpeak": _empty_bucket(),
        })
        m["requests"] += 1
        m["prompt_tokens"] += parsed["prompt_tokens"]
        m["completion_tokens"] += parsed["completion_tokens"]
        m["total_tokens"] += parsed["total_tokens"]
        m["cache_hit_tokens"] += parsed["cache_hit_tokens"]
        m["cache_miss_tokens"] += parsed["cache_miss_tokens"]
        _add(m["peak" if peak else "offpeak"], parsed["cache_hit_tokens"],
             parsed["cache_miss_tokens"], parsed["completion_tokens"])
        _write_day(day, data)
    devlog.log("usage.record", model=key, peak=peak,
               prompt=parsed["prompt_tokens"], completion=parsed["completion_tokens"],
               cache_hit=parsed["cache_hit_tokens"],
               api=api_id, base_url=base_url)
    # 会话明细：上下文里没有会话号（探活 / 群友轮次 / 单测）就只记日聚合
    try:
        record_session(usage, model=key, api_id=api_id, now=local)
    except Exception as exc:      # 会话明细写失败绝不能影响对话
        devlog.log("usage.session_record_failed", error=repr(exc))
    return True


# ---------- 会话级用量 ----------

def sessions_dir() -> Path:
    path = usage_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_sid(sid: str) -> str:
    """会话号 → 文件名安全串；只放行字母数字和 - _ .，防路径穿越"""
    raw = str(sid or "").strip()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "-_.")
    return safe[:80]


def _session_file(sid: str) -> Path:
    return sessions_dir() / f"{sid}.json"


def _read_session(sid: str) -> tuple[dict, bool]:
    """读一个会话明细文件，返回 (数据, 文件原来是否存在)"""
    path = _session_file(sid)
    if not path.exists():
        return {"version": _SESSION_SCHEMA, "sid": sid, "updated": "", "rounds": []}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        devlog.log("usage.session_read_failed", path=str(path), error=repr(exc))
        return {"version": _SESSION_SCHEMA, "sid": sid, "updated": "", "rounds": []}, True
    if not isinstance(data, dict) or not isinstance(data.get("rounds"), list):
        return {"version": _SESSION_SCHEMA, "sid": sid, "updated": "", "rounds": []}, True
    data.setdefault("sid", sid)
    return data, True


def _write_session(sid: str, data: dict) -> None:
    path = _session_file(sid)
    data["version"] = _SESSION_SCHEMA
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _prune_sessions() -> None:
    """只保留最近 SESSION_MAX_FILES 个会话的明细，防止目录无限增长"""
    try:
        files = sorted(sessions_dir().glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[SESSION_MAX_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass


def record_session(usage: dict | None, model: str = "", api_id: str = "",
                   now: datetime | None = None) -> bool:
    """按当前 call_scope 把一次模型调用追加到会话明细；没有上下文时什么也不做"""
    ctx = _CALL_CTX.get()
    if not isinstance(ctx, dict):
        return False
    sid = _safe_sid(ctx.get("sid") or "")
    if not sid or not isinstance(usage, dict):
        return False
    parsed = parse_usage(usage)
    if not parsed["prompt_tokens"] and not parsed["completion_tokens"]:
        return False
    when = _cn(now)
    entry = {
        "ts": int(when.timestamp()),
        "run": round(float(ctx.get("run") or 0.0), 3),
        "call": int(ctx.get("call") or 0),
        "kind": str(ctx.get("kind") or "chat"),
        "model": str(model or ""),
        "api": str(api_id or ""),
        "in": parsed["prompt_tokens"],
        "out": parsed["completion_tokens"],
        "hit": parsed["cache_hit_tokens"],
    }
    with _LOCK:
        data, existed = _read_session(sid)
        rounds = data.get("rounds")
        if not isinstance(rounds, list):
            rounds = data["rounds"] = []
        rounds.append(entry)
        if len(rounds) > SESSION_MAX_ROUNDS:
            del rounds[:-SESSION_MAX_ROUNDS]
        data["updated"] = when.isoformat(timespec="seconds")
        data["updated_ts"] = int(when.timestamp())
        _write_session(sid, data)
    if not existed:
        _prune_sessions()
    return True


def _is_background(kind: str) -> bool:
    return str(kind or "") in BACKGROUND_KINDS


def _round_stats(rounds: list[dict]) -> dict:
    """聚合一个会话的所有模型调用：合计与平均都按真实消耗算，后台调用算进去。

    「轮」只由用户提问定义（同一轮里工具往返 / 自动续写的多次调用共用一个 run 键，
    去重后算一轮）；压缩 / 起标题这类后台调用没有 run，不增加轮数，但它们的 token
    会计入合计，也会摊进「平均每轮」——那正是这个会话真实烧掉的量。
    """
    items = [r for r in rounds if isinstance(r, dict)]
    fg = [r for r in items if not _is_background(r.get("kind"))]
    bg = [r for r in items if _is_background(r.get("kind"))]
    tin = sum(int(r.get("in") or 0) for r in items)
    tout = sum(int(r.get("out") or 0) for r in items)
    hit = sum(int(r.get("hit") or 0) for r in items)
    turns = len({r.get("run") for r in fg if r.get("run")})
    if not turns and fg:
        turns = 1
    calls = len(items)
    out = {
        "turns": turns, "calls": calls, "background": len(bg),
        "in": tin, "out": tout, "total": tin + tout,
        "background_in": sum(int(r.get("in") or 0) for r in bg),
        "background_out": sum(int(r.get("out") or 0) for r in bg),
        "cache_hit_rate": round(hit / tin * 100, 1) if tin else 0.0,
    }
    out["avg_in"] = round(tin / turns) if turns else 0
    out["avg_out"] = round(tout / turns) if turns else 0
    out["avg_total"] = round((tin + tout) / turns) if turns else 0
    out["avg_in_per_call"] = round(tin / calls) if calls else 0
    out["avg_out_per_call"] = round(tout / calls) if calls else 0
    out["avg_total_per_call"] = round((tin + tout) / calls) if calls else 0
    return out


def session_summary(sid: str, recent: int = 10) -> dict:
    """一个会话的用量聚合 + 最近若干次调用明细；没有记录时 has_data=False"""
    safe = _safe_sid(sid)
    data, _ = _read_session(safe) if safe else ({}, False)
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    result = {"sid": str(sid or ""), "has_data": bool(rounds),
              "updated": data.get("updated") or "", "rounds": []}
    result.update(_round_stats(rounds))
    if not rounds:
        return result
    recent = max(1, min(int(recent or 10), 50))
    # 轮次序号：run 键按出现先后编号（后台调用没有 run，显示为「—」）
    order = {}
    for r in rounds:
        key = r.get("run")
        if key and key not in order:
            order[key] = len(order) + 1
    detail = []
    for r in reversed(rounds[-recent:]):
        detail.append({
            "ts": int(r.get("ts") or 0), "turn": order.get(r.get("run"), 0),
            "call": int(r.get("call") or 0), "kind": str(r.get("kind") or "chat"),
            "model": str(r.get("model") or ""), "in": int(r.get("in") or 0),
            "out": int(r.get("out") or 0), "hit": int(r.get("hit") or 0),
        })
    result["rounds"] = detail
    return result


def session_list(limit: int = 12) -> list[dict]:
    """最近使用过、且有用量明细的会话，按最后使用时间倒序"""
    limit = max(1, min(int(limit or 12), 50))
    items = []
    try:
        files = list(sessions_dir().glob("*.json"))
    except OSError:
        return items
    for path in files:
        sid = path.stem
        data, _ = _read_session(sid)
        rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
        if not rounds:
            continue
        stat = _round_stats(rounds)
        items.append({"sid": sid, "updated": data.get("updated") or "",
                      "updated_ts": int(data.get("updated_ts") or 0),
                      "in": stat["in"], "out": stat["out"],
                      "turns": stat["turns"], "calls": stat["calls"],
                      "rounds": len(rounds)})
    items.sort(key=lambda x: x["updated_ts"], reverse=True)
    return items[:limit]


def drop_session(sid: str) -> bool:
    """删掉一个会话的用量明细（会话被删除 / 清空统计时调用）"""
    safe = _safe_sid(sid)
    if not safe:
        return False
    path = _session_file(safe)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        devlog.log("usage.session_drop_failed", path=str(path), error=repr(exc))
        return False


def _model_summary(name: str, m: dict) -> dict:
    hit = int(m.get("cache_hit_tokens") or 0)
    prompt = int(m.get("prompt_tokens") or 0)
    peak_b = m.get("peak") or _empty_bucket()
    off_b = m.get("offpeak") or _empty_bucket()
    peak_cost = cost_for(name, int(peak_b.get("hit") or 0),
                         int(peak_b.get("miss") or 0), int(peak_b.get("out") or 0), True)
    off_cost = cost_for(name, int(off_b.get("hit") or 0),
                        int(off_b.get("miss") or 0), int(off_b.get("out") or 0), False)
    cost = None
    if peak_cost is not None or off_cost is not None:
        cost = round((peak_cost or 0.0) + (off_cost or 0.0), 6)
    return {
        "model": name,
        "requests": int(m.get("requests") or 0),
        "prompt_tokens": prompt,
        "completion_tokens": int(m.get("completion_tokens") or 0),
        "total_tokens": int(m.get("total_tokens") or 0),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": int(m.get("cache_miss_tokens") or 0),
        "cache_hit_rate": round(hit / prompt * 100, 1) if prompt else 0.0,
        "cost_cny": cost,
        "priced": cost is not None,
    }


def _merge(summary: dict, totals: dict) -> None:
    for k in ("requests", "prompt_tokens", "completion_tokens", "total_tokens",
              "cache_hit_tokens", "cache_miss_tokens"):
        totals[k] += int(summary.get(k) or 0)
    if summary.get("cost_cny") is not None:
        totals["cost_cny"] += float(summary["cost_cny"])


def _day_buckets(data: dict):
    """一天文件 → [(api_id, api_name, models)]

    v2 起每个 API 一份桶；v1（升级前）只有全局 models，归到空 id 的「未标记」桶，
    在「全部」里照常计入，按具体 API 过滤时不会误算到某一条配置。
    apis 存在时全局 models 只可能是升级前的旧数据（新记录不再写），两份相加不重复。
    """
    apis = data.get("apis")
    if isinstance(apis, dict):
        for aid, raw in apis.items():
            if isinstance(raw, dict) and isinstance(raw.get("models"), dict):
                yield str(aid), str(raw.get("name") or ""), raw["models"]
    models = data.get("models")
    if isinstance(models, dict) and models:
        yield "", "", models


def snapshot(days: int = 14, now: datetime | None = None,
             base_url: str = "", api_id: str | None = None) -> dict:
    """近 N 天的聚合结果 + 当前时段信息；不做网络请求

    api_id 为空或 "all" 时返回全部配置的汇总；给定 id 时只统计那一组。
    """
    local = _cn(now)
    days = max(1, min(int(days or 14), 400))
    selected = None if api_id in (None, "", "all") else str(api_id)
    start = local.date() - timedelta(days=days - 1)
    totals = {k: 0 for k in ("requests", "prompt_tokens", "completion_tokens",
                             "total_tokens", "cache_hit_tokens", "cache_miss_tokens")}
    totals["cost_cny"] = 0.0
    by_model: dict[str, dict] = {}
    by_api: dict[str, dict] = {}
    out_days = []
    unpriced = set()
    corrupted = []

    with _LOCK:
        files = sorted(usage_dir().glob("*.json"))
        for path in files:
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if day < start or day > local.date():
                continue
            data = _read_day(day)
            if not data.get("models") and not data.get("apis") \
                    and path.exists() and path.stat().st_size == 0:
                corrupted.append(path.name)
            day_models: dict[str, dict] = {}
            day_totals = {k: 0 for k in totals}
            for aid, label, raw_models in _day_buckets(data):
                if selected is not None and aid != selected:
                    continue
                for name, raw in raw_models.items():
                    if not isinstance(raw, dict):
                        continue
                    s = _model_summary(name, raw)
                    dm = day_models.setdefault(name, {
                        "model": name, "requests": 0, "prompt_tokens": 0,
                        "completion_tokens": 0, "total_tokens": 0,
                        "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                        "cost_cny": 0.0, "priced": s["priced"],
                    })
                    _merge(s, dm)
                    _merge(s, day_totals)
                    acc = by_model.setdefault(name, {
                        "model": name, "requests": 0, "prompt_tokens": 0,
                        "completion_tokens": 0, "total_tokens": 0,
                        "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                        "cost_cny": 0.0, "priced": s["priced"],
                    })
                    _merge(s, acc)
                    if not s["priced"]:
                        unpriced.add(name)
                    api = by_api.setdefault(aid, {
                        "id": aid, "name": label, "legacy": not aid,
                        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0, "cache_hit_tokens": 0,
                        "cache_miss_tokens": 0, "cost_cny": 0.0,
                        "priced": s["priced"],
                    })
                    if label:
                        # 改名后标签取最近一次记录的名称；id 不变，历史不会被拆散
                        api["name"] = label
                    _merge(s, api)
            day_totals["cache_hit_rate"] = (
                round(day_totals["cache_hit_tokens"] / day_totals["prompt_tokens"] * 100, 1)
                if day_totals["prompt_tokens"] else 0.0)
            for dm in day_models.values():
                prompt = dm["prompt_tokens"]
                dm["cache_hit_rate"] = (round(dm["cache_hit_tokens"] / prompt * 100, 1)
                                        if prompt else 0.0)
                dm["cost_cny"] = round(dm["cost_cny"], 6)
            day_totals["date"] = day.isoformat()
            day_totals["models"] = sorted(day_models.values(),
                                          key=lambda x: -x["total_tokens"])
            out_days.append(day_totals)
            for k, v in day_totals.items():
                if k in totals and isinstance(v, (int, float)):
                    totals[k] += v

    for acc in list(by_model.values()) + list(by_api.values()):
        prompt = acc["prompt_tokens"]
        acc["cache_hit_rate"] = round(acc["cache_hit_tokens"] / prompt * 100, 1) if prompt else 0.0
        acc["cost_cny"] = round(acc["cost_cny"], 6)

    totals["cost_cny"] = round(totals["cost_cny"], 6)
    totals["cache_hit_rate"] = (
        round(totals["cache_hit_tokens"] / totals["prompt_tokens"] * 100, 1)
        if totals["prompt_tokens"] else 0.0)
    peak = is_peak(local)
    return {
        "days": out_days,
        "totals": totals,
        "models": sorted(by_model.values(), key=lambda x: -x["total_tokens"]),
        "apis": sorted(by_api.values(), key=lambda x: -x["total_tokens"]),
        "api": selected or "all",
        "today": local.date().isoformat(),
        "now": local.isoformat(timespec="seconds"),
        "peak": peak,
        "tier": "peak" if peak else "offpeak",
        "tier_label": tier_label(peak),
        "peak_windows": "北京时间 周一至周五 09:00-12:00、14:00-18:00",
        "pricing": {k: {"label": v["label"],
                        "hit_offpeak": v["hit"][0], "hit_peak": v["hit"][1],
                        "miss_offpeak": v["miss"][0], "miss_peak": v["miss"][1],
                        "out_offpeak": v["out"][0], "out_peak": v["out"][1]}
                    for k, v in PRICING.items()},
        "pricing_source": PRICING_SOURCE,
        "pricing_effective": PRICING_EFFECTIVE,
        "official_endpoint": "deepseek" in (base_url or "").lower(),
        "unpriced_models": sorted(unpriced),
        "corrupted_files": corrupted,
        "note": "费用按官方单价估算，实际扣费以官方账单为准",
    }


def export_data() -> dict:
    """导出全部按日原始聚合（JSON 结构，可直接落盘）"""
    with _LOCK:
        days = {}
        for path in sorted(usage_dir().glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            days[path.stem] = data
    return {"version": _SCHEMA_VERSION,
            "exported_at": _cn().isoformat(timespec="seconds"),
            "days": days}


def clear() -> int:
    """清空本地用量统计（按日聚合 + 会话明细），不碰配置与会话文件"""
    removed = 0
    with _LOCK:
        targets = list(usage_dir().glob("*.json"))
        try:
            targets += list(sessions_dir().glob("*.json"))
        except OSError:
            pass
        for path in targets:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                devlog.log("usage.clear_failed", path=str(path), error=repr(exc))
    return removed


# ---------- 余额 ----------

def balance_urls(base_url: str) -> list[str]:
    """余额接口地址：base_url 可能带 /v1（OpenAI 兼容写法），根路径再补一个候选"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.deepseek.com"
    urls = [base + "/user/balance"]
    if base.endswith("/v1"):
        urls.append(base[:-3].rstrip("/") + "/user/balance")
    return urls


def _error_text(exc: Exception) -> tuple[str, str]:
    """异常 → (机器可读原因, 给用户看的中文说明)"""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "网络超时：服务商没有在规定时间内响应"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else 0
        mapping = {
            400: "请求被拒绝（400）：接口地址可能不是 DeepSeek 官方端点",
            401: "API Key 无效或已失效（401）",
            403: "该 Key 没有余额查询权限（403）",
            404: "该地址不支持余额查询（404）：请确认使用 DeepSeek 官方接口",
            429: "请求过于频繁（429），请稍后再试",
        }
        if code in mapping:
            return f"http_{code}", mapping[code]
        if code >= 500:
            return f"http_{code}", f"服务商暂时不可用（{code}）"
        return f"http_{code}", f"服务商返回异常状态（{code}）"
    if isinstance(exc, httpx.NetworkError):
        return "network", f"网络不通：{type(exc).__name__}"
    return "error", f"查询失败：{type(exc).__name__}: {exc}"


async def fetch_balance(base_url: str, api_key: str, timeout: float = 8.0,
                        client: httpx.AsyncClient | None = None) -> dict:
    """查询官方余额；未配置 Key 直接返回原因，不发起网络请求"""
    if not (api_key or "").strip():
        return {"ok": False, "reason": "unconfigured", "error": "未配置 API Key"}
    # 离线模式：外网余额接口快捷返回原因；本地模型地址放行（不支持时按 404 提示）
    try:
        netmode.check(base_url or "", "余额查询")
    except netmode.OfflineError as exc:
        return {"ok": False, "reason": "offline", "error": str(exc)}
    headers = {"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"}
    own = client is None
    cli = client or httpx.AsyncClient(timeout=timeout)
    last_exc: Exception | None = None
    try:
        for url in balance_urls(base_url):
            try:
                resp = await cli.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return _balance_payload(data)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                # 只有 404 值得换下一个候选地址；401/403 是 Key 的问题，换地址也没用
                if exc.response is not None and exc.response.status_code == 404:
                    continue
                raise
        if last_exc:
            raise last_exc
        return {"ok": False, "reason": "empty", "error": "余额接口没有返回内容"}
    except Exception as exc:
        reason, text = _error_text(exc)
        devlog.log("usage.balance_failed", reason=reason, error=repr(exc))
        return {"ok": False, "reason": reason, "error": text}
    finally:
        if own:
            await cli.aclose()


def _balance_payload(data: dict) -> dict:
    infos = data.get("balance_infos") if isinstance(data, dict) else None
    if not isinstance(infos, list) or not infos:
        return {"ok": False, "reason": "empty", "error": "余额接口未返回 balance_infos"}
    rows = []
    for item in infos:
        if not isinstance(item, dict):
            continue
        rows.append({
            "currency": str(item.get("currency") or ""),
            "total_balance": str(item.get("total_balance") or ""),
            "granted_balance": str(item.get("granted_balance") or ""),
            "topped_up_balance": str(item.get("topped_up_balance") or ""),
        })
    if not rows:
        return {"ok": False, "reason": "empty", "error": "余额接口返回格式无法识别"}
    # 优先展示人民币（官方计价币种），没有就取第一条
    picked = next((r for r in rows if r["currency"].upper() == "CNY"), rows[0])
    return {
        "ok": True,
        "is_available": bool(data.get("is_available")),
        "currency": picked["currency"],
        "total_balance": picked["total_balance"],
        "granted_balance": picked["granted_balance"],
        "topped_up_balance": picked["topped_up_balance"],
        "balances": rows,
        "fetched_at": _cn().isoformat(timespec="seconds"),
    }


def fetch_balance_sync(base_url: str, api_key: str, timeout: float = 8.0) -> dict:
    """同步入口（测试与脚本用）；服务端统一走 fetch_balance"""
    return asyncio.run(fetch_balance(base_url, api_key, timeout))
