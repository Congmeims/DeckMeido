"""状态栏短句：缓存、忙碌跳过、失败回模板。

单独成模块的原因：main.py 依赖 FastAPI，而状态文案的规则应该能在纯 Python
环境里单测。这里不读会话、不读记忆、不写任何文件，输入只有状态摘要。
"""
from __future__ import annotations

import asyncio
import re
import time

# 隐藏提示词默认值：只约束文风；配置 [agent] status_prompt 可覆盖
DEFAULT_STATUS_PROMPT = (
    "你为桌面助手写状态栏短句：只输出一句话，不超过 20 个汉字，"
    "语气自然、有感情；不自称 AI，不解释，不加引号、emoji 或换行。"
)

TTL = 600.0          # 状态 + 游戏 + 昵称 缓存 10 分钟
MAX_TOKENS = 40      # 输出上限，别抢正常对话的额度
TIMEOUT = 3.0        # 3 秒拿不到就回模板，状态栏不能空

_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_inflight: dict[tuple[str, str, str], asyncio.Task] = {}


def prompt_text(cfg: dict | None = None) -> str:
    """从 [agent] 段取提示词；缺失或为空时回默认值"""
    text = str(((cfg or {}).get("status_prompt")) or "").strip()
    return text or DEFAULT_STATUS_PROMPT


def template(state: str, game: str, name: str) -> str:
    """本地模板：模型不可用/超时/忙碌时的唯一文案来源"""
    if state == "busy":
        return f"{name}，正在处理…"
    if state == "error":
        return f"{name}，出了点小状况…"
    if game:
        return f"{name}，在《{game}》里待命~"
    return f"{name}，正在待命~"


def clean_text(raw: str) -> str:
    """只收一句话、20 字以内；超长/多行按失败处理，宁可回模板"""
    text = (raw or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip().strip(" \t\"'“”‘’")
    line = re.sub(r"[ \t]+", "", line)
    return line if 0 < len(line) <= 20 else ""


async def _ask(key, state, game, name, prompt, chat, fallback):
    """真正请求模型；任何失败都回落到模板，日志只记耗时与来源"""
    started = time.monotonic()
    source = "template"
    text = fallback
    try:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"当前状态：{state}；游戏：{game or '无'}；称呼：{name}"},
        ]
        msg = await asyncio.wait_for(chat(messages), timeout=TIMEOUT)
        candidate = clean_text(str((msg or {}).get("content") or ""))
        if candidate:
            text = candidate
            source = "ai"
            _cache[key] = (time.monotonic(), text)
    except Exception:
        # 超时 / 无 Key / 模型不支持：一律走模板，绝不把异常抛给状态栏
        source = "template"
        text = fallback
    finally:
        cost = int((time.monotonic() - started) * 1000)
        print(f"[DeckMeido] 状态栏文案 source={source} 耗时={cost}ms", flush=True)
    return text, source


async def generate(*, state: str = "", game: str = "", game_confidence: str = "",
                   nickname: str = "", persona_name: str = "", prompt: str = "",
                   busy: bool = False, configured: bool = True,
                   chat=None) -> dict:
    """返回 {"text", "source": "ai"|"template", "cached": bool}

    - 状态：idle / busy / error；busy 时不调用模型，直接给模板
    - 游戏名只在置信度 high（或未标注）时使用
    - 昵称缺失时用 persona_name，再缺就用 Meido
    - 没有 Key / chat 为 None 也直接给模板
    """
    name = (nickname or "").strip() or (persona_name or "").strip() or "Meido"
    name = name[:20]
    g = (game or "").strip()[:40]
    if (game_confidence or "").strip().lower() not in ("", "high"):
        g = ""           # 低置信度不透露游戏名，避免说错
    st = (state or "").strip().lower()
    if st not in ("idle", "busy", "error"):
        st = "busy" if busy else "idle"
    if busy:
        # 正在跑一轮对话时不要抢带宽 / 串行等待，直接给模板
        st = "busy"

    fallback = template(st, g, name)
    key = (st, g, name)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < TTL:
        return {"text": hit[1], "source": "ai", "cached": True}
    if not configured or chat is None or st == "busy":
        return {"text": fallback, "source": "template", "cached": False}

    task = _inflight.get(key)
    shared = task is not None and not task.done()
    if not shared:
        task = asyncio.create_task(
            _ask(key, st, g, name, prompt or DEFAULT_STATUS_PROMPT, chat, fallback))
        _inflight[key] = task
    try:
        text, source = await task
    finally:
        if not shared:
            _inflight.pop(key, None)
    return {"text": text, "source": source, "cached": shared}
