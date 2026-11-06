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
import json
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx

from . import devlog, paths

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
_SCHEMA_VERSION = 1


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
    return {"version": _SCHEMA_VERSION, "date": day.isoformat(),
            "updated_at": "", "models": {}}


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
           now: datetime | None = None) -> bool:
    """把一次成功请求的 usage 记到当天（北京时间）聚合文件；失败不抛给调用方"""
    if not isinstance(usage, dict):
        return False
    parsed = parse_usage(usage)
    if not parsed["prompt_tokens"] and not parsed["completion_tokens"]:
        return False
    local = _cn(now)
    peak = is_peak(local)
    day = local.date()
    with _LOCK:
        data = _read_day(day)
        key = model or "unknown"
        m = data["models"].setdefault(key, {
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
               base_url=base_url)
    return True


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


def snapshot(days: int = 14, now: datetime | None = None,
             base_url: str = "") -> dict:
    """近 N 天（含今天）的聚合结果 + 当前时段信息；不做网络请求"""
    local = _cn(now)
    days = max(1, min(int(days or 14), 400))
    start = local.date() - timedelta(days=days - 1)
    totals = {k: 0 for k in ("requests", "prompt_tokens", "completion_tokens",
                             "total_tokens", "cache_hit_tokens", "cache_miss_tokens")}
    totals["cost_cny"] = 0.0
    by_model: dict[str, dict] = {}
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
            if not data.get("models") and path.exists() and path.stat().st_size == 0:
                corrupted.append(path.name)
            models = []
            day_totals = {k: 0 for k in totals}
            for name, raw in data["models"].items():
                if not isinstance(raw, dict):
                    continue
                s = _model_summary(name, raw)
                models.append(s)
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
            day_totals["cache_hit_rate"] = (
                round(day_totals["cache_hit_tokens"] / day_totals["prompt_tokens"] * 100, 1)
                if day_totals["prompt_tokens"] else 0.0)
            day_totals["date"] = day.isoformat()
            day_totals["models"] = models
            out_days.append(day_totals)
            for k, v in day_totals.items():
                if k in totals and isinstance(v, (int, float)):
                    totals[k] += v

    for acc in by_model.values():
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
    """清空本地用量统计（只删 usage 目录下的 json，不碰配置与会话）"""
    removed = 0
    with _LOCK:
        for path in usage_dir().glob("*.json"):
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
