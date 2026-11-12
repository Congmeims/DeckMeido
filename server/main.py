"""DeckMeido 后端服务：FastAPI + WebSocket 广播 + LLM 对话"""
from __future__ import annotations

import asyncio
import base64
import glob
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

import tomllib

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends, UploadFile, Response, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .llm import LLMClient
from . import approval
from . import apicfg, audit, authguard, audiomute, depcheck, devlog, fszones, i18n, media, memory, menu, netcfg, netmode, onebot, pairing, paths, persona, policy, power, presets, prompts, remote_input, remote_terminal, selfcheck, singleton, sleepmode, status_line, sudo_auth, tools, updater, usage, wake
from .session_store import store
from . import session_store
from .tts import (LANGUAGES, SPEAKER_LABELS, TTS, TTSError, normalize_speaker,
                  speaker_from_legacy)
from .stt import STT, STTError, record_supported
from .tomlcfg import ConfigWriteError, write_config

BASE_DIR = Path(__file__).resolve().parent
# 配置在数据根（userdata/config.toml）；数据根不可用时 paths.data_dir 会直接报错
CONFIG_PATH = paths.config_path()
STATIC_DIR = BASE_DIR / "static"

# 安装目录（install.sh 会把代码放在这里，并写入 VERSION 文件）
INSTALL_DIR = BASE_DIR.parent
# 运行时版本：进程启动时读一次，之后不再变。安装/升级换掉磁盘文件后，
# 这个值仍是旧进程的版本 —— 界面必须把「运行版本」和「磁盘版本」分开显示，
# 否则会出现「磁盘已是 0.7.2、界面还写 0.7.0」的错觉。
VERSION = updater.current_version()
# 部署时的短提交号（安装目录 COMMIT）与展示用版本 0.7.5-f2931ae。
# 只在显示上使用，更新比较仍然只看 VERSION 的语义版本。
COMMIT = updater.current_commit()
VERSION_DISPLAY = updater.version_display(VERSION, COMMIT)


def disk_version() -> str:
    """磁盘上 VERSION 文件里的版本（每次现读，用于和运行版本比对）"""
    for p in (INSTALL_DIR / "VERSION", BASE_DIR / "VERSION"):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            val = line.strip().lstrip("v")
            if val:
                return val
    return ""


def version_state() -> dict:
    """版本三元组：运行中 / 磁盘上 / 是否一致（三个接口共用同一份口径）"""
    disk = disk_version()
    return {
        "version": VERSION,
        "running_version": VERSION,
        # 展示用版本（含部署时的短提交号），更新比较仍只看上面的语义版本
        "version_display": VERSION_DISPLAY,
        "commit": COMMIT,
        "disk_version": disk,
        "version_match": bool(disk) and disk == VERSION,
        "pid": os.getpid(),
    }


# ---------- 配置 ----------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(BASE_DIR / "config.example.toml", CONFIG_PATH)
    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # 配置是手改的文件，语法错必须一眼看出是哪一行：
        # 打印「路径 + 行号 + 该行原文 + 解析器原因」，然后非零退出。
        match = re.search(r"line (\d+)", str(exc))
        lineno = getattr(exc, "lineno", None) or (int(match.group(1)) if match else 0)
        detail = str(CONFIG_PATH)
        if lineno:
            detail += f" 第 {lineno} 行"
            src_lines = text.splitlines()
            if 0 < lineno <= len(src_lines):
                detail += f"：{src_lines[lineno - 1].strip()}"
        print(f"[DeckMeido] 配置文件语法错误：{detail}（{exc}）",
              file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    # 旧配置里的 token 键会被忽略：令牌已下线，局域网改用设备配对 Cookie。
    return cfg


def chat_voice_enabled(cfg: dict | None = None) -> bool:
    """「对话与语音」整组总开关（[agent] chat_voice，默认开）。

    关闭后带画面询问、语音输入、R2 按住说话一并停用；分项配置本身保留，
    重新打开总开关就恢复到原来的分项选择，不会被总开关覆盖成 false。
    """
    return bool((cfg if cfg is not None else CFG).get("agent", {}).get("chat_voice", True))


def update_config(updates: dict):
    """把 {section: {key: value}} 写回 config.toml（保留注释），并重载运行配置"""
    write_config(CONFIG_PATH, updates, apply=apply_config)


def save_config(updates: dict):
    """接口层写配置：失败转成 500 + 明确原因，不能让前端以为保存成功了"""
    try:
        update_config(updates)
    except ConfigWriteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入配置失败：{exc}") from exc


CFG = load_config()
# 远程终端默认关闭；启动时把 [terminal] 段的限流/超时参数装进管理器
remote_terminal.manager.configure(CFG)
# [security].sudo_ttl：sudo 授权密码在内存里的复用时长（默认 10 分钟）
sudo_auth.configure(CFG)

# 离线模式：[runtime].offline_mode。尽早装好，后面的 LLM / 更新 / 下载
# 都会在经过 netmode 时快速失败。默认关闭，关闭时行为与没有这个开关时一致。
netmode.configure(CFG)

# 开发者模式与统一脱敏：尽早装好，后面的启动日志、traceback、uvicorn 访问日志
# 都会经过同一层脱敏；开关本身按 [runtime].dev_mode 决定是否记录细节。
devlog.install_redacting_streams()
devlog.configure(CFG)

# 单实例守卫：必须在 init_sessions() 之前跑。init_sessions 在导入期就会
# 读/写会话文件，如果本进程注定绑定端口失败却先把内存里的空历史写了盘，
# 正在服务的那一份就会「丢记录」。
# 端口来源优先级：环境变量 > 配置，和 uvicorn 命令行保持一致。
_PORT_EARLY = os.environ.get("DECKMEIDO_PORT") or CFG["server"].get("port", 18765)
singleton.guard(
    int(_PORT_EARLY),
    allow_second=bool(os.environ.get(singleton.ENV_BYPASS)),
)


def _system_boot_id() -> str:
    """内核这次启动的标识：真重启后才会变。

    用来给 pending_intent 对账：重启前登记的 reboot 变成 executed、
    sudo_password 变成 expired，老密码不会在几十轮之后又被当成有效授权。
    非 Linux（开发机测试）退回进程级 BOOT_ID。
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8").strip() or singleton.BOOT_ID
    except Exception:
        return singleton.BOOT_ID

# 多组 API 配置：旧版单个 base_url / api_key 会在读取时折成一条「默认」并写回，
# 之后 Key 只存在 apis 里。没有任何配置时 active 为 None，LLMClient 得到空地址/空 Key，
# 对话入口据此显示「还没有配置模型」，而不是拿空 Key 去请求再变成 500。
API_STATE = apicfg.load(CFG, CONFIG_PATH)
_active_api = apicfg.active(API_STATE) or {}
llm = LLMClient(
    base_url=_active_api.get("base_url", ""),
    api_key=_active_api.get("api_key", ""),
    api_id=_active_api.get("id", ""),
    api_name=_active_api.get("name", ""),
    model=CFG["llm"]["model"],
    # 每次对话都由 run_agent 按上下文剩余空间显式传入 output 额度，
    # 这里不再从配置读一个全局默认值
    max_tokens=None,
    timeout=CFG["llm"].get("timeout", 60),
    # 留空则完全不传，交由服务商默认行为
    reasoning_effort=str(CFG["llm"].get("reasoning_effort", "high")),
)
# 地址填成 DeepSeek（含手抄的 /v1）时把模型名归一，免得带着 gpt-* 去请求
llm.normalize()

PERSONA = persona.load()

# ---------- 界面语言 ----------
# [ui] language = "zh" / "en"（默认 zh）。只切界面文案与内置提示词模板的
# 默认值，用户自己改过的模板照旧优先；不涉及模型、路径等运行参数。
# 英文文案是机翻版本，集中放在 server/i18n.py，避免散落进逻辑里。
def ui_language(cfg: dict | None = None) -> str:
    raw = str((cfg or CFG).get("ui", {}).get("language", "zh") or "").strip().lower()
    return "en" if raw in ("en", "english", "en-us", "en_us") else "zh"

UI_LANG = ui_language()

# 「截屏询问」的提示词模板。插件点按钮时会把这段连同当前画面发过来，
# 所以模板本身放在配置里，用户可以在设置页随时改（例如换个问法、加一条自己的要求）。
DEFAULT_ASK_TEMPLATE = (
    "看我卡在哪一步，说说现在这个画面里我该做什么。"
    "已经能从上图判断就不要搜索；最多搜 2 次，得到一个答案就停。"
    "再总结成几条可以直接照做的步骤，200 字以内。"
    "不要剧透：只讲眼前这一步怎么过，不要提前说后面的剧情、结局或其他关卡的解法。"
)
# 英文模式的内置默认（机翻）：与上面中文版语义一致，同样是「不剧透、少搜索」
DEFAULT_ASK_TEMPLATE_EN = (
    "Look at where I'm stuck and tell me what to do in this screen. "
    "If the screenshot is enough, don't search; search at most twice and stop at one answer. "
    "Then summarize it as a few steps I can follow directly, under 200 words. "
    "No spoilers: only how to get past this step, no later story, endings, or other levels."
)


def default_ask_template() -> str:
    return DEFAULT_ASK_TEMPLATE_EN if UI_LANG == "en" else DEFAULT_ASK_TEMPLATE


ASK_TEMPLATE = str(CFG.get("agent", {}).get("ask_template") or "").strip() or default_ask_template()
# 「截图翻译」的提示词模板。以前写死在插件前端，用户改不了；现在和截屏询问一样
# 存进 [agent] translate_template，插件设置区保存后下一次翻译就用新的。
DEFAULT_TRANSLATE_TEMPLATE = (
    "把画面上的外文原文先照抄出来，再逐句翻译成简体中文。"
    "已经是中文就原样说明，不要翻译。"
    "只翻译、不解释剧情、不剧透，保持原文的换行和顺序。"
)
DEFAULT_TRANSLATE_TEMPLATE_EN = (
    "Copy the original text on screen first, then translate it sentence by sentence into English. "
    "If it is already English, say so and do not translate. "
    "Translate only: no story explanation, no spoilers, keep the original line breaks and order."
)


def default_translate_template() -> str:
    return DEFAULT_TRANSLATE_TEMPLATE_EN if UI_LANG == "en" else DEFAULT_TRANSLATE_TEMPLATE


TRANSLATE_TEMPLATE = (
    str(CFG.get("agent", {}).get("translate_template") or "").strip()
    or default_translate_template()
)
# 单轮最多几轮工具往返。以前默认 0 = 不限，模型一旦陷进「截图 → 没看懂 → 再截图」
# 这种循环就会一直跑下去，只能靠用户点终止。12 步对稍微复杂一点的活儿（找文件、
# 改代码、跑测试、修错）往往不够，走到一半就被截断；现在放到 50 步，
# 到顶时仍按既有逻辑补一句「到了单轮上限」，把控制权交回用户。
DEFAULT_MAX_TOOL_ROUNDS = 50
MAX_TOOL_ROUNDS = int(CFG["agent"].get("max_tool_rounds", 0)) or DEFAULT_MAX_TOOL_ROUNDS

# 上下文窗口（真实模型支持的总 token 数）。配 0 表示自动按模型名推断。
# 用量优先取服务商返回的 usage.prompt_tokens，拿不到再按字符估算。
CONTEXT_WINDOW = int(CFG["agent"].get("context_window", 0))
COMPACT_RATIO = float(CFG["agent"].get("compact_ratio", 0.95))
# 压缩完后，最近保留的内容大约占窗口的这个比例。0.3 ≈ 压缩掉七成，
# 保证压一次能撑很久，不会「刚压完又满」——这是压缩频率过高的根源。
COMPACT_TARGET_LEFT = float(CFG["agent"].get("compact_target_left", 0.3))
# 单次输出上限（0 / 留空 = 用满上下文剩余空间）。默认跟随上下文窗口，
# 这样长答复不会再被 4096 这种固定值硬截断。
MAX_OUTPUT_TOKENS = int(CFG["llm"].get("max_output_tokens", 0) or 0) or CONTEXT_WINDOW
# 撞到输出上限时最多自动续写几轮（0 = 不续写）
MAX_CONTINUATIONS = int(CFG["llm"].get("max_continuations", 3))
# 最近一次请求的 token 用量：由服务商 usage 或字符估算更新
_ctx_used = 0
# 估算结果的缓存：(会话 sid, 历史条数, 估算值)。每次 /api/status 都全量重算一遍
# 是最浪费的一处 —— 状态接口每秒都在被轮询，而历史只在对话推进时才会变。
# 条数一变就失效，所以「切会话」「追加消息」都会自动重算，不会用到过期值
_est_cache: tuple[str, int, int] = ("", -1, 0)


def _cached_estimate() -> int:
    """按当前会话估算上下文用量，历史没变就复用上次的结果"""
    global _est_cache
    sid = store.current_sid
    n = len(store.history)
    if _est_cache[0] == sid and _est_cache[1] == n:
        return _est_cache[2]
    val = _estimate_tokens(_build_llm_msgs(store.history, SYSTEM_PROMPT, sid))
    _est_cache = (sid, n, val)
    return val


def _guess_context_window(model: str) -> int:
    """按模型名推断上下文窗口，识别不出时给个保守值"""
    m = (model or "").lower()
    table = [
        ("gemini", 1_000_000),
        ("claude", 200_000),
        ("gpt-4.1", 1_000_000),
        ("gpt-4o", 128_000),
        ("gpt-4", 128_000),
        ("o1", 200_000), ("o3", 200_000), ("o4", 200_000),
        ("qwen-long", 10_000_000),
        ("qwen", 128_000),
        ("kimi", 256_000),
        ("glm-4.6", 200_000), ("glm-4.5", 128_000), ("glm", 128_000),
        ("deepseek", 128_000),
        ("llama", 128_000),
        ("mistral", 128_000),
    ]
    for key, size in table:
        if key in m:
            return size
    return 128_000


def context_window() -> int:
    return CONTEXT_WINDOW or _guess_context_window(llm.model)


# ---------- 上下文压缩游标（关键：只压缩「发给模型的上下文」，绝不删界面历史） ----------
# 旧实现把 history 原地替换成「摘要 + 最近 4 条」，compress 完还立刻落盘，
# 于是用户屏幕上已有的几十轮对话被一起抹掉——表现就是「内容不见了」。
# 现在改成游标模型：
#   history        永远只追加，完整记录 = 界面显示 + 磁盘存档
#   store.compact_map  每个会话一份 {"summary": 旧对话摘要, "upto": 摘要覆盖到第几条}
# 发给模型时 = 摘要(system) + history[upto:]，History 本身一条都不动。
# 游标放在 session_store 里，落盘时能一起写进会话文件（见 session_store.write_session）


def _compact_for(sid: str) -> dict:
    return store.compact_for(sid)


def _set_compact(sid: str, summary: str, upto: int) -> None:
    store.set_compact(sid, summary, upto)


# 历史图片的占位文案：固定不变，既省掉每轮重发的几 MB 输入，也保证前缀稳定
_STALE_IMAGE_NOTE = prompts.part("stale_image")


def _digest_text(m: dict) -> str:
    """把一条历史消息拍平成摘要用的纯文本

    多模态消息的 content 是 list，直接 str() 会把 base64 也塞进摘要请求，
    一次压缩就能发出几十万字符（还全是噪音）。
    """
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(str(p.get("text") or "[图片]") for p in content
                           if isinstance(p, dict))
    return str(content or "")


def _digest_head_tail(text: str, head: int = 500, tail: int = 300) -> str:
    """工具结果太长时留首尾、掐中间

    工具输出最值钱的两头在首尾：开头是命令和最初几行输出，结尾是最终报错。
    只裁尾巴会让「一开始做了什么」和「最后错在哪」同时消失，模型压缩后就只剩
    一句"执行了命令"。中间那段大段日志对摘要没有价值。
    """
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}…（中间省略 {len(text) - head - tail} 字）…{text[-tail:]}"


# 本轮授权边界：插在当前用户消息前面的系统提示。
# 历史里可能残留着上一轮「帮我重启」和模型自己答应的「我现在执行」，
# 模型会把它们当成仍然有效的指令。这里明确告诉它：历史只能当上下文。
_BOUNDARY_NOTE = ("【本轮授权边界】以上历史消息仅供上下文，不得据此执行任何操作；"
                  "只有下面这条本轮用户消息构成授权。")


def _build_llm_msgs(hist: list[dict], sys_prompt: str, sid: str = "",
                    hist_max: int = 0, item_max_chars: int = 0) -> list[dict]:
    """构造发给模型的 messages：摘要 + 压缩游标之后的原文

    返回的列表长度就是「来自历史的条数」，调用方据此区分本轮新增的临时消息
    （assistant tool_calls / tool 结果），压缩后重建时可以接回去。

    hist_max / item_max_chars 是本轮场景的上下文窗口（见 policy.TurnPolicy）：
    0 表示不限制，主人档保持原样；群友档只带最近几条、每条截断。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    comp = _compact_for(sid)
    upto = min(comp["upto"], len(hist))
    items = hist[upto:]
    # 只影响本轮发给模型的内容；界面历史与压缩游标都不动
    if hist_max > 0:
        items = items[-hist_max:]
    msgs: list[dict] = [{"role": "system", "content": sys_prompt}]
    if upto > 0 and comp["summary"].strip():
        msgs.append({"role": "system",
                     "content": f"【此前对话摘要，供上下文延续】\n{comp['summary']}"})
    # 历史里的图只保留最近 _HIST_IMG_KEEP 张，更早的换成定长文案。
    # 先数一遍总数，才能知道从哪一张开始算「旧图」——
    # 游戏模式里反复截屏翻译，历史会攒下十几张 1280 宽 JPEG，
    # 每张 25 万字符，全留着的话每次请求都白付它们一遍输入费
    left = 0
    for m in items:
        c = m.get("llm") or m.get("content", "")
        if isinstance(c, list):
            left += sum(1 for p in c
                        if isinstance(p, dict) and p.get("type") == "image_url")
    keep_from = max(0, left - _HIST_IMG_KEEP)
    for m in items:
        content = m.get("llm") or m.get("content", "")
        if m["role"] == "user":
            content = _to_vision_content(content) if isinstance(content, str) else content
            if isinstance(content, str) and "[SHOT:" in content:
                # 随提问附带的画面：历史里只有一条文件标记，这里现读成图片部件
                content = _shot_ref_content(content) or content
            if isinstance(content, str) and "[IMG:" in content:
                # @{} 引用的图片文件：同样只存 [IMG:路径] 标记，这里现读成图片部件
                content = _img_ref_content(content) or content
        # 老图换成固定文案：留最近几张够模型「记得刚才看过什么」，
        # 再往前的画面早已翻篇，每轮重发只是白烧输入费
        if isinstance(content, list):
            kept = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    if keep_from > 0:
                        keep_from -= 1
                        kept.append(_restale_image_part(p))
                        continue
                kept.append(p)
            content = kept
        # 历史里存下来的画面若过大（旧版截的是无损 PNG，一张 12MB），换成占位符。
        # 网关对请求体有上限，超了会被上游截断并返回 503，整轮提问跟着失败；
        # 这些图只是当时那一轮的参考，用一句说明代替即可
        content = _shrink_oversized_images(content)
        # 不支持视觉的模型（DeepSeek 纯文本系列），历史里那几张旧画面本来也看不见：
        # 换成固定文案，省输入费的同时让前缀每轮都一模一样。
        # 支持视觉的（deepseek-v4-pro、GPT/Claude/Qwen 等）保留原图，别再误替换
        if not llm.supports_vision and isinstance(content, list):
            content = [_restale_image_part(p) for p in content]
        # 群友档的逐条窗口：每句只留前 N 字，尾部补省略号，避免半句突然断掉
        if (item_max_chars > 0 and isinstance(content, str)
                and len(content) > item_max_chars):
            content = content[:item_max_chars] + "…"
        # 历史里还存了 ts / events 供界面回放，发给模型时只保留 role 和 content
        msgs.append({"role": m["role"], "content": content,
                     "tool_calls": m.get("tool_calls"),
                     "tool_call_id": m.get("tool_call_id")})
    # 只在最后一条用户消息（本轮提问）前插一条边界说明：
    # 历史里已经有同款提示时不再重复堆叠，别把 prompt 撑爆
    if not any(isinstance(m.get("content"), str)
               and m["content"].startswith("【本轮授权边界】") for m in msgs):
        last_user = next((i for i in range(len(msgs) - 1, 0, -1)
                          if msgs[i].get("role") == "user"), None)
        if last_user is not None:
            msgs.insert(last_user, {"role": "system", "content": _BOUNDARY_NOTE})
    return _drop_orphan_tools(msgs)


# 上一轮终止时，交接说明里最多列几条工具，再多模型也读不出重点
_ABORT_NOTE_MAX = 12


def _tool_results_digest(tool_log: list[dict] | None, max_chars: int = 300) -> str:
    """把本轮已执行完的工具连结果一起列出来（终止 / 超时落盘用）

    以前终止和超时只留一句「已终止」「超时了」，做过什么、结果是什么全丢：
    用户事后再看会话，看不到那条 find 跑出了什么、文件到底改了没有，
    下一轮模型也只能重做一遍。这里每条附上结果的摘要（去掉多余空行、截断）。
    """
    done = [t for t in (tool_log or []) if isinstance(t, dict) and not t.get("interrupted")]
    if not done:
        return ""
    lines = []
    for t in done[:32]:
        name = t.get("name") or "?"
        summary = str(t.get("summary") or "").strip()
        head = f"- {name}：{summary}" if summary else f"- {name}"
        out = str(t.get("output") or "").strip()
        out = re.sub(r"\n{2,}", "\n", out)
        if len(out) > max_chars:
            out = out[:max_chars] + "…"
        lines.append(head + (f"\n  结果：{out}" if out else ""))
    if len(done) > 32:
        lines.append(f"- 还有 {len(done) - 32} 步，略")
    return "\n".join(lines)


def _aborted_note(hist: list[dict]) -> str:
    """上一轮是被用户终止的话，给模型一条交接说明

    终止时 history 里只留下「已经说出口的正文 + 工具块」，没有 tool 调用与结果，
    模型看到自己的话断在半路，下一句就会接着往下做（用户看到的就是「我终止了
    它还在跑」）。这里把做过什么、没做什么补给它，并明确要求不许续做。

    只在「最后一条是终止的 assistant」时生效：中间那些早已翻篇的终止轮次
    不该在之后每一轮都再念一遍。
    """
    if not hist:
        return ""
    last = hist[-1]
    if last.get("role") != "assistant" or not last.get("aborted_turn"):
        return ""
    done = [t for t in (last.get("tool_log") or []) if isinstance(t, dict)]
    lines = [prompts.part("abort_note")]
    finished = [t for t in done if not t.get("interrupted")]
    cut = [t for t in done if t.get("interrupted")]
    if finished:
        lines.append(prompts.part("abort_note_done"))
        for t in finished[:_ABORT_NOTE_MAX]:
            name = t.get("name") or ""
            summary = str(t.get("summary") or "").strip()
            line = f"- {name}：{summary}" if summary else f"- {name}"
            # 带上结果：下一轮模型才知道「这步已经做成了什么」，不会照着重跑一遍
            out = re.sub(r"\n{2,}", "\n", str(t.get("output") or "").strip())
            if out:
                line += "\n  " + (out[:300] + "…" if len(out) > 300 else out)
            lines.append(line)
        if len(finished) > _ABORT_NOTE_MAX:
            lines.append(f"- 还有 {len(finished) - _ABORT_NOTE_MAX} 步，略")
    if cut:
        lines.append(prompts.part("abort_note_cut"))
        for t in cut[:_ABORT_NOTE_MAX]:
            name = t.get("name") or ""
            summary = str(t.get("summary") or "").strip()
            lines.append(f"- {name}：{summary}" if summary else f"- {name}")
    lines.append(prompts.part("abort_note_rule"))
    return "\n".join(lines)


# 单张图 base64 的上限。网关实测 1600 万字符必挂；给到 200 万字符（约 1.5MB 原图）
# 已经远超正常截图，留足余量的同时保证单个请求体不会把链路撑爆
_MAX_IMG_CHARS = 2_000_000
# 历史里最多保留几张原图，更早的换成定长文案。留 2 张是为了让「和上一张比」
# 这类追问还能成立；再往前的画面模型本来也用不上
_HIST_IMG_KEEP = 2
# 收图时的压缩门槛，比上限低得多：插件用 gst 抓的是 1280x800 JPEG q90，
# 一张游戏画面就有 1.8MB（base64 之后 240 万字符），稳稳越过上限被换成占位符。
# 而压到 1024 宽 q75 只有 160KB，看图问答根本看不出差别，所以统一在这里压一遍
_IMG_SOFT_BYTES = 300_000


def _shrink_oversized_images(content):
    """把超大的图片部分替换成文字说明，其余部分原样返回"""
    if not isinstance(content, list):
        return content
    out, changed = [], False
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            if len(url) > _MAX_IMG_CHARS:
                out.append({"type": "text", "text": prompts.part("image_too_large")})
                changed = True
                continue
        out.append(part)
    return out if changed else content


def _restale_image_part(part: dict) -> dict:
    """把历史里的图片换成固定的一句话（DeepSeek 用）

    历史消息里内联的 base64 是最容易让前缀缓存失效的东西：只要某张图在中间
    被压缩、替换或重新编码，它后面的整段历史都跟着失配。更实际的问题是——
    上下文里躺着几张几 MB 的旧画面，每一轮都在为它们重新付输入费，
    而模型真正需要看的只是「当时有过一张画面」。
    这里换成一段不带编号、不带时间的定长文案：同一张图每轮展开的结果都一样，
    前缀才稳；本轮新截的那张（不在历史里）仍然是原图。
    """
    if isinstance(part, dict) and part.get("type") == "image_url":
        return {"type": "text", "text": _STALE_IMAGE_NOTE}
    return part


def _drop_orphan_tools(msgs: list[dict]) -> list[dict]:
    """去掉没有配对的 tool 消息与 tool_calls，保证发出去的消息能通过接口校验

    OpenAI / DeepSeek 都要求：role=tool 的消息必须紧跟在带 tool_calls 的
    assistant 之后，且每个 tool_call_id 都要有对应的结果；反过来 assistant
    的 tool_calls 也必须有结果跟上。压缩游标恰好切在这一对中间，或者某条
    结果没落盘，就会留下半截 —— 接口直接 400，之后整段会话都发不出去。
    宁可丢掉这几条（摘要里还留着结论），也不能让整轮对话失败。
    """
    ids_with_result = {m.get("tool_call_id") for m in msgs if m["role"] == "tool"}
    # DeepSeek 思考模式下，带过工具调用的 assistant 必须把 reasoning_content
    # 原样回传，少一个字段就 400（见官方「思考模式 → 工具调用」）。这里统一补上：
    # 没存过的（切换过服务商、旧会话）就补空字符串
    keep_reasoning = llm.is_deepseek
    out: list[dict] = []
    for m in msgs:
        if m["role"] == "tool":
            # 上一句不是你请求的这次调用（中间被压缩/被插话），结果无处安放
            prev = out[-1] if out else None
            called = {tc.get("id") for tc in (prev or {}).get("tool_calls") or []}
            if m.get("tool_call_id") not in called:
                continue
            content = m.get("content")
            if keep_reasoning and not llm.supports_vision and isinstance(content, list):
                content = [_restale_image_part(p) for p in content]
            m = {"role": "tool", "tool_call_id": m["tool_call_id"], "content": content}
            out.append(m)
            continue
        calls = m.get("tool_calls")
        if calls:
            # 只保留真有结果的调用；一个都不剩就把这条 assistant 降级成普通回复，
            # 否则它挂着空的 tool_calls，接口同样不认
            kept = [tc for tc in calls if tc.get("id") in ids_with_result]
            if not kept:
                if not str(m.get("content") or "").strip():
                    continue
                m = {"role": m["role"], "content": m["content"]}
            else:
                m = {**m, "tool_calls": kept}
                if keep_reasoning:
                    m.setdefault("reasoning_content", "")
        else:
            m = {"role": m["role"], "content": m["content"]}
        out.append(m)
    return out



# 单条工具结果进模型上下文的上限（字符）。只影响「发给模型的那份」，
# 界面上展示的、history 里存的都还是完整结果，回头翻记录照样能看到全文
TOOL_TEXT_CAP = 24000


def _cap_tool_text(text: str) -> str:
    """把过长的工具结果压到上限内：留开头和结尾，中间标出省略了多少字

    留尾巴是为了报错信息 —— 命令刷屏几百行时，真正有用的往往是最后那几行
    （退出码、异常栈、失败的包名）。
    """
    text = str(text or "")
    if len(text) <= TOOL_TEXT_CAP:
        return text
    head = TOOL_TEXT_CAP * 2 // 3
    tail = TOOL_TEXT_CAP - head
    return (text[:head]
            + f"\n…（结果共 {len(text)} 字，中间省略 {len(text) - TOOL_TEXT_CAP} 字）…\n"
            + text[-tail:])


def _estimate_tokens(msgs: list[dict]) -> int:
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，另加每条消息的固定开销"""
    total = 0
    for m in msgs:
        content = m.get("content")
        # 多模态消息的 content 是 list，图片以 base64 形式挂在里面。直接 str()
        # 会把整串 base64 当文本数：一张 1280x800 的 PNG 有 1600 万字符，
        # 估出来四百多万 token，于是每轮都判定「该压缩了」，上来就把对话压成摘要。
        # 图片按固定额度计：主流模型对一张普通图收 800~1600 token 左右。
        if isinstance(content, list):
            text_parts, images = [], 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    images += 1
                elif part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
            total += images * 1500
            text = "".join(text_parts)
        else:
            text = str(content or "")
        text += str(m.get("reasoning_content") or "")
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        total += cjk + (len(text) - cjk) // 4 + 8
        for tc in m.get("tool_calls") or []:
            total += len(str(tc.get("function", {}).get("arguments") or "")) // 4 + 8
    return total


def _split_usage(usage: dict) -> int:
    """从 usage 里取本次请求真正占用的输入 token"""
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)



def _persona_style_block(include_traits: bool = False) -> str:
    """把「人设」设置项拼成一段语气约束，挂在系统提示最后

    语气单独成段、放在最后，是因为前面几十行都是 SteamOS/工具/审批规则，
    人设夹在开头会被淹没：同一份配置，模型每次即兴发挥出来的性格都不一样
    （一会儿正常、一会儿高冷、结尾突然带「喵」）。这里把称呼、语气、口癖
    拆成明确条目，并列出禁用词，让跨会话的表现稳定下来。
    """
    style = PERSONA.get("style") or {}
    name = str(PERSONA.get("name") or "DeckMeido").strip()
    address = str(style.get("address") or "主人").strip()
    tone = str(style.get("tone") or "").strip()
    ending = str(style.get("ending") or "").strip()
    suffix = str(style.get("suffix") or "").strip()
    forbid = [str(x).strip() for x in (style.get("forbid") or []) if str(x).strip()]
    lines = [prompts.part("persona_style_head")]
    if include_traits:
        # 群档默认只有提示词里那句「保持和主人设一致」，模型其实不知道人设是什么。
        # 这里只共享公开的人设描述，不共享 extra（可能写了主人的私事）。
        traits = str(PERSONA.get("traits") or "").strip()
        if traits:
            lines.append(f"- 人设：{traits}")
    lines.append(f"- 你的名字是「{name}」，始终用「{address}」称呼用户，不要换成别的称呼。")
    if tone:
        lines.append(f"- 语气：{tone}")
    if ending:
        lines.append(f"- 句尾习惯：{ending}")
    if suffix:
        lines.append(f"- 落款：{suffix}")
    if forbid:
        lines.append("- 以下词句一次都不许出现（包括句尾）：" + "、".join(f"「{w}」" for w in forbid))
    lines.append(prompts.part("persona_style_rule"))
    return "\n".join(lines)


def build_system_prompt(workspace: Path | None = None) -> str:
    """拼系统提示：主体模板来自 prompts/system.md，人设/模式/记忆实时注入

    主体模板在 import 时就固定在内存里（见 prompts/__init__.py），这里只做
    .format；人设改了会重走一遍本函数，所以设置页保存后立刻生效。
    """
    root = workspace or tools.WORKSPACE
    name = PERSONA.get("name", "DeckMeido")
    traits = PERSONA.get("traits", "")
    extra = PERSONA.get("extra", "")
    # 三档权限模式的说明在 prompts/modes.md 里，按当前模式取一段
    mode_key = str(CFG.get("security", {}).get("permission_mode", "auto")).strip()
    mode_desc = prompts.part(f"perm_{mode_key}") or prompts.part("perm_auto")
    # 明确告知模型自己的多模态能力，消除「我到底能不能看图」的自我怀疑。
    # supports_vision 已按主流多模态模型名清单精确判断，命中就是真能看图。
    vision_note = prompts.part(
        "vision_multimodal" if llm.supports_vision else "vision_text_only")
    p = prompts.system_template().format(
        name=name, traits=traits, root=root, home=Path.home(),
        mode_desc=mode_desc, vision_note=vision_note,
    )
    if extra:
        p += f"\n补充人设：{extra}"
    # 语气约束放最后：这里的规则最贴近用户即将说的话，权重最高
    p += "\n\n" + _persona_style_block()
    return p


def _mode_block(gaming: bool) -> str:
    """当前运行模式段，接在 system prompt 的主体之后

    主体里两套模式是并列描述的，模型得自己猜现在在哪种；猜错的代价是直接调错
    工具（桌面模式拿 gamepad、游戏模式拿 desktop），用户看到的就是「点了没反应」。
    这里只写「当前生效的是哪一套」，不重复主体里已有的键位说明。

    只在这里追加、不改主体：当前模式切换时，受影响的前缀只到这一段为止，
    前面的长前缀照样命中缓存（同 _memory_block 的考虑）。

    文案在 prompts/modes.md，段首自带两个换行，用来和主体隔开。
    """
    return prompts.part("mode_gaming" if gaming else "mode_desktop")


def _memory_block() -> str:
    """长期记忆段，接在 system prompt 最末尾

    必须放在最末尾而不是开头：记忆会随使用增删，放在前面的话，只要加一条，
    后面整段几十行前缀就被打掉，DeepSeek 的缓存命中率会直接掉到 0。
    放在末尾时，一次记忆变动影响的只是这一小段之后的缓存（本来就没什么内容）。
    单条记忆每轮都按「类别 + 创建时间」稳定排序，同样的记忆展开结果一字不差。
    """
    text, ids = memory.block()
    if ids:
        # 只记「谁被注入过」，供记忆条数到上限时优先保留常用的那些
        memory.mark_used(ids)
    return text


SYSTEM_PROMPT = build_system_prompt()

_tts_cfg = CFG.get("tts", {})
tts = TTS(
    enable=bool(_tts_cfg.get("enable", False)),
    # 发音人：新键 speaker 优先；老配置的 engine/voice 由 TTS 内部映射
    speaker=_tts_cfg.get("speaker"),
    engine=_tts_cfg.get("engine"),
    voice=_tts_cfg.get("voice"),
    speed=int(_tts_cfg.get("speed", 170)),
    max_chars=int(_tts_cfg.get("max_chars", 300)),
    speed_scale=float(_tts_cfg.get("speed_scale", 1.0)),
    length_scale=float(_tts_cfg.get("length_scale", 1.0)),
    noise_scale=float(_tts_cfg.get("noise_scale", 0.667)),
    language=str(_tts_cfg.get("language", "auto") or "auto"),
    model_source=str(_tts_cfg.get("model_source", "auto") or "auto"),
    idle_unload_sec=float(_tts_cfg.get("idle_unload_sec", 600)),
    # 播报时系统音量过低就临时抬到可听水平，播完恢复（[tts] boost_volume）
    boost_volume=bool(_tts_cfg.get("boost_volume", True)),
    # 可选日文汉字词表："石川由依,東京大学" —— 中文句里的日文专名按 ja 读
    ja_terms=str(_tts_cfg.get("ja_terms", "") or ""),
)
# 自检页的补装组件要用同一个语音引擎实例（模型状态、下载进度都从它取）
selfcheck.set_tts(tts)
# 语音识别：模型放在数据根（<data>/models/sensevoice），不进插件包也不进 venv。
# 懒加载，第一次识别才建 recognizer
_stt_cfg = CFG.get("stt", {})
stt = STT(
    model_root=paths.stt_model_dir(),
    # 总开关关闭时语音输入整组停用（[stt] enable 本身保持用户原来的选择）
    enable=bool(_stt_cfg.get("enable", True)) and chat_voice_enabled(),
    # 下载源：auto（默认，国内镜像→GitHub）/ mirror / github / 自定义 URL
    model_source=str(_stt_cfg.get("model_source", "auto") or "auto"),
)
# 录音期间是否静音外放：默认开（按住说话时游戏外放会灌进麦克风），
# 设置里可关；工具缺失时 audiomute 自己静默降级，不影响录音
audiomute.set_enabled(bool(_stt_cfg.get("duck_output", True)))

# 智能音箱模式：常驻唤醒检测在后台跑，默认关闭（[wake] enabled=false）。
# 只复用现有 STT/TTS/对话入口，不新起独立进程；状态通过 /api/wake/state 与 WS 广播。
wake_service = wake.WakeService(
    model_root=paths.wake_model_dir(),
    transcribe=stt.transcribe,
    duck=audiomute.duck,
    restore=audiomute.restore,
    # 音乐走 Decky Music 自己的 MPRIS2 服务（server/media.py），只认固定动作
    # 唤醒时不再压低音乐：声音忽高忽低，改由录音静音接管
    media_action=media.execute,
    # 点播走 Decky Music 自带 provider 的搜索接口 + 它自己的队列
    media_resolve=media.resolve_song,
    media_random=media.resolve_random,
    media_play=media.play_plan,
    # 录音期间把音乐静音：喇叭里的歌声被麦克风收进去，会被当成用户的话去追问
    media_record_mute=media.mute_for_record,
    media_record_unmute=media.unmute_for_record,
    power_hold=lambda: power.keeper.hold("wake_mode", "智能音箱模式"),
    power_release=lambda: power.keeper.release("wake_mode"),
)
wake_service.apply(CFG.get("wake") or {})
tts.set_state_listener(wake_service.note_tts)
wake_service.set_tts_stop(tts.stop)


def apply_config(cfg: dict):
    """热更新运行配置（LLM / TTS / 权限模式 / 界面语言），无需重启"""
    global CFG, PERMISSION_MODE, PERSONA, SYSTEM_PROMPT, MAX_TOOL_ROUNDS, ASK_TEMPLATE
    global TRANSLATE_TEMPLATE, UI_LANG, API_STATE
    CFG = cfg
    # [runtime].offline_mode 热更新：保存即生效，所有出网入口下一次调用就走新值
    netmode.configure(cfg)
    # [terminal] 远程终端开关/限流参数热更新
    remote_terminal.manager.configure(cfg)
    # [security].sudo_ttl 热更新：授权复用时长改完即生效
    sudo_auth.configure(cfg)
    # [runtime].dev_mode 热更新：改完保存即生效，不用重启后端
    devlog.configure(cfg)
    devlog.log("config.reload", sections=sorted(cfg.keys()))
    # [ui] language 热更新：先换语言，内置模板默认值才会跟着切
    UI_LANG = ui_language(cfg)
    # 人设不在这里取：它存在 meidosoul.json，改完由 update_config 的调用方
    # 直接 reload_persona()，config.toml 里即使还留着旧的 [persona] 段也不再生效
    MAX_TOOL_ROUNDS = (int(cfg.get("agent", {}).get("max_tool_rounds", 0))
                       or DEFAULT_MAX_TOOL_ROUNDS)
    ASK_TEMPLATE = (str(cfg.get("agent", {}).get("ask_template") or "").strip()
                    or default_ask_template())
    TRANSLATE_TEMPLATE = (
        str(cfg.get("agent", {}).get("translate_template") or "").strip()
        or default_translate_template()
    )
    c = cfg["llm"]
    # 多组配置热切换：保存「当前使用」后立刻换到那一组，不用重启后端
    API_STATE = apicfg.from_config(cfg)
    active = apicfg.active(API_STATE) or {}
    llm.base_url = str(active.get("base_url") or "").rstrip("/")
    llm.api_key = str(active.get("api_key") or "")
    llm.api_id = str(active.get("id") or "")
    llm.api_name = str(active.get("name") or "")
    llm.model = c["model"]
    llm.reasoning_effort = str(c.get("reasoning_effort", "high"))
    llm.normalize()
    t = cfg.get("tts", {})
    was_enabled = tts.enable
    # 引擎/音色/模型源一变，apply 会释放已加载的 ONNX；其余参数直接热更新
    tts.apply(t)
    # 关掉播报时立刻掐断正在念的那句，不能等它自己读完。
    # apply_config 也可能在启动阶段（没跑事件循环）被调用，那时没有 loop 可建任务
    if was_enabled and not tts.enable:
        try:
            asyncio.get_running_loop().create_task(tts.stop())
        except RuntimeError:
            pass
    # 语音输入开关热更新；关掉时顺手把已加载的模型释放掉，别让它白占内存。
    # 总开关关闭时即使 [stt] enable 仍为 true，运行时也按停用处理
    s = cfg.get("stt", {})
    stt.enable = bool(s.get("enable", True)) and chat_voice_enabled(cfg)
    stt.model_source = str(s.get("model_source", "auto") or "auto")
    if not stt.enable:
        stt.unload()
    # 录音时静音外放：热更新；关掉时把可能还静着的输出立刻恢复
    audiomute.set_enabled(bool(s.get("duck_output", True)))
    # 智能音箱模式：开关 / 唤醒词 / 阈值热更新；关掉时 wake 服务自己退出监听
    wake_service.apply(cfg.get("wake") or {})
    # 睡眠模式：音乐停 + 会话空闲满 N 分钟后自动关机；开关热更新
    sleepmode.service.apply(cfg.get("sleep") or {})
    PERMISSION_MODE = approval.normalize_mode(
        cfg.get("security", {}).get("permission_mode", "auto"))
    # 自定义档的四组开关跟着热更新，保存后下一次工具调用就按新值判断
    global CUSTOM_PERM
    CUSTOM_PERM = tools.normalize_custom((cfg.get("security") or {}).get("custom"))
    # 文件工具的边界跟着权限模式走：full 不设限（与 run_command 一致），
    # 其余模式只能碰工作区。这里同步一份，热更新后立刻生效
    tools.path_bound = PERMISSION_MODE != "full"
    # 桌面模式下执行命令/亮遮罩时阻止息屏（热更新即生效，不必重启）
    wake_on = cfg.get("power", {}).get("keep_awake", True)
    power.keeper.enabled = bool(wake_on)
    if not power.keeper.enabled:
        power.keeper.release_all()
    ws_dir = str(cfg.get("workspace", {}).get("dir") or DEFAULT_WORKSPACE)
    ensure_workspace(ws_dir)
    SYSTEM_PROMPT = build_system_prompt()
    # QQ 通道跟着配置热更新：开关/地址/令牌/白名单改完就重连，不用重启后端
    _qq_apply_config(cfg.get("qq") or {})


def reload_persona() -> dict:
    """重新读 meidosoul.json 并重拼系统提示词，保存后立即生效

    人设存在独立文件里，不走 update_config / apply_config 那条热更新路径，
    由设置页和导入接口直接调用这里。
    """
    global PERSONA, SYSTEM_PROMPT
    PERSONA = persona.load()
    SYSTEM_PROMPT = build_system_prompt()
    return PERSONA


# 权限模式：manual=所有命令手动审批 / custom=按四组开关 / auto=危险命令才审批
# full=完全访问。custom 的四组开关见 tools.CUSTOM_GROUPS
PERMISSION_MODE = approval.normalize_mode(
    CFG.get("security", {}).get("permission_mode", "auto"))
# 自定义档的四个开关（True = 先问我）；只有 PERMISSION_MODE == "custom" 时才被读取
CUSTOM_PERM = tools.normalize_custom((CFG.get("security") or {}).get("custom"))
tools.path_bound = PERMISSION_MODE != "full"

# 工作区（默认 ~/MeidoSpace，可在设置里改）
DEFAULT_WORKSPACE = Path.home() / "MeidoSpace"
# 状态里要能看出「工作区为什么不可用」：以前 mkdir 失败被 except: pass 吞掉，
# 界面上只剩一句「目录不存在或不是文件夹」，用户没有任何可执行的信息。
WORKSPACE_OK = True
WORKSPACE_ERROR = ""


def ensure_workspace(path: str | None = None) -> bool:
    """切到工作区并确保目录存在；失败时记住原因，由状态接口带出"""
    global WORKSPACE_OK, WORKSPACE_ERROR
    target = str(path or DEFAULT_WORKSPACE)
    try:
        tools.set_workspace(target)
    except Exception as exc:
        WORKSPACE_OK = False
        WORKSPACE_ERROR = f"{target}：{exc}"
        print(f"[DeckMeido] 工作区不可用：{WORKSPACE_ERROR}", file=sys.stderr)
        return False
    WORKSPACE_OK = True
    WORKSPACE_ERROR = ""
    return True


ensure_workspace(CFG.get("workspace", {}).get("dir") or str(DEFAULT_WORKSPACE))

# ---------- 会话持久化 ----------
# 状态与落盘都在 session_store 里（store.current_sid / store.history / store.turn ...）。
# 这里保留原来的名字，调用点不用改；但这些都是「读 store」的视图，不是第二份状态。


def _turn_detached(epoch: int) -> bool:
    """这轮开始时快照的 epoch 是否已过期（会话被切走或重建）"""
    return store.turn_detached(epoch)


def _turn_tag() -> dict:
    """给广播消息挂上本轮所属会话号；前端据此丢弃不属于当前会话的流式片段。

    发起端自带的 turn_id 也一并带上：插件要认出「哪些 δ 是我这一轮的」，
    只看 sid 不行——同一会话里别的端提问也是这个 sid。
    """
    return store.turn_tag()


def _sess_path(sid: str) -> Path:
    return store.sess_path(sid)


# 会话号只允许普通字符；新接口会把 sid 拼进文件路径，必须先挡住 `../`
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _valid_session_id(sid: str) -> bool:
    return bool(sid) and _SESSION_ID_RE.fullmatch(sid) is not None


def save_current_session(touch: bool = True):
    """把当前会话历史写盘，标题取第一条用户消息"""
    store.save_current(touch=touch)


def _fresh_sid() -> str:
    """生成一个目录里绝对不存在的会话号（实现见 session_store）"""
    return store.fresh_sid()


def save_session_snapshot(sid: str, hist: list[dict]):
    """把指定快照写回指定会话文件（会话中途被切走时，用这招把结果送回原会话）"""
    return store.save_snapshot(sid, hist)


def _write_session(sid: str, hist: list[dict]):
    """原子写入一个会话文件（落盘策略见 session_store.write_session）"""
    store.write_session(sid, hist)


# ---------- 流式正文的增量落盘 ----------
# 正文原本是「整轮生成完才落盘」的，而一轮回答可能长达几分钟；这期间进程被杀
# （客户端退出、升级、OOM）就整段消失，用户看到的就是「我的回复不见了」。
# 下面在流式回调里按节流频率把半截正文写进会话的副档案（<sid>.side.json），
# 进程被杀最多丢 1 秒；下次启动 init_sessions 会把它恢复成「回答中断」的消息。
# 单独存一个小文件而不是塞进会话主文件：写入成本与会话长度无关，
# 200 轮以后的会话不会再因为「每 0.6 秒整份重写一次」把磁盘和 IO 线程拖死。

_STREAM_FLUSH_INTERVAL = 0.6   # 秒（越短丢得越少；写的是小文件，代价可接受）
_STREAM_FLUSH_CHARS = 200      # 或自上次落盘以来又累积这么多字
# events 槽：本轮到目前为止「屏幕上已经出现过」的有序内容块。
# 只存最后那段正文的话，一轮里先说话、再调工具、再说一段的，中途被杀就只剩最后一段
# 按 sid 各存一份：两轮并发（不同会话）时共用单槽会互相覆盖 —— A 的正文被 B 清掉，
# 终止时 A 交给下一轮的交接正文就空了；收尾时还会把 B 的正文写进 A 的文件
_stream_bufs: dict[str, dict] = {}


def _stream_buf(sid: str = "") -> dict:
    """取某个会话的流式缓冲（没有就建一个空的）"""
    sid = sid or store.turn.get("sid") or store.current_sid
    buf = _stream_bufs.get(sid)
    if buf is None:
        buf = {"sid": sid, "text": "", "last": 0.0, "written": 0, "parts": []}
        _stream_bufs[sid] = buf
    return buf


def _inflight_sids() -> set[str]:
    """事件循环线程里：所有「流式写过、还没正式收尾」的会话号"""
    return set(_stream_bufs)


def _inflight_snapshot(sid: str) -> dict | None:
    """给重连的前端一份「这一轮已经出现过什么」

    长任务断线重连时，握手只发 history：这一轮还没收尾，正文和思考记录
    都还没进历史，前端清空重建后那整块思维链就没了。这里把内存里的 parts
    （正文段 + 思维链/工具事件的有序块）一起带上，前端重建后再补回来。
    """
    buf = _stream_bufs.get(sid or "")
    if not buf:
        return None
    parts = _trim_parts(list(buf.get("parts") or []))
    if not parts and not (buf.get("text") or "").strip():
        return None
    return {"text": buf.get("text") or "", "parts": parts, "ts": time.time()}


def _write_streaming(sid: str, text: str, parts: list[dict] | None = None) -> None:
    """把半截正文写进会话的副档案（不碰主文件）

    parts 是「屏幕上已经出现过的全部内容」的有序块列表：
      {"type":"text","content":...}  ← 助手正文段
      {"type":"event","event":{...}} ← 思维链 / 工具时间线
    只存 text 的话，一轮里先说话、再调工具、再说一段的，中途被杀就只剩最后一段。

    真正的写在 IO 线程里排队执行（见 session_store._IO），开销是「一次几十 KB 的
    覆盖写」。以前这里要把整份会话读出来再整份写回，写一次的成本和会话长度成正比，
    200 轮以后一轮回答就能把那条单 IO 线程堵满，流式和落盘全跟着卡。
    """
    stamp = time.time()
    # 兜底再裁一次：调用方有的直接拿 buf["parts"] 传进来，不经过 _snapshot_parts
    streaming = {"text": text, "ts": stamp, "parts": _trim_parts(list(parts or []))}

    def _job() -> None:
        with store.lock:
            if sid in store.deleted_sids:
                return
            # 主文件还没落盘也没关系：副档案是独立文件，恢复时两边各自读，
            # 不再需要「以磁盘那份为底」来拼一个完整会话
            store.write_stream_side(sid, stamp, streaming)

    session_store.io_submit(_job)


def _stream_would_flush(text: str, sid: str = "") -> bool:
    """按 persist_stream 的同一套节流规则预判这次会不会真的落盘

    调用方要在传 parts 之前知道结果：parts 的构造要遍历整轮 events，
    节流命中时白拼一遍纯属浪费。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    buf = _stream_buf(sid)
    grew = len(text) - buf["written"]
    return grew >= _STREAM_FLUSH_CHARS or (
        time.time() - buf["last"]) >= _STREAM_FLUSH_INTERVAL


def persist_stream(text: str, force: bool = False, parts: list[dict] | None = None,
                   sid: str = "") -> None:
    """流式正文的节流落盘；force=True 立即写（终止 / checkpoint / 收尾时用）

    sid 不传时按本轮归属推断；调用方最好显式传，避免跨会话串写。
    parts 传入时同时更新缓冲区里「屏幕上出现过的内容」有序块列表，
    这样即便只存了最后一段正文，恢复时也能按原顺序把所有段落和事件拼回来。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    buf = _stream_buf(sid)
    buf["text"] = text
    if parts is not None:
        buf["parts"] = parts
    now = time.time()
    if not force:
        grew = len(text) - buf["written"]
        if (now - buf["last"]) < _STREAM_FLUSH_INTERVAL and grew < _STREAM_FLUSH_CHARS:
            return
    buf["last"] = now
    buf["written"] = len(text)
    _write_streaming(sid, text, buf.get("parts") or [])


def refresh_stream_parts(sid: str, text: str, events: list[dict]) -> None:
    """把「当前屏幕」重新压进流式缓冲 / 副档案（思维链、工具块、过渡正文都算）

    以前只有正文增量时才更新 parts：模型整段只思考、只调工具的那些回合不刷新，
    缓冲区停在「上一次开口说话」的那一刻。用户中途刷新页面（WS 重连按 parts
    恢复进行中的一轮），屏幕上已经有的一大段思维链就整段消失（88cad046）。
    节流交给 persist_stream，这里只负责重建快照。
    """
    try:
        persist_stream(text, parts=_snapshot_parts(events, text), sid=sid)
    except Exception:
        pass


# 副档案里每块输出 / 每个事件留多少字。一次大输出有 24 万字符（见 tools._OUT_KEEP），
# 一轮 20 多个工具就能让 parts 涨到几 MB，而它每 200 字就要被序列化写盘一次
_PART_TEXT_CAP = 4000
_PART_EVENT_CAP = 4000
# parts 总预算：超出就从最老的一头开始丢。丢的是「屏幕上曾经闪过的东西」，
# 留不住也无所谓；真正要紧的是别让它把进程撑爆
_PARTS_TOTAL_CAP = 400 * 1024
# 进程占用超过这个数（MB）就认定内存吃紧，开始主动丢历史细节。
# 只是「读不到 /proc/meminfo 时的兜底」：真正的判定按系统剩余内存算，见 _mem_pressure
_OOM_SOFT_MB = 900
# 上次打降载日志的时刻（30 秒节流，见 _relieve_memory）
_RELIEVE_AT = 0.0


def _proc_mem_mb() -> int:
    """本进程当前占用的内存（MB）；读不到返回 0（非 Linux 环境）"""
    try:
        with open("/proc/self/statm", "r") as f:
            return int(f.read().split()[1]) * 4096 // (1024 * 1024)
    except Exception:
        return 0


def _mem_pressure() -> bool:
    """现在是不是该降载了：按系统真实剩余内存判断，而不是看自己占了多少

    之前用「本进程超过 900MB」当阈值，那个数是拍脑袋定的。Deck 桌面模式有 12G
    可用，本进程涨到 1G 完全正常，却被判成吃紧，白白把工具输出压碎；反过来
    游戏模式里游戏吃掉 10G 时，本进程只有 400MB 也已经该让路了。
    真正决定会不会被 OOM killer 选中（SIGKILL，Python 留不下任何遗言）的是
    「还剩多少可用」，所以这里直接读 MemAvailable。

    判定标准：可用内存低于「总量的一小部分」就降载，同时留一个绝对下限兜底。
    桌面模式 12G 可用时，剩 1.5G 才报警（正常用根本碰不到）；游戏模式里游戏
    吃掉大半内存、系统只剩几百 MB 时立刻收手。读不到就直接按「不紧张」处理。
    """
    try:
        avail_mb = 0
        total_mb = 0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                elif line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) // 1024
                if avail_mb and total_mb:
                    break
        if not avail_mb:
            return False
    except Exception:
        # 读不到可用内存时退回旧判据，总比完全不管强
        used = _proc_mem_mb()
        return bool(used) and used >= _OOM_SOFT_MB
    # 按总内存的 1/8 算阈值，再兜一个 300MB 的绝对下限。Deck 总内存 16G：
    # 桌面模式剩 1.5G 以下才降载（平时用不到这么多），游戏模式下游戏吃掉大头时
    # 剩几百 MB 立刻让路 —— 这时候下一个被 OOM killer 挑中的就是本进程
    return avail_mb < max(300, total_mb // 8 if total_mb else 0)


def _trim_part(p: dict) -> dict:
    """把单块内容压到上限内（事件只压 output/text 这类长文本字段）"""
    if p.get("type") == "text":
        s = p.get("content") or ""
        if len(s) > _PART_TEXT_CAP:
            p = {"type": "text", "content": s[:_PART_TEXT_CAP] + f"…（省 {len(s) - _PART_TEXT_CAP} 字）"}
        return p
    ev = dict(p.get("event") or {})
    # 思考记录不裁：它要能刷新后原样重放，截断等于把用户看到的内容删掉。
    # 其它事件只压 output/text 这类长文本字段
    if ev.get("type") != "thought":
        for k in ("output", "text"):
            s = ev.get(k)
            if isinstance(s, str) and len(s) > _PART_EVENT_CAP:
                ev[k] = s[:_PART_EVENT_CAP] + f"…（省 {len(s) - _PART_EVENT_CAP} 字）"
    # 截图缩略图是 base64 或路径，两者都别进副档案：恢复时重新截屏即可，
    # 一张图就能顶掉几十条正文的预算
    ev.pop("image", None)
    ev.pop("image_b64", None)
    return {"type": "event", "event": ev}


def _trim_parts(parts: list[dict]) -> list[dict]:
    """给 parts 定容量：单块压到上限，总量超了丢最老的

    这些内容只用于「进程被杀后把屏幕复原」，属于尽力而为。以前它没有上限，
    连续几条大输出就能让这个字典长到几百 MB，而它还是被频繁整体复制、
    序列化写盘的——OOM 就是这么来的，偏偏 OOM 时最需要它活下来。
    """
    out: list[dict] = []
    total = 0
    for p in parts or []:
        q = _trim_part(p)
        blob = q.get("content") or ""
        if not blob:
            ev = q.get("event") or {}
            blob = str(ev.get("output") or ev.get("text") or ev.get("summary") or "")
        n = len(blob) + 64
        out.append(q)
        total += n
    while total > _PARTS_TOTAL_CAP and len(out) > 1:
        q = out.pop(0)
        total -= len(str(q.get("content") or "")) + len(str(q.get("event") or "")) + 64
    return out


def _relieve_memory(events: list[dict]) -> None:
    """内存吃紧时丢掉还能丢的：老事件里的大段输出只留开头

    只压 output/text 这类「屏幕上早就滚过去」的大文本，不动正文、不动工具调用
    结构 —— history 里最终写盘时也要靠它们。真正常见的内存元凶（命令输出、
    工具结果进上下文）已经在源头封顶，这里只是兜底，读不到 /proc 时直接跳过。
    """
    used = _proc_mem_mb()
    if not _mem_pressure():
        return
    # 降载期间每轮工具往返都会走到这里，日志按 30 秒节流，免得刷满
    global _RELIEVE_AT
    if time.time() - _RELIEVE_AT > 30:
        _RELIEVE_AT = time.time()
        print(f"[DeckMeido] 可用内存偏低，降载中（本进程 {used} MB，"
              f"parts {len(_stream_bufs)} 份待裁）", flush=True)
    for ev in events or []:
        # 思考记录不降载：文本就是用户要能重新展开看全的内容。
        # 这里只压工具输出 / 过渡正文这类「屏幕上早滚过去」的大段文本
        if ev.get("type") == "thought":
            continue
        for k in ("output", "text"):
            s = ev.get(k)
            if isinstance(s, str) and len(s) > 800:
                ev[k] = s[:800] + "…（内存吃紧，已省略）"
        ev.pop("image_b64", None)
    # 屏幕快照也跟着瘦身，下一轮重算时同样是裁剪过的版本
    for buf in _stream_bufs.values():
        if buf.get("parts"):
            buf["parts"] = _trim_parts(buf["parts"])


def _snapshot_parts(events: list[dict], tail_text: str = "") -> list[dict]:
    """把本轮事件与当前正文拼成「屏幕上出现过的内容」有序块列表

    事件的 text 段（工具调用前的过渡正文）按顺序穿插在思维链/工具块之间，
    尾部的 tail_text 是最后一个事件之后正在进行的那段正文。
    """
    parts: list[dict] = []
    for ev in events or []:
        if ev.get("type") == "text":
            parts.append({"type": "text", "content": ev.get("text") or ""})
        else:
            parts.append({"type": "event", "event": ev})
    if tail_text:
        parts.append({"type": "text", "content": tail_text})
    return _trim_parts(parts)


# ---------- 流式正文合批 ----------
# 模型吐字按 token 走，一个 chunk 常常只有一两个字。逐片广播等于让浏览器对
# 同一条气泡做几十上百次重排，Deck 的弱 CPU 上肉眼可见地卡。这里攒 40ms
# 再发一次：人眼分辨不出差别，重排次数降一个数量级。
_DELTA_FLUSH_MS = 40
# sid -> {"buf": [片段], "task": asyncio.Task|None, "tag": dict}
_delta_pend: dict[str, dict] = {}
# 思维链单独的攒批：sid -> {"buf": [片段], "task": 定时器, "tag": dict}
# 和正文分开，是因为两类消息在事件流里是交错的，同一个 buffer 会把顺序搅乱
_thought_pend: dict[str, dict] = {}


def _flush_delta(sid: str) -> None:
    """把某个会话攒下的片段一次性广播出去（同步版，供定时器与收尾调用）"""
    st = _delta_pend.get(sid)
    if not st:
        return
    st["task"] = None
    text = "".join(st["buf"])
    st["buf"].clear()
    if not text:
        return
    tag = st["tag"]
    asyncio.create_task(manager.broadcast(
        {"type": "delta", "text": text, "final": False, **tag}))


def _flush_delta_later(sid: str, text: str, tag: dict) -> None:
    """收一片正文，攒够 40ms 就发。同一会话同一时刻只有一个待发的定时器"""
    st = _delta_pend.get(sid)
    if st is None:
        st = _delta_pend[sid] = {"buf": [], "task": None, "tag": tag}
    st["tag"] = tag
    st["buf"].append(text)
    if st["task"] is None:
        st["task"] = asyncio.get_running_loop().call_later(
            _DELTA_FLUSH_MS / 1000, _flush_delta, sid)


def _flush_thought(sid: str) -> None:
    """把攒下的思维链片段一次性广播出去"""
    st = _thought_pend.get(sid)
    if not st:
        return
    st["task"] = None
    text = "".join(st["buf"])
    st["buf"].clear()
    if not text:
        return
    asyncio.create_task(manager.broadcast(
        {"type": "thought_delta", "text": text, **st["tag"]}))


def _flush_thought_later(sid: str, text: str, tag: dict) -> None:
    """收一片思维链，攒够 40ms 就发（理由同正文合批：模型思考时片又小又密）"""
    st = _thought_pend.get(sid)
    if st is None:
        st = _thought_pend[sid] = {"buf": [], "task": None, "tag": tag}
    st["tag"] = tag
    st["buf"].append(text)
    if st["task"] is None:
        st["task"] = asyncio.get_running_loop().call_later(
            _DELTA_FLUSH_MS / 1000, _flush_thought, sid)


def flush_thought_now(sid: str = "") -> None:
    """立刻把攒着的思维链推出去（思维链结束、切到正文前必须调）"""
    sid = sid or store.turn.get("sid") or store.current_sid
    st = _thought_pend.pop(sid, None)
    if not st:
        return
    if st["task"] is not None:
        st["task"].cancel()
    text = "".join(st["buf"])
    if text:
        asyncio.create_task(manager.broadcast(
            {"type": "thought_delta", "text": text, **st["tag"]}))


def flush_delta_now(sid: str = "") -> None:
    """立刻把攒着的正文推出去。

    本轮收尾、被终止、或要发别的类型事件（工具开始/思维链）时必须调一次，
    否则最后不到 40ms 的那几个字会被后一条消息盖掉顺序，前端看起来就是
    「话说了一半，工具卡片插进来了」。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    st = _delta_pend.pop(sid, None)
    if not st:
        return
    if st["task"] is not None:
        st["task"].cancel()
    text = "".join(st["buf"])
    if text:
        asyncio.create_task(manager.broadcast(
            {"type": "delta", "text": text, "final": False, **st["tag"]}))


def clear_stream(sid: str = "") -> None:
    """正文已正式落盘，把残留的流式副档案删掉

    流式正文现在存在独立的副档案里（见 session_store.write_stream_side），
    所以这里的收尾只是「删一个小文件」：不读主文件、不重写主文件。
    以前这一步要判断「磁盘上还有没有 _streaming 残留」，有就整份读-删-写一遍，
    长会话收尾时又是一次几 MB 的往返。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    _stream_bufs.pop(sid, None)

    def _job() -> None:
        with store.lock:
            if sid in store.deleted_sids:
                return
            store.drop_stream_side(sid)
            # 指纹推到现在：后续（若有）针对同一会话的残留判断不会再把主文件当成「刚被流过」
            store.mark_written(sid)

    session_store.io_submit(_job)


_UPLOAD_RE = re.compile(r"!\[[^\]]*\]\((/uploads/[^\s)]+\.(?:png|jpe?g|webp))\)", re.I)
# 随提问附带、已经落成本地文件的画面：文字里留一行标记，发给模型时按文件现读现编码。
# 历史里因此不再躺着几 MB 的 base64 —— 那是最容易被裁掉的东西，一裁图就没了
_SHOT_REF_RE = re.compile(r"\[SHOT:([^\]\s]+)\]")
_IMG_REF_RE = re.compile(r"\[IMG:([^\]\n]+)\]")


def _shot_ref_path(name: str) -> Path | None:
    fname = Path(name).name
    if not name or fname != name:
        return None
    fp = STATIC_DIR / "shots" / fname
    return fp if fp.is_file() else None


def _to_vision_content(text: str):
    """消息里含上传图片引用时转成多模态 content（供 vision 模型看图）"""
    matches = _UPLOAD_RE.findall(text)
    if not matches:
        return text
    parts = []
    for url in matches:
        fp = STATIC_DIR / url.lstrip("/")
        if not fp.is_file():
            continue
        import base64
        ext = "jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else fp.suffix.lstrip(".").lower()
        b64 = base64.b64encode(fp.read_bytes()).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}})
    if not parts:
        return text
    parts.append({"type": "text", "text": _UPLOAD_RE.sub("[图片]", text)})
    return parts


def _shot_caption(seq: int) -> str:
    """给随提问附带的画面配一句说明

    同一条会话里连着截几次，上下文里就并排躺着好几张图，文案还一字不差。
    模型分不清哪张对应当前这一问，经常拿着上一张回答（用户看到的就是
    「截的是游戏画面，答的还是几分钟前的 Steam 菜单」）。所以给这张编号，
    并明确告诉它只看最新这张，前面的都已过时。

    定义位置必须早于 _shot_ref_content：init_sessions() 在模块导入时就跑，
    它会走 _build_llm_msgs → _expand_context → _shot_ref_content，
    若本函数定义在 init_sessions 之后，恢复历史时就是 NameError。
    """
    if seq <= 1:
        return prompts.part("shot_caption_first")
    return prompts.part("shot_caption_more").format(seq=seq)


def _agent_shot_caption(seq: int) -> str:
    """给 agent 自己截的那张画面配说明

    这里不是用户带图提问，而是模型为了看清「刚才那步之后」才截的图，
    所以不能沿用「更早的提问」那套文案（会把它绕回旧图）。
    """
    return prompts.part("shot_latest").format(seq=seq)


def _shot_ref_content(text: str):
    """把文字里的 [SHOT:xxx.jpg] 标记换成图片部件

    发给模型的图只在这一步读，历史里存的是一条几十字节的标记：
    之前库里直接存着 base64，一旦被上下文压缩裁掉，界面上的图也跟着没了，
    而且模型看到的那条内容每次都要重新编码，前缀缓存也用不上。
    """
    names = _SHOT_REF_RE.findall(text or "")
    if not names:
        return None
    parts: list[dict] = []
    # 说明放在图片前面：它在拼接位置上是稳定前缀，后面的历史才命中得了前缀缓存；
    # 顺带让模型先知道「这张才是当前画面，前面那些已过时」
    parts.append({"type": "text", "text": _shot_caption(len(names))})
    for name in names:
        fp = _shot_ref_path(name)
        if not fp:
            continue
        import base64
        ext = "jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else fp.suffix.lstrip(".").lower()
        b64 = base64.b64encode(fp.read_bytes()).decode()
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/{ext};base64,{b64}"}})
    if len(parts) == 1:
        return None
    # 标记本身留在正文里，和上传图片的处理方式一致：模型知道这张图对应哪句话
    parts.append({"type": "text", "text": text})
    return parts


def _img_ref_content(text: str):
    """把文字里的 [IMG:路径] 标记换成图片部件（@{} 引用图片文件时用）

    和 [SHOT:] 的差异：SHOT 是随提问截取的当前画面（有「只看最新这张」的编号说明），
    IMG 是用户主动引用进对话的图片文件，没有过时概念，直接转图。
    历史里只存 [IMG:路径] 标记，这里现读现编码，避免 base64 躺进历史被压缩裁掉。
    """
    paths = _IMG_REF_RE.findall(text or "")
    if not paths:
        return None
    import base64
    parts: list[dict] = []
    for raw in paths:
        fp = Path(raw).expanduser()
        try:
            fp = tools.resolve_in_workspace(str(fp))
        except tools.OutsideWorkspace:
            continue
        if not fp.is_file():
            continue
        ext = "jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else fp.suffix.lstrip(".").lower()
        b64 = base64.b64encode(fp.read_bytes()).decode()
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/{ext};base64,{b64}"}})
    if not parts:
        return None
    # 标记本身换成占位符留在正文，模型知道这段文字对应那张图
    parts.append({"type": "text", "text": _IMG_REF_RE.sub("[图片]", text)})
    return parts


def _restore_intent_map(sid: str, data: dict) -> bool:
    """把会话文件里的 pending_intent 装回内存，并按 boot_id 对账。

    返回 True 表示状态发生了变化（调用方需要落盘，让 executed/expired 留下来）。
    """
    intent = data.get("pending_intent")
    if not isinstance(intent, dict) or not intent:
        store.set_intent(sid, None)
        return False
    rec = policy.reconcile_intent(intent, _system_boot_id(), time.time())
    if not rec:
        store.set_intent(sid, None)
        return bool(intent)
    store.set_intent(sid, rec)
    return rec != intent


def init_sessions():
    """启动时恢复最近的会话；没有历史则创建默认会话"""
    global _ctx_used
    # index.json 是侧栏索引，*.side.json 是流式正文的副档案：
    # 都不是会话文件，混进来的话会被当成坏文件反复改名
    files = session_store.all_session_files()
    if not files:
        save_current_session()
        return

    # 逐个试：以前只认 mtime 最新的那一个，它坏了就 history.clear() 整个重来，
    # 几十轮历史当场归零。现在坏一个就跳过试下一个，全坏才开新会话。
    # 注意要排除「空会话文件」：空文件不是损坏，但拿它当恢复结果同样等于清空，
    # 所以空 history 的文件也当作不合格，继续往旧文件里找。
    def _restore(data: dict, src: Path) -> bool:
        """从一个会话文件恢复出完整状态；成功返回 True"""
        global _ctx_used
        sid = data.get("id") or src.stem
        hist = data.get("history") or []
        if not isinstance(hist, list) or not hist:
            return False
        store.current_sid = sid
        store.history = hist
        # 回填真实创建时间：以前启动恢复只把 history 装回来，created_map 是空的，
        # 下一次 write_session 就用当前时间覆盖掉文件里的 created ——
        # 用户看到的就是「昨天建的会话，今天显示刚创建」
        if data.get("created"):
            try:
                store.created_map.setdefault(sid, float(data["created"]))
            except (TypeError, ValueError):
                pass
        intent_changed = _restore_intent_map(sid, data)
        comp = data.get("compact")
        if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
            _set_compact(sid, comp.get("summary") or "",
                         min(int(comp.get("upto") or 0), len(store.history)))
        else:
            store.drop_compact(sid)
        # 上一轮回答被打断：把当时流式落盘的半截正文恢复成一条「回答中断」的消息，
        # 否则用户只看到自己的提问，像是回答凭空消失了。
        # 半截正文存在独立的副档案里。
        partial = store.read_stream_side(sid)
        partial_text = (partial.get("text") or "").strip() if isinstance(partial, dict) else ""
        partial_parts = (partial.get("parts") or []) if isinstance(partial, dict) else []
        # 恢复门槛已取消：只要落盘过半个字，就恢复成一条「回答中断」的消息。
        # 之前设了 30 字门槛（想避免「好的」这种两个字占一条消息），代价是
        # 用户看到的现象变成「内容凭空消失」——而被吃掉的往往正是开头那段
        # 最有信息量的结论。宁可多一条带「已中断」标记的短消息，也不能丢内容。
        if partial_parts:
            # 分块恢复：屏幕上出现过的每一段正文都按原顺序保留，事件（思维链/工具）挂到
            # 同一条消息的 events 上。旧版只存最后一段正文，前几句过渡话会丢，这里已修
            segs = [(p.get("content") or "") for p in partial_parts if p.get("type") == "text"]
            evs = [p.get("event") for p in partial_parts if p.get("type") == "event"]
            partial_text = "\n\n".join(s.strip() for s in segs if s.strip()).strip()
        else:
            evs = []
        # 本轮最后的正文若比 parts 里更全（流式尾部还没进 parts），补在末尾
        tail_text = (partial.get("text") or "").strip() if isinstance(partial, dict) else ""
        if tail_text and tail_text not in partial_text:
            partial_text = (partial_text + "\n\n" + tail_text).strip() if partial_text else tail_text
        if partial_text or evs:
            note = "\n\n（以上回答被中断，已保留屏幕上出现过的全部内容）"
            store.history.append({"role": "assistant",
                                  "content": (partial_text.rstrip() + note) if partial_text else note.strip(),
                                  "events": evs, "interrupted": True,
                                  "ts": (partial.get("ts") if isinstance(partial, dict) else None) or time.time()})
            print(f"[DeckMeido] 已恢复被中断的回答（{len(partial_text)} 字，{len(evs)} 个事件）",
                  file=sys.stderr)
        _ctx_used = _estimate_tokens(_build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
        if partial is not None:
            # 无论是「恢复成消息」还是「判定太短丢弃」，都要把半截正文抹掉后落盘：
            #   - 不落盘：恢复出来的内容后端再死一次又没了
            #   - 不抹掉：那份半截正文还在，下次启动再恢复一遍，
            #     同一条回答会越重启越多
            try:
                _write_session(store.current_sid, store.history)
            except Exception as e:
                print(f"[DeckMeido] 恢复中断回答后落盘失败：{e}", file=sys.stderr)
            store.drop_stream_side(store.current_sid)
        elif intent_changed:
            # 只有意图状态变了（重启后 reboot→executed / 密码→expired）：
            # 也要落盘，否则下次启动又会按旧状态重新对账
            try:
                _write_session(store.current_sid, store.history)
            except Exception as e:
                print(f"[DeckMeido] 写回 pending_intent 失败：{e}", file=sys.stderr)
        print(f"[DeckMeido] 已从 {src.name} 恢复会话 {store.current_sid}（{len(store.history)} 条）",
              file=sys.stderr)
        return True

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            # 单个文件损坏：留个备份，继续试下一个，别让一个坏文件把历史全毁了
            print(f"[DeckMeido] 会话文件 {f.name} 解析失败（{e}），已留档并尝试下一个",
                  file=sys.stderr)
            try:
                f.rename(f.with_name(f.name + ".broken"))
            except OSError:
                pass
            continue
        try:
            if _restore(data, f):
                return
        except Exception as e:
            print(f"[DeckMeido] 从 {f.name} 恢复失败（{e}），尝试下一个", file=sys.stderr)
            continue

    # 全部不可用：开一个全新会话顶上（换随机 sid，避免和目录里残存的旧文件撞号）
    store.current_sid = _fresh_sid()
    store.history = []
    _ctx_used = 0
    save_current_session()


_init_sessions = init_sessions
# 侧栏索引要先建好：/api/sessions 只读它，不建就是空列表。
# 放在 init_sessions 之前：恢复会话时写的那个文件会顺带把索引补齐一次
store.load_index()
_init_sessions()

# 待确认的危险操作 {name, args, description, sid, epoch, turn_id, client, device}
# sid/epoch/client/device 是「谁在什么会话里提的」：确认只认原会话原来源，
# 否则任何连上的客户端随便一句「好」就能放行别人发起的危险命令
pending_action: dict | None = None


def _pending_matches(p: dict, sid: str, epoch: int,
                     client: str | None, device: str | None) -> bool:
    """这条待确认操作是不是本轮发起者自己提的"""
    if p.get("sid") != sid or p.get("epoch") != epoch:
        return False
    # 来源也一起比：同一会话里换了个端发「好」不算确认（网页和插件能同时连）
    return (p.get("client") or "") == (client or "") \
        and (p.get("device") or "") == (device or "")


def _pending_matches_turn(p: dict, sid: str, epoch: int, turn_id: str,
                          client: str | None, device: str | None) -> bool:
    """这个端的这一轮，能不能认领并确认这条挂起操作

    与 _pending_matches 的区别是多比一道 turn_id：同一个端连发两轮时，
    上一轮挂起的危险操作不能被这一轮的「确认」顺手放行（那一轮模型可能已经
    换了话题，用户说的「确认」指的并不是这件事）。
    """
    if not _pending_matches(p, sid, epoch, client, device):
        return False
    return (p.get("turn_id") or "") == (turn_id or "")


# ---------- WebSocket 连接管理 ----------

class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, msg: dict):
        # 并发发：这里串行 await 的话，一条卡住的连接（手机切到后台、网络半死
        # 但没断）就会把后面所有端的流式输出一起堵住
        targets = list(self.active)
        if not targets:
            return
        devlog.log("ws.broadcast", type=msg.get("type"), clients=len(targets), payload=msg)
        results = await asyncio.gather(*(ws.send_json(msg) for ws in targets),
                                       return_exceptions=True)
        for ws, res in zip(targets, results):
            if isinstance(res, BaseException):
                self.active.discard(ws)

    async def close_all(self, code: int = 1000):
        """按指定原因码断开所有连接（设备被移除后用 4401，让前端回到鉴权页）"""
        for ws in list(self.active):
            try:
                await ws.close(code=code)
            except Exception:
                pass
        self.active.clear()


manager = ConnectionManager()


def _wake_notify(status: dict):
    """唤醒状态机变化：广播给所有页面（眼睛页/设置页都靠它即时刷新）"""
    return manager.broadcast({"type": "wake_state", "data": status})


wake_service.set_handlers(on_state=_wake_notify)


async def _broadcast_remote_input(state: dict):
    """跨屏接管状态变化：广播给所有页面（远程控制端和 Deck 本机都要看到）"""
    await manager.broadcast({"type": "remote_input", **state})


remote_input.manager.set_hook(_broadcast_remote_input)


async def _broadcast_pad_order(action: str):
    """虚拟手柄建好/销毁时喊 QAM 插件去换 Steam 的控制器排序

    排序状态只在 Steam 进程内存里，只能在 Steam 页面上下文里调
    SteamClient.Input.SwapControllerOrder，所以真正干活的是插件前端；
    这里只要把消息广播出去（插件后端收到后再转给它自己的前端）。
    """
    n = len(manager.active)
    print(f"[DeckMeido] 广播手柄换序 {action} → {n} 个客户端")
    await manager.broadcast({"type": "pad_order", "action": action})


tools.set_pad_order_hook(_broadcast_pad_order)

# ---------- 认证 ----------


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _device_days() -> int:
    """设备凭据有效期（天），[security] device_days，默认 30。"""
    try:
        days = int(CFG.get("security", {}).get("device_days", pairing.DEFAULT_DEVICE_DAYS))
    except (TypeError, ValueError):
        days = pairing.DEFAULT_DEVICE_DAYS
    return max(1, min(days, 3650))


def _cookie_device(request: Request) -> dict | None:
    """Cookie 里的设备凭据 → 设备记录（只按哈希查，令牌明文不落盘）。"""
    raw = request.cookies.get(pairing.COOKIE_NAME)
    return pairing.manager.devices.authenticate(raw) if raw else None


def _csrf_ok(request: Request) -> bool:
    """Cookie 认证的写操作必须同源，防跨站伪造请求。"""
    return (authguard.same_origin(request.headers.get("origin"),
                                 request.headers.get("host"))
            and authguard.same_site(request.headers.get("sec-fetch-site")))


def _auth_device(request: Request) -> dict | None:
    device = _cookie_device(request)
    if not device:
        return None
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _csrf_ok(request):
        audit.record("auth", "csrf", "fail", ip=_client_ip(request),
                     detail=request.url.path)
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")
    return device


def _set_device_cookie(response: Response, request: Request, token: str,
                       remember: bool) -> None:
    secure = (request.url.scheme == "https"
              or request.headers.get("x-forwarded-proto", "").lower() == "https")
    response.set_cookie(
        pairing.COOKIE_NAME, token, path="/", httponly=True, samesite="strict",
        secure=secure, max_age=_device_days() * 86400 if remember else None,
    )


# 同一 IP 打同一路径的未授权请求，60 秒内只记一条：未配对的浏览器
# 会周期轮询，不合并会把审计缓冲刷成同一件事的几十条重复
_unauth_marks: dict[tuple[str, str], float] = {}
_UNAUTH_NOTICE_S = 60.0


def _reject_unauthorized(ip: str, action: str, detail: str = "") -> None:
    """未配对设备访问受保护接口：记一条审计并返回 401。

    不按 IP 封禁：配对页会周期轮询，连续 401 是正常流程；暴力猜测由
    单次配对会话最多 3 次尝试挡住（见 server/pairing.py）。
    """
    now = time.time()
    key = (ip, detail or action)
    if now - _unauth_marks.get(key, 0.0) >= _UNAUTH_NOTICE_S:
        _unauth_marks[key] = now
        if len(_unauth_marks) > 200:
            for stale in [k for k, t in _unauth_marks.items()
                          if now - t >= _UNAUTH_NOTICE_S]:
                _unauth_marks.pop(stale, None)
        audit.record("auth", action, "fail", ip=ip, reason="unpaired", detail=detail)
    raise HTTPException(status_code=401, detail="未配对设备：请先在设置页配对")


def require_auth(request: Request):
    """接口总闸：本机回环免验证，局域网必须已配对（HttpOnly 设备 Cookie）。

    令牌已下线。静态页面本身不设防，未配对时前端会弹 PIN 配对，
    受保护接口统一 401，不存在「页面能进但接口通不了」的中间态。
    """
    ip = _client_ip(request)
    if netcfg.is_local(ip):
        return
    if _auth_device(request):
        return
    _reject_unauthorized(ip, "api", request.url.path)


def _ws_cookie_auth(ws: WebSocket) -> bool:
    """WebSocket 的 Cookie 认证：浏览器会自动带上同源 Cookie。

    跨站网页也能发起 WebSocket，所以这里必须校验 Origin 与 Host 同源。
    """
    raw = ws.cookies.get(pairing.COOKIE_NAME)
    if not raw:
        return False
    if not authguard.same_origin(ws.headers.get("origin"), ws.headers.get("host")):
        return False
    return bool(pairing.manager.devices.authenticate(raw))


def _ws_authorized(ws: WebSocket) -> bool:
    # 回环来源（掌机自带客户端 / 插件 / 本地调试）直接放行；局域网必须已配对
    if netcfg.is_local(ws.client.host if ws.client else ""):
        return True
    return _ws_cookie_auth(ws)



def _security_note() -> str:
    """掌机状态行的一句话：有配对请求优先，其次是最近的失败。"""
    pending = pairing.manager.pending()
    if pending:
        s = pending[0]
        return f"{len(pending)} 台设备等待配对（{s['ip'] or '未知来源'}）"
    now = time.time()
    fails = [e for e in audit.recent(50)
             if e["result"] in ("fail", "blocked") and now - e["ts"] <= 300]
    if fails:
        return f"最近 5 分钟有 {len(fails)} 次鉴权/配对失败"
    return ""


# ---------- 系统状态 ----------

def read_first(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def get_status(gaming: bool | None = None) -> dict:
    battery = read_first("/sys/class/power_supply/BAT1/capacity")
    charging = read_first("/sys/class/power_supply/BAT1/status")
    load = ""
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
        load = " ".join(load)
    except OSError:
        pass
    # 模式判据：gamescope 在 PipeWire 里有没有视频节点。以前用
    # `pgrep -x gamescope`，但 SteamOS 上进程名带着一长串参数，comm 被截到
    # 15 字节后反而匹配不上，pgrep 恒返回 1 —— 于是进了游戏模式还一直显示
    # 桌面、「画面」也不可用。PipeWire 节点只在游戏模式存在，而且抓帧用的
    # 就是这个判断，两处不会再打架。它不便宜，所以带缓存（见 tools.mode_is_gaming）。
    # 探针会起子进程，接口层用 to_thread 算好再传进来，别卡事件循环
    if gaming is None:
        gaming = tools.mode_is_gaming()
    win = context_window()
    used = _ctx_used or _cached_estimate()
    pct = round(min(used / win, 1.0) * 100, 1) if win else 0
    # 虚拟手柄的可用性由后端自己判断（插件是 root，权限和这里不一样）
    gamepad_ok, gamepad_reason = tools.pad_available()
    # 按游戏提示词预设：游戏是哪款由插件探测后上报，后端不再重复实现一套探测
    preset_state = presets.active_view()
    return {
        "battery": int(battery) if battery.isdigit() else None,
        "charging": charging or None,
        "load": load or None,
        "mode": "gaming" if gaming else "desktop",
        # 后端是否正在跑一轮对话：前端重连后据此恢复 ⏹ 按钮状态，
        # 避免断联时正在跑的那轮看起来像「已经停了」
        "running": bool(current_task and not current_task.done()),
        # 本轮开始的时间戳（没有在跑则为 0）：前端据此显示「已等 N 秒」，
        # 工具链跑得久时也能看出后端还活着，而不是一屏静默的「没反应」
        "running_since": (store.turn.get("started") or 0) if (current_task and not current_task.done()) else 0,
        "llm_configured": llm.configured,
        "model": llm.model,
        "thinking": bool(llm.reasoning_effort),
        "context": {
            "messages": len(store.history),
            "chars": sum(len(str(m.get("content", ""))) for m in store.history),
            "used": used,
            "window": win,
            "percent": pct,
            "compact_at": round(COMPACT_RATIO * 100),
            # 当前会话编号：顶栏胶囊上显示成 #xxxxxx，方便区分/回溯是哪个会话
            "sid": store.current_sid,
            # 最近一次请求的前缀缓存命中率（DeepSeek 才有；别家是 0，
            # 前端据此决定要不要显示「缓存 xx%」）
            "cache": llm.cache_stats(),
        },
        "workspace": str(tools.WORKSPACE),
        # 工作区是否真的可用（mkdir 失败时不会再被静默吞掉）
        "workspace_ok": WORKSPACE_OK,
        "workspace_error": WORKSPACE_ERROR,
        # 运行版本 vs 磁盘版本：装完没重启后端时两者会不一致，界面据此提示重启
        **version_state(),
        # 前端据此决定要不要回报思考链实际渲染长度（仅 dev_mode 有意义）
        "dev_mode": devlog.enabled(),
        # [runtime].offline_mode：前端顶部横幅据此显示/隐藏
        "offline_mode": netmode.enabled(),
        # 最近一次清日志的结果：有清不掉的文件时前端透出「N 个未能删除」
        "log_clear": devlog.last_clear(),
        # 控制类控件（点击输入/窗口聚焦/原生打字/构建依赖）的实测结果，
        # 前端在电量徽章左侧显示成「x/x + 绿灯/黄灯/红灯」
        "controls": depcheck.summary(),
        # 桌面模式下执行命令/亮遮罩时是否正压着「别息屏」（前端不展示，排障用）
        "keep_awake": power.keeper.status(),
        # 掌机状态行：有人尝试配对 / 连续失败时给出提示，空串表示一切正常
        "security_note": _security_note(),
        "pending_pair": len(pairing.manager.pending()),
        "lan_enable": netcfg.lan_enabled(CFG.get("server") or {}),
        # 虚拟手柄能不能用（后端视角）：给插件/大屏透传，避免靠 root 权限误判
        "gamepad_ok": gamepad_ok,
        "gamepad_reason": gamepad_reason,
        # 语音合成实际用哪个引擎、为什么降级：tts_engine / tts_ready / tts_error
        **tts.summary(),
        # 当前游戏 / 当前提示词预设（状态区显示「当前预设：xxx」用）
        "game_appid": preset_state["game_appid"],
        "game_name": preset_state["game_name"],
        "preset": preset_state,
    }


# 状态层的 TTL 缓存。get_status 要碰 sysfs、/proc、depcheck 探针、tts 状态、
# 预设视图，全是同步 IO / 子进程；/api/status 每 3 秒轮询一次、WS 广播还会再调，
# 以前每次都在事件循环里真跑一遍。事件循环同时管着对话流式输出和 WS，
# 桌面端一卡就是「整个界面停住」。改成：
#   - 慢的那一半（探针类）在 async 侧 await 一个线程算好的值；
#   - 整个返回值按 STATUS_TTL 复用，轮询期间只做一次真正采集。
# 时效性要求高的调用方（刚重启后端、刚改配置）传 fresh=True 跳过缓存。
STATUS_TTL = 1.5
_status_cache: dict | None = None
_status_cache_at = 0.0
_status_lock = threading.Lock()


def status_snapshot(force: bool = False) -> dict:
    """采集一份完整状态（线程安全、带 TTL）。行内是同步 IO，别在 async 里直接调。"""
    global _status_cache, _status_cache_at
    with _status_lock:
        if (not force and _status_cache is not None
                and time.monotonic() - _status_cache_at < STATUS_TTL):
            return _status_cache
    data = get_status(tools.mode_is_gaming())
    with _status_lock:
        _status_cache, _status_cache_at = data, time.monotonic()
    return data


async def status_async(force: bool = False) -> dict:
    """async 路由/WS 广播统一入口：采集放到线程池，事件循环只等结果。"""
    with _status_lock:
        if (not force and _status_cache is not None
                and time.monotonic() - _status_cache_at < STATUS_TTL):
            return _status_cache
    return await asyncio.to_thread(status_snapshot, force)


# ---------- 状态栏短句 ----------

DEFAULT_STATUS_PROMPT = status_line.DEFAULT_STATUS_PROMPT


def status_prompt_text() -> str:
    """当前生效的状态栏隐藏提示词（配置为空时用默认值）

    正在玩且有对应预设的游戏时，这里返回预设内容；全局配置本身不动，
    游戏退出后 presets 会自动把生效值换回配置里的那份。
    """
    base = status_line.prompt_text(CFG.get("agent") or {})
    return presets.effective_prompt(base)


async def build_status_line(state: str = "", game: str = "",
                            game_confidence: str = "", nickname: str = "") -> dict:
    """状态栏文案入口：只传状态摘要，缓存/忙碌/失败规则都在 status_line 模块"""
    return await status_line.generate(
        state=state,
        game=game,
        game_confidence=game_confidence,
        nickname=nickname,
        persona_name=str(PERSONA.get("name") or ""),
        prompt=status_prompt_text(),
        busy=bool(current_task and not current_task.done()),
        configured=llm.configured,
        chat=lambda messages: llm.chat_with_tools_stream(
            messages, [], max_tokens=status_line.MAX_TOKENS, temperature=0.3),
    )


# ---------- API ----------

app = FastAPI(title="DeckMeido")


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """页面本身禁止缓存。

    index.html 是全局唯一的前端入口，改完必须立刻生效。默认 StaticFiles 只带
    ETag + Last-Modified，而 QtWebEngine（原生客户端）和部分浏览器会拿旧副本，
    导致改完代码后客户端里还是老界面，看着像"改动没生效"。
    这里只对 HTML 文档关缓存，静态资源（图片/CSS/JS）照常走 ETag，不影响性能。
    """
    resp = await call_next(request)
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype:
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.middleware("http")
async def _lan_guard(request, call_next):
    """lan_enable 关闭时拒绝非本机来源。

    正常启动会直接绑定 127.0.0.1；这里再兜一层，防止旧快捷方式 / 手写的
    systemd 单元仍然用 0.0.0.0 启动时把局域网暴露出去。
    """
    server_cfg = CFG.get("server", {})
    ip = request.client.host if request.client else ""
    if not netcfg.lan_enabled(server_cfg) and not netcfg.is_loopback(ip):
        return JSONResponse({"detail": "局域网访问未开启，请在掌机设置里启用"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _dev_http_log(request, call_next):
    """开发者模式：记录每个 HTTP 请求的方法 / 路径 / 耗时 / 状态码。

    只记这几项，不记请求体：设置页会提交 API Key，日志里不该出现原文；
    查询参数由 devlog.redact 统一打码。
    """
    if not devlog.enabled():
        return await call_next(request)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        devlog.log(
            "http.error",
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            client=(request.client.host if request.client else ""),
            ms=round((time.perf_counter() - start) * 1000, 1),
            error=repr(exc),
        )
        raise
    devlog.log(
        "http",
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        status=response.status_code,
        ms=round((time.perf_counter() - start) * 1000, 1),
        client=(request.client.host if request.client else ""),
    )
    return response


_CTX_RE = re.compile(r"@\{([^}]+)\}")


async def _run_tool(name: str, args: dict):
    """所有工具调用的统一入口：开发者模式记录参数 / 结果 / 耗时。

    只在这一处落日志，避免每个调用点手写。参数与结果都会过长截断、
    凭据字段打码（devlog 内部负责）。

    回合内闸门：QQ 回合的来源未授权时，任何工具调用一律拒绝。
    这是纵深防御，正常链路在 onebot 入站就已经拦掉了。
    """
    if onebot.turn_authorized() is False:
        onebot.log_tool_blocked(name)
        return f"[{name}] 来源未授权，工具调用被拒绝（onebot.tool.blocked）。"
    if not devlog.enabled():
        return await tools.execute(name, args)
    start = time.perf_counter()
    devlog.log("tool.call", name=name, args=args)
    try:
        result = await tools.execute(name, args)
    except Exception as exc:
        devlog.log("tool.error", name=name,
                   ms=round((time.perf_counter() - start) * 1000, 1),
                   error=repr(exc))
        raise
    devlog.log("tool.result", name=name,
               ms=round((time.perf_counter() - start) * 1000, 1),
               result=result)
    return result


async def _run_tool_busy(name: str, args: dict) -> tuple:
    """跑一个工具，跑久了就定期往对话里推一条「还在跑 · 第 N 秒」。

    长任务（`flatpak update`、apt 升级、编译）期间模型一句话都收不到，
    用户屏幕上只有读秒，看不出到底是在下载还是已经卡死。这里每
    _BUSY_NOTE_EVERY 秒给前端推一条进度（带命令名、已跑秒数、输出尾巴），
    同时记下来，工具结束前由调用方写进 events，刷新页面后仍然看得到。

    返回 (工具结果, 发过的提示文案列表)。
    """
    task = asyncio.ensure_future(_run_tool(name, args))
    notes: list[str] = []
    if name != "run_command":
        return (await task), notes
    started = time.time()
    last_sent = 0.0
    while True:
        try:
            result = await asyncio.wait_for(asyncio.shield(task), _BUSY_NOTE_EVERY)
            return result, notes
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            task.cancel()
            raise
        cur = tools.CURRENT_CMD
        # CURRENT_CMD 会被面板轮询冲掉成别的命令，只认自己这条
        if not cur or abs(float(cur.get("started") or 0) - started) > 5:
            continue
        snap = tools.progress_snapshot(cur)
        elapsed = int(snap["elapsed"])
        if elapsed - last_sent < _BUSY_NOTE_EVERY:
            continue
        last_sent = elapsed
        if not snap["alive"] and not snap["running"]:
            continue
        tail = (snap["output"] or "").strip().splitlines()[-1:] or [""]
        note = f"还在跑 · 已 {elapsed} 秒"
        if tail[0]:
            note += f"：{tail[0][-60:]}"
        notes.append(note)
        try:
            await manager.broadcast({"type": "progress", "text": note,
                                     "ts": time.time(), **_turn_tag()})
        except Exception:
            pass


# 长工具每隔多少秒给用户 / 模型喂一次进度。120 秒是用户提的节奏：
# 一分钟级的小任务不该被打扰，拖到两分钟以上就该有人说句话
_BUSY_NOTE_EVERY = 120


def _pack_image(data: str, prompt: str = "") -> tuple[dict | None, bytes | None]:
    """解析并压缩一张图，返回 (多模态消息, 压缩后的字节)

    字节那份是给 _save_incoming_shot 落盘用的：各处显示和模型看到的必须是同一张，
    否则会出现「界面上一张、模型看到的另一张」。
    data URL 形如 data:image/png;base64,xxxx；裸 base64 按文件头认格式。
    MIME 是唯一的判断依据：宁可返回 None 让调用方报错，也不能把一段
    解析失败的字符串当成图片发给模型，那会变成一条纯粹的空提问。
    """
    import base64
    raw = (data or "").strip()
    if not raw:
        return None, None
    mime = "image/png"
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        meta = head[5:]
        mime = (meta.split(";")[0] or mime).lower()
        if not meta.lower().endswith(";base64"):
            raw = base64.b64encode(raw.encode()).decode()
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        return None, None
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:
        return None, None
    # 裸 base64 没法从字符串本身看出格式，只能认文件头。插件现在发的是 JPEG
    # （PNG 有 12MB，超过 LLM 网关的请求体上限），按 PNG 标上去会让模型收错格式
    if not raw.startswith("data:"):
        if blob[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            mime = "image/webp"
    # 超过软门槛就压成 JPEG：插件那张 q90 的 1.8MB 截图 base64 有 240 万字符，
    # 越过 _MAX_IMG_CHARS 之后，重发历史时会被替换成「图片太大已略过」的占位符，
    # 模型从来没看到过这张图 —— 这就是「插件截的图传不上来」的真正原因
    if len(blob) > _IMG_SOFT_BYTES:
        packed = _jpeg_for_llm(blob)
        if packed is not blob:
            blob, mime = packed, "image/jpeg"
            raw = base64.b64encode(blob).decode()
    part = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{raw}"}},
        {"type": "text", "text": prompt or _shot_caption(1)},
    ]}
    return part, blob


def _jpeg_for_llm(blob: bytes, max_side: int = 1024, quality: int = 75) -> bytes:
    """把一张图压成适合发给模型的 JPEG

    插件截的是 1280x800 全屏 JPEG q90，一张游戏画面就有 1.8MB，base64 之后
    240 万字符，越过 _MAX_IMG_CHARS 就会被 _shrink_oversized_images 换成
    「图片太大已略过」—— 模型只看到这句话，于是让用户重发，用户看到的就是
    「插件截的图传不上来」。而 PC 网页上传的小图没这个问题，所以只有插件中招。
    压到 1024 宽 q75 只有 160KB，看图问答看不出差别，重发历史也安全。
    压不动就原样返回，交给原来的保护逻辑兜底。
    """
    try:
        from io import BytesIO

        from PIL import Image
        img = Image.open(BytesIO(blob))
        img.load()
        img.thumbnail((max_side, max_side))
        buf = BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        # 压完反而更大（本来就小的图）就保留原样
        return out if len(out) < len(blob) else blob
    except Exception:
        return blob


def _save_incoming_shot(blob: bytes) -> dict | None:
    """把随提问附带的画面落成文件，返回一条截图事件

    和模型自己截图（tools.screenshot）用同一个目录、同一个 /shots/ 前缀，
    前端不需要为两种来源写两套渲染。存不下来只影响「看不看得到图」，
    不影响这一轮问答，所以失败就返回 None。
    blob 传的是 _pack_image 压过的那份，界面显示的和模型看到的才是同一张。
    """
    ext = ".png"
    if blob[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        ext = ".webp"
    shots_dir = Path(__file__).resolve().parent / "static" / "shots"
    try:
        # parents=True：static/shots 在干净部署里并不存在，只写 exist_ok
        # 时 mkdir 会直接抛 FileNotFoundError，被下面 except 吞掉，
        # 表现就是「插件带的图永远上不了网页」而日志里一个字都没有
        shots_dir.mkdir(parents=True, exist_ok=True)
        # 清理门槛远大于「留最近 40 张」：模型那边现在也按文件名回读这张图
        # （见 _shot_ref_content），文件一删，历史里那条提问就变成了
        # 「界面破图 + 模型看不到画面」。900 张之后才会动到最老的那批，
        # 早就被上下文压缩带过去了
        old = sorted(shots_dir.glob("ask_*.*"), key=lambda p: p.stat().st_mtime)
        for p in old[:-900]:
            p.unlink(missing_ok=True)
        # 微秒级时间戳：连着两次截图落在同一毫秒会让后一张覆盖前一张
        name = f"ask_{int(time.time() * 1_000_000)}{ext}"
        (shots_dir / name).write_bytes(blob)
    except Exception as e:
        # 以前这里是完全静默的，图片传不上来时无从下手排查；
        # 失败只影响「看不看得到图」，这一轮问答照常继续
        print(f"[DeckMeido] 附带画面落盘失败（{shots_dir}）：{e!r}",
              file=sys.stderr)
        return None
    print(f"[DeckMeido] 附带画面已落盘：{name}（{len(blob)} 字节）",
          file=sys.stderr)
    return {"type": "screenshot", "name": "screenshot",
            "summary": "截屏询问时附带的画面", "image": f"/shots/{name}",
            # 原始文件名：历史里用它拼成 [SHOT:xxx] 标记，发给模型时再按文件读
            "file": name}


def _expand_context(text: str, workspace: Path | None = None) -> str:
    """把 @{路径} 引用展开为文件内容（每个最多 6000 字符）

    workspace 只在解析相对路径时用：Decky 插件以 root 身份跑，Path.home()
    是 /root，不显式指定的话插件发来的 @{文件} 会全部读不到。
    """
    root = workspace or tools.WORKSPACE

    def repl(m):
        p = Path(m.group(1)).expanduser()
        if not p.is_absolute():
            p = root / p
        try:
            # 和文件工具同一套边界：非 full 模式下不许把工作区外的文件
            # （配置里的 Key、会话记录）读进提示词发给上游
            p = tools.resolve_in_workspace(str(p))
            # 图片不能 read_text 成乱码喂给模型：换成 [IMG:路径] 标记，
            # 由 _build_llm_msgs 里的 _img_ref_content 现读成 base64 图片部件，
            # 和截图 [SHOT:] 同一套机制（历史里只存几十字节标记，不存 base64）
            if p.suffix.lower() in _IMG_EXT:
                return f"\n[IMG:{p}]\n"
            # 其余二进制（zip/exe/bin 等）也不能 read_text 成乱码：
            # 用 NUL 字节探测，命中就给一句说明，不再塞二进制给模型
            head = p.read_bytes()[:8192]
            if b"\x00" in head:
                return (f"\n[文件 {p} 是二进制文件，无法作为文本展开。"
                        f"可用 run_command 的 file/xxd/strings 查看片段]\n")
            body = p.read_text(encoding="utf-8", errors="replace")[:6000]
            return f"\n[文件 {p} 的内容]：\n```\n{body}\n```\n"
        except tools.OutsideWorkspace:
            return (f"\n[文件 {p} 不在工作区内，已跳过。"
                    f"需要读它请让用户切到「完全访问」]\n")
        except OSError as e:
            return f"\n[文件 {p} 读取失败：{e}]\n"
    return _CTX_RE.sub(repl, text) if "@{" in text else text


_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".sh",
             ".yml", ".yaml", ".toml", ".ini", ".cfg", ".xml", ".html", ".css",
             ".c", ".h", ".cpp", ".rs", ".go", ".java", ".sql", ".conf"}
# 单个附件上限，防止一次拖进几个 G 的文件把服务拖死
_UPLOAD_MAX = 20 * 1024 * 1024

# 文件管理器搬运文件不再写死 500MB：上限 = 目标磁盘剩余空间 - 安全余量。
# 余量默认 128MB；剩余不足 256MB 时改留一半，避免「还有 100MB 却完全不让传」。
UPLOAD_FREE_RESERVE = 128 * 1024 * 1024


def _fmt_bytes(n: int) -> str:
    """人类可读体积（1024 进制，与前端 fbFmtBytes 同一口径）"""
    value = float(max(0, int(n or 0)))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[idx]}"


def upload_limits(path) -> dict:
    """目标目录所在磁盘的可用空间与单文件上限（唯一判定口径）。

    limit = free - reserve；reserve 是留给系统/其它进程的安全余量。
    拿不到磁盘信息（异常文件系统）时 free 记 0，宁可拒绝也不盲写。
    """
    try:
        free = int(shutil.disk_usage(str(path)).free)
    except OSError:
        free = 0
    free = max(0, free)
    reserve = min(UPLOAD_FREE_RESERVE, free // 2)
    return {"free": free, "reserve": reserve, "limit": max(0, free - reserve)}


def _upload_limit_error(limit: int, size: int) -> dict:
    """413 的统一结构：文案里同时给出「当前上限」和「该文件大小」"""
    return {
        "ok": False,
        "detail": (f"单文件超过当前上限 {_fmt_bytes(limit)}，"
                   f"该文件 {_fmt_bytes(size)}"),
        "limit": int(limit),
        "size": int(size),
    }


def _upload_target_dir(dir: str) -> Path:
    """上传目标目录：展开 ~、转绝对路径、校验存在（三个上传接口共用）"""
    target = Path(dir).expanduser() if (dir or "").strip() else tools.WORKSPACE
    if not target.is_absolute():
        target = tools.WORKSPACE / target
    try:
        target = target.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="目标目录无效")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="目标目录不存在")
    return target


async def _extract_text(raw: bytes, ext: str):
    """尽量把文档抽成纯文本；抽不出来返回 None（按二进制附件处理）"""
    if ext in _TEXT_EXT:
        return raw.decode("utf-8", errors="replace")
    if ext == ".docx":
        # docx 就是个 zip，正文在 word/document.xml，去掉标签即可
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        except (KeyError, zipfile.BadZipFile, OSError):
            return None
        xml = xml.replace("</w:p>", "\n")
        return re.sub(r"<[^>]+>", "", xml).strip()
    if ext == ".pdf":
        # 有 pdftotext 就抽文字层，没有就算了。
        # 走 to_thread：大 PDF 解析可能要十几秒，阻塞事件循环会让所有 ws 心跳停摆
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["pdftotext", "-q", "-", "-"], input=raw,
                capture_output=True, timeout=25,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace") or None
    return None


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def api_upload(file: UploadFile):
    """上传附件。

    图片 → static/uploads/，返回 /uploads/xxx.png，前端插成 ![图片](url) 走多模态；
    其他文件 → 工作区 uploads/，返回相对工作区的路径，前端插成 @{uploads/xxx}，
    由 _expand_context 带上正文（文本类直接读，docx/pdf 现抽文字）。
    """
    try:
        raw = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取上传内容失败：{e}") from e
    if len(raw) > _UPLOAD_MAX:
        raise HTTPException(status_code=413, detail="文件超过 20MB")
    fn = Path(file.filename or "file")
    ext = fn.suffix.lower()
    stem = re.sub(r"[^\w\-.]+", "_", fn.stem)[:40] or "file"
    ts = int(time.time())

    if ext in _IMG_EXT or not ext:
        ext = ext if ext in _IMG_EXT else ".png"
        up = paths.uploads_dir()
        up.mkdir(exist_ok=True)
        name = f"up_{ts}{ext}"
        (up / name).write_bytes(raw)
        return {"url": f"/uploads/{name}", "kind": "image", "name": fn.name}

    up = tools.WORKSPACE / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    body = await _extract_text(raw, ext)
    if body is not None:
        # 有文字层就存成 .txt，@{} 展开时不会喂二进制给模型
        keep = up / f"{ts}_{stem}.txt"
        keep.write_text(f"# {fn.name}\n\n{body}", encoding="utf-8")
        kind = "text"
    else:
        keep = up / f"{ts}_{stem}{ext}"
        keep.write_bytes(raw)
        kind = "binary"
    rel = str(keep.relative_to(tools.WORKSPACE))
    return {"url": f"/uploads/{keep.name}", "path": rel, "kind": kind, "name": fn.name}


@app.post("/api/upload-to", dependencies=[Depends(require_auth)])
async def api_upload_to(file: UploadFile, dir: str = ""):
    """文件管理器里的拖放上传（multipart 版，Android 客户端在用）。

    和 /api/upload 不同，这里不做文本抽取、不塞进对话上下文，就是纯搬运。
    上限按目标磁盘剩余空间动态判定（见 upload_limits），写入仍是分块流式的。
    网页端改用 /api/upload-stream（原始请求体直接落盘，不经过 multipart 临时文件）。
    """
    target_dir = _upload_target_dir(dir)
    denied = _fs_denied_response(target_dir)
    if denied:
        return denied
    fn = Path(file.filename or "file").name
    dest = target_dir / fn
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"已存在同名文件：{fn}")
    caps = upload_limits(target_dir)
    limit = caps["limit"]
    declared = int(file.size or 0)
    if declared > 0 and declared > limit:
        return JSONResponse(status_code=413,
                            content=_upload_limit_error(limit, declared))
    written = 0
    too_large = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    too_large = max(declared, written)
                    break
                out.write(chunk)
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"写入失败：{e}") from e
    if too_large:
        dest.unlink(missing_ok=True)
        return JSONResponse(status_code=413,
                            content=_upload_limit_error(limit, too_large))
    return {"ok": True, "name": fn, "dir": str(target_dir), "size": written}


@app.get("/api/upload-limit", dependencies=[Depends(require_auth)])
async def api_upload_limit(dir: str = ""):
    """上传前查询目标目录的单文件上限（前端据此给出「当前上限 X，该文件 Y」）。"""
    target_dir = _upload_target_dir(dir)
    denied = _fs_denied_response(target_dir)
    if denied:
        return denied
    return {"ok": True, "dir": str(target_dir), **upload_limits(target_dir)}


@app.post("/api/upload-stream", dependencies=[Depends(require_auth)])
async def api_upload_stream(request: Request, dir: str = "", name: str = ""):
    """原始流上传：请求体就是文件本身，边收边写盘。

    网页文件管理器走这条路径。multipart 解析会把整份内容先落到临时文件，
    大文件等于多占一份磁盘、也多了「先落整份拷贝」这一步；这里直接读
    request.stream()，1MB 一块写进目标文件，内存占用恒定。
    权限判定仍走 _fs_denied_response（危险区/只读挂载一律 403，不能绕过）。
    """
    target_dir = _upload_target_dir(dir)
    denied = _fs_denied_response(target_dir)
    if denied:
        return denied
    fn = Path(name or "file").name
    if not fn or fn in (".", ".."):
        raise HTTPException(status_code=400, detail="文件名为空")
    dest = target_dir / fn
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"已存在同名文件：{fn}")
    caps = upload_limits(target_dir)
    limit = caps["limit"]
    try:
        declared = int(request.headers.get("x-file-size") or 0)
    except ValueError:
        declared = 0
    # 客户端声明的大小超限：先回 413，不必让对方把几个 G 传完再报错
    if declared > 0 and declared > limit:
        return JSONResponse(status_code=413,
                            content=_upload_limit_error(limit, declared))
    written = 0
    too_large = 0
    try:
        with open(dest, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                # 写之前先判上限：写到一半发现超了就先停，落盘量不会超过 limit
                if written > limit:
                    too_large = max(declared, written)
                    break
                out.write(chunk)
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"写入失败：{e}") from e
    if too_large:
        dest.unlink(missing_ok=True)
        return JSONResponse(status_code=413,
                            content=_upload_limit_error(limit, too_large))
    return {"ok": True, "name": fn, "dir": str(target_dir), "size": written}


@app.get("/api/files", dependencies=[Depends(require_auth)])
async def api_files():
    """列出工作区文件（@ 引用候选）"""
    out = []
    try:
        for p in sorted(tools.WORKSPACE.rglob("*")):
            if p.is_file() and not any(part.startswith(".") for part in p.parts):
                out.append(str(p.relative_to(tools.WORKSPACE)))
                if len(out) >= 300:
                    break
    except OSError:
        pass
    return {"workspace": str(tools.WORKSPACE), "files": out}


# 文件浏览器预览用的分类。只影响面板里显示什么图标、用哪种方式预览，
# 和「能不能读」无关，所以放成白名单也无所谓
_KIND_MAP = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"},
    "video": {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv"},
    "audio": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".oga"},
    "pdf": {".pdf"},
    "text": {".txt", ".md", ".log", ".csv", ".json", ".toml", ".ini", ".cfg", ".yml",
             ".yaml", ".xml", ".html", ".css", ".js", ".ts", ".py", ".sh", ".c", ".h",
             ".cpp", ".rs", ".go", ".java", ".sql", ".conf", ".srt", ".desktop"},
}


def _file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    for kind, exts in _KIND_MAP.items():
        if ext in exts:
            return kind
    return "other"


def _fs_workspace_roots() -> list[str]:
    """分区判定的工作区：配置的工作区 + 安装目录（Deck 上是 /home/deck/DeckMeido）。"""
    roots: list[str] = []
    for root in (tools.WORKSPACE, INSTALL_DIR):
        if root:
            roots.append(str(root))
    return roots


_FS_DENY_MSG = {
    fszones.REASON_READ_ONLY_ZONE: "危险区域（系统目录）只读，不能执行写操作",
    fszones.REASON_READ_ONLY_MOUNT: "该挂载点当前是只读的（写保护），不能写入",
    fszones.REASON_FORBIDDEN_ZONE: "该目录禁止访问",
}


def _fs_denied_response(path) -> JSONResponse | None:
    """写操作统一拦截：危险区 / 只读挂载返回 403 + 机器可读原因。

    前端也按同一套 zone 置灰按钮，但这里是唯一事实来源——绕过前端直接
    打接口同样会被拦住。
    """
    roots = _fs_workspace_roots()
    info = fszones.zone_info(path, roots)
    reason = fszones.write_block(path, roots)
    if not reason:
        return None
    return JSONResponse(status_code=403, content={
        "ok": False,
        "reason": reason,
        "zone": info["zone"],
        "path": info["path"],
        "error": _FS_DENY_MSG.get(reason, "该位置不可写"),
    })


@app.get("/api/preview", dependencies=[Depends(require_auth)])
async def api_preview(path: str, raw: int = 0):
    """给文件浏览器预览用：吐任意路径的文件

    和 /files/{path} 不一样：那个锁死在工作区里，只给对话附件用；
    面板要能预览 AI 正在看的任何目录，所以这里认绝对路径。
    默认回 JSON（元信息 + 文本前 64KB，前端一次拿齐）；
    raw=1 时直接回文件本体，给 <img>/<video>/<audio>/<iframe> 用——
    视频/音频要能拖进度条就得支持 Range，所以走 FileResponse。
    """
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if fszones.browse_denied(target, _fs_workspace_roots()):
        # /proc、/sys、/dev 连读都不给：这类"文件"多数是设备/内核视图，
        # 预览它们既没意义也容易把进程拖住
        return JSONResponse(status_code=403, content={
            "ok": False, "zone": fszones.ZONE_FORBIDDEN,
            "reason": fszones.REASON_FORBIDDEN_ZONE,
            "path": str(target),
            "error": "该目录禁止浏览（/proc、/sys、/dev）",
        })
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if raw:
        # FileResponse 自带 Range / ETag 处理，浏览器可以边下边播
        return FileResponse(target)
    kind = _file_kind(target.name)
    try:
        stat = target.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    info = {"path": str(target), "name": target.name, "kind": kind,
            "size": stat.st_size, "mtime": stat.st_mtime}
    if kind == "text":
        try:
            info["text"] = target.read_bytes()[:65536].decode("utf-8", "replace")
        except OSError as e:
            info["error"] = f"读取失败：{e}"
    return info


@app.get("/api/browse", dependencies=[Depends(require_auth)])
async def api_browse(path: str = ""):
    """列单层目录内容（右侧文件浏览器用）

    与 /api/files 的区别：那个只吐工作区里的文件、一次全量；这里要能浏览
    任意目录（AI 可能在看 /home/deck/xxx），所以只列一层、按需展开。
    路径不存在或没权限时返回 error，前端显示成一行提示而不是整块报错。
    """
    raw = (path or "").strip()
    # 不传 path 就回到工作区，用户第一次打开面板看到的是自己的工作区
    target = Path(raw).expanduser() if raw else tools.WORKSPACE
    if not target.is_absolute():
        # 相对路径一律相对工作区解释，避免 cwd 变化导致行为漂移
        target = tools.WORKSPACE / target
    # AI 给的是文件路径时列它所在的目录并把它标出来，面板据此高亮；
    # 联动事件里不带 is_dir 也能靠这个兜住
    select = ""
    parent = None
    try:
        requested = target
        target = target.resolve()
        # 先算父级：无权限/不可读/不是目录时也要能靠「上一级」退回去，
        # 不能因为列表失败就把导航一起判死
        parent = str(target.parent) if str(target.parent) != str(target) else None
        # 断链：链接还在、目标没了。明确说清楚，别让用户点进去看到空白页
        if requested.is_symlink() and not requested.exists():
            z = fszones.zone_info(target, _fs_workspace_roots())
            link_parent = (str(requested.parent)
                           if str(requested.parent) != str(requested) else None)
            return {"path": str(requested), "parent": link_parent, "entries": [],
                    "link_broken": True, "link_target": str(target),
                    "zone": z["zone"], "readonly": z["readonly"],
                    "zone_label": z["label"], "zone_reason": z["reason"],
                    "error": f"链接目标不存在：{requested} -> {target}"}
        if target.is_file():
            select = target.name
            target = target.parent
            parent = str(target.parent) if str(target.parent) != str(target) else None
    except OSError:
        return {"path": raw, "parent": None, "entries": [], "error": "路径无效"}

    zone = fszones.zone_info(target, _fs_workspace_roots())
    zone_fields = {"zone": zone["zone"], "readonly": zone["readonly"],
                   "zone_label": zone["label"], "zone_reason": zone["reason"]}
    if zone["zone"] == fszones.ZONE_FORBIDDEN:
        # /proc、/sys、/dev 不是只读，是根本不给进；前端按 reason 显示提示
        return JSONResponse(status_code=403, content={
            "ok": False, "path": str(target), "parent": parent, "entries": [],
            "reason": fszones.REASON_FORBIDDEN_ZONE, **zone_fields,
            "error": f"{target} 属于禁止浏览的系统目录（/proc、/sys、/dev），已拒绝进入",
        })

    if not target.is_dir():
        # 工作区目录可能被用户/清理脚本删掉：打开文件管理器前先按工作区重建一次，
        # 仍失败就把具体原因说清楚，别只回一句「目录不存在或不是文件夹」
        if not raw and str(target) == str(Path(tools.WORKSPACE).resolve()):
            if ensure_workspace(str(tools.WORKSPACE)):
                target = Path(tools.WORKSPACE)
            else:
                return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                        "error": f"工作区目录 {target} 建不出来（{WORKSPACE_ERROR}）。"
                                 "请检查该路径是否有写权限，或在设置里换一个目录"}
        else:
            return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                    "error": f"目录 {target} 不存在"}
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                # 软链接要跟随判断：/run/media/ 下的 SDCARD 是"指向 SD 卡的目录链接"，
                # 用 follow_symlinks=False 会把它当成普通文件，点不进去
                try:
                    is_link = e.is_symlink()
                except OSError:
                    is_link = False
                if is_link:
                    try:
                        is_dir = e.is_dir(follow_symlinks=True)
                    except OSError:
                        is_dir = False
                else:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                item = {"name": e.name, "dir": is_dir}
                if is_link:
                    item["link"] = True
                    try:
                        item["link_target"] = os.path.realpath(e.path)
                    except OSError:
                        item["link_target"] = ""
                    # 断链：exists 会跟随链接，False 说明目标没了。
                    # 注意 DirEntry 没有 exists() 方法（用了会 AttributeError → 整目录 500）
                    try:
                        item["broken"] = not os.path.exists(e.path)
                    except OSError:
                        item["broken"] = False
                if not is_dir:
                    # kind 给前端挑图标和预览方式用（image/video/audio/pdf/text/other）
                    item["kind"] = _file_kind(e.name)
                # 大小与修改时间：目录也带 mtime，条目一律显示「类型/大小/修改时间」
                try:
                    st = e.stat(follow_symlinks=False)
                    item["mtime"] = st.st_mtime
                    if not is_dir:
                        item["size"] = st.st_size
                except OSError:
                    pass
                entries.append(item)
    except PermissionError:
        return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                "error": "没有权限读取这个目录"}
    except OSError as e:
        return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                "error": f"读取失败：{e}"}
    # 文件夹在前、同类按名字排，隐藏文件照样列出（用户可能就是要看 .config）
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = str(target.parent) if str(target.parent) != str(target) else None
    return {"path": str(target), "parent": parent, "entries": entries,
            "select": select, **zone_fields}


class _FsReq(BaseModel):
    op: str
    path: str
    path2: str = ""


@app.post("/api/fs", dependencies=[Depends(require_auth)])
async def api_fs(req: _FsReq):
    """文件面板的右键操作：rename / delete / copy / move / mkdir。

    面板本来就能浏览任意目录（/api/browse、/api/preview 都认绝对路径），
    所以这里同样不做工作区限制——这些是用户在面板上手动点的操作，
    不是 AI 越权读文件。
    写操作按 fszones 的三级分区统一拦截：危险区只读，谨慎区可写（挂载 ro 时只读），
    返回 403 + reason（read_only_zone / read_only_mount / forbidden_zone）。
    """
    op = req.op
    try:
        p = Path(req.path).expanduser().resolve()
    except OSError:
        return {"ok": False, "error": "路径无效"}

    # 分区写保护：危险区和只读挂载一律 403。改名/移动/复制要连目标一起判，
    # 防止「从谨慎区搬到 /etc」或者经软链接父目录落到危险区。
    check_paths = []
    if op in ("delete", "mkdir", "rename", "move"):
        check_paths.append(p)
    p2 = None
    if op in ("rename", "move", "copy"):
        try:
            p2 = Path(req.path2).expanduser().resolve()
        except OSError:
            return {"ok": False, "error": "路径无效"}
        check_paths.append(p2)
    for cand in check_paths:
        denied = _fs_denied_response(cand)
        if denied:
            return denied

    # 根目录和家目录本身不给动，防止手滑把整个盘删了
    if op in ("delete", "rename", "move") and (p == Path(p.anchor) or p == Path.home()):
        return {"ok": False, "error": "不允许对根目录/家目录执行此操作"}

    try:
        # 这几个操作都是同步阻塞的，而 copytree / rmtree 碰上大目录能跑上几十秒。
        # 直接写在 async 函数里，整段时间事件循环是停的：流式输出断掉、心跳发不出去、
        # 其它接口全部排队。丢进线程池跑，循环照常转
        if op == "delete":
            if p.is_dir() and not p.is_symlink():
                await asyncio.to_thread(shutil.rmtree, p)
            else:
                await asyncio.to_thread(p.unlink, missing_ok=True)
        elif op == "mkdir":
            # path 是父目录，path2 是目录名
            name = (req.path2 or "").strip()
            if not name or "/" in name or name in (".", ".."):
                return {"ok": False, "error": "目录名不合法"}
            (p / name).mkdir(parents=False, exist_ok=False)
        elif op in ("rename", "move", "copy"):
            if p2 is None:
                return {"ok": False, "error": "目标路径无效"}
            if p2 == p:
                return {"ok": False, "error": "源和目标相同"}
            if p2.exists():
                return {"ok": False, "error": "目标已存在"}
            p2.parent.mkdir(parents=True, exist_ok=True)
            if op == "rename" or op == "move":
                await asyncio.to_thread(shutil.move, str(p), str(p2))
            else:
                if p.is_dir():
                    await asyncio.to_thread(shutil.copytree, p, p2)
                else:
                    await asyncio.to_thread(shutil.copy2, p, p2)
        else:
            return {"ok": False, "error": "未知操作"}
    except FileNotFoundError:
        return {"ok": False, "error": "文件不存在"}
    except FileExistsError:
        return {"ok": False, "error": "目标已存在"}
    except PermissionError:
        return {"ok": False, "error": "没有权限"}
    except OSError as e:
        return {"ok": False, "error": f"操作失败：{e}"}
    return {"ok": True}


@app.get("/api/terminal", dependencies=[Depends(require_auth)])
async def api_terminal():
    """当前正在跑（或刚跑完）的那条命令，给右侧终端面板轮询

    只返回最近一条：历史在面板本地攒。没有命令时 state 为 null。
    """
    return {"state": tools.CURRENT_CMD}


@app.post("/api/terminal/stop", dependencies=[Depends(require_auth)])
async def api_terminal_stop():
    """终止面板里正在跑的那条命令（小窗上的终止按钮）

    先砍进程树（按登记表），再取消正在等它的对话轮次。不 cancel 的话，
    /api/terminal/run 那个请求会一直挂在里面等子进程，前端按钮永远转圈；
    但它可能是用户自己敲的，没有对话轮次可取消，所以这一步允许为空。
    """
    killed = await tools.abort_all_procs()
    if current_task and not current_task.done():
        current_task.cancel()
        set_overlay(False)
    return {"ok": True, "killed": killed}


# 面板自己敲的命令：和白名单无关，但同样要过危险判定，
# 用户手动敲的也按当前权限模式审批，避免网页端变成绕过审批的后门
_term_lock: asyncio.Lock | None = None


def _term_lock_get() -> asyncio.Lock:
    """惰性建锁：模块导入时还没有事件循环，提前 new 出来会绑到错的循环上"""
    global _term_lock
    if _term_lock is None:
        _term_lock = asyncio.Lock()
    return _term_lock


@app.post("/api/terminal/run", dependencies=[Depends(require_auth)])
async def api_terminal_run(payload: dict):
    """面板里手动执行一条命令（终端 tab 的输入框）

    改造后：起一个真 PTY 会话（ptyprocess），不再走 script -qefc 模拟。
    命令的生命周期由前端 /ws/terminal WebSocket 接管——输出实时推流，
    按键实时写回。这里只负责「起命令 + 审批」，不阻塞等待输出。
    """
    cmd = str(payload.get("command") or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="命令为空")
    # 和对话里同一个审批通道（approval.resolve）：手动模式一律先问，智能/自定义
    # 模式只拦危险命令，完全访问直接跑。面板不能变成绕过审批的后门，也不该自己
    # 另写一套规则 —— 规则只在 approval.py 里维护一份
    verdict = approval.resolve(
        "run_command", {"command": cmd},
        policy.TurnContext(boot_id=_system_boot_id()),
        PERMISSION_MODE, CUSTOM_PERM)
    if verdict.deny:
        return {"ok": False, "denied": True, "reason": verdict.reason,
                "output": f"已拒绝执行（{verdict.label}）：{verdict.reason}"}
    if verdict.ask:
        # 挂进和对话同一份 pending_action，之后由对话里那句「确认」放行
        global pending_action
        args = {"command": cmd, "reason": "终端面板手动执行"}
        pending_action = {"name": "run_command", "args": args,
                          "description": tools.describe("run_command", args),
                          "hard": verdict.hard,
                          "sid": store.current_sid, "epoch": store.epoch,
                          "turn_id": store.turn.get("turn_id") or "",
                          "client": store.turn.get("client") or "",
                          "device": store.turn.get("device") or ""}
        return {"ok": False, "pending": True,
                "description": pending_action["description"]}
    # 串行执行：面板同一时间只有一条 PTY 会话
    lock = _term_lock_get()
    if lock.locked():
        raise HTTPException(status_code=409, detail="上一条命令还在执行")
    async with lock:
        try:
            cols = int(payload.get("cols") or 120)
            rows = int(payload.get("rows") or 36)
            sess = await tools.start_pty_session(
                cmd, cols=cols, rows=rows, reason="面板手动执行")
        except Exception as e:
            return {"ok": False, "output": f"执行失败：{e}"}
    # 返回会话 id（用 pid），前端拿它连 WebSocket
    return {"ok": True, "pid": sess.proc.pid,
            "state": {k: v for k, v in tools.CURRENT_CMD.items() if k != "_pty"}}


@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    """终端专用 WebSocket：PTY ↔ xterm.js 双向字节流透传

    协议（JSON 帧）：
      前端 → 后端：
        {"type":"input","data":"<base64 字节>"}   按键写入 PTY stdin
        {"type":"resize","cols":N,"rows":N}        窗口尺寸变化
        {"type":"stop"}                            终止当前 PTY 会话
      后端 → 前端：
        {"type":"output","data":"<base64 字节>"}   PTY stdout 推流
        {"type":"exited","code":N}                 PTY 进程退出
        {"type":"error","msg":"..."}               出错

    用 base64 传字节：WebSocket 文本帧不能塞任意字节，二进制帧又不好混 JSON 头。
    """
    client_ip = ws.client.host if ws.client else ""
    if not _ws_authorized(ws):
        audit.record("auth", "ws_terminal", "fail", ip=client_ip, reason="unpaired")
        await ws.accept()
        await ws.close(code=4401)
        return
    await ws.accept()
    sess = tools.get_active_pty()
    if sess is None:
        await ws.send_json({"type": "error", "msg": "没有活跃的终端会话"})
        await ws.close()
        return
    import base64

    async def _push_output():
        """从 PTY 输出队列取字节，推给前端"""
        while sess.is_alive() or not sess.out_q.empty():
            chunk = await sess.out_q.get()
            if not chunk:
                # 哨兵：PTY 已退出
                break
            await ws.send_json({"type": "output",
                                "data": base64.b64encode(chunk).decode("ascii")})
        # 推退出码
        await ws.send_json({"type": "exited", "code": sess.exit_code})

    push_task = asyncio.create_task(_push_output())
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "input":
                raw = base64.b64decode(data.get("data") or "")
                if raw:
                    sess.write(raw)
            elif t == "resize":
                cols = int(data.get("cols") or 120)
                rows = int(data.get("rows") or 36)
                sess.resize(cols, rows)
            elif t == "stop":
                sess.stop()
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        push_task.cancel()
        try:
            await push_task
        except asyncio.CancelledError:
            pass


# ---------- 远程终端（多会话 / 真 PTY） ----------
# 与上面的单会话面板终端完全独立：不复用 run_command 的审批 ContextVar，
# 开关默认关闭，危险命令按 policy 判定并要求客户端显式确认。

class RemoteTerminalCreate(BaseModel):
    cwd: str | None = None
    cols: int = 120
    rows: int = 36
    # 来源标识（手机 / 网页 / 客户端名），只进审计，不参与鉴权
    source: str = ""


class RemoteTerminalExec(BaseModel):
    command: str
    confirm: bool = False
    source: str = ""


class RemoteTerminalInput(BaseModel):
    # UTF-8 文本或 base64 二进制（控制序列用它）
    data: str = ""
    encoding: str = "utf-8"
    confirm: bool = False
    source: str = ""


def _terminal_settings() -> remote_terminal.TerminalSettings:
    return remote_terminal.settings_from(CFG)


def _terminal_guard() -> JSONResponse | None:
    """开关关闭时统一 403；CFG 每次现读，热更新/测试直改都能生效。"""
    settings = _terminal_settings()
    remote_terminal.manager.configure(CFG)
    if not settings.enabled:
        return _terminal_error(remote_terminal.TerminalError(
            "remote_shell_disabled",
            "远程终端未开启：[terminal] remote_shell = false，可在设置页打开",
            403))
    return None


def _terminal_error(exc: remote_terminal.TerminalError) -> JSONResponse:
    body = {"ok": False, "error_code": exc.code, "reason": exc.reason}
    body.update(exc.extra)
    return JSONResponse(status_code=exc.status, content=body)


def _terminal_session_or_error(sid: str):
    session = remote_terminal.manager.get(sid)
    if session is None:
        return None, _terminal_error(remote_terminal.TerminalError(
            "session_not_found", "会话不存在或已过期", 404))
    return session, None


def _terminal_confirm_error(result: dict) -> JSONResponse:
    return _terminal_error(remote_terminal.TerminalError(
        "confirm_required",
        str(result.get("reason") or "危险命令需要确认"),
        428,
        {
            "need_confirm": True,
            "level": result.get("level"),
            "rule": result.get("rule"),
            "command": result.get("command"),
        }))


@app.get("/api/terminal/sessions", dependencies=[Depends(require_auth)])
async def api_terminal_sessions():
    """远程终端开关、默认值与当前会话列表。"""
    denied = _terminal_guard()
    if denied is not None:
        return denied
    settings = _terminal_settings()
    return {
        "ok": True,
        "enabled": settings.enabled,
        "settings": settings.public(),
        "sessions": [s.snapshot() for s in remote_terminal.manager.sessions()],
    }


@app.post("/api/terminal/sessions", dependencies=[Depends(require_auth)])
async def api_terminal_session_create(req: RemoteTerminalCreate, request: Request):
    """创建一个常驻 shell 会话。"""
    denied = _terminal_guard()
    if denied is not None:
        return denied
    ip = _client_ip(request)
    try:
        session = await remote_terminal.manager.create(
            owner=ip, source=(req.source or "").strip()[:60],
            cwd=req.cwd, cols=req.cols, rows=req.rows)
    except remote_terminal.TerminalError as exc:
        return _terminal_error(exc)
    return {"ok": True, "session": session.snapshot()}


@app.get("/api/terminal/sessions/{sid}", dependencies=[Depends(require_auth)])
async def api_terminal_session_get(sid: str):
    denied = _terminal_guard()
    if denied is not None:
        return denied
    session, error = _terminal_session_or_error(sid)
    if error is not None:
        return error
    return {"ok": True, "session": session.snapshot()}


@app.post("/api/terminal/sessions/{sid}/exec", dependencies=[Depends(require_auth)])
async def api_terminal_session_exec(sid: str, req: RemoteTerminalExec,
                                    request: Request):
    """提交一条命令：命中危险判定时返回 428，客户端带 confirm=true 再提交。"""
    denied = _terminal_guard()
    if denied is not None:
        return denied
    session, error = _terminal_session_or_error(sid)
    if error is not None:
        return error
    try:
        result = session.run(
            req.command, req.confirm, _client_ip(request),
            (req.source or "").strip()[:60])
    except remote_terminal.TerminalError as exc:
        return _terminal_error(exc)
    if result.get("need_confirm"):
        return _terminal_confirm_error(result)
    return {
        "ok": True,
        "token": result["token"],
        "confirmed": result["confirmed"],
        "level": result["level"],
        "session": result["session"],
    }


@app.post("/api/terminal/sessions/{sid}/input", dependencies=[Depends(require_auth)])
async def api_terminal_session_input(sid: str, req: RemoteTerminalInput,
                                     request: Request):
    """原始 stdin 写入：交互程序的按键 / 密码输入走这里。"""
    denied = _terminal_guard()
    if denied is not None:
        return denied
    session, error = _terminal_session_or_error(sid)
    if error is not None:
        return error
    encoding = (req.encoding or "utf-8").strip().lower()
    if encoding in ("utf-8", "utf8", "text"):
        raw = req.data.encode("utf-8")
    elif encoding in ("base64", "b64"):
        try:
            raw = base64.b64decode(req.data, validate=True)
        except Exception:
            return _terminal_error(remote_terminal.TerminalError(
                "bad_encoding", "data 不是合法的 base64", 400))
    else:
        return _terminal_error(remote_terminal.TerminalError(
            "bad_encoding", "encoding 只能是 utf-8 或 base64", 400))
    try:
        result = session.feed(
            raw, req.confirm, _client_ip(request),
            (req.source or "").strip()[:60])
    except remote_terminal.TerminalError as exc:
        return _terminal_error(exc)
    if result.get("need_confirm"):
        return _terminal_confirm_error(result)
    return {"ok": True, "bytes": result.get("bytes", len(raw))}


@app.post("/api/terminal/sessions/{sid}/terminate",
          dependencies=[Depends(require_auth)])
async def api_terminal_session_terminate(sid: str, request: Request):
    """终止会话：先 SIGTERM，1 秒不退再强杀，进程组一起收。"""
    denied = _terminal_guard()
    if denied is not None:
        return denied
    session, error = _terminal_session_or_error(sid)
    if error is not None:
        return error
    was_alive = session.alive
    await session.terminate("terminated")
    if was_alive:
        audit.record(
            "terminal", "close", "ok", ip=_client_ip(request),
            reason="terminated", detail="", extra={"session_id": sid})
    return {"ok": True, "already_ended": not was_alive,
            "session": session.snapshot()}


@app.get("/api/terminal/sessions/{sid}/output",
         dependencies=[Depends(require_auth)])
async def api_terminal_session_output(
        sid: str,
        cursor: int = Query(default=0, ge=0),
        max_bytes: int = Query(
            default=remote_terminal.DEFAULT_OUTPUT_PAGE_BYTES,
            ge=1024, le=remote_terminal.MAX_OUTPUT_PAGE_BYTES)):
    """按游标读取输出块（base64）；客户端断线重连后从这里补历史。"""
    denied = _terminal_guard()
    if denied is not None:
        return denied
    session, error = _terminal_session_or_error(sid)
    if error is not None:
        return error
    chunks, next_cursor, dropped = session.read_output(cursor, max_bytes)
    return {
        "ok": True,
        "session": session.snapshot(),
        "chunks": [
            {"seq": seq, "encoding": "base64",
             "data": base64.b64encode(data).decode("ascii")}
            for seq, data in chunks
        ],
        "next_cursor": next_cursor,
        "dropped": dropped,
        "truncated": session.truncated,
    }


@app.websocket("/ws/terminal/{sid}")
async def ws_terminal_session(ws: WebSocket, sid: str):
    """远程终端输出订阅（独立于面板用的 /ws/terminal）。

    服务端 → 客户端：
      {"type":"hello","session":{...},"cursor":N}
      {"type":"output","seq":N,"encoding":"base64","data":"..."}
      {"type":"cursor","next_cursor":N,"dropped":bool}
      {"type":"truncated","total_bytes":N,"limit":N}
      {"type":"exited","state":"exited|terminated|timeout","exit_code":N}
      {"type":"confirm_required","level":"deny|hard_confirm","rule":"...",
       "command":"...","reason":"..."}
      {"type":"exec_started","token":"...","confirmed":bool}
      {"type":"pong","ts":N}
      {"type":"error","code":"...","reason":"..."}
    客户端 → 服务端：
      {"type":"exec","command":"...","confirm":false}
      {"type":"input","data":"<base64>","confirm":false}
      {"type":"resize","cols":N,"rows":N}
      {"type":"ping"}
      {"type":"stop"}
    """
    client_ip = ws.client.host if ws.client else ""
    if not _ws_authorized(ws):
        audit.record("auth", "ws_remote_terminal", "fail", ip=client_ip,
                     reason="unpaired")
        await ws.accept()
        await ws.close(code=4401)
        return

    await ws.accept()
    settings = _terminal_settings()
    if not settings.enabled:
        await ws.send_json({
            "type": "error",
            "code": "remote_shell_disabled",
            "reason": "远程终端未开启：[terminal] remote_shell = false",
        })
        await ws.close(code=4403)
        return
    session = remote_terminal.manager.get(sid)
    if session is None:
        await ws.send_json({
            "type": "error", "code": "session_not_found",
            "reason": "会话不存在或已过期",
        })
        await ws.close(code=4404)
        return

    try:
        cursor = max(0, int(ws.query_params.get("cursor") or 0))
    except ValueError:
        cursor = 0
    session.touch()
    queue = session.subscribe(cursor)
    await ws.send_json({
        "type": "hello", "session": session.snapshot(), "cursor": cursor,
    })

    async def _pump():
        while True:
            event = await queue.get()
            if event is None:
                break
            await ws.send_json(event)
        try:
            await ws.close()
        except Exception:
            pass

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "ping":
                session.touch()
                await ws.send_json({"type": "pong", "ts": time.time()})
            elif kind == "input":
                try:
                    raw = base64.b64decode(str(data.get("data") or ""),
                                           validate=True)
                except Exception:
                    await ws.send_json({
                        "type": "error", "code": "bad_encoding",
                        "reason": "data 不是合法的 base64",
                    })
                    continue
                try:
                    result = session.feed(
                        raw, bool(data.get("confirm")), client_ip,
                        str(data.get("source") or "ws")[:60])
                except remote_terminal.TerminalError as exc:
                    await ws.send_json({
                        "type": "error", "code": exc.code, "reason": exc.reason,
                    })
                    continue
                if result.get("need_confirm"):
                    await ws.send_json({
                        "type": "confirm_required",
                        "level": result.get("level"),
                        "rule": result.get("rule"),
                        "command": result.get("command"),
                        "reason": result.get("reason"),
                    })
            elif kind == "exec":
                try:
                    result = session.run(
                        str(data.get("command") or ""),
                        bool(data.get("confirm")), client_ip,
                        str(data.get("source") or "ws")[:60])
                except remote_terminal.TerminalError as exc:
                    await ws.send_json({
                        "type": "error", "code": exc.code, "reason": exc.reason,
                    })
                    continue
                if result.get("need_confirm"):
                    await ws.send_json({
                        "type": "confirm_required",
                        "level": result.get("level"),
                        "rule": result.get("rule"),
                        "command": result.get("command"),
                        "reason": result.get("reason"),
                    })
                else:
                    await ws.send_json({
                        "type": "exec_started",
                        "token": result.get("token"),
                        "confirmed": result.get("confirmed", False),
                    })
            elif kind == "resize":
                try:
                    session.resize(int(data.get("cols") or 120),
                                   int(data.get("rows") or 36))
                except Exception:
                    pass
            elif kind == "stop":
                await session.terminate("terminated")
                # 留一点时间把 exited 事件发出去，再关连接
                try:
                    await asyncio.wait_for(asyncio.shield(pump_task), 1.0)
                except Exception:
                    pass
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        session.unsubscribe(queue)
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass


@app.on_event("shutdown")
async def _stop_remote_terminal():
    """服务重启/停止时把所有远程终端会话一起收掉，别留孤儿 shell。"""
    try:
        await remote_terminal.manager.close_all("shutdown")
    except Exception:
        pass


@app.websocket("/ws/input")
async def ws_input(ws: WebSocket):
    """跨屏键鼠接管 WebSocket（PC 浏览器 ↔ Deck 输入注入）

    协议（JSON 帧，前后端共同约定）：
      浏览器 → 后端：
        {"t":"move","dx":int,"dy":int}      相对位移（已乘灵敏度）
        {"t":"btn","b":0|1|2,"p":true|false} 左/右/中键，0=左 1=右 2=中
        {"t":"wheel","d":int}               滚轮格数，正=向下
        {"t":"key","code":"KeyA","p":bool}  浏览器 KeyboardEvent.code
        {"t":"ping"}                        心跳（建议 3 秒一次）
        {"t":"release"}                     结束接管
      后端 → 浏览器：
        {"t":"state","ok":bool,"code":"RI_..."}

    鉴权失败直接 4401 关闭；其余失败先回一条 state 再关，原因码不吞。
    位移在 remote_input.InputSession 里按 ~16ms 合并，不按每个 mousemove 刷进程。
    """
    client_ip = ws.client.host if ws.client else ""
    cfg = CFG.get("remote_input") or {}
    # 已配对浏览器走设备 Cookie，本机回环免验证；两者都算已授权
    authed = _ws_cookie_auth(ws) or netcfg.is_local(client_ip)
    code = remote_input.access_error(cfg, client_ip, authed)
    if code == remote_input.RI_UNAUTHORIZED:
        await ws.accept()
        await ws.close(code=4401)
        return
    if code:
        await ws.accept()
        await ws.send_json({"t": "state", "ok": False, "code": code})
        await ws.close(code=1008)
        return

    await ws.accept()
    try:
        sess = await remote_input.manager.begin(cfg, owner=client_ip)
    except remote_input.RemoteInputError as exc:
        await ws.send_json({"t": "state", "ok": False, "code": exc.code,
                            "detail": exc.detail})
        await ws.close(code=1008)
        return
    await ws.send_json({"t": "state", "ok": True, "code": "RI_ACTIVE",
                        "backend": sess.backend.kind, "max_minutes": sess.max_minutes})

    end_code = remote_input.RI_DISCONNECTED
    try:
        while True:
            remain = sess.deadline - time.monotonic()
            if remain <= 0:
                end_code = remote_input.RI_TIMEOUT
                try:
                    await ws.send_json({"t": "state", "ok": False,
                                        "code": remote_input.RI_TIMEOUT})
                except Exception:
                    pass
                break
            # 同时等「下一条消息」和「外部释放」：客户端点 logo / 停止按钮时
            # 由 /api/remote-input/release 结束会话，这里必须立刻醒来收尾
            recv_task = asyncio.create_task(ws.receive_json())
            stop_task = asyncio.create_task(sess.stop_event.wait())
            done, pending = await asyncio.wait(
                {recv_task, stop_task},
                timeout=min(remote_input.HEARTBEAT_S, remain),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                end_code = remote_input.RI_TIMEOUT
                try:
                    await ws.send_json({"t": "state", "ok": False,
                                        "code": remote_input.RI_TIMEOUT})
                except Exception:
                    pass
                break
            if stop_task in done:
                end_code = sess.end_code or remote_input.RI_RELEASED
                if end_code != remote_input.RI_RELEASED:
                    try:
                        await ws.send_json({"t": "state", "ok": False,
                                            "code": end_code})
                    except Exception:
                        pass
                break
            try:
                msg = recv_task.result()
            except WebSocketDisconnect:
                end_code = remote_input.RI_DISCONNECTED
                break
            except Exception:
                end_code = remote_input.RI_DISCONNECTED
                break
            sess.touch()
            try:
                action = await sess.handle(msg)
            except remote_input.RemoteInputError as exc:
                end_code = exc.code
                try:
                    await ws.send_json({"t": "state", "ok": False,
                                        "code": exc.code, "detail": exc.detail})
                except Exception:
                    pass
                break
            if action == remote_input.RI_RELEASED:
                end_code = remote_input.RI_RELEASED
                try:
                    await ws.send_json({"t": "state", "ok": True,
                                        "code": remote_input.RI_RELEASED})
                except Exception:
                    pass
                break
    except WebSocketDisconnect:
        end_code = remote_input.RI_DISCONNECTED
    except Exception as exc:
        print(f"[DeckMeido] 跨屏接管异常：{exc}", file=sys.stderr)
        end_code = remote_input.RI_INJECT_FAILED
    finally:
        # 无论正常释放、断线、超时还是异常，都走同一个结束路径释放所有按键
        try:
            await remote_input.manager.finish(end_code)
        finally:
            try:
                await ws.close()
            except Exception:
                pass


@app.get("/api/remote-input/status", dependencies=[Depends(require_auth)])
async def api_remote_input_status():
    """接管开关 + 当前会话状态：页面加载/重连后据此恢复横幅与 logo 状态"""
    cfg = CFG.get("remote_input") or {}
    return {
        # 合并后的单开关：true = 已开启且允许局域网来源
        "enabled": remote_input.enabled(cfg),
        "sensitivity": float(cfg.get("sensitivity", 1.5) or 1.5),
        "max_minutes": int(cfg.get("max_minutes", 30) or 30),
        **remote_input.manager.status(),
    }


@app.post("/api/remote-input/release", dependencies=[Depends(require_auth)])
async def api_remote_input_release():
    """任何已鉴权页面都能释放接管（Deck 客户端点 logo 走这条）"""
    released = await remote_input.manager.finish(remote_input.RI_RELEASED)
    return {"ok": True, "released": released}


@app.get("/files/{path:path}", dependencies=[Depends(require_auth)])
async def api_workspace_file(path: str):
    """下载工作区里的文件（网页端附件卡片用）

    路径必须落在工作区内：模型给的链接是相对工作区的路径，这里只认这个根，
    防止 ../ 一路穿到工作区外面去。
    """
    root = tools.WORKSPACE.resolve()
    try:
        target = (root / path).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if root != target and root not in target.parents:
        raise HTTPException(status_code=403, detail="outside workspace")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


def _workspace_file(rel: str) -> Path:
    """把「相对工作区的路径」解析成真实文件，越界或不存在一律 404

    模型给的链接是相对工作区的路径，这里只认这个根，防止 ../ 一路穿到工作区外面去。
    """
    root = tools.WORKSPACE.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if root != target and root not in target.parents:
        raise HTTPException(status_code=403, detail="outside workspace")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return target


@app.post("/api/file_stat_batch", dependencies=[Depends(require_auth)])
async def api_file_stat_batch(payload: dict):
    """附件卡片补大小（批量）：{paths: [...]} → {sizes: {path: 字节数}}

    一条消息里可能有十几张卡片，逐张发请求在手机上就是十几次往返，
    这里一次问完；查不到的路径直接不出现在结果里，前端就不显示大小。
    """
    paths = payload.get("paths") or []
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="paths must be a list")
    sizes: dict[str, int] = {}
    for rel in paths[:200]:          # 条数封顶：这是一次 UI 补全，不是文件浏览器
        try:
            sizes[str(rel)] = _workspace_file(str(rel)).stat().st_size
        except (HTTPException, OSError):
            continue
    return {"sizes": sizes}


@app.get("/api/zip", dependencies=[Depends(require_auth)])
async def api_zip(path: list[str] | None = Query(default=None), name: str = Query(default="files.zip")):
    """把多个工作区文件打成一个 zip 下载（网页端「打包下载」用）

    在内存里打：一次打包的通常是模型刚生成的几个文档，几十 MB 量级，
    落到临时文件反而要操心清理。
    """
    path = path or []          # 不用 [] 作默认值：可变对象会被所有请求共享
    if not path:
        raise HTTPException(status_code=400, detail="no files")
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in path[:200]:
            try:
                target = _workspace_file(rel)
            except HTTPException:
                continue
            # 同名文件加序号，别让后面的覆盖前面的
            arc = target.name
            n = 1
            while arc in used:
                n += 1
                arc = f"{target.stem}_{n}{target.suffix}"
            used.add(arc)
            try:
                zf.write(target, arcname=arc)
            except OSError:
                continue
    if not used:
        raise HTTPException(status_code=404, detail="file not found")
    data = buf.getvalue()
    safe = re.sub(r"[^\w\-.]+", "_", Path(name).name)[:60] or "files.zip"
    if not safe.lower().endswith(".zip"):
        safe += ".zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.post("/api/file_stat", dependencies=[Depends(require_auth)])
async def api_file_stat(payload: dict):
    """附件卡片补大小：{path: 相对工作区的路径} → {size, name}"""
    rel = str(payload.get("path", "")).strip()
    if not rel:
        raise HTTPException(status_code=404, detail="file not found")
    target = _workspace_file(rel)
    try:
        return {"size": target.stat().st_size, "name": target.name}
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def api_status():
    # 完整采集（模式探针 pw-dump、depcheck、tts）全部在线程里跑并带 TTL 缓存，
    # 别阻塞事件循环（WS 与流式输出都在同一个 loop 上）
    return await status_async()


@app.get("/api/net/probe", dependencies=[Depends(require_auth)])
async def api_net_probe(force: bool = False):
    """公网连通性：online / lan_only / offline

    按需探测、结果缓存；总预算 2 秒，不会拖住启动或设置页。界面拿到非 online
    时只提示「可以切到离线模式」，绝不自动改开关。
    """
    return await asyncio.to_thread(netmode.probe, bool(force))


@app.get("/api/status/line", dependencies=[Depends(require_auth)])
async def api_status_line(state: str = Query(default=""),
                          game: str = Query(default=""),
                          game_confidence: str = Query(default=""),
                          nickname: str = Query(default="")):
    """插件状态栏的一句话（idle/busy/error + 高置信度游戏名 + 昵称）

    不写会话历史、不写 memory.json；失败自动回本地模板，状态栏永不空白。
    """
    return await build_status_line(state, game, game_confidence, nickname)


# 当前正在处理的对话任务（用于终止按钮）
current_task: asyncio.Task | None = None


async def start_chat(text: str, announce: bool = True, **kwargs):
    """把一轮对话放到可取消的任务里执行。announce=False 时不重复广播用户消息（重试用）

    **kwargs 原样转给 handle_chat（图片、工作区根），插件那条路要用。
    """
    global current_task
    current_task = asyncio.create_task(handle_chat(text, announce, **kwargs))
    try:
        return await current_task
    except asyncio.CancelledError:
        return {"reply": "已终止当前操作", "events": [], "cancelled": True}
    finally:
        current_task = None
        # 收尾必须补一条 running=false。插件（大屏 / QAM）只吃 ws 推送、不轮询
        # HTTP，而 run_agent 中途那几次 status 广播都发生在 current_task 置空之前，
        # 少了这一条，最后一次「running=true」会永久挂在面板上——表现就是
        # 网页端这一轮早就结束，插件面板一打开却立刻显示「思考中…」。
        # 成功、异常、取消（含用户点终止）都会走到这里，三种收尾口径一致。
        try:
            await asyncio.shield(manager.broadcast(
                {"type": "status", "data": get_status()}))
        except Exception:
            pass


def _wake_cancel_chat() -> None:
    """唤醒轮次的终止：start_chat 内部的 handle_chat 任务不会被外层取消连带，
    这里显式取消登记在案的 current_task。"""
    task = current_task
    if task is not None and not task.done():
        task.cancel()


wake_service.set_handlers(chat=start_chat, cancel_chat=_wake_cancel_chat)


@app.post("/api/cancel", dependencies=[Depends(require_auth)])
async def api_cancel(request: Request, client: str = "", device: str = ""):
    """终止当前这一轮，并把「谁点的」记进日志。

    以前这里只留一行 `200 OK`，于是出现「这一轮莫名其妙被终止」时，
    只能去背调哪个浏览器标签页在发请求（会话 #65e2b8 里就为此猜错了两轮）。
    现在把调用方的 client/device 和来源 IP 一起打出来，插件、客户端、
    网页各自带自己的标识，对不上就说明是别人的页面在点。
    """
    who = f"client={client or '?'} device={device or '?'} ip={request.client.host if request.client else '?'}"
    if current_task and not current_task.done():
        print(f"[DeckMeido] 终止请求：{who}（有正在执行的轮次，已取消）",
              file=sys.stderr, flush=True)
        current_task.cancel()
        # 遮罩进程在遮罩消失前会重新拉一次状态，这里立刻翻成 false，
        # 用户点完遮罩就不会出现「已终止」又闪回操作中的画面
        set_overlay(False)
        # 第二道保险：取消链要是有哪一处 await 卡住了（孙进程占着 stdout 管道就
        # 会这样），光 cancel() 停不下来，界面会一直卡在「正在执行」。这里直接把
        # 登记在案的子进程树砍掉，让那一轮无论如何都能收尾。
        killed = await tools.abort_all_procs()
        return {"ok": True, "killed": killed}
    print(f"[DeckMeido] 终止请求：{who}（当前没有在执行的任务，仅清残留子进程）",
          file=sys.stderr, flush=True)
    # 任务已结束但可能还留着后台子进程，一并清掉，避免下次点终止没反应
    killed = await tools.abort_all_procs()
    return {"ok": False, "detail": "no active task", "killed": killed}


class ApiProfileIn(BaseModel):
    """一组 API 配置的提交值；api_key 留空或回传打码串 = 保持原 Key"""
    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""


class ConfigUpdate(BaseModel):
    # 多组接口配置：提交完整列表（新增/改名/删除/切换都在这里），后端负责合并 Key
    api_profiles: list[ApiProfileIn] | None = None
    # 只切换「当前使用」，不要求前端把整张列表都发回来
    current_api: str | None = None
    # 旧版单组字段：仍接受，改动会作用到「当前使用」那一条（老客户端兼容）
    base_url: str | None = None
    api_key: str | None = None
    # 显式要求清空 Key；不传则空字符串一律按「不改」处理
    clear_api_key: bool = False
    model: str | None = None
    tts_enable: bool | None = None
    # [tts] 发音人：robot（机器人，espeak-ng）/ maid（女仆，piper-plus）。
    # 仍是唯一来源，写回 config.toml 后热更新；下面的旧字段只为老客户端兼容
    tts_speaker: str | None = None
    # [tts] 引擎/音色/语言/语速：写回 config.toml，apply_config 热更新
    # 值非法时归一化或夹到合法区间，不让一次手滑把播报弄哑
    tts_engine: str | None = None
    tts_voice: str | None = None
    tts_language: str | None = None
    tts_speed: int | None = None
    tts_speed_scale: float | None = None
    tts_noise_scale: float | None = None
    tts_length_scale: float | None = None
    # 播报音量过低时临时抬升（[tts] boost_volume，默认开）
    tts_boost_volume: bool | None = None
    # 语音输入开关：和 tts_enable 一样走热更新，插件端 /api/stt/toggle 写的是同一份 [stt]
    stt_enable: bool | None = None
    # 录音期间静音外放：写入 [stt] duck_output
    stt_duck_output: bool | None = None
    # 智能音箱模式总开关：写入 [wake] enabled，打开后后端自动进入待机监听
    wake_enabled: bool | None = None
    # 界面语言："zh"（默认）/ "en"；写 [ui] language
    ui_language: str | None = None
    # 发送时是否附带给游戏画面（面板上的「画面：开/关」）
    with_shot: bool | None = None
    # 「对话与语音」整组总开关：写入 [agent] chat_voice；关闭后该组功能整体停用
    chat_voice: bool | None = None
    permission_mode: str | None = None
    # 自定义档的四组开关 {read/write/cmd/net: bool}，True = 先问我
    permission_custom: dict | None = None
    # 深度思考档位：空串 / low / medium / high（前端滑条的「无 / 低 / 中 / 高」）
    reasoning_effort: str | None = None
    workspace: str | None = None
    # 人设：整体写回 meidosoul.json，空字符串表示「恢复默认」
    persona_name: str | None = None
    # 身份定位：拼在 system prompt 第一句「你是 X，<traits>」，决定她是谁
    persona_traits: str | None = None
    # 补充设定：整段追加到 system prompt 末尾，比上面的表单自由
    persona_extra: str | None = None
    persona_address: str | None = None
    persona_tone: str | None = None
    persona_ending: str | None = None
    persona_suffix: str | None = None
    persona_forbid: str | None = None
    # 「截屏询问」提示词模板：空字符串表示恢复默认
    ask_template: str | None = None
    # 状态栏隐藏提示词：空字符串表示恢复默认（只影响 /api/status/line 的文风）
    status_prompt: str | None = None
    # 双源模式是否探测 GitHub（关掉就只用镜像）
    update_github_enable: bool | None = None
    # 跨屏键鼠接管：合并成单开关（设置页只展示这一项）、灵敏度、单次时长上限
    remote_input_enabled: bool | None = None
    remote_input_sensitivity: float | None = None
    remote_input_max_minutes: int | None = None
    # 睡眠模式：机器闲置（无人使用且音乐已停）满多久后自动关机
    sleep_enabled: bool | None = None
    sleep_idle_minutes: int | None = None
    sleep_countdown_sec: int | None = None
    # 局域网访问开关与监听端口：写入 [server]，绑定地址要重启后端才生效
    lan_enable: bool | None = None
    port: int | None = None
    # 开发者模式：写入 [runtime]，保存即生效（日志级别与细节记录不需要重启）
    dev_mode: bool | None = None
    # 离线模式：写入 [runtime]，保存即生效，所有出网入口立即快速失败
    offline_mode: bool | None = None
    # 远程终端：默认关闭的高危能力，写入 [terminal]，保存即生效
    terminal_enabled: bool | None = None
    terminal_idle_timeout: int | None = None
    terminal_max_sessions: int | None = None
    terminal_max_output_bytes: int | None = None
    terminal_shell: str | None = None


class TTSPreviewRequest(BaseModel):
    # 试听文本；留空用一句内置短句
    text: str = ""


class TTSSayRequest(BaseModel):
    # 手机端要播报的文本；长度上限在接口里再夹一次
    text: str
    language: str = ""


class RenderReport(BaseModel):
    """前端回报：本轮思考链实际渲染了多少字符、几段（仅 dev_mode 接收）"""
    sid: str = ""
    reasoning_chars: int = 0
    segments: int = 0


class LogClearRequest(BaseModel):
    """清空日志：debug / run / install / all"""
    category: str = "all"


class PairConfirmRequest(BaseModel):
    pair_id: str
    pin: str
    # 保持登录：勾选时写 30 天有效期的持久 Cookie，否则是会话 Cookie
    remember: bool = True


class PairDecisionRequest(BaseModel):
    pair_id: str
    allow: bool


class PairIdRequest(BaseModel):
    pair_id: str


class DeviceRenameRequest(BaseModel):
    device_id: str
    name: str


class DeviceIdRequest(BaseModel):
    device_id: str


class PairQrRequest(BaseModel):
    """生成扫码配对二维码：host 可选，用于指定 Deck 在局域网/Tailscale 中的地址"""
    host: str = ""


class PairQrRedeemRequest(BaseModel):
    """手机扫码后换取设备凭据；token 只出现在二维码里，一次性、短有效期"""
    token: str
    name: str = ""
    remember: bool = True


class OneBotConfigUpdate(BaseModel):
    """远程连接页的 QQ 通道配置：只改界面上能编辑的字段

    限流、分段、脱敏、群常识缓存的边界在 onebot.normalize_advanced 里
    统一校验（非法值直接 400），字段为 None 表示本次不改。
    """
    enable: bool | None = None
    # 重启后自动连接：与 enable 解耦，默认 false（缺键按 false）
    auto_connect: bool | None = None
    ws_url: str | None = None
    access_token: str | None = None
    # 接受数组（新前端）或逗号/换行分隔的字符串（手写/旧调用）
    admin_qq: list[str] | str | None = None
    allow_groups: list[str] | str | None = None
    # 传空字符串 = 恢复内置默认；超长由 normalize_guest_prompt 拒绝（400）
    guest_prompt: str | None = None
    allow_image: bool | None = None
    max_image_mb: float | None = None
    image_save_dir: str | None = None
    image_recent_minutes: float | None = None
    text_log_path: str | None = None
    auto_launch_after_install: bool | None = None
    auto_launch_wait_seconds: float | None = None
    # 群常识只读缓存
    group_memo: bool | None = None
    group_memo_size: int | None = None
    group_memo_ttl: float | None = None
    group_memo_max_chars: int | None = None
    # 限频 / 分段 / 输出脱敏
    rate_per_minute: int | None = None
    group_rate_per_minute: int | None = None
    # 主动接话概率（0~100）：没人喊机器人时自己插话的几率
    proactive_chance: int | None = None
    reply_chunk_chars: int | None = None
    max_reply_chunks: int | None = None
    redact_words: list[str] | str | None = None
    redact_mask: str | None = None
    # 语音条（NapCat record 段）：私聊只对主人 QQ，群聊由 voice_group_members 决定
    voice_reply: bool | None = None
    voice_group_members: bool | None = None
    voice_only: bool | None = None
    voice_max_seconds: float | None = None
    voice_max_kb: int | None = None


class AskTemplateUpdate(BaseModel):
    # 空 / 全空白表示恢复默认模板
    text: str = ""


class TranslateTemplateUpdate(BaseModel):
    # 空 / 全空白表示恢复默认模板
    text: str = ""


class PresetSaveBody(BaseModel):
    """插件「保存为预设」：按当前正在跑的游戏存一条提示词"""
    appid: str = ""
    game_name: str = ""
    prompt: str = ""
    title: str | None = None
    # 插件保存时明确知道这就是当前游戏，可立刻套用；网页端编辑走 PATCH 不碰这里
    activate: bool = False


class PresetUpdateBody(BaseModel):
    """设置页编辑：只改内容/显示名，不改变绑定的游戏"""
    prompt: str | None = None
    title: str | None = None


class ActiveGameBody(BaseModel):
    """插件上报当前游戏（退出游戏时两项都为空）"""
    appid: str = ""
    name: str = ""


class MemoryItem(BaseModel):
    """设置页手工教学用的记忆条目（text 为空 = 删除这条）

    scope 决定归属：private（默认）/ group:<群号> / person:<QQ>。
    网页没有会话上下文，新增时默认写私聊；从群聊页面带过来时显式传 scope。
    """
    id: str | None = None
    kind: str = "note"
    text: str = ""
    scope: str = memory.SCOPE_PRIVATE


class SoulImport(BaseModel):
    """导入备份：人设 + 记忆。两部分都可缺省，缺的保持原样"""
    persona: dict | None = None
    memory: list[dict] | None = None
    replace: bool = True


def _mask_key(key: str) -> str:
    """把 Key 打码成 sk-7sw…56D5 这种样子：够认出是哪把，又不可用。

    为什么不直接把明文发给前端：任何一台已配对设备打开设置页就能把 Key 抄走。
    打码后前端只用于「展示已配置」，
    真正用 Key 的请求都在服务端完成。
    """
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def api_get_config():
    return {
        # 旧字段 = 当前使用那一组，老客户端照旧能读；多组列表见 api_profiles
        "base_url": llm.base_url,
        # 明文 Key 不再下发，只给打码值和「有没有配」的布尔量
        "api_key": _mask_key(llm.api_key),
        "api_key_set": bool(llm.api_key.strip()),
        "api_key_masked": _mask_key(llm.api_key),
        "current_api": API_STATE.get("current") or "",
        # 每一组都只给掩码；接口响应里不会出现明文 Key
        "api_profiles": apicfg.public(API_STATE),
        "model": llm.model,
        "reasoning_effort": llm.reasoning_effort,
        "tts_enable": tts.enable,
        # 语音合成的发音人/音色/语速：设置页「女仆人格」下面的语音回复卡片读这些
        "tts_speaker": tts.speaker,
        "tts_speaker_label": SPEAKER_LABELS.get(tts.speaker, ""),
        "tts_engine": tts.engine,
        "tts_voice": tts.voice,
        "tts_language": tts.language,
        "tts_speed": tts.speed,
        "tts_speed_scale": tts.speed_scale,
        "tts_noise_scale": tts.noise_scale,
        "tts_boost_volume": tts.boost_volume,
        # 带画面询问的开关：面板重开后按它恢复，不能只存在前端内存里。
        # 对外给「有效值」：总开关关闭时插件端据此不再附加截图；
        # 分项原始值另给 with_shot_pref，设置页回显不被总开关污染
        "with_shot": chat_voice_enabled()
        and bool((CFG.get("agent") or {}).get("with_shot", True)),
        "with_shot_pref": bool((CFG.get("agent") or {}).get("with_shot", True)),
        # 语音输入开关：和语音播报一样，面板上要能显示和切换（stt.enable 已是有效值）
        "stt_enable": stt.enable,
        "stt_enable_pref": bool((CFG.get("stt") or {}).get("enable", True)),
        # 整组总开关：设置页标题行的开关与分项置灰都看它
        "chat_voice": chat_voice_enabled(),
        # 录音期间静音外放（[stt] duck_output，默认开）；duck 里带工具可用性，
        # 设置页据此提示「这台机器不支持静音」，但开关仍可保存
        "stt_duck_output": audiomute.enabled(),
        "stt_duck": audiomute.status(),
        # 智能音箱模式：设置页的开关/状态；默认 false，唤醒后直接发送
        "wake_enabled": wake_service.enabled,
        "wake_state": wake_service.status(),
        # [ui] language：网页/客户端据此显示语言下拉，切换后整页重载
        "ui_language": UI_LANG,
        "permission_mode": PERMISSION_MODE,
        # 自定义档的四组开关：设置页打开时按它落位
        "permission_custom": dict(CUSTOM_PERM),
        "workspace": str(tools.WORKSPACE),
        "port": CFG["server"].get("port", 18765),
        # 局域网访问开关：默认 false 只监听 127.0.0.1；开启后设置页显示地址与配对入口
        "lan_enable": netcfg.lan_enabled(CFG.get("server") or {}),
        # [runtime].dev_mode：网页设置页的开发者模式开关；保存后立即生效
        "dev_mode": devlog.enabled(),
        # [runtime].offline_mode：设置页开关与界面横幅都读它
        "offline_mode": netmode.enabled(),
        # 局域网访问地址：掌机 / 大屏 / 客户端设置页直接展示，别的设备照这个连
        "lan_url": f"http://{_lan_ip()}:{CFG['server'].get('port', 18765)}",
        "device_days": _device_days(),
        "llm_configured": llm.configured,
        "version": VERSION,
        # 人设：设置页表单用，默认值和后台提示词里的一致
        "persona_name": PERSONA.get("name", "DeckMeido"),
        "persona_traits": PERSONA.get("traits", ""),
        "persona_extra": PERSONA.get("extra", ""),
        "persona_address": (PERSONA.get("style") or {}).get("address", "主人"),
        "persona_tone": (PERSONA.get("style") or {}).get("tone", ""),
        "persona_ending": (PERSONA.get("style") or {}).get("ending", ""),
        "persona_suffix": (PERSONA.get("style") or {}).get("suffix", ""),
        # 禁用词列表在界面上是一行、用逗号隔开
        "persona_forbid": "，".join((PERSONA.get("style") or {}).get("forbid") or []),
        # 状态栏隐藏提示词：这里是用户配置的全局值；插件/网页只读它做展示，
        # 改走 POST /api/config。当前游戏有预设时生效值见 status_prompt_effective
        "status_prompt": status_line.prompt_text(CFG.get("agent") or {}),
        "status_prompt_effective": status_prompt_text(),
        # 按游戏提示词预设：当前游戏/当前预设状态 + 预设条数
        "preset": presets.active_view(),
        "presets_count": len(presets.list_presets()),
        # 数据根（配置/会话/记忆/上传/QQ 归档都在这里），排障用
        "data_dir": str(paths.data_dir()),
        "data_root": paths.data_root_label(),
        # 「截屏询问」的提示词模板：插件设置区里可以直接改
        "ask_template": ASK_TEMPLATE,
        "ask_template_default": DEFAULT_ASK_TEMPLATE,
        # 「截图翻译」的提示词模板：同一套存储与热更新路径
        "translate_template": TRANSLATE_TEMPLATE,
        "translate_template_default": DEFAULT_TRANSLATE_TEMPLATE,
        # 更新源：updater 从 config.toml 的 [update] 段读，缺省即默认值
        "update_source_label": updater.source_label(),
        "update_mirror": updater.update_cfg()["mirror"],
        "update_github_enable": bool(updater.update_cfg().get("github_enable", True)),
        # 长期记忆的条数概览（正文单独走 /api/memory，设置页按需拉）
        "memory": memory.stats(),
        # 跨屏键鼠接管：设置页表单与安全提示用（单开关：开 = 允许局域网）
        "remote_input_enabled": remote_input.enabled(CFG.get("remote_input") or {}),
        "remote_input_sensitivity": float((CFG.get("remote_input") or {}).get("sensitivity", 1.5) or 1.5),
        "remote_input_max_minutes": int((CFG.get("remote_input") or {}).get("max_minutes", 30) or 30),
        # 睡眠模式：设置页表单用开关与分钟数，眼睛页用倒计时（countdown_left）
        "sleep": sleepmode.service.status(),
        # 远程终端：设置页开关与限流参数（默认全部关闭/保守值）
        "terminal_remote_shell": _terminal_settings().enabled,
        "terminal_idle_timeout": _terminal_settings().idle_timeout_s,
        "terminal_max_sessions": _terminal_settings().max_sessions,
        "terminal_max_output_bytes": _terminal_settings().max_output_bytes,
        "terminal_shell": _terminal_settings().shell,
        "terminal_cwd": _terminal_settings().cwd,
    }


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def api_get_memory(scope: str | None = None):
    """长期记忆，按「类别 + 创建时间」排好，和注入顺序一致

    scope 不传 = 返回全部（兼容现有网页）；传了只返回该 scope，
    设置页按当前会话查看群 / 人 / 私聊记忆都走这一条。
    """
    if scope:
        items = memory.list_scope(scope)
    else:
        items = memory.load()["items"]
        order = list(memory.KINDS)
        items = sorted(items, key=lambda x: (order.index(x["kind"]), x["created"]))
    return {"items": items, "scope": memory.norm_scope(scope) if scope else "",
            "kinds": [{"id": k, "name": v} for k, v in memory.KINDS.items()]}


@app.post("/api/memory", dependencies=[Depends(require_auth)])
async def api_save_memory(req: MemoryItem):
    """新增 / 修改 / 删除一条记忆

    带 id 且 text 为空 = 删除；带 id 且 text 非空 = 修改；不带 id = 新增。
    """
    if not req.id:
        item = memory.add(req.kind, req.text, scope=req.scope)
        if not item:
            raise HTTPException(status_code=400, detail="内容不能为空")
        return {"ok": True, "item": item}
    if not memory.update(req.id, req.text, req.kind):
        raise HTTPException(status_code=404, detail="这条记忆已不存在")
    return {"ok": True}


@app.delete("/api/memory/{mid}", dependencies=[Depends(require_auth)])
async def api_delete_memory(mid: str):
    if not memory.remove(mid):
        raise HTTPException(status_code=404, detail="这条记忆已不存在")
    return {"ok": True}


@app.delete("/api/memory", dependencies=[Depends(require_auth)])
async def api_clear_memory(scope: str | None = None):
    """清空全部，或只清空指定 scope（网页设置页的批量删除）"""
    if scope:
        removed = memory.clear_scope(scope)
        return {"ok": True, "removed": removed, "scope": memory.norm_scope(scope)}
    memory.clear()
    return {"ok": True, "removed": -1, "scope": ""}


# ---------- 人设 + 记忆的导出 / 导入 ----------
# 这两样就是「她这个人」的全部：人设在 meidosoul.json，记忆在 memory.json，
# 换机器、重装系统、想备份一份，都应该一次拷走。

@app.get("/api/soul/export", dependencies=[Depends(require_auth)])
async def api_export_soul():
    """导出人设与全部记忆，返回一个可以直接存盘的 JSON"""
    items = memory.load()["items"]
    order = list(memory.KINDS)
    items = sorted(items, key=lambda x: (order.index(x["kind"]), x["created"]))
    return {
        "kind": "meidosoul",
        "version": 1,
        "exported": time.time(),
        "persona": persona.load(),
        "memory": items,
    }


@app.post("/api/soul/import", dependencies=[Depends(require_auth)])
async def api_import_soul(req: SoulImport):
    """导入备份。replace=True 覆盖，False 则只补缺（人设仍整体覆盖）"""
    if req.persona is not None:
        persona.save(req.persona)
        reload_persona()
    added = 0
    if req.memory is not None:
        if req.replace:
            memory.clear()
        # 逐条走 memory.add：它自己会去重、截断、按上限淘汰，
        # 备份文件里就算有脏数据也不会把记忆撑坏
        for it in req.memory:
            # 备份里的 scope 原样带回来（旧备份没有这个字段 → private），
            # 否则导入一次就把群记忆 / 个人记忆全并进私聊了
            if isinstance(it, dict) and memory.add(str(it.get("kind") or "note"),
                                                   str(it.get("text") or ""),
                                                   scope=memory.norm_scope(it.get("scope"))):
                added += 1
    return {"ok": True, "added": added, "memory": memory.stats()}


def _norm_base_url(url: str) -> str:
    """接口地址去掉末尾斜杠，空串原样返回（= 清空）

    地址原样保存，不擅自补 /v1：DeepSeek 官方两种写法都认，OpenAI 必须带 /v1，
    本地推理服务多半不能带 —— 统一改写只会在界面上留下和用户填的不一样的值。
    误粘的完整端点（.../chat/completions）会被剥掉，避免拼成双重后缀后 404。
    """
    return apicfg.norm_base(url)


async def _pick_model(base_url: str, api_key: str) -> str:
    """换了服务商后自动挑一个模型：原模型还在就用它，否则取列表里第一个

    这个函数只负责「猜个更合适的模型名」，猜不到就不该拦着用户保存配置。
    旧版会把探活异常（Key 写错、服务商抽风、断网 → 401/超时）直接冒到
    外层，整个 POST /api/config 变成 500：新填的 Key 存不进去，
    用户看到「保存失败」但输入框又被清空，等于把配置搞坏了。
    现在一律吞掉异常，退回当前模型名，由「保存后拉模型列表」那步另行提示。
    """
    fallback = llm.model
    try:
        probe = LLMClient(base_url=_norm_base_url(base_url), api_key=api_key,
                          model=fallback, max_tokens=None, timeout=30.0)
        try:
            names = await probe.list_models()
        finally:
            await probe.close()
    except Exception:
        return fallback
    if fallback in names:
        return fallback
    if not names:
        return fallback
    return names[0]


@app.post("/api/config", dependencies=[Depends(require_auth)])
async def api_set_config(req: ConfigUpdate, request: Request):
    llm_updates = {}
    profile_state = None
    if req.api_profiles is not None:
        # 列表是全量提交：新增 / 改名 / 改地址 / 删除都在这一步合并 Key
        profile_state = apicfg.merge_update(
            API_STATE, [p.model_dump() for p in req.api_profiles], req.current_api)
    elif req.current_api is not None:
        profile_state = apicfg.select(API_STATE, req.current_api)

    # 旧版单组字段仍接受：作用到「当前使用」那一条，避免老客户端保存时报 400。
    # Key 的三条「不覆盖」规则不变：没传 / 空串 / 打码串都按不改处理；
    # 想真清空用 clear_api_key=true。
    legacy_url = _norm_base_url(req.base_url) if req.base_url is not None else None
    legacy_key = None
    if req.clear_api_key:
        legacy_key = ""
    elif req.api_key is not None:
        incoming = req.api_key.strip()
        if incoming and "…" not in incoming and "..." not in incoming:
            legacy_key = incoming
    key_changed = False
    if legacy_url is not None or legacy_key is not None:
        state = profile_state or API_STATE
        active = apicfg.active(state)
        if active:
            profiles = [dict(p) for p in state["profiles"]]
            item = next(p for p in profiles if p["id"] == active["id"])
            if legacy_url is not None:
                item["base_url"] = legacy_url
            if legacy_key is not None:
                key_changed = legacy_key != item["api_key"]
                item["api_key"] = legacy_key
            profile_state = {"profiles": profiles, "current": state["current"]}
        elif legacy_url or legacy_key:
            profile_state = apicfg.merge_update(state, [{
                "id": "", "name": apicfg.DEFAULT_NAME,
                "base_url": legacy_url or "", "api_key": legacy_key or "",
            }], None)
            key_changed = bool(legacy_key)
    if profile_state is not None:
        llm_updates.update(apicfg.to_updates(profile_state))
    if req.model is not None:
        llm_updates["model"] = req.model.strip()
    elif key_changed and profile_state is not None:
        # 换了 Key 而没指定模型：按新 Key 探一次模型名；探不到就保持原值
        target = apicfg.active(profile_state) or {}
        llm_updates["model"] = await _pick_model(
            str(target.get("base_url") or ""), str(target.get("api_key") or ""))
    updates = {}
    if llm_updates:
        updates["llm"] = llm_updates
    # 语音合成：开关与引擎参数写同一段 [tts]，用 setdefault 合并避免互相覆盖。
    # 前端只传改了的那几项；非法值在这里拦住，别让保存接口 500
    tts_updates = {}
    if req.tts_enable is not None:
        tts_updates["enable"] = bool(req.tts_enable)
    # 发音人只有一个配置项：新字段优先，旧 engine/voice 映射到同一项
    if req.tts_speaker is not None:
        tts_updates["speaker"] = normalize_speaker(req.tts_speaker)
    elif req.tts_engine is not None or req.tts_voice is not None:
        tts_updates["speaker"] = speaker_from_legacy(req.tts_engine, req.tts_voice)
    if req.tts_voice is not None:
        voice = req.tts_voice.strip()
        if not voice or len(voice) > 40 or any(ch.isspace() for ch in voice):
            raise HTTPException(status_code=400, detail="音色名称不合法")
        tts_updates["voice"] = voice
    if req.tts_language is not None:
        language = req.tts_language.strip().lower()
        if language not in LANGUAGES:
            raise HTTPException(
                status_code=400,
                detail=f"语言只能是 {', '.join(LANGUAGES)} 之一")
        tts_updates["language"] = language
    if req.tts_speed is not None:
        tts_updates["speed"] = max(80, min(320, int(req.tts_speed)))
    if req.tts_speed_scale is not None:
        tts_updates["speed_scale"] = round(max(0.5, min(2.0, float(req.tts_speed_scale))), 2)
    if req.tts_noise_scale is not None:
        tts_updates["noise_scale"] = round(max(0.0, min(1.0, float(req.tts_noise_scale))), 3)
    if req.tts_length_scale is not None:
        tts_updates["length_scale"] = round(max(0.4, min(3.0, float(req.tts_length_scale))), 2)
    if req.tts_boost_volume is not None:
        tts_updates["boost_volume"] = bool(req.tts_boost_volume)
    if tts_updates:
        updates["tts"] = tts_updates
    if req.stt_enable is not None:
        # 语音输入：apply_config 里关掉时会顺手 unload 模型，和插件端 toggle 走同一条路径
        updates["stt"] = {"enable": bool(req.stt_enable)}
    if req.stt_duck_output is not None:
        # 和 stt_enable 写同一段，用 setdefault 合并，避免后一项覆盖前一项
        updates.setdefault("stt", {})["duck_output"] = bool(req.stt_duck_output)
    if req.wake_enabled is not None:
        # 智能音箱模式：apply_config 里 wake_service.apply 会负责进入/退出监听
        updates["wake"] = {"enable": bool(req.wake_enabled)}
    if req.ui_language is not None:
        updates["ui"] = {"language": ui_language({"ui": {"language": req.ui_language}})}
    if req.with_shot is not None:
        # 带画面开关落盘到 [agent]：面板关掉再打开要还是上次选的那个
        updates.setdefault("agent", {})["with_shot"] = bool(req.with_shot)
    if req.chat_voice is not None:
        # 整组总开关：只写 [agent] chat_voice，分项（with_shot / [stt] enable）原样保留，
        # 重新打开总开关时按原来的分项选择恢复
        updates.setdefault("agent", {})["chat_voice"] = bool(req.chat_voice)
    if req.reasoning_effort is not None and req.reasoning_effort in ("", "low", "medium", "high"):
        # 深度思考滑条：空串 = 不带参数（不思考），其余原样写进 [llm]。
        # 空串要显式落盘，不能当作「没传」忽略，否则关掉之后再打开还是旧的档位
        llm_updates["reasoning_effort"] = req.reasoning_effort
    if req.permission_mode in ("manual", "auto", "custom", "full"):
        sec = {"permission_mode": req.permission_mode}
        # 自定义档的四组开关跟着一起落盘；没传就沿用当前值，
        # 避免「只切模式」把用户配好的开关洗回默认
        if req.permission_custom is not None:
            sec["custom"] = tools.normalize_custom(req.permission_custom)
        else:
            sec["custom"] = dict(CUSTOM_PERM)
        updates["security"] = sec
    if req.workspace is not None and req.workspace.strip():
        updates["workspace"] = {"dir": req.workspace.strip()}
    if req.ask_template is not None:
        # 清空即恢复默认：模板是「截屏询问」唯一的提问来源，
        # 留成空串会让这个按钮发不出去任何内容
        tpl = req.ask_template.strip() or DEFAULT_ASK_TEMPLATE
        updates.setdefault("agent", {})["ask_template"] = tpl
    if req.status_prompt is not None:
        # 状态栏提示词同理：清空恢复默认，避免状态栏只剩一句空话
        updates.setdefault("agent", {})["status_prompt"] = \
            req.status_prompt.strip() or DEFAULT_STATUS_PROMPT
    if req.persona_name is not None:
        # 人设存 meidosoul.json（和记忆放在一起，方便导出备份），不写 config.toml。
        # 留空就写回默认值，避免空串把人设搞没。
        # 只按中英文逗号/顿号切分：chip 里允许带空格的短语，不能被空白再拆开
        forbid = [w.strip() for w in re.split(r"[,，、]", req.persona_forbid or "") if w.strip()]
        persona.save({
            "name": req.persona_name.strip() or "DeckMeido",
            # traits / extra 没传（旧客户端）就保持原样，别把身份定位洗成默认
            "traits": PERSONA.get("traits", "") if req.persona_traits is None else req.persona_traits,
            "extra": PERSONA.get("extra", "") if req.persona_extra is None else req.persona_extra,
            "style": {
                "address": req.persona_address or "",
                "tone": req.persona_tone or "",
                "ending": req.persona_ending or "",
                "suffix": req.persona_suffix or "",
                "forbid": forbid,
            },
        })
        reload_persona()
    if req.update_github_enable is not None:
        updates.setdefault("update", {})["github_enable"] = bool(req.update_github_enable)
    ri = {}
    if req.remote_input_enabled is not None:
        ri["enabled"] = bool(req.remote_input_enabled)
    if req.remote_input_sensitivity is not None:
        # 灵敏度夹在 0.5~3，太小指针几乎不动，太大一碰就飞出屏幕
        ri["sensitivity"] = max(0.5, min(3.0, float(req.remote_input_sensitivity)))
    if req.remote_input_max_minutes is not None:
        ri["max_minutes"] = max(1, min(120, int(req.remote_input_max_minutes)))
    if ri:
        updates["remote_input"] = ri
    sl = {}
    if req.sleep_enabled is not None:
        sl["enabled"] = bool(req.sleep_enabled)
    if req.sleep_idle_minutes is not None:
        # 下限 5 分钟：太短会在「歌刚放完、人还没动」时误关机
        sl["idle_minutes"] = max(5, min(720, int(req.sleep_idle_minutes)))
    if req.sleep_countdown_sec is not None:
        sl["countdown_sec"] = max(10, min(600, int(req.sleep_countdown_sec)))
    if sl:
        updates["sleep"] = sl
    if req.lan_enable is not None:
        updates.setdefault("server", {})["lan_enable"] = bool(req.lan_enable)
        audit.record("config", "lan", "ok", ip=_client_ip(request),
                     detail="on" if req.lan_enable else "off")
    if req.port is not None:
        port = netcfg.valid_port(req.port)
        if port is None:
            raise HTTPException(status_code=400, detail="端口需为 1024-65535 之间的整数")
        updates.setdefault("server", {})["port"] = port
        audit.record("config", "port", "ok", ip=_client_ip(request), detail=str(port))
    if req.dev_mode is not None:
        # 写 [runtime]，apply_config 里 devlog.configure 会立刻切换级别与文件。
        # 关闭开发者模式不再自动清理 debug 日志（设置页已无该开关，固定为「不清理」），
        # 需要清理时由用户点「清空调试日志」。
        updates.setdefault("runtime", {})["dev_mode"] = bool(req.dev_mode)
        audit.record("config", "dev_mode", "ok", ip=_client_ip(request),
                     detail="on" if req.dev_mode else "off")
    if req.offline_mode is not None:
        # 写 [runtime]；apply_config 里 netmode.configure 立即切换，
        # 之后所有出网入口（模型、更新、余额、下载、联网工具）直接返回原因
        updates.setdefault("runtime", {})["offline_mode"] = bool(req.offline_mode)
    term = {}
    if req.terminal_enabled is not None:
        term["remote_shell"] = bool(req.terminal_enabled)
    if req.terminal_idle_timeout is not None:
        term["idle_timeout_seconds"] = max(
            remote_terminal.MIN_IDLE_TIMEOUT_S,
            min(remote_terminal.MAX_IDLE_TIMEOUT_S, int(req.terminal_idle_timeout)))
    if req.terminal_max_sessions is not None:
        term["max_sessions"] = max(
            remote_terminal.MIN_MAX_SESSIONS,
            min(remote_terminal.MAX_MAX_SESSIONS, int(req.terminal_max_sessions)))
    if req.terminal_max_output_bytes is not None:
        term["max_output_bytes"] = max(
            remote_terminal.MIN_MAX_OUTPUT_BYTES,
            min(remote_terminal.MAX_MAX_OUTPUT_BYTES,
                int(req.terminal_max_output_bytes)))
    if req.terminal_shell is not None:
        shell = req.terminal_shell.strip()
        if len(shell) > remote_terminal.MAX_SHELL_CHARS:
            raise HTTPException(status_code=400, detail="shell 配置过长")
        term["shell"] = shell
    if term:
        updates["terminal"] = term
    if updates:
        save_config(updates)
    if req.terminal_enabled is False:
        # 关闭开关立刻回收全部会话，不允许旧进程继续跑
        await remote_terminal.manager.close_all("disabled")
    if ri.get("enabled") is False:
        # 关掉总开关时立刻结束正在进行的接管，不等下一次心跳
        await remote_input.manager.finish(remote_input.RI_DISABLED)
    return {"ok": True, "llm_configured": llm.configured,
            "current_api": API_STATE.get("current") or "",
            "api_profiles": apicfg.public(API_STATE),
            "workspace": str(tools.WORKSPACE),
            # 工作区目录建不出来时把原因原样带回去（前端显示成人话，不再只报「不存在」）
            "workspace_ok": WORKSPACE_OK,
            "workspace_error": WORKSPACE_ERROR,
            # 回传归一之后的实值：前端滑条按它落位（比如 DeepSeek 上的「中」实际是 high）
            "reasoning_effort": llm.reasoning_effort,
            "with_shot": chat_voice_enabled()
            and bool((CFG.get("agent") or {}).get("with_shot", True)),
            "with_shot_pref": bool((CFG.get("agent") or {}).get("with_shot", True)),
            "chat_voice": chat_voice_enabled(),
            "model": llm.model,
            # 绑定地址与端口都只在后端启动时读取，改完必须重启核心
            "port": CFG["server"].get("port", 18765),
            "lan_enable": netcfg.lan_enabled(CFG.get("server") or {}),
            # dev_mode 保存即生效，不需要重启；重启项只算绑定地址与端口
            "dev_mode": devlog.enabled(),
            # 离线模式保存即生效
            "offline_mode": netmode.enabled(),
            # 远程终端开关回传实值，设置页不用再拉一次
            "terminal_remote_shell": _terminal_settings().enabled,
            "terminal_idle_timeout": _terminal_settings().idle_timeout_s,
            "terminal_max_sessions": _terminal_settings().max_sessions,
            "terminal_max_output_bytes": _terminal_settings().max_output_bytes,
            "terminal_shell": _terminal_settings().shell,
            "restart_required": bool(req.lan_enable is not None or req.port is not None)}


# ---------- 设备配对 ----------
# 浏览器侧：start → 等掌机允许 → confirm（PIN）→ 下发设备 Cookie。
# 掌机侧：pending / refresh / decision 三个接口都要求本机回环或已配对 Cookie。

def _pair_http(exc: pairing.PairError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@app.get("/api/auth/session")
async def api_auth_session(request: Request):
    """未登录也能查：当前是不是已经凭设备 Cookie 或本机回环登录。

    网页打开时先问一次，已配对就直接进主界面；未配对则弹 PIN 配对。
    """
    local = netcfg.is_local(_client_ip(request))
    device = None if local else _cookie_device(request)
    authenticated = bool(local or device)
    return {
        "authenticated": authenticated,
        "local": local,
        "device": device,
        "lan_enable": netcfg.lan_enabled(CFG.get("server") or {}),
        "device_days": _device_days(),
        "version": VERSION,
        "version_display": VERSION_DISPLAY,
    }


@app.get("/api/logs/usage", dependencies=[Depends(require_auth)])
async def api_logs_usage():
    """日志占用：总量 / 分项 / 文件数 / debug 轮转上限"""
    # 目录扫描放线程，避免日志多时卡住事件循环
    return await asyncio.to_thread(devlog.usage)


@app.post("/api/logs/clear", dependencies=[Depends(require_auth)])
async def api_logs_clear(req: LogClearRequest, request: Request):
    """清空日志（不碰配置 / 会话 / 记忆 / 模型）。

    正在写的 backend.log / overlay.log / 当前 debug 文件用 truncate，
    其余直接删除；返回 freed_bytes 与失败清单，前端据此即时刷新占用。
    """
    try:
        result = await asyncio.to_thread(devlog.clear_logs, req.category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # 清理本身出任何意外都不该变成 500 纯文本（前端会 JSON.parse 失败）：
        # 把异常类型和原因带回去，让界面能直接显示为什么没清掉
        detail = f"{type(exc).__name__}: {exc}"
        print(f"[DeckMeido] 清空日志失败：{detail}", file=sys.stderr)
        result = {
            "ok": False,
            "category": req.category,
            "deleted": 0,
            "truncated": 0,
            "failed": 1,
            "failed_files": [{"name": "logs", "error": detail}],
            "freed_bytes": 0,
            "usage": None,
        }
    audit.record("config", "logs_clear", "ok" if result["ok"] else "warn",
                 ip=_client_ip(request), detail=req.category)
    return result


@app.get("/api/usage", dependencies=[Depends(require_auth)])
async def api_usage(days: int = 14, api: str = ""):
    """本地用量统计（按日 / 按模型 / 按峰谷时段），不做网络请求

    api 为空 = 当前使用的那组；api=all = 全部汇总（含升级前未标记的历史）。
    返回里的 api_profiles 带每组的名称与是否有 Key（只有掩码），给下拉框用。
    """
    selected = api or (API_STATE.get("current") or "")
    # 峰谷单价按「被统计的那一组」的地址判断：中转等非官方端点不能用官方标记，
    # 「全部」时才沿用当前使用的地址
    pricing_base = llm.base_url if selected == "all" else ""
    if selected != "all":
        for p in API_STATE.get("profiles") or []:
            if p["id"] == selected:
                pricing_base = p.get("base_url") or ""
                break
    data = await asyncio.to_thread(usage.snapshot, days, None, pricing_base,
                                   None if selected == "all" else selected)
    data["api"] = selected or "all"
    data["api_profiles"] = [{**row, "current": row["id"] == API_STATE.get("current")}
                            for row in apicfg.public(API_STATE)]
    return data


@app.get("/api/usage/balance", dependencies=[Depends(require_auth)])
async def api_usage_balance(api: str = ""):
    """查询官方余额；失败原因原样带回，前端不做静默处理

    api 指定用哪一组配置查（空 = 当前使用）；不同 base_url 支持余额的走官方接口，
    不支持的按现有 reason/error 显示（404 = 该地址不支持余额查询）。
    """
    target = None
    if api and api != "all":
        for p in API_STATE.get("profiles") or []:
            if p["id"] == api:
                target = p
                break
        # 指定了账户却找不到（例如下拉还停在被删掉的那条）时明确报错，
        # 不能退回当前使用、把另一组的余额当成它的显示出来
        if target is None:
            return {"ok": False, "reason": "not_found",
                    "error": "该账户已不在接口列表里", "api": api, "api_name": ""}
    if target is None:
        target = apicfg.active(API_STATE) or {}
    result = await usage.fetch_balance(str(target.get("base_url") or ""),
                                       str(target.get("api_key") or ""))
    result["api"] = target.get("id") or ""
    result["api_name"] = target.get("name") or ""
    return result


@app.get("/api/usage/export", dependencies=[Depends(require_auth)])
async def api_usage_export():
    """导出按日聚合的原始 JSON（浏览器直接下载）"""
    data = await asyncio.to_thread(usage.export_data)
    body = json.dumps(data, ensure_ascii=False, indent=1)
    name = "deckmeido-usage-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/usage/clear", dependencies=[Depends(require_auth)])
async def api_usage_clear(request: Request):
    """清空本地用量统计（不碰配置、会话与余额）"""
    removed = await asyncio.to_thread(usage.clear)
    audit.record("config", "usage_clear", "ok", ip=_client_ip(request),
                 detail=str(removed))
    return {"ok": True, "removed": removed}


@app.post("/api/debug/render", dependencies=[Depends(require_auth)])
async def api_debug_render(req: RenderReport):
    """前端回报本轮思考链实际渲染了多少字（仅 dev_mode 可用）

    和 reasoning.end / session.save 三条长度放在一起，能直接看出是哪一环短的：
    LLM 收到 → 落盘 → 界面渲染。
    """
    if not devlog.enabled():
        raise HTTPException(status_code=403, detail="开发者模式未开启")
    devlog.log("ui.render", sid=req.sid, reasoning_chars=req.reasoning_chars,
               segments=req.segments)
    return {"ok": True}


@app.post("/api/pair/start")
async def api_pair_start(request: Request):
    """浏览器发起配对：返回 pair_id，此时还不能访问任何接口。"""
    ip = _client_ip(request)
    try:
        session = pairing.manager.start(ip, request.headers.get("user-agent", ""))
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc
    return {"ok": True, **session}


@app.get("/api/pair/status")
async def api_pair_status(pair_id: str = Query(...)):
    """浏览器轮询自己的配对状态（看不到 PIN，PIN 只在掌机侧展示）。"""
    try:
        return {"ok": True, **pairing.manager.status(pair_id)}
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc


@app.post("/api/pair/confirm")
async def api_pair_confirm(req: PairConfirmRequest, request: Request,
                           response: Response):
    """提交 PIN：一次性、60 秒有效、最多 3 次；成功后下发设备 Cookie。"""
    ip = _client_ip(request)
    try:
        device, token = pairing.manager.confirm(req.pair_id, req.pin, ip)
    except pairing.PairError as exc:
        # 不按 IP 计数：未允许 / 已过期 / 已失效都是正常流程状态。
        # 输错 PIN 的上限由单次配对会话自己管（3 次作废）。
        raise _pair_http(exc) from exc
    _set_device_cookie(response, request, token, bool(req.remember))
    return {"ok": True, "device": device, "remember": bool(req.remember),
            "device_days": _device_days()}


@app.get("/api/pair/pending", dependencies=[Depends(require_auth)])
async def api_pair_pending(history: bool = Query(False)):
    """掌机设置页：待配对设备 + PIN + 倒计时。

    history=1 时连同最近 5 分钟内已结束的会话一起返回，让设置页能显示
    「已允许 / 已拒绝」而不是点完就消失。插件与计数仍用默认的进行中列表。
    """
    sessions = pairing.manager.pending(include_finished=bool(history))
    return {"ok": True, "sessions": sessions, "device_days": _device_days()}


@app.post("/api/pair/refresh", dependencies=[Depends(require_auth)])
async def api_pair_refresh(req: PairIdRequest):
    try:
        return {"ok": True, **pairing.manager.refresh(req.pair_id)}
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc


@app.post("/api/pair/decision", dependencies=[Depends(require_auth)])
async def api_pair_decision(req: PairDecisionRequest, request: Request):
    try:
        return {"ok": True, **pairing.manager.decide(
            req.pair_id, bool(req.allow), _client_ip(request))}
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc


# ---------- 扫码配对 ----------
# 二维码里只有「后端地址 + 一次性短 token」：
#   - token 只存内存（内存里保留明文用于出图），落盘从不写，日志只记哈希前 8 位；
#   - 默认 90 秒有效、只能换一次，换成功后立即置为 used；
#   - 重新生成 / 显式撤销都会让旧 token 立刻作废；
#   - 换到的设备凭据与 PIN 配对走同一张 devices 表、同一个 Cookie，权限完全一致。
# PIN 配对流程保持不变，两条路径并存。

QR_TTL_S = 90.0
QR_KEEP_S = 300.0         # 失效后再留 5 分钟，用于区分「已使用 / 已过期 / 已撤销」
QR_MAX_FAILS = 20         # 单个 IP 每分钟最多允许的失败次数（只用于挡无效猜测刷审计）

_qr_lock = threading.RLock()
_qr_tokens: dict[str, dict] = {}          # sha256(token) -> 记录
_qr_fails: dict[str, tuple[float, int]] = {}


def _qr_fingerprint(raw: str) -> str:
    """日志里用的脱敏标识：token 哈希的前 8 位，绝不记录 token 明文。"""
    return pairing.token_hash(raw)[:8] if raw else ""


def _qr_cleanup(now: float) -> None:
    for key, rec in list(_qr_tokens.items()):
        if now >= rec["expires"] + QR_KEEP_S:
            _qr_tokens.pop(key, None)


def _qr_probe_ip() -> str:
    """UDP connect 探测本机出口地址（不真发包）；拿不到就返回空串。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return ""
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0] or ""
    except OSError:
        return ""
    finally:
        sock.close()


def _qr_request_port(request: Request) -> int:
    raw = (request.headers.get("host") or "").strip()
    try:
        port = urllib.parse.urlsplit(f"//{raw}").port
    except ValueError:
        port = None
    if port:
        return int(port)
    try:
        return int(CFG["server"].get("port", 18765))
    except (TypeError, ValueError):
        return 18765


def _qr_normalize(raw: str, request: Request) -> str:
    """用户/调用方给的地址 → http(s)://host:port（只保留根地址，不带路径）。"""
    value = (raw or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "http://" + value
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port or _qr_request_port(request)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not host or any(ch in host for ch in " /?#"):
        return ""
    if ":" in host:                       # IPv6 还原成 [::1] 形式
        host = f"[{host}]"
    scheme = "https" if parts.scheme.lower() == "https" else "http"
    return f"{scheme}://{host}:{port}"


def _qr_address(request: Request, override: str) -> str:
    """选一个手机连得上的地址：显式指定 > 请求 Host（非回环）> 局域网 IP > UDP 探测。

    Deck 上没有公网直连，只保证局域网和 Tailscale/WireGuard；Tailscale 的
    100.x 地址不走 RFC1918 枚举，需要用户在页面里手填（会原样写进二维码）。
    """
    fixed = _qr_normalize(override, request)
    if fixed:
        return fixed
    host_header = (request.headers.get("host") or "").strip()
    try:
        hostname = urllib.parse.urlsplit(f"//{host_header}").hostname or ""
    except ValueError:
        hostname = ""
    if hostname and not netcfg.is_loopback(hostname):
        return _qr_normalize(hostname, request)
    for ip in (_lan_ip(), _qr_probe_ip()):
        if ip and not netcfg.is_loopback(ip):
            return _qr_normalize(f"{ip}:{_qr_request_port(request)}", request)
    return ""


def _qr_fail(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "detail": detail},
                        status_code=status)


def _qr_rate_limited(ip: str, now: float) -> bool:
    start, count = _qr_fails.get(ip, (0.0, 0))
    return now - start < 60 and count >= QR_MAX_FAILS


def _qr_note_fail(ip: str, now: float) -> None:
    start, count = _qr_fails.get(ip, (now, 0))
    if now - start >= 60:
        start, count = now, 0
    _qr_fails[ip] = (start, count + 1)
    if len(_qr_fails) > 200:
        for key, (t0, _n) in list(_qr_fails.items()):
            if now - t0 >= 60:
                _qr_fails.pop(key, None)


def _qr_png_cv2(payload: str) -> io.BytesIO | None:
    """qrcode 未安装时的兜底：OpenCV 编码 + Pillow 放大加静区。"""
    try:
        import cv2
        from PIL import Image
    except Exception:
        return None
    try:
        matrix = cv2.QRCodeEncoder_create().encode(payload)
        scale, border = 8, 4
        img = Image.fromarray(matrix).convert("L")
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        canvas = Image.new("L", (img.width + border * 2 * scale,
                                 img.height + border * 2 * scale), 255)
        canvas.paste(img, (border * scale, border * scale))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf
    except Exception:
        return None


def _qr_png(payload: str) -> str:
    """后端出图（data URL）。网页端不引入任何联网库，二维码全部由服务端生成。"""
    buf: io.BytesIO | None = None
    try:
        import qrcode
        img = qrcode.make(payload)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    except Exception:
        buf = _qr_png_cv2(payload)
    if buf is None:
        raise HTTPException(
            status_code=501,
            detail="二维码生成失败：需要 qrcode 依赖（或 opencv + pillow）")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@app.post("/api/pair/qr", dependencies=[Depends(require_auth)])
async def api_pair_qr(request: Request, req: PairQrRequest | None = None):
    """Deck 生成扫码配对二维码（鉴权；生成新的会立刻作废旧 token）。"""
    address = _qr_address(request, req.host if req else "")
    if not address:
        raise HTTPException(
            status_code=409,
            detail="找不到可用的局域网地址：请在地址栏填写 Deck 的局域网 IP 或 Tailscale 名称")
    now = time.time()
    token = secrets.token_urlsafe(12)
    payload = ("deckmeido://pair?v=1&u="
               + urllib.parse.quote(address, safe="") + "&t=" + token)
    with _qr_lock:
        _qr_cleanup(now)
        for rec in _qr_tokens.values():
            if rec["state"] == "active":
                rec["state"] = "revoked"
        _qr_tokens[pairing.token_hash(token)] = {
            "token": token,
            "payload": payload,
            "address": address,
            "created": now,
            "expires": now + QR_TTL_S,
            "state": "active",
            "used_at": 0.0,
            "ip": _client_ip(request),
        }
    audit.record("pair", "qr", "ok", ip=_client_ip(request),
                 detail=f"{_qr_fingerprint(token)} {address}")
    return {"ok": True, "address": address, "expires_in": int(QR_TTL_S),
            "payload": payload, "qr": _qr_png(payload)}


@app.post("/api/pair/qr/redeem")
async def api_pair_qr_redeem(req: PairQrRedeemRequest, request: Request,
                             response: Response):
    """手机扫码换取设备凭据：未鉴权，但必须过一次性的有效 token。

    失败一律返回明确原因（invalid / used / expired / revoked / too_many），
    与 PIN 配对成功后下发的 Cookie 完全同权。
    """
    ip = _client_ip(request)
    now = time.time()
    token = (req.token or "").strip()
    if _qr_rate_limited(ip, now):
        return _qr_fail("too_many", "尝试过于频繁，请稍后再试", 429)
    rec = _qr_tokens.get(pairing.token_hash(token)) if token else None
    if not rec:
        _qr_note_fail(ip, now)
        audit.record("pair", "qr_redeem", "fail", ip=ip, reason="invalid",
                     detail=_qr_fingerprint(token))
        return _qr_fail("invalid", "二维码无效或已被刷新，请在 Deck 上生成新的二维码", 400)
    with _qr_lock:
        state = rec["state"]
        if state == "active" and now < rec["expires"]:
            rec["state"] = "used"
            rec["used_at"] = now
            rec["used_ip"] = ip
        elif state == "active":
            state = "expired"
    if state != "active":
        _qr_note_fail(ip, now)
        reason = {
            "used": ("used", "二维码已被使用，请刷新后重新扫码", 409),
            "revoked": ("revoked", "二维码已被 Deck 撤销，请刷新后重新扫码", 410),
            "expired": ("expired", "二维码已过期，请刷新后重新扫码", 410),
        }.get(state, ("invalid", "二维码无效或已被刷新，请在 Deck 上生成新的二维码", 400))
        audit.record("pair", "qr_redeem", "fail", ip=ip, reason=reason[0],
                     detail=_qr_fingerprint(token))
        return _qr_fail(*reason)
    name = (req.name or "").strip() or "扫码设备"
    device, device_token = pairing.manager.devices.create(
        name, ip, request.headers.get("user-agent", ""))
    _set_device_cookie(response, request, device_token, bool(req.remember))
    _qr_fails.pop(ip, None)
    audit.record("pair", "qr_redeem", "ok", ip=ip,
                 detail=f"{_qr_fingerprint(token)} {device.get('name', '')}")
    return {"ok": True, "device": device, "remember": bool(req.remember),
            "device_days": _device_days()}


@app.post("/api/pair/qr/revoke", dependencies=[Depends(require_auth)])
async def api_pair_qr_revoke(request: Request):
    """Deck 主动撤销当前二维码（扫码后提示「已被撤销」）。"""
    revoked = 0
    with _qr_lock:
        for rec in _qr_tokens.values():
            if rec["state"] == "active":
                rec["state"] = "revoked"
                revoked += 1
    if revoked:
        audit.record("pair", "qr_revoke", "ok", ip=_client_ip(request),
                     detail=str(revoked))
    return {"ok": True, "revoked": revoked}


# 独立的扫码配对页：index.html 属禁改范围，这里给 Deck 一个可直接打开的页面
QR_PAGE_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeckMeido 扫码配对</title>
<style>
:root{color-scheme:light dark}
body{margin:0;padding:24px;font:15px/1.6 system-ui,-apple-system,"Noto Sans SC",sans-serif;
     background:#0f1115;color:#e8eaed;display:flex;justify-content:center}
.card{width:min(420px,100%);background:#171a21;border:1px solid #262b36;border-radius:16px;
      padding:22px;box-shadow:0 10px 30px #0008}
h1{font-size:18px;margin:0 0 6px}
p.sub{margin:0 0 18px;color:#9aa0ab;font-size:13px}
#qrbox{display:flex;align-items:center;justify-content:center;min-height:260px;
       background:#0b0d11;border:1px solid #262b36;border-radius:12px}
#qr{width:248px;height:248px;image-rendering:pixelated;display:none}
#empty{color:#7d8494;font-size:13px;text-align:center;padding:16px}
#addr{margin:14px 0 0;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;
      color:#cfd3da;word-break:break-all;text-align:center}
#state{margin-top:8px;text-align:center;font-size:13px;color:#9aa0ab}
.bar{display:flex;gap:8px;margin-top:14px}
button{flex:1;padding:10px 12px;border-radius:10px;border:1px solid #2f3644;
       background:#20242e;color:#e8eaed;font-size:14px;cursor:pointer}
button.primary{background:#3b6ef6;border-color:#3b6ef6}
button:disabled{opacity:.45;cursor:not-allowed}
input{width:100%;box-sizing:border-box;margin-top:14px;padding:10px 12px;border-radius:10px;
      border:1px solid #2f3644;background:#0b0d11;color:#e8eaed;font-size:13px}
.hint{margin-top:8px;color:#7d8494;font-size:12px}
</style></head><body>
<div class="card">
<h1>手机扫码配对</h1>
<p class="sub">用 DeckMeido 安卓客户端扫描二维码即可配对，无需输入地址和 PIN。</p>
<div id="qrbox"><img id="qr" alt="配对二维码"><div id="empty">正在生成…</div></div>
<div id="addr"></div>
<div id="state"></div>
<input id="host" placeholder="可选：指定地址（如 100.101.102.103:18765 或 Tailscale 名称）">
<div class="hint">二维码 90 秒有效，只能使用一次；刷新会让旧二维码立刻失效。</div>
<div class="bar">
  <button id="refresh" class="primary">刷新二维码</button>
  <button id="revoke">撤销</button>
</div>
</div>
<script>
const img=document.getElementById('qr'),empty=document.getElementById('empty');
const addr=document.getElementById('addr'),state=document.getElementById('state');
let deadline=0,timer=null;
function tick(){
  const left=Math.max(0,Math.round((deadline-Date.now())/1000));
  if(!left){state.textContent='已过期，请刷新二维码';clearInterval(timer);timer=null;
            img.style.opacity=.35;return}
  state.textContent='剩余 '+left+' 秒，等待手机扫描';
}
async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body||{})});
  let data={};try{data=await r.json()}catch(e){}
  if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));
  return data;
}
async function refresh(){
  state.textContent='正在生成…';img.style.display='none';empty.style.display='block';
  empty.textContent='正在生成…';
  try{
    const host=document.getElementById('host').value.trim();
    const data=await post('/api/pair/qr',{host});
    img.src=data.qr;img.style.display='block';img.style.opacity=1;
    empty.style.display='none';
    addr.textContent=data.address;
    deadline=Date.now()+data.expires_in*1000;
    if(timer)clearInterval(timer);timer=setInterval(tick,1000);tick();
  }catch(e){
    img.style.display='none';empty.style.display='block';empty.textContent=e.message;
    addr.textContent='';state.textContent='';
  }
}
document.getElementById('refresh').onclick=refresh;
document.getElementById('revoke').onclick=async()=>{
  try{
    await post('/api/pair/qr/revoke',{});
    img.style.display='none';img.removeAttribute('src');empty.style.display='block';
    empty.textContent='二维码已撤销';addr.textContent='';state.textContent='';
    if(timer){clearInterval(timer);timer=null}
  }catch(e){state.textContent=e.message}
};
refresh();
</script></body></html>"""


@app.get("/pair/qr", dependencies=[Depends(require_auth)], include_in_schema=False)
async def pair_qr_page():
    """Deck 本机可直接打开 http://127.0.0.1:<port>/pair/qr 查看二维码。"""
    return HTMLResponse(QR_PAGE_HTML)


# ---------- 已配对设备 ----------

@app.get("/api/devices", dependencies=[Depends(require_auth)])
async def api_devices():
    return {"ok": True, "devices": pairing.manager.devices.list(),
            "device_days": _device_days()}


@app.post("/api/devices/rename", dependencies=[Depends(require_auth)])
async def api_device_rename(req: DeviceRenameRequest, request: Request):
    try:
        device = pairing.manager.devices.rename(req.device_id, req.name)
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc
    audit.record("device", "rename", "ok", ip=_client_ip(request),
                 detail=device.get("name", ""))
    return {"ok": True, "device": device}


@app.post("/api/devices/remove", dependencies=[Depends(require_auth)])
async def api_device_remove(req: DeviceIdRequest, request: Request):
    if not pairing.manager.devices.remove(req.device_id):
        raise HTTPException(status_code=404, detail="设备不存在或已被移除")
    audit.record("device", "remove", "ok", ip=_client_ip(request),
                 detail=req.device_id[:8])
    return {"ok": True, "devices": pairing.manager.devices.list()}


@app.post("/api/devices/remove_all", dependencies=[Depends(require_auth)])
async def api_device_remove_all(request: Request):
    n = pairing.manager.devices.remove_all()
    audit.record("device", "remove_all", "ok", ip=_client_ip(request), detail=str(n))
    return {"ok": True, "removed": n}


# ---------- 登录与配对审计 ----------

@app.get("/api/audit", dependencies=[Depends(require_auth)])
async def api_audit(limit: int = Query(default=50, ge=1, le=audit.MAX_EVENTS)):
    """只读审计：最近若干条鉴权失败 / 配对尝试，不包含 PIN 或设备令牌。"""
    return {"ok": True, "events": audit.recent(limit)}


@app.post("/api/tts", dependencies=[Depends(require_auth)])
async def api_set_tts(enabled: bool = Query(...)):
    """只切语音播报开关：给 Decky 插件面板用

    QAM 里没法像网页设置页那样填一整张表单，只带一个参数，走的还是
    update_config 同一条热更新路径（tts.enable 立即生效，不用重启）。
    """
    save_config({"tts": {"enable": bool(enabled)}})
    return {"ok": True, "tts_enable": tts.enable}


@app.get("/api/tts", dependencies=[Depends(require_auth)])
async def api_tts_status():
    """语音合成状态：引擎是否可用、模型是否已下载、下载进度、署名信息。"""
    return {"ok": True, **tts.status()}


@app.post("/api/tts/download", dependencies=[Depends(require_auth)])
async def api_tts_download():
    """后台下载语音模型（约 38MB）；重复点击不会开第二个线程。"""
    # 离线模式：模型必须联网下载，直接返回原因，不启动下载线程
    if netmode.enabled():
        raise HTTPException(status_code=503, detail=netmode.reason("语音模型下载"))
    return {"ok": True, **tts.start_download()}


@app.post("/api/tts/download/cancel", dependencies=[Depends(require_auth)])
async def api_tts_download_cancel():
    return {"ok": True, **tts.request_cancel()}


@app.get("/api/selfcheck", dependencies=[Depends(require_auth)])
async def api_selfcheck():
    """桌面控件自检的可补装组件清单 + 当前安装任务"""
    return {"ok": True, **selfcheck.snapshot()}


@app.post("/api/selfcheck/install", dependencies=[Depends(require_auth)])
async def api_selfcheck_install(component: str = Query(...)):
    """后台线程安装单个组件；同一时间只允许一个任务（重复点击返回 busy）"""
    loop = asyncio.get_running_loop()

    def hook(payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "selfcheck_progress", **payload}), loop)
        except Exception:
            pass

    return selfcheck.start(component, hook)


@app.post("/api/selfcheck/uninstall", dependencies=[Depends(require_auth)])
async def api_selfcheck_uninstall(component: str = Query(...)):
    """后台卸载我们自己管理的组件（模型 / nltk 数据 / 守护进程托管）。

    系统包不在可卸载范围内：包由 pacman 管理，卸载要用户自己决定。
    """
    loop = asyncio.get_running_loop()

    def hook(payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "selfcheck_progress", **payload}), loop)
        except Exception:
            pass

    res = selfcheck.start_uninstall(component, hook)
    if not res.get("ok"):
        code = 409 if res.get("reason") == "busy" else 400
        return JSONResponse(res, status_code=code)
    return res


class SelfcheckPasswordBody(BaseModel):
    component: str
    password: str


@app.post("/api/selfcheck/password", dependencies=[Depends(require_auth)])
async def api_selfcheck_password(req: SelfcheckPasswordBody):
    """界面提交管理员密码，交给正在等待授权的系统包安装任务。

    密码只在后端内存里转交，不落日志、不写配置、不在响应里回显。
    """
    res = await asyncio.to_thread(
        selfcheck.provide_password, req.component, req.password)
    if not res.get("ok"):
        code = 409 if res.get("reason") == "not_waiting" else 400
        return JSONResponse(res, status_code=code)
    return res


@app.get("/api/sudo/status", dependencies=[Depends(require_auth)])
async def api_sudo_status():
    """当前 sudo 授权状态：是否可用、还要多久失效、是否有待输入密码的请求"""
    st = sudo_auth.cache_status()
    return {"ok": True, **st, "pending": sudo_auth.pending_status(),
            "ttl": int(sudo_auth.ttl())}


class SudoPasswordBody(BaseModel):
    password: str


@app.post("/api/sudo/password", dependencies=[Depends(require_auth)])
async def api_sudo_password(req: SudoPasswordBody):
    """界面提交管理员密码：`sudo -A -v` 校验一次，通过后内存缓存 TTL 时长。

    密码只在后端内存里流转，不落日志、不写配置、不在响应里回显。
    """
    res = await asyncio.to_thread(sudo_auth.provide_password, req.password, "ui")
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return res


@app.get("/api/selfcheck/progress", dependencies=[Depends(require_auth)])
async def api_selfcheck_progress():
    return {"ok": True, "job": selfcheck.job_snapshot()}


@app.post("/api/tts/preview", dependencies=[Depends(require_auth)])
async def api_tts_preview(req: TTSPreviewRequest):
    """就地试听：用当前发音人念一句短的，返回实际用到的引擎与失败原因。"""
    result = await tts.preview(req.text)
    return {
        "ok": bool(result.get("engine")) and not result.get("timed_out"),
        # 引擎名只用于日志/诊断；界面按 speaker 展示
        "engine": result.get("engine", ""),
        "speaker": tts.speaker,
        "speaker_label": SPEAKER_LABELS.get(tts.speaker, ""),
        "error": result.get("error", ""),
    }


@app.post("/api/tts/say", dependencies=[Depends(require_auth)])
async def api_tts_say(req: TTSSayRequest):
    """给远程客户端（手机）合成语音，直接返回 WAV 字节。

    和 /api/tts/preview 的区别：preview 在掌机本机播放，只回状态；这个接口
    只合成、不播放，生成的文件随响应结束删除。鉴权和其它 API 一样走设备
    Cookie，不额外开旁路。
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text 超过 2000 字上限")
    lang = (req.language or "").strip()
    if lang and lang not in LANGUAGES:
        raise HTTPException(status_code=400, detail="不支持的 language")
    try:
        path = await asyncio.to_thread(tts.synth_wav, text, lang)
    except TTSError as e:
        raise HTTPException(status_code=503, detail=f"语音合成失败：{e}") from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"语音合成失败：{e}") from e
    return FileResponse(
        path, media_type="audio/wav", filename="tts.wav",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(lambda: path.unlink(missing_ok=True)),
    )


@app.get("/api/ask-template", dependencies=[Depends(require_auth)])
async def api_get_ask_template():
    """「截屏询问」提示词模板：插件面板展开设置区时读一次"""
    return {"ok": True, "text": ASK_TEMPLATE, "default": DEFAULT_ASK_TEMPLATE}


@app.post("/api/ask-template", dependencies=[Depends(require_auth)])
async def api_set_ask_template(req: AskTemplateUpdate):
    """改「截屏询问」提示词模板：空 / 全空白表示恢复默认

    模板只影响下一次点「截屏询问」，已经发出的那一轮不受影响。
    """
    tpl = req.text.strip() or DEFAULT_ASK_TEMPLATE
    save_config({"agent": {"ask_template": tpl}})
    return {"ok": True, "text": ASK_TEMPLATE}


@app.get("/api/translate-template", dependencies=[Depends(require_auth)])
async def api_get_translate_template():
    """「截图翻译」提示词模板：和大屏模型页那一栏是同一份配置"""
    return {
        "ok": True,
        "text": TRANSLATE_TEMPLATE,
        "default": DEFAULT_TRANSLATE_TEMPLATE,
    }


@app.post("/api/translate-template", dependencies=[Depends(require_auth)])
async def api_set_translate_template(req: TranslateTemplateUpdate):
    """改「截图翻译」提示词模板：空 / 全空白表示恢复默认

    保存后写回 config.toml 的 [agent] translate_template 并立即热更新；
    下一次插件发起的翻译请求就会带上这段新提示词。
    """
    tpl = req.text.strip() or DEFAULT_TRANSLATE_TEMPLATE
    save_config({"agent": {"translate_template": tpl}})
    return {"ok": True, "text": TRANSLATE_TEMPLATE}


# ---------- 按游戏提示词预设 ----------

def _presets_view() -> dict:
    """预设列表 + 当前生效状态；设置页和插件都读这一份"""
    return {
        "ok": True,
        "presets": presets.list_presets(),
        "current": presets.active_view(),
        # 用户配置里的全局值：改预设不会写回它，界面要能分清两份数据
        "base_prompt": status_line.prompt_text(CFG.get("agent") or {}),
        "effective_prompt": status_prompt_text(),
        "default_prompt": DEFAULT_STATUS_PROMPT,
    }


@app.get("/api/presets", dependencies=[Depends(require_auth)])
async def api_get_presets():
    return _presets_view()


@app.post("/api/presets", dependencies=[Depends(require_auth)])
async def api_save_preset(req: PresetSaveBody):
    """插件「保存为预设」：按当前游戏存一份提示词（同一游戏重复保存即更新）"""
    try:
        item = presets.upsert(req.appid, req.game_name, req.prompt, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    current = presets.active_view()
    if req.activate:
        current = presets.set_active_game(req.appid, req.game_name)
    return {"ok": True, "preset": item, "current": current}


@app.post("/api/presets/active", dependencies=[Depends(require_auth)])
async def api_set_active_game(req: ActiveGameBody):
    """插件检测到游戏开始/退出时上报，命中预设就套用，退出就回默认

    游戏探测只在插件里做（那里本来就有 Steam appid/游戏名的唯一实现），
    后端不重复写第二套；这个接口只负责切换运行时提示词。
    """
    return {"ok": True, "current": presets.set_active_game(req.appid, req.name)}


@app.patch("/api/presets/{preset_id}", dependencies=[Depends(require_auth)])
async def api_update_preset(preset_id: str, req: PresetUpdateBody):
    """设置页编辑内容 / 改名；改名不影响绑定的游戏"""
    try:
        item = presets.update(preset_id, prompt=req.prompt, title=req.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="预设不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "preset": item, "current": presets.active_view()}


@app.delete("/api/presets/{preset_id}", dependencies=[Depends(require_auth)])
async def api_delete_preset(preset_id: str):
    if not presets.delete(preset_id):
        raise HTTPException(status_code=404, detail="预设不存在")
    return {"ok": True, "current": presets.active_view()}


# ---------- 语音输入 ----------

@app.get("/api/stt", dependencies=[Depends(require_auth)])
async def api_stt_status():
    """语音输入状态：前端据此决定按钮能不能按、要不要提示下模型

    runtime / model 分开报，是为了区分「没装 sherpa-onnx」和「装了但模型没下」
    两种不可用原因 —— 前者要 pip，后者点一下就下。
    """
    rec = record_supported()
    return {
        "ok": True,
        **stt.status(),
        "record": rec["ok"],
        "record_reason": rec["reason"],
        # 录音期间静音外放的开关与可用性（工具缺失时只提示，不影响录音）
        "duck_output": audiomute.enabled(),
        "duck": audiomute.status(),
    }


@app.post("/api/stt/download", dependencies=[Depends(require_auth)])
async def api_stt_download():
    """启动语音模型下载（镜像直取约 250MB，GitHub 回退为 1GB 压缩包）

    改成「后台任务 + 轮询进度」：原先这个接口要同步等几分钟，Decky 桥接请求
    超时后前端只能看到失败，模型其实还在下；下完了也得等下一次重启才认。
    现在秒回状态快照，进度由 /api/stt 里的 download 字段给出。
    """
    # 离线模式：模型必须联网下载，直接返回原因，不启动下载线程
    if netmode.enabled():
        raise HTTPException(status_code=503, detail=netmode.reason("语音识别模型下载"))
    return {"ok": True, **stt.start_download()}


@app.post("/api/stt/download/cancel", dependencies=[Depends(require_auth)])
async def api_stt_download_cancel():
    """取消正在下载的模型（已下载的 .part 会被清掉，不会留下半个包）"""
    return {"ok": True, **stt.request_cancel()}


@app.post("/api/stt/toggle", dependencies=[Depends(require_auth)])
async def api_set_stt(enabled: bool = Query(...)):
    """只切语音输入开关：给 Decky 面板用

    关掉时 apply_config 会顺手 stt.unload()，把识别模型从内存里放掉
    （SenseVoice int8 常驻约 100MB+），Dock 上的 15W 预算也少一份占用。
    """
    save_config({"stt": {"enable": bool(enabled)}})
    return {"ok": True, "stt_enable": stt.enable}


@app.post("/api/stt/duck", dependencies=[Depends(require_auth)])
async def api_stt_duck(on: bool = Query(...)):
    """录音开始/结束时静音、恢复系统输出

    失败一律静默降级：接口永远返回 ok=True，前端不需要处理错误，
    更不会因为静音失败把录音拦下来。多端同时录音时按引用计数恢复。
    """
    if on:
        return audiomute.duck()
    return audiomute.restore()


@app.post("/api/stt/transcribe", dependencies=[Depends(require_auth)])
async def api_stt_transcribe(req: Request):
    """整段 WAV 转文字。请求体直接是音频字节，不做 multipart

    前端用 MediaRecorder / AudioWorklet 录完一次性 POST 上来，
    返回的文字由前端填进输入框，**不自动发送**（用户还要改）。
    """
    wav = await req.body()
    if not wav:
        raise HTTPException(status_code=400, detail="没有收到音频")
    # 10 秒 16k 单声道 16bit ≈ 320KB，留 20 倍余量防滥用
    if len(wav) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="音频太大（上限 8MB）")
    try:
        text = await asyncio.to_thread(stt.transcribe, wav)
    except STTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "text": text}


# ---------- 智能音箱模式（声控唤醒） ----------
# 这些接口只允许本机回环访问：唤醒/录音涉及麦克风与家庭对话，即使开了局域网
# 配对也不能让别的设备开关它、更不能读走音频（这里本来也没有取音频的接口）。


def _wake_local_only(request: Request) -> None:
    ip = request.client.host if request.client else ""
    if not netcfg.is_loopback(ip):
        raise HTTPException(status_code=403, detail="智能音箱模式只允许本机访问")


@app.get("/api/wake/state", dependencies=[Depends(require_auth)])
async def api_wake_state(request: Request):
    _wake_local_only(request)
    # 睡眠模式的倒计时一起带上：眼睛页本来就每 250ms 轮询这个接口，
    # 再单独去拉整份 /api/status 太重，而倒计时恰恰需要它够快。
    return {"ok": True, **wake_service.status(),
            "sleep": sleepmode.service.status()}


@app.get("/api/music/now", dependencies=[Depends(require_auth)])
async def api_music_now():
    """侧栏音乐条：当前在放什么 + 封面 + 进度。没在放就返回 playing=False。

    和 /api/sysinfo 同一档鉴权（5 秒一刷的轮询接口）；封面是播放器给的远端
    图片地址，由浏览器自己去取，不经过这里。
    """
    try:
        info = await asyncio.to_thread(media.now_playing)
    except Exception:
        info = None
    return {"ok": True, "playing": False} if not info else {"ok": True, **info}


@app.post("/api/music/action", dependencies=[Depends(require_auth)])
async def api_music_action(payload: dict):
    """侧栏音乐条的三个按钮：上一首 / 播放暂停 / 下一首。"""
    action = str((payload or {}).get("action") or "").strip().lower()
    if action == "toggle":
        info = await asyncio.to_thread(media.now_playing)
        action = "pause" if info and info.get("playing") else "play"
    if action not in ("next", "previous", "pause", "play"):
        raise HTTPException(status_code=400, detail="不支持的动作")
    try:
        await asyncio.to_thread(media.execute, action)
    except media.MediaError as exc:
        raise HTTPException(status_code=503, detail=exc.reason) from exc
    return {"ok": True, "action": action}


@app.post("/api/wake/toggle", dependencies=[Depends(require_auth)])
async def api_wake_toggle(request: Request, on: bool | None = Query(default=None)):
    """开关常驻监听；打开时写 [wake] enabled=true，关闭时同时退出监听。"""
    _wake_local_only(request)
    target = (not wake_service.enabled) if on is None else bool(on)
    save_config({"wake": {"enable": target}})
    try:
        status = await (wake_service.arm() if target else wake_service.disarm())
    except wake.WakeError as exc:
        # 起不来就把开关退回关闭：否则设置页显示「已开」、眼睛显示错误，
        # 而后端每次重启都会白试一次（缺模型/缺 sherpa/麦克风被占都属于这种）。
        if target:
            try:
                save_config({"wake": {"enable": False}})
            except HTTPException:
                pass
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **status}


@app.post("/api/wake/stop", dependencies=[Depends(require_auth)])
async def api_wake_stop(request: Request):
    """停止聆听：立刻停采集并退出智能音箱模式；播报用 /api/wake/tts-stop。"""
    _wake_local_only(request)
    save_config({"wake": {"enable": False}})
    return {"ok": True, **await wake_service.disarm()}


@app.post("/api/wake/exit", dependencies=[Depends(require_auth)])
async def api_wake_exit(request: Request):
    """退出智能音箱模式：关总开关并释放麦克风/电源保持。"""
    _wake_local_only(request)
    save_config({"wake": {"enable": False}})
    return {"ok": True, **await wake_service.disarm()}


@app.post("/api/sleep/cancel", dependencies=[Depends(require_auth)])
async def api_sleep_cancel(request: Request):
    """取消睡眠模式的关机倒计时。眼睛页收到任何键鼠/触摸输入时调用。

    只在本机有效：局域网设备不该能替这台机器「延后关机」。
    """
    _wake_local_only(request)
    return {"ok": True, "cancelled": sleepmode.service.cancel()}


@app.on_event("startup")
async def _wake_startup():
    """配置里 enabled=true 时，后端起来后自动进入待机监听（失败只记状态）。"""
    if not wake_service.enabled:
        return

    async def _boot():
        try:
            await wake_service.arm()
        except Exception:
            pass

    asyncio.create_task(_boot())


@app.on_event("shutdown")
async def _wake_shutdown():
    try:
        await wake_service.disarm()
    except Exception:
        pass
    # 进程退出兜底：即使上面哪一步异常，也不能把用户的音乐音量停在静音状态
    try:
        media.unmute_for_record()
    except Exception:
        pass


# ---------- 版本与升级 ----------

# 不校验配对：鉴权页在登录前就要显示版本号
@app.get("/api/version")
async def api_version():
    return {
        # version / running_version / disk_version / version_match / pid
        **version_state(),
        "repo": updater.REPO,
        "update_source_label": updater.source_label(),
        "is_git": (INSTALL_DIR / ".git").exists(),
        "install_dir": str(INSTALL_DIR),
        "update": update_cache,
    }


@app.get("/api/i18n")
async def api_i18n():
    """界面语言与英文替换表（纯静态文案，无需鉴权）

    [ui] language = "en" 时网页/客户端启动后替换静态文案；中文模式返回空表，
    前端保持原样。插件端暂未接入，可后续直接读这个接口。
    """
    return {
        "language": UI_LANG,
        "strings": i18n.strings() if UI_LANG == "en" else {},
    }


@app.get("/api/update/check", dependencies=[Depends(require_auth)])
async def api_update_check():
    try:
        info = await updater.check()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return info


@app.post("/api/update/apply", dependencies=[Depends(require_auth)])
async def api_update_apply():
    """从网页一键升级：拉取代码后交给 install.sh upgrade 重启服务

    升级会把正在运行的这个服务换掉，所以交给独立会话的脚本进程去做，
    接口只负责把任务踢起来并立刻返回。
    """
    try:
        return await asyncio.to_thread(updater.apply, True)
    except RuntimeError as exc:
        msg = str(exc)
        raise HTTPException(status_code=400 if "不是 git" in msg else 502, detail=msg)


@app.post("/api/restart", dependencies=[Depends(require_auth)])
async def api_restart():
    """重启核心（后端服务自身）

    这里**不能自己杀自己**。后端多半是被 Decky 插件用
    `runuser -u deck -- <venv>/bin/python -m uvicorn ...` 降权拉起来的，
    自己 `pkill -f 'uvicorn server.main:app'` 会把外层的 runuser 一起带走，
    再用 nohup 就地拉起的那份既没有 runuser 包装、又跟插件失去联系，
    结果就是旧进程没了、新进程没起来，重启变成「把后端关掉」。
    而且插件是唯一知道完整启动命令的一方。

    所以这里只表明「需要重启」，由调用方（插件）去执行。插件拿到的
    `delegate: true` 就是让它走自己的 runuser 命令重放一遍。
    直接 curl 这个接口不会真的重启，这也避免了误调用把服务弄挂。

    但现在网页端也需要一个能用的「重启后端」入口（版本不一致时提示用）。
    这里只落一个重启请求标记：桌面客户端的看门狗每 5 秒看一次，插件在
    拉状态时也会看一次，谁先看到谁用自己的启动命令重放，后端自己不杀自己。
    """
    marker = paths.data_dir() / "restart.request"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写重启标记失败：{exc}")
    return {"ok": True, "version": VERSION, "delegate": True, "requested": True}


# 后台自动检查升级：启动时查一次，之后每天一次；只提示，不自动安装
update_cache: dict = {}
UPDATE_INTERVAL = 24 * 3600


async def update_watch_loop():
    global update_cache
    while True:
        try:
            info = await updater.check()
            # used=实际问到结果的源；fallback=前面失败的源（用于提示「已切备用源」）
            used = info.get("source") or ""
            base = {"source": used, "source_label": info.get("source_label", ""),
                    "source_used": info.get("source_used") or used,
                    "mirror_version": info.get("mirror_version", ""),
                    "github_version": info.get("github_version", ""),
                    "github_reachable": bool(info.get("github_reachable")),
                    "fallback": info.get("fallback_from") or []}
            if info["has_update"]:
                update_cache = {"has_update": True, "latest": info["tag"],
                                "current": info["current"], "url": info.get("url", ""),
                                "notes_summary": updater.notes_summary(info.get("notes")),
                                **base}
                await manager.broadcast({"type": "update_available", **update_cache})
            else:
                update_cache = {"has_update": False, "current": info["current"],
                                "notes_summary": updater.notes_summary(info.get("notes")),
                                **base}
            devlog.log("updater.check", has_update=bool(info.get("has_update")),
                       current=info.get("current"), latest=info.get("tag"),
                       source=used, fallback=info.get("fallback_from") or [])
        except Exception as exc:
            # 两个源都连不上（断网、被墙）属正常情况，记下原因下次再试
            update_cache = {"has_update": False, "current": updater.current_version(),
                            "error": str(exc)[:300]}
            devlog.log("updater.error", error=repr(exc))
        await asyncio.sleep(UPDATE_INTERVAL)


def _uvicorn_binding() -> tuple[str | None, int | None]:
    """从启动命令行/环境变量里取实际监听地址与端口（取不到返回 None）。"""
    host = port = None
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if i + 1 >= len(argv):
            continue
        if arg == "--host":
            host = argv[i + 1]
        elif arg in ("--port", "-p"):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
    if port is None:
        try:
            port = int(os.environ.get("DECKMEIDO_PORT") or "") or None
        except ValueError:
            port = None
    return host, port


@app.on_event("startup")
async def _check_binding():
    """启动时对账：实际监听端口/地址与 config.toml 不一致就明确告警。

    端口可能被 DECKMEIDO_PORT 或启动命令覆盖，这是允许的；但配置里写的是
    另一个值时，设置页显示、局域网地址和用户预期都会对不上，必须说出来。
    """
    # uvicorn 启动时可能重设日志级别；这里再拉一次，保证 dev_mode 保存前
    # 就已经生效的配置不会因为初始化顺序被盖回去
    devlog.apply_levels()
    devlog.log(
        "backend.start",
        version=VERSION,
        argv=sys.argv,
        cwd=os.getcwd(),
        pid=os.getpid(),
        env=devlog.env_summary(),
        bind=_uvicorn_binding(),
    )
    host, actual_port = _uvicorn_binding()
    cfg_port = int((CFG.get("server") or {}).get("port", 18765))
    if actual_port is not None and actual_port != cfg_port:
        print(f"[DeckMeido] 警告：实际监听端口 {actual_port} 与配置端口 {cfg_port} 不一致；"
              "请同步 DECKMEIDO_PORT、config.toml [server].port 与启动命令",
              file=sys.stderr)
    if host and host not in ("127.0.0.1", "::1", "localhost") \
            and not netcfg.lan_enabled(CFG.get("server") or {}):
        print("[DeckMeido] 警告：lan_enable=false 但仍在 0.0.0.0 上监听；"
              "HTTP 层已拒绝局域网请求，建议用 127.0.0.1 重启后端", file=sys.stderr)


@app.on_event("startup")
async def _start_update_watch():
    loop = asyncio.get_running_loop()

    def hook(payload: dict) -> None:
        # 授权请求可能来自工作线程，统一投递回事件循环里广播
        try:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "sudo_auth", **payload}), loop)
        except Exception:
            pass

    sudo_auth.set_hook(hook)
    asyncio.create_task(update_watch_loop())


@app.get("/api/models", dependencies=[Depends(require_auth)])
async def api_models(base_url: str = "", api_key: str = ""):
    """向服务商拉取可用模型列表，供前端下拉框自动填充

    带上 base_url/api_key 时用面板里刚填的值临时探测（不落盘），
    这样用户粘贴新地址和 Key 后不点保存也能先看到有哪些模型。
    """
    try:
        if base_url.strip():
            probe = LLMClient(
                base_url=_norm_base_url(base_url),
                api_key=(api_key or llm.api_key).strip(),
                model=llm.model,
                max_tokens=None,
                timeout=30.0,
            )
            try:
                names = await probe.list_models()
            finally:
                await probe.close()
        else:
            names = await llm.list_models()
    except netmode.OfflineError as exc:
        # 离线模式：不是服务商出错，别包成 502 的「拉取失败」；直接给原因
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"拉取模型列表失败：{exc}")
    return {"models": names}


@app.post("/api/retry", dependencies=[Depends(require_auth)])
async def api_retry():
    global pending_action
    # 只有本会话挂起的操作才挡重试；别的会话挂着的与本端无关
    if pending_action and _pending_matches(
            pending_action, store.current_sid, store.epoch, None, None):
        raise HTTPException(status_code=400, detail="有待确认的操作，先回复确认或取消")
    if store.history and store.history[-1]["role"] == "assistant":
        store.history.pop()
    if not store.history or store.history[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="没有可重试的用户消息")
    text = store.history.pop()["content"]
    return await start_chat(text, announce=False)


def _safe_save(sid: str, hist: list[dict]) -> None:
    """收尾落盘：磁盘满/权限出错时不能让「复位 turn.active」也跟着跳过"""
    try:
        save_session_snapshot(sid, hist)
    except Exception as e:
        print(f"[DeckMeido] 会话 {sid} 落盘失败：{e}", file=sys.stderr)


def _safe_clear_stream(sid: str) -> None:
    """清流式残留：失败只是下次启动多恢复一条「中断回答」，不该打断收尾"""
    try:
        clear_stream(sid)
    except Exception as e:
        print(f"[DeckMeido] 清理流式残留失败（{sid}）：{e}", file=sys.stderr)


def _qq_log(level: str, event: str, detail: str = "") -> None:
    """往 QQ 日志区补一条事件（onebot_client 在模块后段才创建，这里延迟取）"""
    try:
        onebot_client.log_event(level, event, detail)
    except Exception:
        pass


def _guest_system_prompt() -> str:
    """群友档的系统提示：内置/自定义群友提示词 + 公开人设（不含 extra）"""
    qq_cfg = CFG.get("qq") or {}
    return onebot.guest_prompt(
        qq_cfg.get("guest_prompt"), _qq_nickname(),
        style_block=_persona_style_block(include_traits=True))


def _apply_reply_policy(reply: str, pol: policy.TurnPolicy) -> str:
    """按场景策略收尾回复：强制脱敏 + 字符上限

    群友档的长度/脱敏必须在代码层兜底，不能只靠提示词；主人档这两项都是关闭的。
    """
    out = str(reply or "")
    if pol.redact:
        out, changed = onebot.redact_output(out)
        if changed:
            _qq_log("info", "output_redacted", "群档回复命中脱敏规则")
    if pol.reply_max_chars > 0:
        out = onebot.clip_guest_reply(out, pol.reply_max_chars)
    return out


async def _guest_reply(hist: list[dict], sys_prompt: str, text: str,
                       pol: policy.TurnPolicy | None = None) -> str:
    """普通群友的一轮：只带 guest prompt + 策略表规定的上下文窗口，零工具"""
    p = pol or policy.GUEST_POLICY
    if onebot.looks_like_injection(text):
        _qq_log("warn", "injection_suspect", "群友消息命中提示注入关键词")
    if not llm.configured:
        return "（还没配置模型，等主人配好我再来陪你聊。）"
    msgs = [{"role": "system", "content": sys_prompt or onebot.DEFAULT_GUEST_PROMPT}]
    items = hist or []
    if p.history_max_messages > 0:
        items = items[-p.history_max_messages:]
    for item in items:
        role = item.get("role")
        content = str(item.get("content") or "")
        if role in ("user", "assistant") and content:
            if p.history_item_max_chars > 0 and len(content) > p.history_item_max_chars:
                content = content[:p.history_item_max_chars] + "…"
            msgs.append({"role": role, "content": content})
    try:
        out = await llm.chat(msgs)
    except Exception as e:
        print(f"[DeckMeido][onebot] 群友轮次失败：{e!r}", file=sys.stderr)
        return "这一轮没接上，稍后再试。"
    out, changed = onebot.redact_output(str(out or ""))
    if changed:
        _qq_log("info", "output_redacted", "群友回复命中脱敏规则")
    # 回复长度是群档策略的一部分，必须在代码层兜底，不能只靠提示词
    out = onebot.clip_guest_reply(out)
    return out or "嗯，我在。"


async def handle_chat(text: str, announce: bool = True,
                      image: str | None = None,
                      image_prompt: str = "（这是当前游戏画面，结合画面回答用户的问题）",
                      workspace: Path | None = None,
                      client: str | None = None,
                      device: str | None = None,
                      brief: str = "",
                      turn_id: str = "",
                      surface: str | None = None,
                      sid: str = "",
                      hist: list[dict] | None = None,
                      force_confirm: bool = False,
                      guest: bool = False,
                      force_tts: bool = False,
                      guest_prompt_text: str = "",
                      turn_policy: policy.TurnPolicy | None = None) -> dict:
    """brief 是给界面看的短文案（可选）：截屏询问那套提示词有一百多字，
   插件面板长显会把整个面板撑开。它只随消息存下来供各端显示，
   真正发给模型、算进上下文的仍然是 text。

    turn_policy 是本轮的场景策略表：不传按主人档（能力与以前完全一致），
    群友档传 policy.GUEST_POLICY；工具集 / 记忆 / 回复上限 / 脱敏都从这张表走。
    """
    global pending_action, _ctx_used
    pol = turn_policy or policy.OWNER_POLICY
    # 本轮归属快照：整轮全程只认这份 sid/epoch，中途别的端新建/切换会话也不影响它
    # sid 可由外部通道（QQ）显式指定：QQ 用自己的独立会话，不往本地会话里写
    turn_sid = sid or store.current_sid
    turn_epoch = store.epoch
    store.turn.update({"sid": turn_sid, "epoch": turn_epoch, "active": True,
                       "turn_id": turn_id,
                       # 本轮开始时刻：/api/status 的 running_since 用它，
                       # 界面显示耗时，也用来确认后端没死
                       "started": time.time(),
                       # 谁提的这一轮：危险操作挂起时要跟着记下，确认时只认它
                       "client": client or "", "device": device or ""})
    # 每轮开头强制复位一次遮罩状态：上一轮若在取消/异常路径里漏了
    # set_overlay(False)，这里不清就会一直盖着；passthrough 漏了就反过来，
    # 遮罩会一直藏着不显示（用户就没了终止入口），所以两个都复位
    # 上一轮若有没发出的正文批次，这里一并清掉，免得它出现在新一轮里
    flush_delta_now(turn_sid)
    if _overlay_state.get("show"):
        set_overlay(False)
    _overlay_state["passthrough"] = False
    # 本轮使用的历史对象。会话操作已改成「换对象」而不是原地清空，
    # 所以这里拿到的引用在被切走之后依然只属于这一轮。
    # 外部通道（QQ）带来的是它自己那份历史，不拿 store.history 去覆盖
    hist = store.history if hist is None else hist
    sid_tag = {"sid": turn_sid}
    # 发起端自带的 turn_id 原样回显到这一轮的每条广播上：sid 是整个会话共用的，
    # 别的端提问时 sid 一样，发起端没法靠它认出「哪些 δ 是我这一轮的」
    if turn_id:
        sid_tag["turn_id"] = turn_id
    # 图片解析失败不能静默丢掉：用户以为「发了张图」，模型却只收到了文字
    image_blob = _pack_image(image)[1] if image else None
    if image and not image_blob:
        return {"reply": "附带的画面没能解析成图片（只支持 PNG / JPEG / WebP），这一轮没有发送。",
                "events": []}
    # 同一次调用取一个时间戳，同时用于「广播给界面的 ts」和「历史里的 ts」。
    # 前端删除消息时靠这个 ts 找回后端对应的那条：两处各自 time.time() 会差几毫秒，
    # 按 ts 精确匹配就永远对不上，删除按钮会退化成「删最后一条同角色消息」
    user_ts = time.time()
    # 来源标记：网页/手机/桌面客户端/QAM 插件各标各的，同一条对话里混着来
    # 也能看清谁说的。只挂在 user 消息上，且为空时不写字段，老数据零影响。
    # device 是每台浏览器各自的身份：client 只说「哪种浏览器」，两台电脑都是 web，
    # 分不清「这条是不是我这台发的」，所以「本端」角标要靠 device 比对
    src_tag = {"client": client} if client else {}
    if device:
        src_tag["device"] = device
    # 只给界面看的短文案：历史里留着，各端渲染时优先用它
    if brief:
        src_tag["brief"] = brief
    # 随本轮附带的画面（Decky 插件截的游戏画面）先落成文件：各端回放历史时会跳过
    # 「content 是数组」的消息（那是给模型看图的素材），只靠内联 base64 的话
    # 屏幕上永远没有图，表现就是「答得头头是道，却不知道它看的是什么」
    shot_event = _save_incoming_shot(image_blob) if image_blob else None
    if announce:
        # 画面要先落盘再广播：各端收到 user_message 时如果没带图，
        # 这一轮的画面就一直空着，只有刷新历史才补得回来
        await manager.broadcast({"type": "user_message", "text": text, "ts": user_ts,
                                 **sid_tag, **src_tag,
                                 **({"events": [shot_event]} if shot_event else {})})
    # @{文件} 展开为内容；图片标记在 run_agent 里转成多模态 content
    # 画面在历史里只留一条 [SHOT:文件名] 标记（见 _shot_ref_content）。
    # 标记挂在 llm 字段上；content 仍然只有文字 —— 各端渲染的是 content，
    # 于是插件截的图和网页截的图在界面上走同一条路
    shot_ref = f"\n[SHOT:{shot_event['file']}]" if shot_event and shot_event.get("file") else ""
    hist.append({"role": "user", "content": text, "llm": _expand_context(text, workspace) + shot_ref,
                 "ts": user_ts, **src_tag,
                 **({"events": [shot_event]} if shot_event else {})})
    # 本轮授权上下文：user_text 是用户这次真正说的话（不是历史拼接结果）。
    # 一路传到工具执行层，policy 只认它 + 挂起意图 + 工具参数
    turn_ctx = policy.TurnContext(
        sid=turn_sid,
        turn_id=turn_id or store.turn.get("turn_id") or "",
        user_text=text,
        client=client or "",
        device=device or "",
        boot_id=_system_boot_id(),
        readonly=pol.readonly,
        force_confirm=force_confirm or pol.force_confirm(),
        # 完全访问：删除类硬约束让路（会话历史除外，见 policy.check_delete）
        full_access=(PERMISSION_MODE == "full"),
    )
    # 带图轮的单轮策略按入口区分：大屏聊天页放开多轮；询问页/截图翻译
    # （包括 QAM 的全部截图轮）继续一问一答，行为保持原样
    shot_lock = policy.shot_lock_for(client, surface)
    # 本轮新截的这张放在 llm 标记里（_build_llm_msgs 现读现编码），
    # 不再另补一条内联 base64 的消息：那条一旦被上下文压缩裁掉，界面上的图就没了
    events: list[dict] = []
    # run_agent 在终止时会往这里塞本轮执行过的工具，供落盘时做交接（见 _aborted_note）
    tool_log: list[dict] = []
    # 标题取自第一条用户消息，所以用户一发出就落盘并刷新侧栏，不必等整轮回答结束
    _safe_save(turn_sid, hist)
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})

    # 上下文逼近模型窗口上限时自动压缩（摘要替换旧对话，不再直接丢弃）。
    # _ctx_used 在切换会话等路径上会归零，这时按当前历史重新估算一次
    # （_cached_estimate 会按条数缓存，刚追加过用户消息，这里必然重算）；
    # 压缩失败也不能让这一轮起不来
    try:
        if _need_compact(_ctx_used or _cached_estimate()):
            await compact_history(auto=True, hist=hist)
    except Exception as e:
        print(f"[DeckMeido] 自动压缩失败，按原上下文继续：{e}", file=sys.stderr)

    try:
        # 遮罩不再整轮常显：只有真的会动屏幕的工具（desktop 注入、xdotool/ydotool
        # 一类命令、拉起可见窗口）才在屏幕最上层盖一层，给用户一个「随时点一下终止」
        # 的入口 —— 那时候用户的手没法直接按终止。翻文件、跑后台命令、查天气这些
        # 不需要看屏幕的活儿不再挡视线，省得连「清空回收站」都要盖住全屏。
        # 遮罩的起落就在下面调用工具的前后（见 tools.touches_screen），这里不再常开。
        if guest:
            # 群友档：走统一引擎，但策略表是 GUEST_POLICY（零工具、无长期记忆、
            # 400 字上限、强制脱敏、只给公开人设）。_guest_reply 保留作灰度回退：
            # 引擎这一轮起不来（模型没配好 / 调用异常）就退回旧路径，群里的体验不中断
            if onebot.looks_like_injection(text):
                _qq_log("warn", "injection_suspect", "群友消息命中提示注入关键词")
            if not llm.configured:
                reply = await _guest_reply(
                    hist, guest_prompt_text or _guest_system_prompt(), text, pol=pol)
            else:
                try:
                    reply = await _run_agent_guarded(
                        events, hist=hist, image_part=shot_ref, tool_log=tool_log,
                        client=client, turn_ctx=turn_ctx, shot_lock=shot_lock,
                        surface=surface, turn_policy=pol)
                except Exception as e:
                    print(f"[DeckMeido][onebot] 群友档引擎回退：{e!r}", file=sys.stderr)
                    events.clear()
                    reply = await _guest_reply(
                        hist, guest_prompt_text or _guest_system_prompt(), text, pol=pol)
        elif not llm.configured:
            reply = "还没有配置模型。点右上角「设置」填好 API 地址和 Key 就能开始对话。"
        elif pending_action and not _pending_matches_turn(
                pending_action, turn_sid, turn_epoch, turn_id, client, device):
            # 挂起的操作不是这个会话/这个端提的：既不能替别人确认，也不该
            # 因为别处的对话把这边的手头操作清掉，按普通消息继续
            reply = await _run_agent_guarded(events, hist=hist, image_part=shot_ref,
                                             tool_log=tool_log, client=client,
                                             turn_ctx=turn_ctx,
                                             shot_lock=shot_lock, surface=surface,
                                             turn_policy=pol)
        elif pending_action and (tools.is_confirm(text) or policy.is_hard_confirm(text)):
            # 用户确认了挂起的危险操作：这是显式的「窗口里点了确认」，
            # 不是历史文本推断出来的授权，policy 用 approved=True 放行并记账
            action, pending_action = pending_action, None
            decision = policy.check(action["name"], action["args"], turn_ctx,
                                    approved=True)
            events.append({"type": "tool", "name": action["name"],
                           "summary": action["description"], "files": [], "approved": True,
                           **({} if decision.allowed else {"denied": True,
                                                           "reason": decision.reason})})
            if decision.allowed:
                power_act = (policy.power_action(str(action["args"].get("command") or ""))
                             if action["name"] == "run_command" else None)
                if power_act:
                    _arm_power_intent(turn_ctx, power_act, hist)
                result = await _run_tool(action["name"], action["args"])
                _record_power_intent(turn_ctx, action["name"], action["args"], result)
            else:
                print(f"[DeckMeido][policy] 策略拒绝已确认操作 {action['name']}："
                      f"{decision.reason}", file=sys.stderr)
                result = f"策略拒绝：{decision.reason}"
            tool_log.append({"name": action["name"], "summary": action["description"]})
            # 补上执行结果，QQ 的「装完自动打开」要靠 exit=0 判断成功
            if events:
                events[-1]["output"] = str(result)[:1500]
            reply = (f"已完成：{action['description']}\n{result}" if decision.allowed
                     else f"未执行：{action['description']}\n{result}")
        elif pending_action:
            # 有新消息但未确认，取消挂起操作
            action, pending_action = pending_action, None
            reply = f"已取消：{action['description']}。\n\n" + await run_agent(
                events, hist=hist, image_part=shot_ref, tool_log=tool_log,
                from_client=client, turn_ctx=turn_ctx,
                _shot_lock=shot_lock, surface=surface, turn_policy=pol)
        else:
            # 画面要一路传进 run_agent：插件端才知道这一轮不用自己再截图
            reply = await _run_agent_guarded(events, hist=hist, image_part=shot_ref,
                                             tool_log=tool_log, client=client,
                                             turn_ctx=turn_ctx,
                                             shot_lock=shot_lock, surface=surface,
                                             turn_policy=pol)
    except asyncio.CancelledError:
        # 用户点了终止：广播终止消息并向上传递
        await manager.broadcast({"type": "assistant_message", "text": "已终止当前操作",
                                 "ts": time.time(), "events": events, "cancelled": True,
                                 **sid_tag, **src_tag})
        # 终止时把已经生成的部分保住：屏幕上出现过的每一段正文都留下，
        # 不能只用最后那段（工具调用前的过渡话也在屏幕上显示过）
        partial = _stream_buf(turn_sid).get("text") or ""
        parts = _stream_buf(turn_sid).get("parts") or []
        killed_segs = [p.get("content") or "" for p in parts if p.get("type") == "text"]
        killed_text = "\n\n".join(s.strip() for s in killed_segs if s.strip()).strip()
        if partial.strip() and partial.strip() not in killed_text:
            killed_text = (killed_text + "\n\n" + partial.strip()).strip() if killed_text else partial.strip()
        # 一句话都还没说出口就被终止时（工具刚开跑就点了终止），
        # 以前会落一条 content 为空串的 assistant 消息 —— 会话里看着像「没有回答」，
        # 用户也看不到到底跑过什么。这里把已执行的步骤和结果补成正文
        digest = _tool_results_digest(tool_log)
        if not killed_text.strip():
            killed_text = ("（已终止，这一轮还没生成正文）"
                           + (f"\n\n终止前已经执行的步骤：\n{digest}" if digest
                              else "\n\n（还没有执行任何工具）"))
        # 无论有没有留下正文，都要落一条带 aborted_turn 的 assistant 消息：
        # 它带着这一轮做过哪些工具，下一轮靠它告诉模型「已经终止，别接着做」
        # （见 _aborted_note）。以前只在有正文时才落，于是刚开跑就被终止的
        # 那一轮在历史里不留痕迹，模型下一句就照旧把半截任务续上
        hist.append({"role": "assistant", "content": killed_text,
                     "events": events, "interrupted": True, "aborted_turn": True,
                     "tool_log": list(tool_log), "ts": time.time(),
                     **src_tag})
        _safe_save(turn_sid, hist)
        _safe_clear_stream(turn_sid)
        raise
    except Exception as e:
        # 非终止类异常（网络、磁盘、工具内部炸了）：以前这里直接往上抛，
        # 把回复落盘、清 _streaming、复位 active 三步全跳过 —— 于是这一轮
        # 只留下用户那句话，屏幕上出现过的工具链全丢，而且 turn["active"]
        # 一直是 True，后面所有前端的「本轮归属」判定都跟着错。
        # 统一在这里收成一条带错误说明的回复，走与成功路径相同的收尾
        print(f"[DeckMeido] 本轮异常：{e}", file=sys.stderr)
        reply = f"（出错了：{e}）"
        digest = _tool_results_digest(tool_log)
        if digest:
            reply += f"\n\n出错前已经执行的步骤：\n{digest}"
        elif events:
            reply += "\n\n（上面这些步骤已经执行，结果未完整汇总）"
    finally:
        set_overlay(False)

    # 场景策略的收尾：群档在这里做强制脱敏和 400 字硬截断（代码层兜底），
    # 主人档两项都不生效，回复保持原样
    reply = _apply_reply_policy(reply, pol)

    # 助手消息同样只取一次时间戳：落盘用的和广播给界面的是同一个值，
    # 前端按 data-ts 删除才能精确命中这一条
    asst_ts = time.time()
    # 回答也带上这条提问的来源：QAM 面板把各端的问答混在一列显示，
    # 只看 user 的标记分不清哪条回答是应哪一句的
    hist.append({"role": "assistant", "content": reply, "events": events,
                 "ts": asst_ts, **src_tag})
    # 关键：写回「本轮所属」的会话文件。中途被切走时 current_sid 已经指向别处，
    # 用 save_current_session 会把这一轮错写进新会话（串回复的根源）
    _safe_save(turn_sid, hist)
    # 正文已正式进 history，清掉流式残留（否则下次启动会多恢复出一条重复的「中断回答」）
    _safe_clear_stream(turn_sid)
    detached = _turn_detached(turn_epoch)
    store.turn["active"] = False
    turn_deleted = turn_sid in store.deleted_sids
    # 第一轮跑完、会话还在时，后台把首条消息总结成短标题替换默认标题（不阻塞收尾）
    if not detached and not turn_deleted:
        user_msgs = [m for m in hist if m.get("role") == "user"]
        if len(user_msgs) == 1:
            first = str(user_msgs[0].get("brief") or user_msgs[0].get("content") or "")
            asyncio.create_task(_summarize_title(turn_sid, first))
    if turn_deleted:
        # 这一轮跑到一半会话被删了：结果按用户意愿丢弃，但要明确告知，
        # 否则用户看到的是「回答消失了」
        await manager.broadcast({"type": "system", "text": "该会话已被删除，本轮回答未保存",
                                 "ts": time.time()})
    # 收尾前把还攒着的正文批次推出去：final=True 那条必须排在它后面，
    # 否则残留批次会晚于终态到达，界面刚定稿又被追加一截
    flush_delta_now(turn_sid)
    if not detached and not turn_deleted and reply.strip():
        # 给前端一次「最终正文」：带 final=True，前端据此把那句界面上的气泡存进
        # localStorage 崩溃缓冲。中途被截断的工具过渡话不带这个标记，回滚时不会残留
        await manager.broadcast({"type": "delta", "text": "", "final": True,
                                 "reply": reply, "ts": time.time(), **sid_tag})
    if detached:
        # 本轮结束时用户已经切到别的会话：结果留在原会话文件里，不往当前屏幕推
        # （否则新会话里会冒出一段不属于它的回答），只提示一句
        await manager.broadcast({"type": "system", "text": "上一轮对话已完成，结果保存在原会话中",
                                 "ts": time.time()})
    else:
        await manager.broadcast({"type": "assistant_message", "text": reply, "ts": asst_ts,
                                 "events": events, **sid_tag, **src_tag})
        # 语音播报：只播报最终回复，不播报思维链/工具过程（后台进行，不阻塞）。
        # QQ 通道的回答不在本机播报：那是别人在手机上说的话，不该从 Deck 的喇叭里出来。
        # 智能音箱模式（force_tts）必须等这一句读完再收尾，否则唤醒轮次会在
        # 播放结束前就把状态切回监听，眼睛与半双工都会错位。
        if (client or "") != "qq":
            if force_tts:
                await tts.speak(reply, force=True)
            else:
                asyncio.create_task(tts.speak(reply))
    return {"reply": reply, "events": events}


def _need_compact(used: int) -> bool:
    """用量是否已经该压缩了。

    两处触发（每轮开始前、单轮工具循环中间）共用这一个判断，避免阈值语义分叉。
    """
    win = context_window()
    return bool(win) and used / win >= COMPACT_RATIO


def _recent_limit() -> int:
    """一次请求里最多带多少条历史

    旧版 DeepSeek 接口有「单次最多 64 条 message、超出静默丢弃」的限制，
    现在这条限制已经取消，按 token 压缩就够了，不必为了凑条数去砍历史——
    砍掉中间一段反而会让后面的前缀全部失配（缓存直接掉到 0）。
    这里仍留一个宽松上限兜底，防止超长工具循环把请求撑到离谱。
    """
    return 200


def _current_turn_start(hist: list[dict]) -> int:
    """找到「当前这一轮」在 history 里的起点

    一轮 = 一条用户消息，加上它后面紧跟的 assistant(tool_calls)/tool 往返。
    压缩时这一轮必须整段留在原文里：它是模型眼下正在做的事，被摘要成一句
    "执行了命令" 之后，模型下一轮就不知道自己刚才跑过什么、卡在哪个报错上，
    典型表现是重复执行同一条命令。

    从末尾往前找，遇到「既不是 tool 结果、也不是 tool_calls 的 user」就停。
    找不到（比如历史开头就是工具链）返回 0，等于不做特殊保留。
    """
    for i in range(len(hist) - 1, -1, -1):
        m = hist[i]
        if m.get("role") == "user" and not m.get("tool_calls"):
            return i
    return 0


def _compact_end(hist: list[dict], start: int) -> int:
    """算出这次压缩要把游标推进到第几条（即 end：history[start:end] 会被摘要）

    从末尾往前累积 token，一直收到「剩下的最近内容约占窗口的
    COMPACT_TARGET_LEFT」为止。这样压缩完上下文会明显低于触发阈值，
    而不是像以前那样只压掉几条、下一轮工具调用又顶回去。
    最近 2 条（用户那句 + 上一条）永远保留，避免新一轮刚开始就没有前文。

    条数上限按「请求里最多带多少条」反推（见 _recent_limit）：超过上限的消息
    DeepSeek 会直接忽略、而且不报错，表现就是「它突然忘了刚才说的事」。
    所以宁可多压一点，也不能让留给模型的那段被服务端悄悄截掉。
    """
    total = len(hist)
    if total - start <= 2:
        return total
    budget = int(context_window() * COMPACT_TARGET_LEFT)
    acc = 0
    end = total
    # 当前这一轮整段不压：模型正靠它知道自己刚才做了什么、下一步接哪里。
    # 它占的 token 从预算里先扣掉，相当于压缩时少留一点别的原文，
    # 但不让压缩后立刻又顶回阈值（下面按预算累加已经在管这件事）。
    turn_start = _current_turn_start(hist)
    if turn_start > start:
        acc = _estimate_tokens(hist[turn_start:total])
        end = turn_start
    # 从最新一条往前累，直到快超出剩余额度为止。
    # 位置上限 = 请求条数上限减去「本轮新加的工具往返」的余量
    oldest = max(start, total - _recent_limit())
    # 本轮已经整段保留时就不再从前面续收，否则会把它占的额度又花一遍
    for i in (range(end - 1, oldest, -1) if end == total else ()):
        acc += _estimate_tokens([hist[i]])
        if acc > budget:
            break
        end = i
    end = max(end, start + 1)
    # 游标不能落在一次工具调用中间：assistant(tool_calls) 被压进摘要、它的
    # tool 结果还留在原文（或反过来），发出去的 messages 就是半截配对，接口 400。
    # 一路往后挪到「不是 tool 结果、也没有 tool_calls」那条消息上，整对一起压掉
    while start < end < total and (hist[end].get("role") == "tool"
                                   or hist[end].get("tool_calls")):
        end += 1
    return min(end, total)


async def compact_history(auto: bool = False, hist: list[dict] | None = None,
                          sid: str = "") -> str:
    """把较早的对话压缩成摘要，**只影响发给模型的上下文，不动界面历史**

    旧实现 hist[:] = [摘要] + hist[-4:] + 立刻落盘 —— 界面和磁盘上的几十轮
    对话被一起抹掉，用户看到的就是「内容凭空消失」。现在只推进压缩游标：
    摘要与 upto 存进文件，history 保持完整，重启后界面能一条不少地回放。
    """
    global _ctx_used
    hist = store.history if hist is None else hist
    sid = sid or store.turn.get("sid") or store.current_sid
    comp = _compact_for(sid)
    # 已经压缩过的部分不再重复参与摘要。
    #
    # keep 以前写死 4 条，后果是「刚压缩完马上又要压缩」：一次工具往返就占
    # assistant(tool_calls) + tool 两条，压缩后只剩 4 条的余量，下一轮工具链
    # 立刻把上下文顶回阈值，于是用户每几条消息就看到一次「已自动压缩上下文」。
    # 现在按 token 算：从末尾往前留出约 COMPACT_KEEP_RATIO 的窗口，
    # 压缩要真的腾出空间，压缩频率自然就降下来了。
    start = min(comp["upto"], len(hist))
    end = _compact_end(hist, start)
    old = hist[start:end]
    if len(old) < 2:
        return "对话还很短，无需压缩"
    if not llm.configured:
        return "未配置 LLM，跳过上下文压缩（历史原样保留）"
    # 老摘要一起喂进去，让新摘要接着往下写，否则前情会丢
    parts = []
    if comp["summary"].strip():
        parts.append("【前情摘要】\n" + comp["summary"])
    parts.append("【新增对话】\n" + "\n".join(
        # 每条按固定长度截断：摘要请求不带 tools、system 也固定，
        # 只要游标是从同一位置往后压，前缀就能和上一轮对齐（DeepSeek 硬盘缓存）
        # 工具结果留首尾（命令 + 最终报错都在两头），普通消息截前 500 字
        f"{'用户' if m['role'] == 'user' else '助手'}: "
        + (_digest_head_tail(_digest_text(m)) if m["role"] == "tool"
           else _digest_text(m)[:500]) for m in old))
    digest = "\n\n".join(parts)
    sys_msg = {"role": "system", "content": prompts.part("compact_summary")}
    try:
        summary = await llm.chat([sys_msg, {"role": "user", "content": digest}])
        # 弱模型有时只回一句「用户询问了某问题」，那么长的历史一压就没了。
        # 摘要短到不成比例时补一次：明确指出漏了哪些栏位，仍不合格就用原文兜底。
        if len(summary.strip()) < max(120, len(digest) // 20):
            retry_sys = {"role": "system", "content": prompts.part("compact_summary_retry")}
            again = await llm.chat([retry_sys, {"role": "user", "content": digest}])
            if len(again.strip()) > len(summary.strip()):
                summary = again
    except Exception as e:
        return f"压缩失败，历史原样保留：{e}"
    if sid in store.deleted_sids:
        # 会话在压缩过程中被删了：游标与落盘一律作废，否则删掉的会话会被写回来
        return summary
    _set_compact(sid, summary, end)
    _ctx_used = _estimate_tokens(_build_llm_msgs(hist, SYSTEM_PROMPT, sid))
    # 游标要和会话文件一起持久化，否则重启后模型会重新吃到全量历史
    if sid == store.current_sid:
        save_current_session()
    else:
        save_session_snapshot(sid, hist)
    note = (f"{'已自动压缩上下文' if auto else '已压缩上下文'}"
            f"（{len(old)} 条旧消息 → 摘要，聊天记录完整保留）")
    await manager.broadcast({"type": "system", "text": note, "ts": time.time()})
    return summary


@app.post("/api/compact", dependencies=[Depends(require_auth)])
async def api_compact(preview: bool = False):
    """压缩上下文。preview=true 时只报「会压掉多少」，不真正执行

    用户点压缩前需要知道代价：条数、能省多少 token。不然按下去只看到一句
    「已压缩」，既不知道自己毁了哪段，也不知道值不值得。
    """
    if preview:
        hist = store.history
        sid = store.turn.get("sid") or store.current_sid
        start = min(_compact_for(sid)["upto"], len(hist))
        end = _compact_end(hist, start)
        old = hist[start:end]
        return {"ok": True, "preview": True, "count": len(old), "total": len(hist),
                "tokens": _estimate_tokens(old),
                "left_tokens": _estimate_tokens(hist[end:])}
    summary = await compact_history()
    return {"ok": True, "summary": summary[:300]}


async def _summarize_title(sid: str, first_user_text: str) -> None:
    """会话第一轮结束后，用 LLM 把首条用户消息总结成短标题替换默认标题。

    默认标题只是把第一条消息原样截断（可能又长又口语），一眼看不出这会话在聊啥。
    这里后台异步做一次总结，成功就写回会话文件并广播刷新侧栏；失败静默保留默认标题。
    """
    if not llm.configured or sid in store.deleted_sids:
        return
    # QQ 会话的标题由会话标识（群号 / QQ 号后 5 位）固定，不交给模型总结，
    # 否则第一轮结束后侧栏里的 QQxxxxx 会被一句话标题顶掉
    if session_store.qq_session_title(sid):
        return
    if not first_user_text.strip():
        return
    try:
        sys_msg = {"role": "system", "content": prompts.part("session_title")}
        title = (await llm.chat([sys_msg, {"role": "user",
                                           "content": first_user_text[:200]}])) or ""
        title = title.strip().strip('"“”\n').replace("\n", " ")[:16]
    except Exception:
        return
    if not title or sid in store.deleted_sids:
        return

    # 读-改-写整份会话只为改个标题太亏，整段丢给 IO 线程：
    # 索引先在内存里更新（侧栏马上能看到新标题），文件那头排着队改。
    def _job() -> None:
        with store.lock:
            if sid in store.deleted_sids:
                return
            data = store.read_session(sid)
            if not data:
                return
            data.pop("_from_disk_mtime", None)
            data["title"] = title
            # 顺带刷新 updated：标题是这一轮结束才总结出来的，
            # 用旧时间排序会让刚聊完的会话沉到侧栏底部
            data["updated"] = time.time()
            store.rewrite_file(sid, data)
            hist = data.get("history") or []
            store.index_put_mem(sid, title, data["updated"], len(hist))

    session_store.io_submit(_job)
    _now = time.time()
    store.index_put_mem(sid, title, _now,
                        len(store.history) if sid == store.current_sid
                        else (store.index().get(sid) or {}).get("messages") or 0)
    if sid == store.current_sid:
        await manager.broadcast({"type": "session_changed", "current": sid})
    else:
        await manager.broadcast({"type": "session_changed", "current": store.current_sid})


def _sd_card_info() -> dict:
    """内存卡占用：找 /dev/mmcblk* 分区（SteamOS 自动挂在 /run/media/<用户>/<卷名>）

    没插卡就返回空 dict，前端据此整行隐藏。
    """
    import psutil
    try:
        parts = psutil.disk_partitions(all=True)
    except Exception:
        return {}
    for part in parts:
        if not part.device.startswith("/dev/mmcblk"):
            continue
        try:
            du = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        mount = part.mountpoint.rstrip("/")
        return {
            "sd_label": os.path.basename(mount) or mount,
            "sd_mount": mount,
            "sd_pct": du.percent, "sd_used": du.used, "sd_total": du.total,
        }
    return {}


@app.get("/api/sysinfo", dependencies=[Depends(require_auth)])
async def api_sysinfo():
    """侧栏状态卡：CPU / 内存 / SSD / 内存卡占用 / 电量 / 运行时长 / 局域网 IP

    采集全程要读 /proc、遍历挂载点、还要 fork 一次 `ip`，放到线程里做：
    这个接口是 5 秒一刷的，同步跑会把事件循环卡住。
    """
    return await asyncio.to_thread(_read_sysinfo)


def _battery_minutes(status: str, capacity: str) -> int | None:
    """估算「还有多久充满 / 还能用多久」，单位分钟，估不出来返回 None

    优先用内核导出的 time_to_full_now / time_to_empty_now（单位分钟）；
    SteamOS 的 BAT1 没这两个文件，就退回到 charge_now / current_now 自己算。
    充满（Full）或数值不合理时返回 None，让前端显示「已充满」或干脆不显示。
    """
    try:
        if status == "Full":
            return 0
        field = "time_to_full_now" if status == "Charging" else "time_to_empty_now"
        raw = read_first(f"/sys/class/power_supply/BAT1/{field}")
        if raw.lstrip("-").isdigit() and int(raw) > 0:
            return int(raw)

        cur = int(read_first("/sys/class/power_supply/BAT1/current_now") or 0)
        if cur <= 0:
            return None
        if status == "Charging":
            now = int(read_first("/sys/class/power_supply/BAT1/charge_now") or 0)
            full = int(read_first("/sys/class/power_supply/BAT1/charge_full") or 0)
            remain = full - now
        else:
            # 放电：用当前电量除以电流。capacity 是百分比，用 charge_full
            # 折算成实际容量，比直接拿 charge_now 更抗驱动单位差异
            now = int(read_first("/sys/class/power_supply/BAT1/charge_now") or 0)
            remain = now
        if remain <= 0:
            return None
        minutes = int(remain / cur * 60)
        # 超过 20 小时基本是读数异常（刚插上电流极小），不如不显示
        return minutes if 0 < minutes <= 20 * 60 else None
    except Exception:
        return None


def _read_sysinfo() -> dict:
    try:
        import psutil
        vm = psutil.virtual_memory()
        data = {
            "cpu": psutil.cpu_percent(),
            "mem_pct": vm.percent, "mem_used": vm.used, "mem_total": vm.total,
            "ip": _lan_ip(),
            # 设备名：优先用户自己设的 hostname（SteamOS 默认 deck@steamdeck）
            "hostname": socket.gethostname(),
        }
        # 内置盘：SteamOS 根分区只读且很小，用户数据都在 /home，看它才有意义
        try:
            du = psutil.disk_usage("/home")
            data.update({"ssd_pct": du.percent, "ssd_used": du.used, "ssd_total": du.total})
        except OSError:
            pass
        battery = read_first("/sys/class/power_supply/BAT1/capacity")
        charging = read_first("/sys/class/power_supply/BAT1/status")
        data["battery"] = int(battery) if battery.isdigit() else None
        data["charging"] = charging or None
        # 剩余时间：SteamOS 的 BAT1 驱动不导出 time_to_full_now / time_to_empty_now，
        # 只给 charge_now / charge_full / current_now（单位 µAh、µA），
        # 所以自己按「待补/待放容量 ÷ 电流」估算，结果换回秒。
        # current_now 在充满或拔插瞬间可能为 0 或异常小，除零和天文数字都要挡掉。
        data["battery_minutes"] = _battery_minutes(charging, battery)
        data.update(_sd_card_info())
        return data
    except Exception as e:
        return {"error": str(e)}


class OpenFileReq(BaseModel):
    path: str


@app.post("/api/open_file", dependencies=[Depends(require_auth)])
async def api_open_file(req: OpenFileReq):
    """原生客户端里点开图片/文件 → 用系统默认工具打开（仅限 static 目录内）"""
    rel = req.path.split("?")[0].lstrip("/")
    root = STATIC_DIR.resolve()
    p = (root / rel).resolve()
    # 按路径层级判断，不能拼字符串：/static-other/x 的 startswith("/static") 也成立
    if not (p == root or root in p.parents) or not p.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    subprocess.Popen(["xdg-open", str(p)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ---------- 会话管理 ----------

@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def api_sessions():
    """侧栏列表：只读索引，不再把每个会话文件整份解析一遍

    以前会话一多，切会话 / 删会话（各端都会重拉这个接口）要解析几 MB JSON，
    而且全在事件循环里。索引只存四行字段，落盘时顺带更新。
    """
    # 次级键用 id：同一秒落盘的多个会话顺序固定，强刷不会来回抖
    return {"current": store.current_sid,
            "items": session_store.session_order(store.index().values())}


def _current_session_untouched() -> bool:
    """当前会话是不是「一句话都没说过」的空会话

    判据是历史里一条 user / assistant 消息都没有（系统提示、tool 消息不算）。
    只看 store.history 而不是磁盘：内存里的历史就是唯一真相，磁盘文件
    可能因为空写保护还没落盘。
    """
    return not any(m.get("role") in ("user", "assistant") for m in store.history)


@app.post("/api/sessions/create", dependencies=[Depends(require_auth)])
async def api_session_create():
    global _ctx_used
    # 只是要离开当前会话：内容照存，但不刷新 updated，避免旧会话被顶到首位
    save_current_session(touch=False)
    # 连点「新会话」会把侧栏刷成一排空会话，一个能用的都没有。
    # 已经在空会话里就原地不动，各端也不用重刷列表
    if _current_session_untouched():
        return {"ok": True, "id": store.current_sid, "unchanged": True}
    store.current_sid = _fresh_sid()
    # 换对象而不是原地 clear：还在跑的那一轮持有旧列表引用，
    # 原地清空会把它的历史一起抹掉，结果就变成「有问无答」
    store.history = []
    # 上下文占用计数必须一起清零，否则新会话会沿用上一个会话的用量
    _ctx_used = 0
    # 代次 +1：正在跑的那一轮会据此发现自己的会话已被换掉
    store.bump_epoch()
    store.turn["active"] = False
    save_current_session()
    await manager.broadcast({"type": "history", "messages": store.history,
                             "sid": store.current_sid})
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    await manager.broadcast({"type": "system", "text": "已开始新会话", "ts": time.time()})
    return {"ok": True, "id": store.current_sid}


class SwitchReq(BaseModel):
    id: str


@app.post("/api/sessions/switch", dependencies=[Depends(require_auth)])
async def api_session_switch(req: SwitchReq):
    global _ctx_used
    p = _sess_path(req.id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="session not found")
    # 查看动作不写时间戳：仅切走前的收尾写入，排序仍按最近真实活动
    save_current_session(touch=False)
    try:
        # 整份会话可能有几 MB，读盘同样不能压在事件循环里
        body = await asyncio.to_thread(p.read_text, encoding="utf-8")
        data = json.loads(body)
    except Exception as e:
        # 文件坏了就明确拒绝：以前这里直接抛 500，前端只能笼统提示「切换失败」，
        # 而用户看到的是会话列表里明明有这一条
        raise HTTPException(status_code=500,
                            detail=f"会话文件读取失败：{e}") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="会话文件格式异常")
    store.current_sid = data.get("id", req.id)
    # 同样换对象：在跑的那一轮仍持有旧列表，不受切换影响
    store.history = data.get("history", [])
    comp = data.get("compact")
    if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
        _set_compact(store.current_sid, comp.get("summary") or "",
                     min(int(comp.get("upto") or 0), len(store.history)))
    else:
        store.drop_compact(store.current_sid)
    # 切换会话也要换 pending_intent：否则 A 会话的密码授权会被带进 B 会话
    if _restore_intent_map(store.current_sid, data):
        try:
            _write_session(store.current_sid, store.history)
        except Exception as e:
            print(f"[DeckMeido] 切换会话时写回 pending_intent 失败：{e}", file=sys.stderr)
    _ctx_used = _estimate_tokens(
        _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
    store.bump_epoch()
    store.turn["active"] = False
    await manager.broadcast({"type": "history", "messages": store.history,
                             "sid": store.current_sid})
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    return {"ok": True}


@app.get("/api/sessions/{sid}/history", dependencies=[Depends(require_auth)])
async def api_session_history(sid: str):
    """只读历史：给手机端预览会话，不改变服务端 current_sid。

    网页控制台的「切换会话」是全局动作（会同时广播给掌机和其他客户端），
    手机翻历史不该把别人正在看的会话切走。这里只读文件，不碰内存里的
    current_sid / history / compact，也不触发 history 广播。
    """
    if not _valid_session_id(sid):
        raise HTTPException(status_code=404, detail="session not found")
    p = _sess_path(sid)
    if not p.exists():
        raise HTTPException(status_code=404, detail="session not found")
    try:
        body = await asyncio.to_thread(p.read_text, encoding="utf-8")
        data = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"会话文件读取失败：{e}") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="会话文件格式异常")
    hist = data.get("history")
    if not isinstance(hist, list):
        hist = []
    idx = store.index().get(sid) or {}
    return {
        "ok": True,
        "id": data.get("id") or sid,
        "title": data.get("title") or idx.get("title") or "",
        "created": data.get("created"),
        "updated": data.get("updated") or idx.get("updated") or 0,
        "messages": hist,
    }


class MsgDeleteReq(BaseModel):
    ts: float | None = None          # 按消息时间戳定位（前端每条消息都带 data-ts）
    role: str = "assistant"          # 兜底：没有 ts 时按角色删最后一条
    with_reply: bool = False         # 删用户消息时，连它下面那条回答一起删


@app.post("/api/messages/delete", dependencies=[Depends(require_auth)])
async def api_message_delete(req: MsgDeleteReq):
    """删除会话里的单条消息。

    只从「当前会话的历史」里摘掉那一条，并立刻落盘；不发 history 全量重推，
    否则整屏会重绘、滚动位置跳到顶上。前端自己把 DOM 上的那条摘掉即可。

    为什么按 ts 定位而不是按序号：界面上的顺序和 history 不完全一一对应
    （一轮助手回答在界面上可能拆成 思考/正文/工具 好几块，但在 history 里
    只有一条），用序号会删错行。ts 是每条消息落盘时就写进去的，唯一且稳定。
    """
    global _ctx_used
    if store.turn.get("active"):
        raise HTTPException(status_code=409, detail="本轮回答还在生成，请等它结束再删除")

    hidx = -1
    if req.ts:
        # 时间戳浮点比较留一点容差：JSON 往返可能丢精度
        best = None
        for i, m in enumerate(store.history):
            t = m.get("ts")
            if not isinstance(t, (int, float)):
                continue
            if abs(float(t) - float(req.ts)) <= 0.001:
                best = i
                break
        hidx = best if best is not None else -1
    if hidx < 0:
        # 没有匹配到 ts：退化成「删同角色最后一条」，避免前端按钮完全失效
        want = req.role if req.role in ("user", "assistant") else "assistant"
        for i in range(len(store.history) - 1, -1, -1):
            if store.history[i].get("role") == want:
                hidx = i
                break
    if hidx < 0:
        raise HTTPException(status_code=404, detail="找不到这条消息")

    target = store.history[hidx]
    removed = 1
    # 删用户消息时，把紧跟着的那条助手回答一起带走：
    # 只删提问留着回答，模型下次读历史会看到「没有问题的答案」，很容易胡接
    if req.with_reply and target.get("role") == "user":
        if hidx + 1 < len(store.history) and store.history[hidx + 1].get("role") == "assistant":
            del store.history[hidx + 1]
            removed += 1
    del store.history[hidx]

    # 压缩游标要跟着收：否则游标指向的条数比历史还长，
    # 发给模型的上下文会整段错位（摘要跳过了还没被摘要的消息）
    sid = store.current_sid
    comp = _compact_for(sid)
    if comp["upto"] > len(store.history):
        _set_compact(sid, comp["summary"], len(store.history))

    save_current_session()
    _ctx_used = _estimate_tokens(
        _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    return {"ok": True, "removed": removed, "left": len(store.history)}


@app.delete("/api/sessions/{sid}", dependencies=[Depends(require_auth)])
async def api_session_delete(sid: str, request: Request):
    # pending_action 在本函数里既读又写（下面要清掉指向被删会话的挂起操作），
    # 没有 global 声明时 Python 会把它当局部变量，读到第 4570 行就 UnboundLocalError
    global _ctx_used, pending_action
    # 删除一个还在跑的会话前，先强制终止它在跑的那一轮，否则它那轮 run_agent
    # 协程仍持有 store.history 并继续往外写，会把上一个会话的流程串进新会话。
    if sid == store.current_sid and current_task and not current_task.done():
        current_task.cancel()
        set_overlay(False)
        await tools.abort_all_procs()
        # 给 cancel 留一拍，让 handle_chat 的 CancelledError 分支先落一条终止消息，
        # 避免删除后它又异步补写回历史
        try:
            await asyncio.wait_for(asyncio.shield(current_task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            pass
    # 先拉黑再删文件：反过来的话，删完到拉黑之间那一瞬间的落盘（流式回调）
    # 就能把文件写回来，等于白删
    store.deleted_sids.add(sid)
    # 内存里按 sid 挂着的摘要/意图/流式缓冲也一并清掉。QQ 通道的 sid 是按
    # uin/群号算死的，不清的话同一个人下一条消息又会把旧摘要当上文复读
    store.forget_session(sid)
    _stream_bufs.pop(sid, None)
    if sid.startswith("qq-"):
        try:
            onebot_client.drop_session(sid)
        except Exception:
            pass
    pend = pending_action
    if isinstance(pend, dict) and (pend.get("sid") or "") == sid:
        pending_action = None
    # 删除是低频操作，但这里要「等」而不是「发出去就走」：删完到 IO 线程真正执行
    # 之间的那一小段里若有落盘排队，文件就会被写回来，等于白删。等它又不该卡住事件循环，
    # 所以用 to_thread 把 .result() 挪到线程里
    await asyncio.to_thread(session_store.io_submit(store.index_drop, sid).result)
    p = _sess_path(sid)
    if p.exists():
        try:
            # 软删除：移进 userdata/trash/ 而不是直接 unlink。误删（含自动化脚本
            # 连点两次确认）还能从回收站找回，列表则因为索引已摘除而不再返回
            await asyncio.to_thread(
                session_store.io_submit(session_store.trash_session, sid).result)
        except OSError as e:
            store.deleted_sids.discard(sid)
            audit.record("session", "delete", "fail",
                         ip=_client_ip(request), detail=f"{sid}: {e}")
            raise HTTPException(status_code=500, detail=f"删除失败：{e}")
        audit.record("session", "delete", "ok",
                     ip=_client_ip(request), detail=f"{sid} -> trash/")
    else:
        audit.record("session", "delete", "ok",
                     ip=_client_ip(request), detail=f"{sid} (文件已不存在)")
    # 流式副档案跟着一起删：留着的话下次启动会把它当「被中断的回答」恢复出来，
    # 删掉的会话就凭空冒出一条消息
    await asyncio.to_thread(session_store.io_submit(store.drop_stream_side, sid).result)
    if sid == store.current_sid:
        # 删掉的是当前会话。以前这里无条件 _fresh_sid() 再落盘一个空会话，
        # 于是「删一个」必然「多一个」；现在先试着切到剩下最新的那个，
        # 真的一个都不剩了才新建（且标题带时间，不再和别的「新会话」重名）
        store.bump_epoch()
        store.turn["active"] = False
        # 候选顺序按索引里的 updated（比 stat 每个文件便宜得多），
        # 真正要恢复的那一份才读文件
        cand = [i["id"] for i in session_store.session_order(store.index().values())
                if i.get("messages") and i["id"] not in store.deleted_sids]
        nxt = None
        for cand_sid in cand:
            d = await asyncio.to_thread(session_store.io_submit(store.read_session, cand_sid).result)
            if d.get("history"):
                nxt = d
                break
        if nxt is None:
            store.current_sid = _fresh_sid()
            store.history = []
            _ctx_used = 0
            save_current_session()
        else:
            store.current_sid = nxt.get("id") or _fresh_sid()
            # 换对象而不是原地 clear：在跑的那一轮仍持有旧列表引用
            store.history = nxt.get("history") or []
            comp = nxt.get("compact")
            if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
                _set_compact(store.current_sid, comp.get("summary") or "",
                             min(int(comp.get("upto") or 0), len(store.history)))
            else:
                store.drop_compact(store.current_sid)
            _ctx_used = _estimate_tokens(
                _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
        await manager.broadcast({"type": "history", "messages": store.history,
                                 "sid": store.current_sid})
        await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    return {"ok": True, "current": store.current_sid}


@app.post("/api/checkpoint", dependencies=[Depends(require_auth)])
async def api_checkpoint():
    """客户端退出前先来这儿「打个招呼」：把还没落盘的东西全部刷下去。

    以前客户端退出直接 SIGTERM，后端跑着的这一轮回答就整段消失。
    现在客户端先调这里（拿到 ok 后再发 SIGTERM），正在生成中的正文至少保住到此刻。
    """
    flushed = False
    try:
        buf = _stream_buf()
        text = buf.get("text") or ""
        if store.turn.get("active") and text.strip():
            persist_stream(text, force=True, parts=buf.get("parts") or [])
            flushed = True
    except Exception:
        pass
    # 写盘已经丢给 IO 线程排队了，这里要等它真的落完再放客户端走，
    # 否则 SIGTERM 一到，排队中的那一次写入就跟着一起没了。
    # 等待要放到线程里：这一等最长 5 秒，直接在事件循环里等就别想再处理任何请求了
    try:
        await asyncio.to_thread(session_store.io_submit(lambda: None).result, 5)
    except Exception:
        pass
    return {"ok": True, "flushed": flushed, "sid": store.current_sid,
            "turns": len(store.history)}


_lan_cache: tuple[float, str] = (0.0, "")
_lan_lock = threading.Lock()


def _lan_ip() -> str:
    """取真实局域网 IP：枚举网卡，优先 192.168.x，避开 VPN/加速器 TUN 虚拟段

    结果缓存 60 秒：这个函数每次都要 fork 一次 `ip`，而状态卡是 5 秒一刷的。
    锁只护住缓存读写那几行，真正的 `ip` 调用仍在锁外跑：多端同时刷状态时
    最多多 fork 几次，绝不因为一个网卡探测把整个事件循环堵住。
    """
    global _lan_cache
    now = time.monotonic()
    with _lan_lock:
        if _lan_cache[1] and now - _lan_cache[0] < 60:
            return _lan_cache[1]
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3).stdout
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    except Exception:
        ips = []

    # 只在 RFC1918 私有段里挑：198.18.x 是 Clash/加速器的 fake-ip 段、
    # 169.254.x 是本地链接保留段，都不是别的设备能连上的局域网地址，
    # 混进来会让状态卡显示一个「代理地址」而不是真实局域网 IP。
    ips = [a for a in ips if a.startswith("192.168.") or a.startswith("10.")
           or re.match(r"172\.(1[6-9]|2\d|3[01])\.", a)]

    def rank(a: str) -> int:
        if a.startswith("192.168."):
            return 0
        if a.startswith("10."):
            return 1
        return 2

    ips.sort(key=rank)
    ip = ips[0] if ips else "127.0.0.1"
    with _lan_lock:
        _lan_cache = (now, ip)
    return ip


def phone_url() -> str:
    """手机扫码/直连用的地址（扫码后走 PIN 配对，不再携带令牌）"""
    port = int(CFG["server"].get("port", 18765))
    return f"http://{_lan_ip()}:{port}/"


@app.get("/api/qrcode", dependencies=[Depends(require_auth)])
async def api_qrcode():
    """局域网访问地址 + 二维码 PNG（base64）"""
    url = phone_url()
    png_b64 = ""
    try:
        import base64
        import io

        import qrcode

        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return {"url": url, "qr": png_b64}


# ---------- 操作遮罩 ----------
# agent 真正操作设备时，桌面上盖一层半透明遮罩；用户点一下遮罩即终止当前操作

_overlay_proc = None
_overlay_state = {"show": False, "text": "", "passthrough": False, "lift": False, "pointer": False}
_overlay_ack = {"passthrough": False, "lift": False}  # 遮罩进程确认已让开鼠标 / 已抬高


def _xauthority() -> str:
    """当前可用的 X 授权文件

    Xwayland 每次会话都会换一个 xauth_XXXXXX 文件，后端进程环境里那份
    早晚会失效。这里先认环境里的路径（存在才算数），否则按修改时间取
    最新的一份；用 setdefault 是不够的——环境里留着旧路径时新文件永远
    不会被找到，遮罩会一直报 "Authorization required"。
    """
    cur = os.environ.get("XAUTHORITY", "")
    if cur and Path(cur).exists():
        return cur
    cands = glob.glob(f"/run/user/{os.getuid()}/xauth_*")
    cands = [c for c in cands if Path(c).exists()]
    if cands:
        return max(cands, key=lambda p: Path(p).stat().st_mtime)
    home = Path.home() / ".Xauthority"
    return str(home) if home.exists() else ""


def _overlay_env() -> dict:
    """桌面会话环境：systemd 用户服务里默认没有 DISPLAY / XAUTHORITY"""
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    auth = _xauthority()
    if auth:
        env["XAUTHORITY"] = auth
    else:
        env.pop("XAUTHORITY", None)
    env["DECKMEIDO_API"] = f"http://127.0.0.1:{CFG['server'].get('port', 18765)}"
    # 走 Xwayland（xcb）：Wayland 原生窗口没有「不进任务栏」的协议，遮罩必须走 X11 才能隐藏
    env["QT_QPA_PLATFORM"] = "xcb"
    return env


_overlay_lock = threading.Lock()


def ensure_overlay() -> None:
    """遮罩进程常驻，按需显示；崩溃后自动重启

    用 --force 启动：先让可能残留的旧遮罩自己退位，再起新的一层。
    否则后端重启留下的孤儿遮罩会越积越多，操作时叠出好几层。

    起完之后要核对「这一层真的拿到套接字了」：overlay 的 force 接管
    先 ping 旧实例、再等它死透，接口正常时这一步不该失败；但万一旧实例
    卡在 X11 调用里没退，新进程会自己退出，此时 _overlay_proc 是个
    已死的句柄，遮罩层等于没起。核对一遍并补拉一次，避免「点了停止
    没反应」这种更难受的故障。

    这个函数里有最长 6 秒的等待（等旧实例退位），所以异步调用点必须用
    asyncio.to_thread 包起来，不能直接在事件循环里调。
    """
    # 两轮对话同时开始工具调用时，别各自拉起一层遮罩
    with _overlay_lock:
        _ensure_overlay_locked()


def _ensure_overlay_locked() -> None:
    global _overlay_proc
    if _overlay_proc is not None and _overlay_proc.poll() is None:
        return

    def spawn() -> bool:
        """拉起一层遮罩并确认它活着且有套接字"""
        try:
            log = open(paths.logs_dir() / "overlay.log", "ab")
        except OSError:
            log = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "overlay.py"), "--force"],
                cwd=str(BASE_DIR), env=_overlay_env(),
                stdout=log, stderr=log, start_new_session=True,
            )
        except Exception:
            return False
        finally:
            try:
                log.close()
            except Exception:
                pass
        # 接管要等旧实例退位，给足时间；退出码非 0 说明没抢到
        for _ in range(40):          # 最多 6 秒
            if proc.poll() is not None:
                _ = proc.poll()      # 非 0 = 没接管成功
                return proc.returncode == 0
            time.sleep(0.15)
        globals()["_overlay_proc"] = proc
        return True

    for _ in range(2):               # 失败一次再补一次
        if spawn():
            return



def stop_overlay() -> None:
    """服务退出时收掉自己拉起的遮罩，避免留下孤儿进程

    两处兜底，都是踩过的坑：
    1. 只 terminate 不 kill：遮罩若卡在 X11 调用里不响应 SIGTERM，
       这个同步 wait 就会一直占着 AnyIO worker 线程，把整个后端的
       shutdown 拖死——uvicorn 收不到中断，端口释放不掉，下次启动
       要么被单实例守卫挡回，要么去抢 socket。所以超时后必须 kill。
    2. 兜底按 pid 清理：进程句柄可能已经丢了（后端重启过），此时靠
       _overlay_proc 一个都收不到，孤儿就留在桌面上继续画遮罩。
    """
    global _overlay_proc
    proc, _overlay_proc = _overlay_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
    # 句柄之外的残留（含拿不到套接字的聋子孤儿）统一清掉
    for pid in _overlay_pids():
        try:
            if proc is not None and pid == proc.pid:
                continue
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    # 关键：僵尸子进程必须回收（waitpid）。
    # overlay.py 被 --force 接管时会被别人 SIGKILL，它成了僵尸挂在 CMD_START 的
    # 事件循环里没被 wait 过；若不 reap，主进程被 SIGTERM 后 uvicorn 的 shutdown
    # 会一直卡在「Waiting for background tasks to complete」，端口不释放，
    # 表现为一个既不服务也不退出的幽灵后端（还会被单实例守卫挡着不让重启）。
    # 只回收我们自己那个 pid：waitpid(-1) 会把 asyncio 管着的命令进程一起摘走
    if proc is not None:
        _reap_children(proc.pid)


def _reap_children(only_pid: int | None = None) -> None:
    """回收已退出但还没 wait 的子进程（僵尸）。

    `waitpid(-1)` 会摘走**任意**一个子进程 —— 包括 asyncio 子进程管理器
    正在管着的 run_command 进程。那些进程的退出码需要由事件循环回收，
    被这里抢走后 asyncio 会一直等不到终止状态，命令永远「不结束」。
    所以只回收我们自己拉起的那个 pid（遮罩），没有指定就什么都不做。
    """
    if only_pid is None:
        return
    try:
        os.waitpid(only_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _overlay_pids() -> list[int]:
    """找出本机所有 overlay.py 进程（含孤儿）"""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", str(BASE_DIR / "overlay.py")],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.split():
            if line.strip().isdigit():
                pid = int(line)
                if pid != os.getpid():
                    pids.append(pid)
    except Exception:
        pass
    return pids



def set_overlay(show: bool, text: str = "") -> None:
    _overlay_state["show"] = show
    if text:
        _overlay_state["text"] = text
    if not show:
        _overlay_state["since"] = 0.0
        # 收起遮罩 = 这一轮不再往屏幕上写正文了（终止 / 收尾 / 开始操作桌面）：
        # 攒着没发的字先推出去，保证界面上的正文与随后的系统提示顺序正确。
        # 运行在事件循环里时不能直接建任务（这里也可能是异步任务之外的路径），
        # 所以只在有事件循环时交给 call_soon；没有就留给收尾的 flush
        try:
            asyncio.get_running_loop().call_soon(flush_delta_now, "")
        except RuntimeError:
            pass
        # 不要在这里顺手把 passthrough 清掉：可能正好有一次点击注入在跑，清了之后
        # 遮罩会重新冒出来挨这一下点击，于是又被当成「用户终止」。passthrough 只由
        # set_overlay_passthrough 收
        # 遮罩收起：不再需要为它压着电源抑制（命令还在跑的话那条原因仍在，
        # 计数归零才真的释放，见 power.AwakeKeeper）
        power.keeper.release("overlay")
    else:
        # 记下「从什么时候开始要它亮」。看门狗据此判断是不是有哪条异常路径
        # 漏了 set_overlay(False)，把遮罩永远留在屏幕上。
        if not _overlay_state.get("since"):
            _overlay_state["since"] = time.time()
        # 遮罩亮着 = 用户可能正盯着看操作过程，别在这时候息屏
        power.keeper.hold("overlay", text or "正在思考")
        ensure_overlay()


async def set_overlay_async(show: bool, text: str = "") -> None:
    """set_overlay 的异步版本：拉起遮罩那段（最长 6 秒）丢到线程里做

    直接 await 这个而不是调 set_overlay：后者会 fork 进程并轮询等它接管，
    在事件循环里跑会把所有 ws 广播和心跳按住。
    """
    if not show:
        set_overlay(False, text)
        return
    _overlay_state["show"] = True
    if text:
        _overlay_state["text"] = text
    if not _overlay_state.get("since"):
        _overlay_state["since"] = time.time()
    power.keeper.hold("overlay", text or "正在思考")
    await asyncio.to_thread(ensure_overlay)


# 遮罩最多连亮这么久；超过就认为状态没被复位（异常/取消路径逃逸），强制收掉。
# 一整轮最长思考时间可能有一两分钟（多轮工具 + 模型推理），这里给足余量；
# 正常结束时 handle_chat 的 finally 会收掉，用不到这个兜底。
_OVERLAY_MAX_HOLD = 300.0


def _overlay_watchdog() -> None:
    """兜底线程：遮罩亮着太久就强制收起，别让它一直挡住屏幕。

    遮罩在「会动屏幕」的工具执行期间常显（见 tools.touches_screen），正常收尾由
    调用方 set_overlay(False)；取消、异常、工具执行到一半被终止，任一环节漏掉，
    遮罩就会一直盖在那儿。这里按 _OVERLAY_MAX_HOLD 兜一个上限。
    """
    while True:
        time.sleep(1.0)
        try:
            if not _overlay_state.get("show"):
                continue
            since = _overlay_state.get("since") or 0.0
            if since and (time.time() - since) > _OVERLAY_MAX_HOLD:
                print("[DeckMeido] 遮罩超时未复位，已强制收起", file=sys.stderr)
                set_overlay(False)
        except Exception:
            pass


threading.Thread(target=_overlay_watchdog, name="overlay-watchdog", daemon=True).start()


async def set_overlay_passthrough(on: bool) -> None:
    """让遮罩暂时让开，使 agent 的点击能落到下面的窗口。

    遮罩窗口跑在 XWayland 里，Wayland 合成器做命中测试时看不见 X11 的
    input shape，所以「窗口标志让开」是不成立的；遮罩进程收到这个标志后
    会把窗口整块 hide()，并回报「确实藏好了」。这里必须等那个回报再放行，
    否则点击会打在还没撤下去的遮罩上，被当成「用户终止」。
    """
    _overlay_state["passthrough"] = on
    _overlay_ack["passthrough"] = False
    if not on:
        return
    if _overlay_proc is None or _overlay_proc.poll() is not None:
        return  # 遮罩没跑，不用等
    for _ in range(80):  # 最多等 800ms（hide + 合成器撤层需要几帧）
        if _overlay_ack["passthrough"]:
            return
        await asyncio.sleep(0.01)


tools.ON_OVERLAY_PASSTHROUGH = set_overlay_passthrough


def set_overlay_pointer(on: bool) -> None:
    """预告「下一步会按下鼠标键」，让遮罩别抢这一下点击。

    遮罩是 XWayland 窗口，非穿透状态下会把点击吃掉（=用户终止）。而
    set_overlay_passthrough 在遮罩还没显示时什么都不做，工具内部自己去调也来
    不及。所以这里把意图做成后端状态，遮罩进程看到 pointer 立刻转 25ms 快轮询，
    显示后马上 hide 并回报，agent 随后注入的点击就落在下面的窗口上。
    """
    _overlay_state["pointer"] = bool(on)


async def set_overlay_lift(on: bool) -> None:
    """截图期间把遮罩收起来，拍完再放回来。

    打开时要等遮罩真正隐藏（它每 25ms 轮询并回报 ack），否则这张截图里还是有遮罩。
    """
    _overlay_state["lift"] = on
    if not on:
        return
    if _overlay_proc is None or _overlay_proc.poll() is not None:
        return  # 遮罩没跑，不用等
    for _ in range(20):  # 最多等 200ms
        if _overlay_ack["lift"]:
            return
        await asyncio.sleep(0.01)


tools.ON_OVERLAY_LIFT = set_overlay_lift


@app.get("/api/controls", dependencies=[Depends(require_auth)])
async def api_controls(force: int = Query(default=0)):
    """控件自检：force=1 时跳过缓存重新实测（网页上点徽章刷新用）

    自检里是一串 subprocess 探针，必须走线程，否则缓存过期的瞬间会把事件循环卡住。
    """
    return await asyncio.to_thread(depcheck.summary, bool(force))


@app.post("/api/controls/fix", dependencies=[Depends(require_auth)])
async def api_controls_fix():
    """修复控件：只做不需要 root 的那半——把 ydotoold 交给 systemd 用户单元托管。

    装系统包需要 sudo 密码，而这里是 systemd 托管的后台进程，接不了终端输入，
    也没有可用的免密策略，所以一律引导用户到终端执行 install.sh。
    """
    out: dict = {"results": []}

    ok, msg = await asyncio.to_thread(depcheck.fix_ydotoold)
    out["results"].append({"key": "ydotoold", "ok": ok, "message": msg})
    out["need_sudo"] = True
    out["hint"] = ("安装系统控件需要 root 权限。请在终端执行："
                   "bash install.sh check --fix（会提示输入 sudo 密码）")

    # 走线程：刚点完修复就要再跑一遍全套探针，同步跑会把事件循环卡住
    out["controls"] = await asyncio.to_thread(depcheck.summary, True)
    # 刚修完控件必须给最新的：跳过 TTL 缓存
    await manager.broadcast({"type": "status", "data": await status_async(force=True)})
    return out


@app.get("/api/overlay_state", dependencies=[Depends(require_auth)])
async def api_overlay_state(ack: int = 0, hidden: int = 1):
    # 遮罩进程带 ack=1 轮询时，表示它已经按本次返回的状态生效过了。
    # hidden 是它自报的「当前窗口是否已隐藏」：要求让开时只有真的藏了才算确认，
    # 否则 agent 那一下点击仍可能打在还显示着的遮罩上，被当成用户终止
    if ack:
        # pointer 期间遮罩按「让开」处理，但它由后端先置、遮罩后看到，所以
        # 要不要等同样看它有没有真的藏：两个标志任一为真都按穿透算
        want = _overlay_state["passthrough"] or _overlay_state["pointer"]
        if want and not _overlay_state["lift"]:
            _overlay_ack["passthrough"] = bool(hidden)
        else:
            _overlay_ack["passthrough"] = want
        _overlay_ack["lift"] = _overlay_state["lift"]
    return _overlay_state


@app.on_event("startup")
async def _boot_overlay():
    ensure_overlay()


@app.on_event("shutdown")
async def _shutdown_overlay():
    # 服务被重启/停止时把自己拉起的遮罩一起收掉，别留孤儿
    stop_overlay()
    # 电源抑制也一起放掉，别让下一个进程/系统替我们背着
    try:
        power.keeper.shutdown()
    except Exception:
        pass
    # 退出前抢救一次：正在生成的正文与内存历史立刻落盘。
    # 客户端退出、systemctl restart、升级重启都走这条路径，
    # 少了这一步，被打断的那一轮回答就会整段消失（用户视角就是「记录丢了」）
    try:
        text = _stream_buf().get("text") or ""
        if store.turn.get("active") and text.strip():
            persist_stream(text, force=True, parts=_stream_buf().get("parts") or [])
            print(f"[DeckMeido] 退出前已保存未完成的回答（{len(text)} 字）", file=sys.stderr)
        # 这里刻意不写内存 history：正文在 handle_chat 收尾时已落盘，用户消息开头就落盘了，
        # 内存不比文件新。而「拿内存覆盖文件」在内存为空/偏旧时会直接把历史冲掉——
        # 那才是真正的「聊天记录丢了」。只补流式残留，别碰已落盘的历史。
    except Exception as e:
        print(f"[DeckMeido] 退出前落盘失败：{e}", file=sys.stderr)
    # 侧栏索引平时是延迟合并写的，退出前补一次，别让最后几条会话的标题/条数丢了
    try:
        store.flush_index()
    except Exception:
        pass


def _exit_code(result: str) -> int | None:
    """run_command 返回串以 `exit=<code>` 开头，解析出来给意图记账用"""
    m = re.match(r"^exit=(-?\d+)", str(result or ""))
    return int(m.group(1)) if m else None


def _arm_power_intent(ctx: policy.TurnContext, action: str, hist: list[dict]) -> None:
    """执行电源命令前登记 confirmed 意图：即便命令把机器关掉、来不及写收尾，
    下次启动也能按 boot_id 认出「这次重启已经执行过」"""
    it = policy.make_intent(action, ctx)
    it["status"] = "confirmed"
    store.set_intent(ctx.sid, it)
    try:
        _write_session(ctx.sid, hist)
    except Exception as e:
        print(f"[DeckMeido][policy] 写回电源意图失败：{e}", file=sys.stderr)


def _record_power_intent(ctx: policy.TurnContext, name: str, args: dict,
                         result: str) -> None:
    """命令返回后按退出码记账：成功=executed，失败=cancelled"""
    if name != "run_command":
        return
    action = policy.power_action(str((args or {}).get("command") or ""))
    if not action:
        return
    code = _exit_code(result)
    it = policy.make_intent(action, ctx)
    it["status"] = "executed" if code == 0 else "cancelled"
    it["finished_at"] = time.time()
    store.set_intent(ctx.sid, it)


_REFUSAL_MARKS = ["我不会", "我不能", "无法执行", "无法直接执行", "恕我不能", "不执行", "拒绝执行",
                  "我不会执行", "我没办法", "不允许", "不建议执行"]


def _looks_like_refusal(reply: str) -> bool:
    return any(m in reply for m in _REFUSAL_MARKS)


async def _run_agent_guarded(events: list[dict], hist: list[dict] | None = None,
                             image_part: str | None = None,
                             tool_log: list[dict] | None = None,
                             client: str | None = None,
                             turn_ctx: policy.TurnContext | None = None,
                             shot_lock: bool | None = None,
                             surface: str | None = None,
                             turn_policy: policy.TurnPolicy | None = None) -> str:
    """带总超时和拒绝重试的 agent 入口：任何环节卡死都不会让对话无限挂起

    image_part 非空表示本轮提问自带画面（这里是历史里那条 [SHOT:文件名] 标记，
    内容已由 _build_llm_msgs 展开成图片部件）。client 决定要不要套插件端那套
    「带图就一问一答」的限制：插件端会自己连着截屏，桌面网页端不会
    """
    hist = store.history if hist is None else hist
    pol = turn_policy or policy.OWNER_POLICY
    # 单轮超时：配了 max_tool_rounds 才有上限；不限轮数时给足时间（仍有硬超时兜底）
    agent_timeout = 280 if MAX_TOOL_ROUNDS > 0 else 1800
    try:
        reply = await asyncio.wait_for(
            run_agent(events, hist=hist, image_part=image_part, tool_log=tool_log,
                      from_client=client, turn_ctx=turn_ctx,
                      _shot_lock=shot_lock, surface=surface, turn_policy=pol),
            timeout=agent_timeout)
    except asyncio.TimeoutError:
        # 超时不能只丢一句「超时了」：这一轮里已经跑完的命令、改过的文件全都要留下，
        # 否则用户既看不到结果，下一轮模型也会把同样的活重做一遍
        note = ("（这轮处理超时了——可能是模型响应慢或命令太久。任务可能已在后台执行，"
                "换个问法或让我检查下结果）")
        digest = _tool_results_digest(tool_log)
        return note + (f"\n\n超时前已经执行的步骤：\n{digest}" if digest else "")
    # 系统层兜底：full 模式下模型空口拒绝（没调任何工具）→ 注入纠正指令重试一次。
    # 群友档零工具，「拒绝」是正常回答，不做这套纠正
    if (PERMISSION_MODE == "full" and not pol.readonly
            and not any(e.get("type") == "tool" for e in events)
            and _looks_like_refusal(reply)):
        events.append({"type": "thought", "text": "（系统检测到模型越权拒绝，已自动纠正重试）"})
        try:
            reply = await asyncio.wait_for(run_agent(
                events, hist=hist, tool_log=tool_log, from_client=client,
                extra_system=prompts.part("retry_refuse"), turn_ctx=turn_ctx,
                _shot_lock=shot_lock, surface=surface, turn_policy=pol,
            ), timeout=agent_timeout)
        except asyncio.TimeoutError:
            return "（重试也超时了，请再发一次）"
    return reply


def _llm_error_text(e: Exception) -> str:
    """把服务商/中转的原始报错整理成一句主人能看懂的话。

    httpx 抛出的 "Server error '503 Service Unavailable' for url ..." 后面
    还跟着 MDN 文档链接，直接铺进聊天既吓人也没用。这里只保留三件事：
    哪家账户、什么状态、下一步能做什么；没有状态码时（例如中转把错误塞进
    200 的流里）再退回服务商自己的说明。
    """
    raw = re.sub(r"\s*For more information check:.*", "", str(e))
    raw = re.sub(r"\s*Request url:.*", "", raw).strip()
    code = getattr(getattr(e, "response", None), "status_code", None)
    m = re.search(r"\b(401|402|403|404|408|409|429|500|502|503|504)\b", raw)
    status = int(code) if code else (int(m.group(1)) if m else None)
    name = getattr(llm, "api_name", "") or "当前接口"
    try:
        host = urllib.parse.urlsplit(str(getattr(llm, "base_url", "") or "")).hostname or ""
    except ValueError:
        host = ""
    label = f"「{name}」" + (f"（{host}）" if host else "")
    # 先分辨「连不上」和「连上了但服务慢」：两者都可能是超时，但下一步完全不同。
    # 局域网端点连接阶段只等 2 秒（见 LLMClient），这里照实说出来。
    if isinstance(e, (httpx.ConnectTimeout, httpx.ConnectError)):
        wait = "2 秒内连不上" if netmode.is_local_url(str(getattr(llm, "base_url", "") or "")) \
            else "连接失败"
        return (f"LLM 调用失败：{label} {wait}。请确认模型服务已启动、地址与端口可访问；"
                "如果这是局域网模型，检查设备是否还在同一网络")
    if isinstance(e, httpx.ReadTimeout):
        return (f"LLM 调用失败：{label} 已连上，但等待生成响应超时；"
                "服务可能正在计算或负载较高，稍后再试，或在设置里调大超时")
    hints = {
        401: "API Key 无效或已过期",
        402: "账户余额不足",
        403: "服务商拒绝了这次请求（权限或地区限制）",
        404: "接口路径不存在，请检查 base_url",
        429: "请求被限流，请稍后再试",
        500: "服务商内部错误",
        502: "网关错误，服务商暂时不可用",
        503: "服务商暂时不可用（上游模型服务异常，或该模型暂无可用节点）",
        504: "服务商响应超时",
    }
    if status:
        msg = f"LLM 调用失败：{label} {hints.get(status, '服务商返回错误')}（HTTP {status}）"
    else:
        brief = raw[:240] + ("…" if len(raw) > 240 else "")
        msg = f"LLM 调用失败：{label} {brief}"
    return msg + "。可在设置页「接口账户」里切换账户，或换一个模型后重试"


async def run_agent(events: list[dict], extra_system: str = "",
                   hist: list[dict] | None = None,
                   image_part: str | None = None,
                   tool_log: list[dict] | None = None,
                   from_client: str | None = None,
                   _shot_lock: bool | None = None,
                   surface: str | None = None,
                   turn_ctx: policy.TurnContext | None = None,
                   turn_policy: policy.TurnPolicy | None = None) -> str:
    """LLM + 工具调用循环（流式），思考链/工具调用/终端输出写入 events 供 UI 展示

    image_part 非空表示本轮提问自带画面：插件端这一轮把截图工具摘掉（见 round_tools）
    client 是发起端（网页 web / 插件 qam …）；_shot_lock 是否为插件端那套
    「带图就一问一答」的限制，由外面算好后随每轮工具往返递归传下来
    tool_log 由调用方传入：本轮执行过的工具记在这里，被终止时随回答落盘
    （见 handle_chat 的 CancelledError 分支与 _aborted_note）

    turn_policy 是本轮的场景策略表（不传按主人档）：工具集、记忆 scopes、
    是否拼模式段/extra、确认级别都从它取，引擎里不再散落场景判断
    """
    global pending_action, _ctx_used
    pol = turn_policy or policy.OWNER_POLICY
    hist = store.history if hist is None else hist
    # 本轮独占的截图状态：两轮并发时共用模块全局会互相清空（上一轮刚截的图
    # 被这一轮的 clear 抹掉，注入的图片就丢了一半）
    shot_state = tools.new_shot_state()
    # 本轮归属（中途切会话也不改这份 sid）
    turn_sid = store.turn.get("sid") or store.current_sid
    # 没传 TurnContext 的调用点（老代码 / 内部重试）按「没有本轮授权」处理：
    # user_text 留空，电源命令一律会被 policy 拒绝，而不是默认放行
    ctx = turn_ctx or policy.TurnContext(
        sid=turn_sid,
        turn_id=store.turn.get("turn_id") or "",
        user_text="",
        client=store.turn.get("client") or "",
        device=store.turn.get("device") or "",
        boot_id=_system_boot_id(),
        readonly=pol.readonly,
        force_confirm=pol.force_confirm(),
        # 完全访问：删除类硬约束让路（会话历史除外，见 policy.check_delete）
        full_access=(PERMISSION_MODE == "full"),
    )
    if pol.public_persona:
        # 群友档：公开人设 + 群聊上下文；不拼模式段、extra 和长期记忆，
        # 也不需要为拼提示词去探测当前桌面/游戏模式
        sys_prompt = _guest_system_prompt()
    else:
        # 当前模式必须实测后告诉模型：游戏模式用 gamepad 注入、游戏模式下从 gamescope
        # 抓帧、遮罩提示挂 gamescope 覆盖层；桌面模式走 desktop 点按 + spectacle 截图。
        # 两套操作手段不通用，猜错就是「点了没反应」或「抓不到画面」。探针会起子进程，
        # 所以放到线程里跑，别卡住这一轮
        gaming = await asyncio.to_thread(tools.mode_is_gaming)
        sys_prompt = SYSTEM_PROMPT + _mode_block(gaming) + (f"\n{extra_system}" if extra_system else "")
        # 长期记忆拼在最末尾（见 _memory_block 里的缓存说明）。每轮都重新取，
        # 用户刚在对话里让它「记住」，这一轮之后就能生效。
        # 记忆 scopes：None = 跟随当前会话；() = 本轮不注入；显式元组 = 临时切到这些 scope
        mem = ""
        if pol.memory_scopes is None:
            mem = _memory_block()
        elif pol.memory_scopes:
            tokens = memory.set_context(pol.memory_scopes, memory.current_write_scope())
            try:
                mem = _memory_block()
            finally:
                memory.reset_context(tokens)
        if mem:
            sys_prompt += "\n\n" + mem
    # 只把「摘要 + 游标之后的原文」发给模型；界面历史一条不删。
    # 群友档带策略表里的上下文窗口，主人档两项都是 0，行为不变
    msgs = _build_llm_msgs(hist, sys_prompt, turn_sid,
                           hist_max=pol.history_max_messages,
                           item_max_chars=pol.history_item_max_chars)
    hist_msgs = len(msgs)
    streaming = False  # 是否已开始向用户流式输出正文
    # 本轮正文累积：流式回调里按节流频率落盘，进程被杀最多丢一小段
    stream_text: list[str] = []
    # 上一轮被用户终止时做过、没做完的工具（见 _aborted_note）：终止就是「别干了」，
    # 不把这段交接说清楚，模型看到自己的话成了半截，下一句就会自作主张接着做
    aborted_note = _aborted_note(hist)
    if aborted_note:
        msgs.append({"role": "system", "content": aborted_note})
    # 本轮真正执行过的工具，终止时随回答一起落盘，供下一轮做交接
    tool_log: list[dict] = [] if tool_log is None else tool_log
    # 上下文里一共有几张画面（含本轮提问自带的那张）。模型自己截图时，新注入的
    # 那张要接着编号，否则它和旧图的说明一字不差，模型分不清哪张是当前的
    turn_shot_no = sum(1 for m in hist if isinstance(m.get("content"), list))

    def _stamp_parts(tail: str = "") -> list[dict]:
        """当前「屏幕上出现过的内容」快照，按原顺序

        events 本身就是有序流（思维链 / 工具块，以及工具调用前已经显示过的
        过渡正文段——那些以 {"type":"text"} 的形式插在 events 里）。
        尾部再补上当刻正在生成的那段正文，就是完整的屏幕内容。
        裁剪统一交给 _snapshot_parts：两处各写一份的话，上限迟早只补到一处。
        """
        return _snapshot_parts(events, tail)

    # 屏幕快照的刷新节流：思维链可能一个字一片，每片都重建一遍 parts 没必要
    _screen_at = 0.0

    def _persist_screen(force: bool = False) -> None:
        """把当前屏幕（思维链 / 工具块 / 过渡正文）刷进流式缓冲

        只在正文增量时刷新的话，模型整段只思考、只调工具的那些回合缓冲区不更新，
        用户中途刷新页面拿到的是旧快照，思维链会整段消失（见 refresh_stream_parts）
        """
        nonlocal _screen_at
        now = time.time()
        if not force and now - _screen_at < 0.8:
            return
        _screen_at = now
        refresh_stream_parts(turn_sid, "".join(stream_text), events)

    async def on_delta(text: str):
        """把 LLM 的增量正文转给前端，顺便增量落盘

        合批：模型一个 chunk 可能只有一两个字，原样转发等于每来一片就让前端
        重排一次整段气泡，Deck 上看着就是一卡一卡的。这里先攒进 _delta_buf，
        到 _DELTA_FLUSH_MS 再一次性发出；跨批次的边界不丢字（buffer 带锁）。
        """
        nonlocal streaming, thought_open
        stream_text.append(text)
        if not streaming:
            streaming = True
            # 模型开始写正式回答 = 不再操作桌面。若刚才那一步动屏的遮罩还亮着
            # （正常情况工具收尾就收掉了），这里再兜一次，把屏幕还给用户
            set_overlay(False)
            await manager.broadcast({"type": "stream_start", "ts": time.time(), **_turn_tag()})
        # 增量落盘（此函数是 async 的，前端不会感知到这点磁盘开销）
        # _stamp_parts 要遍历全部 events，先问一句要不要写，免得在节流命中的
        # 情况下白拼一遍字符串
        try:
            full = "".join(stream_text)
            if _stream_would_flush(full, sid=turn_sid):
                persist_stream(full, parts=_stamp_parts(full), sid=turn_sid)
        except Exception:
            pass
        # 前端按批渲染，这里把这一片先攒起来
        _flush_delta_later(turn_sid, text, _turn_tag())
        # 开头已经说话了，就把思维链收尾。模型经常是「先吐一两句正文，再补一段
        # reasoning」，如果只等本轮结束才发 thought_end，那段思考会挂到正文后面，
        # 界面上看起来就像「答完了还在想」（最终答复末尾下面又冒出一堆思考行）
        if thought_open:
            flush_thought_now(turn_sid)
            await manager.broadcast({"type": "thought_end", "ts": time.time(), **_turn_tag()})
            thought_open = False
            _rs_end("body_start")
        # final=False：这段正文可能只是工具调用前的过渡话，前端不写进 crash 缓冲；
        # 本轮结束时会有一次带 final=True 的完整正文推送（内容相同）

    thought_open = False
    # 本轮思维链在 events 里的那条记录：从第一片开始就建好、按到达顺序追加，
    # 保证 session 里存下来的思考就是屏幕上实时出现过的完整那份。
    # 以前只在最终响应里再补记，供应商只流式给 reasoning_content、最终字段
    # 为空时，刷新重放就整段消失。
    thought_ev = None
    # 本轮实时播给前端的思考原文（仅用于判断最终字段是否比实时片段更全）
    thought_live = []
    # dev_mode 埋点：每段思考累积到 500 字才记一行，不按分片刷屏
    _rs = {"started": False, "chars": 0, "segs": 0, "next": 500}

    def _rs_end(reason: str) -> None:
        """一段思考结束：记总字符、段数、结束原因"""
        if not _rs["started"]:
            return
        _rs["started"] = False
        devlog.log("reasoning.end", sid=turn_sid, round=round_no,
                   total_chars=_rs["chars"], segments=_rs["segs"], reason=reason)

    async def on_thought(text: str):
        nonlocal thought_open, thought_ev, thought_live
        if not thought_open:
            thought_open = True
            # 先切话题：正文字要排在思维链前面，不然顺序会乱
            flush_delta_now(turn_sid)
            await manager.broadcast({"type": "thought_start", "ts": time.time(), **_turn_tag()})
            thought_ev = {"type": "thought", "text": ""}
            events.append(thought_ev)
            _rs.update({"started": True, "chars": 0, "segs": 0, "next": 500})
            devlog.log("reasoning.start", sid=turn_sid, round=round_no)
        # 思考阶段模型吐的片比正文还密（往往一个字一片），逐片广播最费前端
        _flush_thought_later(turn_sid, text, _turn_tag())
        thought_live.append(text)
        thought_ev["text"] += text
        _rs["chars"] += len(text)
        _rs["segs"] += 1
        _persist_screen()
        if _rs["chars"] >= _rs["next"]:
            devlog.log("reasoning.delta", sid=turn_sid, round=round_no,
                       total_chars=_rs["chars"], segments=_rs["segs"])
            _rs["next"] = (_rs["chars"] // 500 + 1) * 500

    # 重试时重新开始思考：上一个半截思维链已经显示过，不补一个 thought_start
    # 的话新来的片段会接着拼到那块旧内容后面
    def _reset_thought() -> None:
        nonlocal thought_open, thought_ev
        flush_thought_now(turn_sid)
        _rs_end("retry")
        thought_open = False
        _thought_pend.pop(turn_sid, None)
        # 上一段的半截记录留在 events 里（屏幕上出现过），新一段另起一块
        thought_ev = None

    def _output_budget(used_msgs: list[dict], used: int | None = None) -> int:
        """按当前上下文剩余空间算出本次调用的输出上限。

        旧逻辑固定 4096，长答复/长工具参数会在中途被硬截断，正文漏出半截标记。
        现在：输出额度 = min(配置上限, 上下文窗口 - 已用 - 预留)，且不低于 4096。
        used 传入时直接用调用方刚算好的用量；不传才现数一遍（msgs 只增不减，
        数一次的结果可以直接复用）。
        """
        win = context_window()
        if used is None:
            used = _estimate_tokens(used_msgs)
        # 预留一小块给下一轮的用户消息 / 工具结果
        room = max(win - used - 2048, 4096)
        cap = MAX_OUTPUT_TOKENS if MAX_OUTPUT_TOKENS > 0 else win
        return max(min(cap, room), 4096)

    try:
        round_no = 0
        # 本轮已经注入对话的那条画面消息：只保留最新一张（模型反复截图就是想确认
        # 刚才那步之后的画面，留着早先那张它会对着旧帧下判断）
        shot_msg = None
        # 同一工具同参数连续重复的计数（死循环闸门，见 policy.LOOP_*）
        loop_state: dict = {}
        # 本轮提问自带画面时，模型再自己截一张就是多余的：它拍到的往往是
        # DeckMeido 面板本身，或者和用户发的那张不是同一帧，多一张图只会让
        # 它挑错图回答。这时把截图工具从这一轮的清单里摘掉
        # 这里的 image_part 是本轮的 [SHOT:文件名] 标记（见 handle_chat），
        # 只用来判断「这一轮带没带画面」，内容本身已经在 msgs 里展开成图片了
        has_shot = image_part is not None
        # 「带图就问一句、不许自己再截」只约束插件端（QAM 截屏询问）：它挂在
        # gamescope 上，模型在那里反复截图会把游戏画面一顿乱拍。桌面/网页端
        # 是照着屏幕干活的，截图是它的眼睛，不能因为带了张图就不许它看。
        # 这里用 from_client 而不是 client：下面工具循环里的 for call 会用到
        # client 这个名字（handle_chat 里本来就是这么命名的），别被它盖掉
        if _shot_lock is None:
            _shot_lock = policy.shot_lock_for(from_client, surface)
        locked_shot = has_shot and _shot_lock
        # 工具集由场景策略表决定（群友档是空集 = 零工具），再叠加带图轮的截图限制
        round_tools = [t for t in pol.filter_tools(tools.TOOLS)
                       if tools.tool_ready(t["function"]["name"], {})]
        if locked_shot:
            round_tools = [t for t in round_tools
                           if t["function"]["name"] != "screenshot"]
        # 带图提问是「一问一答」：模型答完通常会顺手再截一张确认，于是又对着
        # 新画面续一轮，用户看到的是一条提问连着好几张图和好几段回答。
        # 所以带图这轮答完正文就直接收工，不再继续跑工具轮
        if locked_shot:
            extra_system = (extra_system + "\n" if extra_system else "") + prompts.part("locked_shot")
        # chat 入口的「只说不做」纠偏次数：模型偶尔回一句「我去看看」就不调工具了，
        # 这里最多补两次催促（policy.chat_should_continue），用完仍不动手就按最终答复收尾
        stall_nudges = 0
        while MAX_TOOL_ROUNDS <= 0 or round_no < MAX_TOOL_ROUNDS:
            round_no += 1
            # 单轮里工具往返也会堆积上下文，超过阈值就先压缩再继续。
            # 压缩只推进游标、不删 history，所以这里重建 msgs 时要保留
            # 本轮新加的工具往返消息（hist_msgs 之后的部分），否则工具链断掉。
            # 每轮只数一遍 msgs：压缩判断和输出额度都用这个值。
            # _estimate_tokens 要遍历全部历史（含几 MB 的图片 part），
            # 一轮算两遍纯属白烧 CPU
            used_now = _estimate_tokens(msgs)
            if _need_compact(used_now):
                # 压缩失败（网络抖动、摘要模型报错）不该把整轮对话弄崩：
                # 压缩只是省上下文，压不了就带着原上下文继续跑
                try:
                    await compact_history(auto=True, hist=hist, sid=turn_sid)
                    rebuilt = _build_llm_msgs(
                        hist, sys_prompt, turn_sid,
                        hist_max=pol.history_max_messages,
                        item_max_chars=pol.history_item_max_chars)
                    extra = [m for m in msgs[hist_msgs:]
                             if m.get("role") in ("tool", "assistant")]
                    msgs[:] = rebuilt + extra
                    hist_msgs = len(rebuilt)
                    # 重建后内容变了，用量跟着重算一次
                    used_now = _estimate_tokens(msgs)
                    streaming = False
                    # 压缩前已经显示的过渡正文：作为 text 事件留在 events 里继续保存
                    if "".join(stream_text).strip():
                        events.append({"type": "text", "text": "".join(stream_text)})
                    stream_text.clear()
                except Exception as e:
                    print(f"[DeckMeido] 工具轮中压缩失败，继续：{e}", file=sys.stderr)

            thought_ev = None
            thought_live = []
            msg = await llm.chat_with_tools_stream(msgs, round_tools, on_delta=on_delta,
                                                   on_thought=on_thought,
                                                   on_retry=_reset_thought,
                                                   max_tokens=_output_budget(msgs, used_now))
            if thought_open:
                # 先把手里的思考片段推完，再发 thought_end，否则尾部几个字会
                # 落在 thought_end 之后，前端那块已经收起来了，看起来就是「思考末尾丢字」
                flush_thought_now(turn_sid)
                await manager.broadcast({"type": "thought_end", "ts": time.time(), **_turn_tag()})
                thought_open = False
                _rs_end("round_end")

            # 撞到输出上限时自动续写，把后半段接上（不再把用户丢给「请拆小步骤」）
            cont = 0
            while (msg.get("finish_reason") == "length" and not (msg.get("tool_calls") or [])
                   and cont < MAX_CONTINUATIONS):
                cont += 1
                partial = (msg.get("content") or "")
                if not partial.strip():
                    break
                msgs.append({"role": "assistant", "content": partial})
                msgs.append({"role": "user", "content": prompts.part("continue_truncated")})
                used_now = _estimate_tokens(msgs)
                cont_msg = await llm.chat_with_tools_stream(
                    msgs, [], on_delta=on_delta,
                    max_tokens=_output_budget(msgs, used_now))
                more = (cont_msg.get("content") or "")
                # 续写阶段的思维链同样不能补记：正文已经流式播到屏幕上了，
                # 这条 thought 追加到 events 里就排在本轮正文之后
                msg = {"role": "assistant", "content": partial + more,
                       "finish_reason": cont_msg.get("finish_reason")}
                if cont_msg.get("usage"):
                    msg["usage"] = cont_msg["usage"]
                # 续写时模型改了主意想调工具：停止续写，按无工具收尾处理
                if cont_msg.get("tool_calls"):
                    break
                if not more.strip():
                    break

            # 记录本轮真实占用：优先服务商的 prompt_tokens，拿不到就用上面数过的估算值
            _ctx_used = _split_usage(msg.get("usage")) or used_now
            await manager.broadcast({"type": "status", "data": await status_async()})

            # 水位自检：Deck 的可用内存在跑会话时往往只剩一两百 MB，等内核动手就晚了
            # （SIGKILL 之后 Python 一句遗言都留不下）。这里提前把还能丢的东西丢掉：
            # 老工具结果只留给人看的那一截，parts 也已经自带上限
            _relieve_memory(events)

            # 思维链（reasoning_content，DeepSeek/Qwen 等支持）
            # 实时片段已经逐片追加到 thought_ev（见 on_thought），落盘内容与
            # 屏幕上出现过的一致；这里只处理「供应商没有逐片回调、整段只出现在
            # 最终响应里」的情况，并把最终字段比实时片段多出来的尾巴补上。
            # 不再按「是否实时播过」整体跳过 —— 那会让 session 里一条思考记录
            # 都不留，刷新重放时整段消失。
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                if thought_ev is None:
                    thought_ev = {"type": "thought", "text": reasoning}
                    events.append(thought_ev)
                    # 这一段只出现在最终响应里（没有逐片回调）
                    devlog.log("reasoning.start", sid=turn_sid, round=round_no)
                    devlog.log("reasoning.end", sid=turn_sid, round=round_no,
                               total_chars=len(reasoning), segments=1,
                               reason="final_only")
                else:
                    saved = thought_ev.get("text") or ""
                    if reasoning not in saved:
                        stripped = saved.strip()
                        if stripped and reasoning.startswith(stripped):
                            # 最终字段是实时片段的超集：补上没播到的那段
                            thought_ev["text"] = saved + reasoning[len(stripped):]
                        elif not stripped:
                            thought_ev["text"] = reasoning
                        else:
                            # 内容对不上（重试换了思路）：另起一块，别把新的丢掉
                            thought_ev = {"type": "thought", "text": reasoning}
                            events.append(thought_ev)

            tool_calls = msg.get("tool_calls") or []
            # 每轮模型返回都记一笔：这一步到底动没动手、动了什么。
            # 「AI 说按了但游戏没反应」时，这条能立刻区分是模型没调工具
            # （纯嘴上说），还是调了但注入没生效
            print(f"[DeckMeido] 模型第 {round_no} 轮：工具 {len(tool_calls)} 个"
                  + (f"（{'、'.join(c['function']['name'] for c in tool_calls)}）"
                     if tool_calls else "，没有动手，直接作答")
                  + f"，正文 {len(msg.get('content') or '')} 字")
            if not tool_calls:
                body = (msg.get("content") or "").strip()
                # 聊天页的任务型提问：模型只回了「我去看看」这类过渡话就停住，
                # 给它一次明确的催促，让它真的调工具继续做；用完次数仍不动手
                # 就按最终答复收尾（不会无限循环）
                if policy.chat_should_continue(from_client, surface, body, stall_nudges):
                    stall_nudges += 1
                    # 这段正文已经流式播给用户了，必须作为 text 事件留在 events 里，
                    # 否则最终答复一来它就从界面上消失
                    if "".join(stream_text).strip():
                        events.append({"type": "text", "text": "".join(stream_text)})
                    streaming = False
                    stream_text.clear()
                    msgs.append({"role": "assistant", "content": body})
                    msgs.append({"role": "user", "content": prompts.part("continue_task")})
                    print(f"[DeckMeido] chat 入口第 {round_no} 轮只说不做，"
                          f"催促第 {stall_nudges} 次继续执行")
                    continue
                # 续写后仍被截断：内容已经流式显示给用户了，别丢，末尾加一句说明
                if msg.get("finish_reason") == "length":
                    note = ("\n\n（以上内容到了输出上限，续写也没能收尾；"
                            "要我接着写就说「继续」。）")
                    return (body + note) if body else note.strip()
                print("[DeckMeido] 本轮收尾：模型没有再调用工具")
                return body or "（没有回复内容）"

            # 带图这轮只答一次：模型拿到工具结果后常常再截一张确认，
            # 于是又对着新画面续一轮，用户看到一条提问连着好几张图和好几段回答。
            # 所以带上图提问时，答完第一段正文就收工
            if locked_shot and "".join(stream_text).strip():
                return "".join(stream_text).strip()

            # 动屏幕的工具一律不给：插件端带图这轮只要「看图说怎么办」，动手会换掉
            # 用户截图时的画面，答的内容也就对不上他看到的那一帧了
            if locked_shot:
                # touches_screen 要的是解析后的参数（要判断 desktop 的 action、
                # run_command 的命令内容），而 arguments 这里是 JSON 字符串
                def _screen_call(c: dict) -> bool:
                    try:
                        a = json.loads(c["function"].get("arguments") or "{}")
                    except (ValueError, TypeError):
                        a = {}
                    a = a if isinstance(a, dict) else {}
                    n = c["function"]["name"]
                    # 电源命令不碰屏幕，但截图轮也绝不能执行：模型可能一边
                    # 「看图回答」一边把机器关掉
                    return (tools.touches_screen(n, a)
                            or policy.blocks_in_locked_shot(n, a))

                dropped = [t["function"]["name"] for t in tool_calls if _screen_call(t)]
                if len(dropped) == len(tool_calls):
                    # 整批都是屏幕操作：连同模型那句「我先截个图」一起作废，
                    # 让它直接按已有画面作答，界面上不留一次空工具调用
                    stream_text.clear()
                    msgs.append({"role": "assistant", "content": msg.get("content") or ""})
                    msgs.append({"role": "user", "content": prompts.part("no_screen_op")})
                    continue
                tool_calls = [c for c in tool_calls if not _screen_call(c)]

            # 有工具调用：本轮流式输出的内容是中间话，最终答复会重新流式。
            # 但用户屏幕上已经看到了，必须以 text 事件留在 events 里，
            # 否则中途进程被杀，快照里只剩最后一段 → 「只看到一点点内容」
            if "".join(stream_text).strip():
                events.append({"type": "text", "text": "".join(stream_text)})
            streaming = False
            stream_text.clear()
            msgs.append(msg)
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                # 死循环闸门：模型拿不到新反馈时会把同一个调用反复发出来。
                # 第 LOOP_SKIP_AT 次不再执行、明确告诉它结果不会变；
                # 仍继续重复就按 LOOP_ABORT_AT 收尾，把现状交给用户
                repeats = policy.repeat_step(loop_state,
                                             policy.call_signature(name, args))
                if repeats >= policy.LOOP_SKIP_AT:
                    label = tools.cn_name(name)
                    summary = tools.summarize_tool(name, args)
                    reason = ("重复调用已中断" if repeats >= policy.LOOP_ABORT_AT
                              else "重复调用已跳过")
                    loop_event = {"type": "tool", "name": name, "label": label,
                                  "summary": summary, "files": [], "denied": True,
                                  "reason": f"{reason}（连续第 {repeats} 次）"}
                    events.append(loop_event)
                    _persist_screen(force=True)
                    await manager.broadcast({"type": "tool_done", "event": loop_event,
                                             "ts": time.time(), **_turn_tag()})
                    print(f"[DeckMeido][loop] 重复调用 {name} 连续 {repeats} 次"
                          f"（会话 {turn_sid}）：{summary}", file=sys.stderr)
                    if repeats >= policy.LOOP_ABORT_AT:
                        return (f"同一个操作我连着做了 {repeats} 次（{label}：{summary}），"
                                f"结果没有变化，就先停在这里。\n\n"
                                f"要接着做的话换个做法，或者直接告诉我下一步；"
                                f"确实需要我重试同一个操作也请明说。")
                    msgs.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": f"[{name}] 同一操作已经连续做了 {repeats} 次，"
                                            f"结果不会再变化，这次没有执行。别再重复，"
                                            f"直接向用户说明现在卡在哪一步。"})
                    continue

                # ask_user：提问 / 选择题。不执行任何东西，直接把问题抛给界面，
                # 这一轮就此收工；用户点的那一项作为下一条普通消息发回来，
                # 于是它天然走完一轮「回答→再提问」的循环，不用另立挂起状态
                if name == "ask_user":
                    q = str(args.get("question") or "").strip()
                    opts = [str(o).strip() for o in (args.get("options") or [])
                            if str(o).strip()][:6]
                    # 要密码 / 问要不要重启：登记一条有状态、会过期的 pending_intent，
                    # 这样用户下一轮回「2333」或「好」才算授权；历史里那句密码
                    # 不会在几十轮之后又被当成有效凭据
                    ask_kind = policy.ask_intent_kind(q)
                    if ask_kind:
                        it = policy.make_intent(ask_kind, ctx)
                        store.set_intent(turn_sid, it)
                        try:
                            _write_session(turn_sid, hist)
                        except Exception as e:
                            print(f"[DeckMeido][policy] 写回待授权意图失败：{e}",
                                  file=sys.stderr)
                        print(f"[DeckMeido][policy] 登记待授权意图 {ask_kind}"
                              f"（会话 {turn_sid}）", file=sys.stderr)
                    ask = {"question": q, "options": opts}
                    events.append({"type": "ask", **ask})
                    await manager.broadcast({"type": "ask", "ts": time.time(),
                                             **ask, **_turn_tag()})
                    return q or prompts.part("ask_empty")

                # 远程三档：只有 QQ（force_confirm）才走这里，本机入口完全不变。
                # deny 直接回固定文案并终止本轮；hard 必须回「确认执行」；
                # 其余落到原来的单段确认流程。
                remote_risk = (policy.classify_remote_tool(name, args, remote=True)
                               if ctx.force_confirm else policy.REMOTE_NORMAL)
                if remote_risk.level == "deny":
                    label = tools.cn_name(name)
                    summary = tools.summarize_tool(name, args)
                    deny_event = {"type": "tool", "name": name, "label": label,
                                  "summary": summary, "files": [], "denied": True,
                                  "reason": remote_risk.reason}
                    events.append(deny_event)
                    await manager.broadcast({"type": "tool_done", "event": deny_event,
                                             "ts": time.time(), **_turn_tag()})
                    print(f"[DeckMeido][risk] 远程拒绝 {remote_risk.rule}"
                          f"（会话 {turn_sid}）：{remote_risk.reason}", file=sys.stderr)
                    _qq_log("warn", "远程风险拒绝",
                            f"{remote_risk.rule}: {remote_risk.reason}")
                    return (f"这条操作在 QQ 远程被拒绝了"
                            f"（{remote_risk.rule}：{remote_risk.reason}）。"
                            f"为了安全不会执行，也不要换一种写法绕过。")

                # 授权闸门：电源类命令只认本轮 user_text + 有效的 pending_intent。
                # full 模式也必须过这一关 —— 它只决定「要不要弹审批」，
                # 不决定「模型有没有资格执行」
                decision = policy.check(name, args, ctx, store.get_intent(turn_sid))
                if not decision.allowed:
                    label = tools.cn_name(name)
                    summary = tools.summarize_tool(name, args)
                    deny_event = {"type": "tool", "name": name, "label": label,
                                  "summary": summary, "files": [], "denied": True,
                                  "reason": decision.reason}
                    events.append(deny_event)
                    await manager.broadcast({"type": "tool_done", "event": deny_event,
                                             "ts": time.time(), **_turn_tag()})
                    msgs.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": f"[{name}] 策略拒绝：{decision.reason}"})
                    print(f"[DeckMeido][policy] 策略拒绝 {name}（会话 {turn_sid}）："
                          f"{decision.reason} args={json.dumps(args, ensure_ascii=False)[:200]}",
                          file=sys.stderr)
                    continue
                if decision.intent:
                    # 密码 / 确认被这次调用消费掉：落盘，刷新了状态才算有效
                    store.set_intent(turn_sid, decision.intent)
                    print(f"[DeckMeido][policy] 授权通过（{decision.code}）"
                          f"：{name}", file=sys.stderr)
                    try:
                        _write_session(turn_sid, hist)
                    except Exception as e:
                        print(f"[DeckMeido][policy] 写回授权状态失败：{e}", file=sys.stderr)

                # 审批闸门：档位 × 危险级别的判断全部收敛在 approval.resolve，
                # 这里只负责按结论做事（拒绝 / 挂起等确认 / 直接执行）。
                # 极危操作（写盘、格式化、清空账号文件…）不给确认通道，任何
                # 档位包括完全访问都直接拒绝 —— 那是不可逆的破坏，不该由一次
                # 误判换来一块被抹掉的盘
                extra_danger = ""
                if name == "run_command":
                    cmd = str(args.get("command", ""))
                    # QQ 远程额外高危表（menu.py）：卸载软件、清理包缓存、自动清理等，
                    # 现有 tools/policy 规则没覆盖到的也一律按硬确认拦下
                    extra_danger = (menu.remote_command_danger(cmd)
                                    if ctx.force_confirm else "")
                verdict = approval.resolve(name, args, ctx, PERMISSION_MODE,
                                           CUSTOM_PERM, extra_danger=extra_danger)
                if verdict.deny:
                    label = tools.cn_name(name)
                    summary = tools.summarize_tool(name, args)
                    deny_event = {"type": "tool", "name": name, "label": label,
                                  "summary": summary, "files": [], "denied": True,
                                  "reason": verdict.reason}
                    events.append(deny_event)
                    await manager.broadcast({"type": "tool_done", "event": deny_event,
                                             "ts": time.time(), **_turn_tag()})
                    msgs.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": f"[{name}] 审批拒绝：{verdict.reason}"})
                    print(f"[DeckMeido][approval] 拒绝 {name}（{verdict.rule}，"
                          f"会话 {turn_sid}）：{verdict.reason}", file=sys.stderr)
                    return (f"这条操作被拦下了（{verdict.label}：{verdict.reason}）。"
                            f"它属于不可逆的破坏性操作，不会执行，也不要换一种写法绕过。"
                            f"如果确实需要，请自己在桌面终端里手动操作。")
                needs_approval = verdict.ask

                if needs_approval or remote_risk.level == "hard_confirm":
                    hard = verdict.hard or remote_risk.level == "hard_confirm"
                    # 挂起危险操作，等用户下一轮确认。把「谁在哪提的」一并记下，
                    # 确认时校验（见 _pending_matches），别的端说的「好」不算数。
                    # hard 档只认「确认执行」，onebot 侧会按这个标记收紧确认口径
                    pending_action = {"name": name, "args": args,
                                      "description": tools.describe(name, args),
                                      "hard": hard,
                                      "sid": store.turn.get("sid") or store.current_sid,
                                      "epoch": store.turn.get("epoch") or store.epoch,
                                      "turn_id": store.turn.get("turn_id") or "",
                                      "client": store.turn.get("client") or "",
                                      "device": store.turn.get("device") or ""}
                    print(f"[DeckMeido] 工具待确认 {name}（模式 {PERMISSION_MODE}，"
                          f"{verdict.label}{'/' + verdict.reason if verdict.reason else ''}）："
                          f"{pending_action['description']}")
                    text_out = prompts.part("pending_approval").format(
                        description=pending_action['description'])
                    if pending_action["hard"]:
                        text_out += ("\n这项操作风险较高"
                                     f"（{verdict.label}：{verdict.reason or '高危操作'}），"
                                     "只有回复「确认执行」才会执行。")
                    return text_out

                # 文件改动识别（run_command）
                files = tools.extract_writes(str(args.get("command", ""))) if name == "run_command" else []
                label = tools.cn_name(name)
                summary = tools.summarize_tool(name, args)
                event = {"type": "tool", "name": name, "label": label,
                         "summary": summary, "files": files}
                # 路径一律给全：AI 给的常常是相对工作区的路径，面板直接拿去
                # 跳目录会落到后端进程的 cwd 上（一般是家目录），跟 AI 看的地方对不上
                if name in ("read_file", "write_file", "edit_file", "list_dir"):
                    raw_path = str(args.get("path", ""))
                    if raw_path:
                        # 相对路径按工作区解释（和文件工具一致），不按后端 cwd
                        p = Path(raw_path).expanduser()
                        event["path"] = str(p if p.is_absolute() else tools.WORKSPACE / p)
                # run_command 走 shell 命令（cat/ls/cd…）时，从命令串里把路径扒出来，
                # 让文件面板跟着跳，而不是只有终端联动。extract_paths 返回的是目录
                elif name == "run_command":
                    p = tools.extract_paths(str(args.get("command", "")))
                    if p:
                        event["path"] = p
                        event["is_dir"] = os.path.isdir(p)
                # 全盘扫描类命令执行前就把「可能较久」说出来，别让人以为卡住了
                slow_hint = (tools.slow_scan_hint(str(args.get("command") or ""))
                             if name == "run_command" else "")
                activity = f"正在{label}：{summary}" if summary else f"正在{label}"
                if slow_hint:
                    activity += f"（{slow_hint}）"
                # 工具卡片要插在正文后面：先把手头攒的 delta 推出去，
                # 否则这批字会晚于工具卡片到达，前端顺序就是乱的
                flush_delta_now(turn_sid)
                await manager.broadcast({"type": "tool_activity", "text": activity, "ts": time.time(), **_turn_tag()})
                # 终端联动：必须在执行之前推「开跑」，面板据此立刻挂一条执行中的记录
                # 并开始轮询实时输出。放到执行后面就只剩「已结束」一条，
                # 执行中的动画和进度全看不见了
                if name == "run_command" and str(args.get("command") or "").strip():
                    claim = tools.CURRENT_CMD
                    await manager.broadcast({"type": "term_start",
                                             "command": str(args.get("command")),
                                             "reason": str(args.get("reason") or ""),
                                             "slow": slow_hint,
                                             "cwd": (claim or {}).get("cwd", ""),
                                             "track": bool(claim and claim.get("command") == args["command"]),
                                             # 面板记下推送时刻，用来对齐轮询拿到的 started：
                                             # 对不上说明这条命令已经跑完、CURRENT_CMD 换了，
                                             # 别把它当成还在执行的那条一直转圈
                                             "ts_sent": time.time(),
                                             "ts": time.time(), **_turn_tag()})
                # 文件浏览器联动：凡是带路径的工具都推一条，面板据此自动跳目录。
                # 只推路径不推内容 —— 面板自己再走 /api/browse 取，权限和越界检查
                # 都留在那一个接口里
                if event.get("path"):
                    await manager.broadcast({"type": "fs_follow", "path": event["path"],
                                             "is_dir": bool(event.get("is_dir")) or name == "list_dir",
                                             "ts": time.time(), **_turn_tag()})
                # 遮罩只在真的要动屏幕的这一步亮起（见上面 handle_chat 里的说明）。
                # 放在真正执行前才 set_overlay(True)：鼠标注入期间 tools 会让遮罩
                # 穿透（set_overlay_passthrough），否则那一下点击会点在遮罩上
                # 被当成「用户终止」。
                # pointer：这一步会不会按下鼠标键（点击/滚动/拖拽/滑动）。之前在 tools
                # 内部自己开穿透，而穿透在遮罩还没显示时什么都不做，于是遮罩随后
                # 才冒出来，正好接住那一下点击 —— 这里必须先让遮罩就位再看这个标志
                on_screen = tools.touches_screen(name, args)
                pointer = on_screen and tools.pointer_action(name, args)
                set_overlay_pointer(pointer)
                if on_screen:
                    # 拉起遮罩最长要等 6 秒旧实例退位，必须走线程，否则整个 ws 卡住
                    await set_overlay_async(True, activity)
                # 先记一条「没执行完」的：被终止时 CancelledError 会从这里穿过去，
                # 于是交接说明里能准确指出「这一步断了」。正常跑完再把它改成已完成
                log_item = {"name": name, "summary": summary, "interrupted": True}
                tool_log.append(log_item)
                # 电源命令执行前先落一条 confirmed 意图：真重启会掐断收尾，
                # 下次启动按 boot_id 把它标成 executed（见 _restore_intent_map）
                power_act = (policy.power_action(str(args.get("command") or ""))
                             if name == "run_command" else None)
                if power_act:
                    _arm_power_intent(ctx, power_act, hist)
                try:
                    result, notes = await _run_tool_busy(name, args)
                finally:
                    if on_screen:
                        set_overlay(False)
                    set_overlay_pointer(False)
                log_item["interrupted"] = False
                # 长任务中途发过的「还在跑 · 第 N 秒」，收尾时连同结果一起落盘，
                # 否则刷新页面后那几句进度提示就没了
                for note in notes:
                    events.append({"type": "progress", "text": note,
                                   "ts": time.time() + 0.001 * len(notes)})
                # 退出码为 0 记 executed，失败记 cancelled；真重启通常走不到这里，
                # 由下次启动的 boot_id 对账补上
                _record_power_intent(ctx, name, args, result)
                # 结果摘要随 tool_log 落盘：终止 / 超时那一轮靠它把「做过什么、
                # 结果是什么」留在会话里（见 _tool_results_digest）
                log_item["output"] = result[:2000]
                # 终端输出截断后随事件展示（网页端显示为终端块）
                event["output"] = result[:1500] + ("\n…（截断）" if len(result) > 1500 else "")
                if name in ("write_file", "edit_file") and tools.LAST_DIFF:
                    event["diff"] = tools.LAST_DIFF
                if name == "screenshot":
                    m = re.search(r"/shots/\S+?\.png", result)
                    if m:
                        event["image"] = m.group(0)
                    else:
                        # 抓帧失败：接口层要把原因带到前端，不能让用户只看到
                        # 一行「已截屏」却一直没有画面
                        event["error"] = shot_state.get("error") or "未知原因"
                events.append(event)
                await manager.broadcast({"type": "tool_done", "event": event, "ts": time.time(), **_turn_tag()})
                _persist_screen()
                # 工具结果前面统一加「[工具名] 」，这类元信息在请求最前面：
                # 同一次调用无论正文多长，前缀都一样，DeepSeek 的前缀缓存才命中得上。
                # 反过来，若这里塞「本次命中缓存」之类的可变文案，后面整段历史
                # 立刻全部失配，一次工具往返就能把几万 token 从命中打回未命中。
                # 长度必须封顶：cat 一个大文件、npm install 刷屏都能吐几十 MB，
                # 原样塞进来这一轮的请求体就会一路涨到发不动，之后每次都带着它重发。
                # 前缀是工具名，截断点之后的内容对缓存无影响，所以封顶不影响命中
                msgs.append({"role": "tool", "tool_call_id": call["id"],
                             "content": f"[{name}] {_cap_tool_text(result)}"})
                # 截图后把画面注入对话，多模态模型可直接看图回答。
                # 只保留最新一张：模型反复截图往往是因为拿不到「刚才那步之后」的画面，
                # 留着早先那张它就只能对着旧帧判断，于是原地连截七八次停不下来。
                # 每截一张就把旧图从对话里换掉，上下文里始终只有一帧，也不越堆越大
                if name == "screenshot" and shot_state.get("vision"):
                    turn_shot_no += 1
                    try:
                        import base64
                        b64 = base64.b64encode(Path(shot_state["vision"]).read_bytes()).decode()
                        fresh = {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            {"type": "text", "text": _agent_shot_caption(max(turn_shot_no, 1))},
                        ]}
                        if shot_msg is not None:
                            try:
                                msgs.remove(shot_msg)
                            except ValueError:
                                pass
                        msgs.append(fresh)
                        shot_msg = fresh
                    except Exception:
                        pass
        # 工具轮数用尽：最后再不给工具地问一次，让它基于已有结果收尾回答
        msg = await llm.chat_with_tools_stream(
            msgs, [], on_delta, max_tokens=_output_budget(msgs, _estimate_tokens(msgs)))
        if msg.get("reasoning_content") and thought_ev is None:
            thought_ev = {"type": "thought", "text": msg["reasoning_content"]}
            events.append(thought_ev)
        answer = (msg.get("content") or "").strip()
        limit_note = (f"（已经连续执行 {MAX_TOOL_ROUNDS} 步，到了单轮上限，后面的步骤没有继续。"
                      f"要接着做的话说一声，或把 max_tool_rounds 调大）")
        # 收尾时模型经常什么都不写：这不是正常答复，要把「为什么停」说清楚
        return f"{answer}\n\n{limit_note}" if answer else limit_note
    except Exception as e:
        # 把失败当正常回复返回，下一轮模型会在自己的历史里读到一句「LLM 调用失败」，
        # 于是把这句话当成它说过的话继续往下聊。明确标成中断，交给上层提示
        friendly = _llm_error_text(e)
        print(f"[DeckMeido] {friendly}", file=sys.stderr)
        raise RuntimeError(friendly) from e


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    client_ip = ws.client.host if ws.client else ""
    if not _ws_authorized(ws):
        # 先 accept 再 close，浏览器才能拿到 4401 这个原因码，否则只会看到 1006 而无限重连
        audit.record("auth", "ws", "fail", ip=client_ip, reason="unpaired")
        await ws.accept()
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    # 来源：优先信调用方显式声明的 client（插件标 qam，桌面客户端标 client），
    # 没声明就按 UA 猜——手机浏览器自动算 mobile，其余算 web。
    # ws_mode 用来记住「这条连接是在什么模式下连上的」：状态广播是全局的，
    # 游戏模式的判定（PipeWire 里的 gamescope 节点）对所有连着的端都成立，桌面网页就会
    # 跟着显示成「游戏模式」。所以按连接自己的会话判定，桌面网页永远是桌面
    ua = ws.headers.get("user-agent", "")
    ua_client = "mobile" if ("Mobile" in ua or "Android" in ua) else "web"
    ws_mode = (await asyncio.to_thread(get_status))["mode"]
    # 这两项告诉前端「此刻后端进程里还在跑哪一轮」：
    # inflight = 流式已写过、还没收尾的会话（这一轮的正文还只在半路上，没进 history）
    # boot_id  = 本进程的启动标识，前端据此判断这条连接是「进程重启后的第一条」
    # 还是「只是链路断了一下」。以前前端区分不了，任何一次重连都当成崩溃，
    # 把本地缓存补回整段对话，屏幕上就冒出「已由本机缓存补回」
    inflight = sorted(_inflight_sids())
    boot_id = singleton.BOOT_ID
    try:
        # 新连接同步历史（带上 sid，前端才能建立会话代次基线，
        # 否则 currentSid 一直是空值，代次过滤整条失效）
        await ws.send_json({"type": "history", "messages": store.history,
                            "sid": store.current_sid, "my_client": ua_client,
                            "inflight": inflight, "boot": boot_id,
                            "partial": _inflight_snapshot(store.current_sid)})
        devlog.log("ws.send", type="history", messages=len(store.history),
                   sid=store.current_sid, inflight=inflight, boot=boot_id)
        await ws.send_json({"type": "status",
                            "data": {**await status_async(), "mode": ws_mode}})
        while True:
            data = await ws.receive_json()
            # 不记录图片 base64（可能有几十 MB），只记类型、文本摘要和是否带图
            devlog.log("ws.recv", type=data.get("type"),
                       text=str(data.get("text") or "")[:300],
                       has_image=bool(data.get("image")),
                       workspace=data.get("workspace"),
                       client=data.get("client"))
            if data.get("type") == "ping":
                # 前端心跳：回一条 pong 让它知道链路还活着
                await ws.send_json({"type": "pong"})
                continue
            if data.get("type") == "history":
                # 客户端断线重连后主动要一份：握手时后端已经发过一次，
                # 但断线期间别的端说的话它没收到，得重新对齐
                await ws.send_json({"type": "history", "messages": store.history,
                                    "sid": store.current_sid, "my_client": ua_client,
                                    "inflight": sorted(_inflight_sids()),
                                    "boot": boot_id,
                                    "partial": _inflight_snapshot(store.current_sid)})
                continue
            if data.get("type") == "chat":
                # 开始发问就刷新：这条连接此刻在什么模式下，就是它自己的模式
                ws_mode = (await asyncio.to_thread(get_status))["mode"]
                try:
                    # 插件那条路要从 ws 直接带图和真实工作区进来：
                    # 它挂在 gamescope 上，请求和流式都在同一条连接里，
                    # 走不了"先 POST 拿结果再订阅"的两段式。
                    ws_root = data.get("workspace")
                    await start_chat(
                        str(data.get("text", "")).strip(),
                        image=data.get("image") or None,
                        image_prompt=data.get("image_prompt")
                        or "（这是当前游戏画面，结合画面回答用户的问题）",
                        workspace=Path(ws_root).expanduser() if ws_root else None,
                        client=str(data.get("client") or ua_client),
                        device=str(data.get("device") or "").strip() or None,
                        brief=str(data.get("brief") or "").strip(),
                        turn_id=str(data.get("turn_id") or ""),
                        surface=str(data.get("surface") or "").strip() or None,
                    )
                except asyncio.CancelledError:
                    pass  # 用户点了终止，保持 ws 连接
                except Exception as exc:
                    # 一轮对话内部报错不能把整条 ws 打死：旧逻辑异常直接冒到外层，
                    # 连接被断 → 前端 busy 卡死 → 后续消息全排队发不出去
                    try:
                        await ws.send_json({
                            "type": "assistant_message",
                            "text": f"这轮出错了：{exc}",
                            "ts": time.time(), "events": [],
                        })
                    except Exception:
                        pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 兜底：不管怎么退出都要摘掉连接，否则 broadcast 会一直往死连接上发
        await manager.disconnect(ws)


@app.get("/shots/local", dependencies=[Depends(require_auth)])
async def shots_local(path: str = Query(default="")):
    """把插件截图落在本机 /tmp 下的那份读出来给各端显示

    插件的图在交给后端之前只存在于 Deck 的临时目录：电脑网页那头刷新历史时，
    后端这边还没落盘，屏幕上就只有一条没有画面的提问。所以允许按绝对路径取。
    只认 /tmp 下、JPEG 结尾的文件，别的一概不认——否则这个接口等于开放整台
    Deck 的文件读取。
    """
    p = Path(path)
    try:
        real = p.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if (real.suffix.lower() not in (".jpg", ".jpeg")
            or not str(real).startswith("/tmp/") or not real.is_file()):
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(real, media_type="image/jpeg")


@app.post("/api/screenshot", dependencies=[Depends(require_auth)])
async def api_screenshot():
    """插件端专用：抓一张当前画面，返回压缩 JPEG 的 base64

    游戏模式下插件自己从 gamescope 的 PipeWire 节点抓帧，不走这条；桌面
    模式（含从桌面进 Big Picture）插件没有抓帧能力，转到这里复用本后端
    spectacle/mss 那条桌面路径，避免插件端再养一套抓帧逻辑。
    """
    return await tools.screenshot_payload()


# ---------- QQ 远程控制通道（OneBot 11 / NapCat 客户端） ----------

def _qq_nickname() -> str:
    """触发前缀就是软件里配置的昵称；昵称为空时由 onebot 退回默认词"""
    return str(PERSONA.get("name") or "")


async def _qq_status_info() -> dict:
    gaming = await asyncio.to_thread(tools.mode_is_gaming)
    return {
        "core": "运行中",
        "mode": "游戏模式" if gaming else "桌面模式",
        "version": VERSION,
    }


async def _qq_screenshot() -> tuple[bytes | None, str]:
    """抓一张当前画面给 /shot 用；返回 (图片字节, 说明)

    走和图聊同一套截图能力（1280×800，游戏模式抓 gamescope，桌面模式用
    spectacle/mss），优先回传压缩过的 vision JPEG，避免 base64 太大。
    """
    try:
        note = await _run_tool("screenshot", {})
    except Exception as e:
        return None, f"截图失败（{type(e).__name__}）"
    holder = tools.SHOT_STATE
    if str(holder.get("error") or ""):
        return None, str(holder["error"])
    path = holder.get("vision")
    if path:
        try:
            p = Path(path)
            if p.is_file():
                return await asyncio.to_thread(p.read_bytes), note
        except OSError:
            pass
    # 状态容器可能被别的轮次换掉：退一步取最近一张压缩图
    try:
        shots = sorted((STATIC_DIR / "shots").glob("vision_*.jpg"),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        shots = []
    for p in reversed(shots):
        try:
            return await asyncio.to_thread(p.read_bytes), note
        except OSError:
            continue
    return None, note


# 远程「装完就打开」：只认明确的安装类命令，且必须 exit=0
_QQ_INSTALL_RE = re.compile(
    r"(install\.sh|"
    r"\b(?:apt|apt-get|dnf|yum|zypper)\b[^\n]*\binstall\b|"
    r"\bpacman\b[^\n]*\s-{1,2}S\w*|"
    r"\bflatpak\s+install\b|"
    r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:bash|sh))",
    re.IGNORECASE)


def _qq_install_target(cmd: str) -> str:
    """从安装命令里猜要启动的可执行名（猜不到返回空串）"""
    text = str(cmd or "")
    patterns = (
        r"\b(?:apt|apt-get|dnf|yum|zypper)\b[^\n]*\binstall\b([^\n]*)",
        r"\bpacman\b[^\n]*\s-{1,2}S\w*\s+([^\n]*)",
        r"\bflatpak\s+install\b([^\n]*)",
    )
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        tail = m.group(1)
        for token in re.split(r"\s+", tail.strip()):
            token = token.strip("\"'")
            if token and not token.startswith("-") and not token.endswith(".sh"):
                return token
    return ""


async def _qq_maybe_launch_after_install(events: list) -> dict:
    """安装类命令跑成功后自动拉起程序并截一张图；只用于 QQ 管理员轮次"""
    cfg = onebot.parse_config(CFG.get("qq") or {})
    if not cfg.auto_launch_after_install:
        return {}
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("name") != "run_command":
            continue
        if ev.get("denied"):
            continue
        cmd = str(ev.get("summary") or "")
        if not _QQ_INSTALL_RE.search(cmd):
            continue
        output = str(ev.get("output") or "")
        if not output.startswith("exit=0"):
            _qq_log("warn", "自动启动跳过", "安装命令未成功退出")
            continue
        target = _qq_install_target(cmd)
        if not target:
            _qq_log("info", "自动启动跳过", "猜不到可执行名")
            continue
        if cfg.auto_launch_wait_seconds > 0:
            await asyncio.sleep(min(30.0, cfg.auto_launch_wait_seconds))
        exe = shutil.which(target) or target
        try:
            await asyncio.to_thread(
                subprocess.Popen, [exe],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, cwd=str(Path.home()))
        except Exception as e:
            _qq_log("error", "自动启动失败", f"{type(e).__name__}: {e}")
            return {"reply": f"已装好，但没能自动打开 {exe}（{type(e).__name__}）。"}
        _qq_log("info", "自动启动", f"{exe}")
        blob, _note = await _qq_screenshot()
        caption = (f"已装好：{target}，已经自动打开了。"
                   f"游戏模式下窗口可能看不到，切回桌面看一眼。")
        if blob:
            return {"reply": caption, "caption": caption, "image": blob}
        return {"reply": caption + "\n（截图没抓到，就不附图了。）"}
    return {}


def _qq_clear_pending(device: str, sid: str) -> None:
    """QQ 侧确认超时后，把仍然属于这条来源的挂起操作撤掉"""
    global pending_action
    p = pending_action
    if (isinstance(p, dict) and (p.get("client") or "") == "qq"
            and (p.get("device") or "") == device
            and (p.get("sid") or "") == sid):
        pending_action = None


async def _qq_run_turn(text: str, *, sid: str, device: str,
                       readonly: bool, turn_id: str = "",
                       image: bytes | None = None,
                       proactive: bool = False) -> dict:
    """QQ 通道的一轮对话

    走现有 handle_chat，但用 QQ 自己的会话（sid）和来源标记（device）：
    本地界面正在看的会话不会被追答写进去，历史里也能分清每条来自哪个 QQ / 群。
    proactive=True 表示这一轮是「没人叫、按概率自己插话」，只用于日志区分。

    来源授权闸门：只有 onebot 明确标记为已授权（True）才开回合。
    未授权时直接返回，不写会话、不广播、不跑工具——避免界面里出现一条
    「已开始询问卸载哪个」的会话，让用户误以为它在干活。
    """
    global _ctx_used, pending_action
    if not onebot.qq_turn_authorized():
        onebot.log_tool_blocked("qq_turn")
        _qq_log("warn", "回合拒绝",
                f"reason=unauthorized_source uin={device}")
        return {"reply": "", "denied": True, "unauthorized": True}
    if not turn_id:
        turn_id = "qq-" + secrets.token_hex(6)
    # 挂起的危险操作只认「同一个 QQ 来源」的确认，并且必须沿用那一轮的 turn_id，
    # 才符合 handle_chat 的认领校验（见 _pending_matches_turn）。
    # 会话已经切走（epoch 变了）或者 sid 对不上的，确认也放行不了，直接作废
    pend = pending_action
    if (isinstance(pend, dict) and (pend.get("client") or "") == "qq"
            and (pend.get("device") or "") == device):
        if onebot.is_confirm_text(text) or onebot.is_hard_confirm(text):
            if ((pend.get("sid") or "") == sid
                    and (pend.get("epoch") or 0) == store.epoch
                    and (pend.get("turn_id") or "")):
                turn_id = str(pend.get("turn_id"))
            else:
                pending_action = None
    # 本地端正在跑另一轮时不要插进来：store.turn / pending_action 都是全局单份的，
    # 两边同时跑会把「这一轮是谁提的」搅乱。等本地这轮结束再发即可
    if (store.turn.get("active") and (store.turn.get("client") or "") != "qq"):
        return {"reply": "设备正在处理本地端发起的操作，等它结束再发；这一条没有提交。",
                "busy": True}
    # QQ 的会话独立于本地当前会话：历史从它自己的文件读，不碰 store.history
    if sid in store.deleted_sids:
        # 这个 sid 之前被用户删过。QQ 的 sid 是按 uin/群号算死的，
        # 上一轮已收尾的话就允许开一条全新的，但历史必须从空开始 ——
        # 绝不能把删掉那轮的内容（或它的摘要）再带回来
        active_sid = str(store.turn.get("sid") or "") if store.turn.get("active") else ""
        if not store.allow_fresh(sid, active_sid):
            return {"reply": "上一条会话还在收尾，等它结束再发；这一条没有提交。",
                    "busy": True}
        hist = []
    elif sid == store.current_sid:
        hist = store.history
    else:
        data = await asyncio.to_thread(store.read_session, sid)
        hist = data.get("history") if isinstance(data.get("history"), list) else []
    image_part = _qq_image_data_url(image)
    turn_started = time.time()
    # 场景策略表：群友档零工具/无长期记忆/400 字/脱敏；主人档能力不变
    turn_policy = policy.GUEST_POLICY if readonly else policy.OWNER_POLICY
    # 群友提示词也留着给 _guest_reply 做灰度回退用
    guest_text = _guest_system_prompt() if readonly else ""
    try:
        result = await handle_chat(text, client="qq", device=device,
                                   turn_id=turn_id, surface=policy.SURFACE_CHAT,
                                   sid=sid, hist=hist,
                                   force_confirm=True, image=image_part,
                                   guest=bool(readonly),
                                   guest_prompt_text=guest_text,
                                   turn_policy=turn_policy)
    finally:
        # handle_chat 会把本轮的 token 用量写进全局估算值，那是给本地会话看的；
        # QQ 这轮结束后归零，让本地/status 下次重新按自己的历史估算
        _ctx_used = 0
    # 排障：QQ 会话到底有没有写进索引。dev_mode 打开时这条会落到
    # userdata/logs/debug-<时间>.log，搜 qq.session 即可；平时不写文件
    try:
        devlog.log("qq.session", sid=sid, readonly=bool(readonly),
                   messages=int(store.index().get(sid, {}).get("messages") or 0))
    except Exception:
        pass
    out = {"reply": str(result.get("reply") or "")}
    p = pending_action
    if (isinstance(p, dict) and (p.get("client") or "") == "qq"
            and (p.get("device") or "") == device and (p.get("sid") or "") == sid
            and (p.get("epoch") or 0) == store.epoch):
        out["pending"] = {"turn_id": p.get("turn_id") or "",
                          "description": p.get("description") or "",
                          "hard": bool(p.get("hard"))}
    if not readonly:
        extra = await _qq_maybe_launch_after_install(result.get("events") or [])
        if extra:
            out.update(extra)
    # 模型这一轮自己截了屏（比如「打开设置看看」）：画面留在 /shots/ 下，
    # 网页端能看到，QQ 侧却只有一句文字。把这张新图随回复发回去，
    # 否则用户会觉得「截图截到了但没发上来」。只认本轮新生成的 vision 文件，
    # 避免把上一轮的旧图重发一遍。
    if not out.get("image"):
        blob = await asyncio.to_thread(_turn_screenshot_blob, turn_started)
        if blob:
            out["image"] = blob
            out["caption"] = out.get("caption") or out["reply"] or "截图已奉上"
            _qq_log("info", "回合附图", f"{len(blob)} 字节")
    return out


def _turn_screenshot_blob(since: float) -> bytes | None:
    """取本轮截图工具产出的压缩画面；不是本轮生成的就返回 None"""
    try:
        path = tools.SHOT_STATE.get("vision")
        if not path:
            return None
        p = Path(str(path))
        if not p.is_file() or p.stat().st_mtime < since:
            return None
        return p.read_bytes()
    except OSError:
        return None


def _qq_image_data_url(blob: bytes | None) -> str | None:
    """QQ 图片字节 → handle_chat 能吃的 data URL"""
    if not blob:
        return None
    if blob[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif blob[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        mime = "image/webp"
    elif blob[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    else:
        return None
    return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")


def _qq_apply_config(raw: dict) -> None:
    onebot_client.configure(onebot.parse_config(raw))


def _qq_voice_wav(text: str) -> Path:
    """QQ 语音条合成（线程内跑）：按 tts.speaker 选发音人，产出临时 wav，调用方负责删

    robot 走 espeak-ng，maid 走 piper-plus；两者失败都抛 TTSError，
    由 onebot 记 voice.synth 后只发文字（现有回退逻辑不变）。
    """
    return tts.synth_wav(text)


async def _qq_synth_voice(text: str) -> Path:
    return await asyncio.to_thread(_qq_voice_wav, text)


# 语音落盘用的文件名：只允许 16 位十六进制 + .wav，路由按这个格式白名单校验
VOICE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.wav$")


async def _qq_voice_saved(ev, path: Path, seconds: float, size: int) -> None:
    """QQ 语音条实际发出后：留一份 wav 到 userdata/voice，并写进会话消息元数据

    历史里只存 {url, seconds, speaker, bytes}，音频本体放独立文件，
    不会把 base64 撑进会话 JSON。写入排在会话 IO 线程里，和正常落盘串行。
    """
    sid = onebot.session_sid(ev)
    name = f"{secrets.token_hex(8)}.wav"
    dest = paths.voice_dir() / name
    await asyncio.to_thread(shutil.copyfile, str(path), str(dest))
    meta = {
        "url": f"/api/voice/{name}",
        "seconds": round(float(seconds), 1),
        "speaker": SPEAKER_LABELS.get(tts.speaker, "机器人"),
        "bytes": int(size),
    }
    loop = asyncio.get_running_loop()

    def _work() -> None:
        with store.lock:
            if sid in store.deleted_sids:
                return
            hist = (store.history if sid == store.current_sid
                    else (store.read_session(sid).get("history") or []))
            ts = session_store.attach_voice(hist, meta)
            if ts is None:
                return
            store.write_session(sid, hist)
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type": "voice_added", "sid": sid,
                               "ts": ts, "voice": meta}), loop)

    session_store.io_submit(_work)


onebot_client = onebot.OneBotClient(
    runner=_qq_run_turn,
    screenshot=_qq_screenshot,
    status_provider=_qq_status_info,
    nickname=_qq_nickname,
    pending_clear=_qq_clear_pending,
    # 单条图文的 base64 超 3MB 时，压到长边 1024 / q82 再发
    image_optimizer=lambda blob: _jpeg_for_llm(blob, max_side=1024, quality=82),
    voice_synth=_qq_synth_voice,
    voice_saved=_qq_voice_saved,
)
onebot_client.configure(onebot.parse_config(CFG.get("qq") or {}))


@app.on_event("startup")
async def _boot_onebot():
    # 默认不自启：enabled 只表示「允许运行」，重启后必须手动点连接。
    # 只有显式打开 auto_connect 才会在 core 启动时自动建连。
    if onebot_client.should_auto_connect():
        await onebot_client.start()


@app.on_event("shutdown")
async def _stop_onebot():
    await onebot_client.stop()


@app.get("/api/qq/status", dependencies=[Depends(require_auth)])
async def api_qq_status():
    """QQ 通道的连接状态与统计（令牌不回显），供远程连接页用"""
    return onebot_client.state()


@app.get("/api/qq/group-names", dependencies=[Depends(require_auth)])
async def api_qq_group_names(ids: str = Query(default="")):
    """群号 -> 群名，仅用于设置页辅助展示。

    通道未连接直接返回空，前端回退显示号码；单次最多 50 个，接口失败也不报错。
    """
    if not onebot_client.state().get("connected"):
        return {"names": {}, "connected": False}
    items = [x.strip() for x in ids.split(",") if x.strip()][:50]
    if not items:
        return {"names": {}, "connected": True}
    try:
        names = await asyncio.wait_for(onebot_client.group_names(items), timeout=8.0)
    except Exception:
        names = {}
    return {"names": names, "connected": True}


@app.get("/api/qq/logs", dependencies=[Depends(require_auth)])
async def api_qq_logs(limit: int = Query(default=100, ge=1, le=300)):
    """QQ 事件日志：只含事件与计数，不含令牌/消息正文；新的在前"""
    return {"logs": onebot_client.logs(limit), "state": onebot_client.state()}


@app.post("/api/qq/disconnect", dependencies=[Depends(require_auth)])
async def api_qq_disconnect():
    """手动断开 OneBot：只停连接，不改配置（开启状态保持不变）"""
    await onebot_client.stop()
    return {"ok": True, "state": onebot_client.state()}


@app.post("/api/qq/connect", dependencies=[Depends(require_auth)])
async def api_qq_connect():
    """重新连接 OneBot；未启用或配置不完整时明确报错，交给界面提示"""
    state = onebot_client.state()
    if not state.get("enable"):
        raise HTTPException(status_code=400,
                            detail="OneBot 通道未启用（先在设置页打开）")
    await onebot_client.reconnect()
    state = onebot_client.state()
    if state.get("state") == "misconfigured":
        raise HTTPException(status_code=400,
                            detail=state.get("detail") or "OneBot 配置不完整")
    return {"ok": True, "state": state}


# ---------- 语音回放（QQ 语音条留档） ----------

class VoicePlayRequest(BaseModel):
    url: str


# 本机播放进程：并发点击时先停掉上一个，保证不叠音
_voice_proc = None


def _voice_path(url: str) -> Path:
    """把 /api/voice/xxx.wav 解析成文件路径；不在白名单格式一律拒绝"""
    name = str(url or "").rsplit("/", 1)[-1]
    if not VOICE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="语音地址不合法")
    path = paths.voice_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="语音文件不存在")
    return path


@app.get("/api/voice/{name}", dependencies=[Depends(require_auth)])
async def api_voice_file(name: str):
    """语音条原件（wav）；网页走配对 Cookie，插件走本机回环"""
    if not VOICE_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="语音文件不存在")
    path = paths.voice_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="语音文件不存在")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/voice/play", dependencies=[Depends(require_auth)])
async def api_voice_play(req: VoicePlayRequest):
    """QAM / WebKit 播不了 <audio> 时的降级：用后端现有播放器在本机播放"""
    global _voice_proc
    path = _voice_path(req.url)
    player = TTS._player()
    if not player:
        raise HTTPException(status_code=501,
                            detail="没有可用的本机播放器（pw-play / paplay / aplay 都找不到）")
    if _voice_proc is not None and _voice_proc.returncode is None:
        try:
            _voice_proc.terminate()
        except ProcessLookupError:
            pass
    try:
        _voice_proc = await asyncio.create_subprocess_exec(
            player, str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"播放失败：{type(exc).__name__}") from exc
    return {"ok": True, "fallback": "server"}


def _qq_id_list(value) -> list[str]:
    """把数组或「逗号/换行分隔」的文本归一成去重后的 QQ 号列表"""
    return onebot.normalize_ids(value)


def _qq_public_config() -> dict:
    """远程连接页要回填的字段；超时/心跳这类只在配置文件里调"""
    cfg = onebot.parse_config(CFG.get("qq") or {})
    return {
        "enable": cfg.enable,
        "auto_connect": cfg.auto_connect,
        "ws_url": cfg.ws_url,
        # 这个页面本身要已配对或本机回环才能打开，回明文才能「点编辑才可见」；
        # 状态接口始终不回显
        "access_token": cfg.access_token,
        "admin_qq": list(cfg.admin_qq),
        "allow_groups": list(cfg.allow_groups),
        **onebot.guest_prompt_fields(cfg.guest_prompt),
        "guest_prompt_max": onebot.MAX_GUEST_PROMPT,
        "allow_image": cfg.allow_image,
        "max_image_mb": cfg.max_image_mb,
        "image_save_dir": cfg.image_save_dir,
        "image_recent_minutes": cfg.image_recent_minutes,
        "text_log_path": cfg.text_log_path,
        "auto_launch_after_install": cfg.auto_launch_after_install,
        "auto_launch_wait_seconds": cfg.auto_launch_wait_seconds,
        "archive_dir_default": str(onebot.archive_root("")),
        "rate_per_minute": cfg.rate_per_minute,
        "group_rate_per_minute": cfg.group_rate_per_minute,
        "proactive_chance": cfg.proactive_chance,
        "max_message_chars": cfg.max_message_chars,
        "reply_chunk_chars": cfg.reply_chunk_chars,
        "max_reply_chunks": cfg.max_reply_chunks,
        "group_memo": cfg.group_memo,
        "group_memo_size": cfg.group_memo_size,
        "group_memo_ttl": cfg.group_memo_ttl,
        "group_memo_max_chars": cfg.group_memo_max_chars,
        "redact_words": list(cfg.redact_words),
        "redact_mask": cfg.redact_mask,
        "voice_reply": cfg.voice_reply,
        "voice_group_members": cfg.voice_group_members,
        "voice_only": cfg.voice_only,
        "voice_max_seconds": cfg.voice_max_seconds,
        "voice_max_kb": cfg.voice_max_kb,
        "confirm_timeout": cfg.confirm_timeout,
        "prefix": onebot.prefix_for(_qq_nickname()),
    }


@app.get("/api/qq/config", dependencies=[Depends(require_auth)])
async def api_qq_config():
    return _qq_public_config()


@app.post("/api/qq/config", dependencies=[Depends(require_auth)])
async def api_save_qq_config(req: OneBotConfigUpdate):
    """保存 QQ 配置；只改配置与下次启动行为，不顺手触发连接

    写盘统一走 write_config（先校验后原子写）。连接/重连只能由
    /api/qq/connect 或设置页「重新连接」显式触发。
    """
    updates: dict = {}
    if req.enable is not None:
        updates["enable"] = bool(req.enable)
    if req.auto_connect is not None:
        updates["auto_connect"] = bool(req.auto_connect)
    if req.ws_url is not None:
        updates["ws_url"] = req.ws_url.strip()
    if req.access_token is not None:
        updates["access_token"] = req.access_token.strip()
    if req.admin_qq is not None:
        updates["admin_qq"] = _qq_id_list(req.admin_qq)
    if req.allow_groups is not None:
        updates["allow_groups"] = _qq_id_list(req.allow_groups)
    if req.guest_prompt is not None:
        try:
            updates["guest_prompt"] = onebot.normalize_guest_prompt(req.guest_prompt)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.allow_image is not None:
        updates["allow_image"] = bool(req.allow_image)
    if req.max_image_mb is not None:
        updates["max_image_mb"] = max(0.1, float(req.max_image_mb))
    if req.image_save_dir is not None:
        updates["image_save_dir"] = req.image_save_dir.strip()
    if req.image_recent_minutes is not None:
        updates["image_recent_minutes"] = max(0.0, float(req.image_recent_minutes))
    if req.text_log_path is not None:
        updates["text_log_path"] = req.text_log_path.strip()
    if req.auto_launch_after_install is not None:
        updates["auto_launch_after_install"] = bool(req.auto_launch_after_install)
    if req.auto_launch_wait_seconds is not None:
        updates["auto_launch_wait_seconds"] = max(
            0.0, float(req.auto_launch_wait_seconds))
    # 高级项（群常识缓存 / 限频 / 分段 / 脱敏）统一走边界校验
    advanced_raw = {name: getattr(req, name) for name in onebot.ADVANCED_FIELDS
                    if getattr(req, name) is not None}
    advanced, err = onebot.normalize_advanced(advanced_raw)
    if err:
        raise HTTPException(status_code=400, detail=err)
    updates.update(advanced)
    if updates:
        save_config({"qq": updates})
        # 保存只改配置/下次启动行为。关闭通道时断开现有连接；
        # 其余情况一律不自动连接——连接必须由用户显式点击。
        if not onebot_client.state().get("enabled"):
            await onebot_client.stop()
    return {"ok": True, "state": onebot_client.state(), "config": _qq_public_config()}


# 图片上传落在数据根（userdata/uploads），不再写进 server/static
app.mount("/uploads", StaticFiles(directory=str(paths.uploads_dir())), name="uploads")


def _serving_on_steamos() -> bool:
    """服务端是否跑在 SteamOS 上（字体作用域的判定前提之一）。

    只认发行版标记，不用 UA 里的 "Linux" 之类容易误判的字符串：
    PC 上装个 Linux 浏览器不该被当成掌机。
    """
    if sys.platform != "linux":
        return False
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            fields[key.strip()] = val.strip().strip('"')
    return (fields.get("ID", "").lower() == "steamos"
            or "steamos" in fields.get("NAME", "").lower())


SERVING_ON_STEAMOS = _serving_on_steamos()


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def index_page(request: Request):
    """首屏注入运行环境标记：字体在 CSS 里按 data-runtime 切换，不靠 JS 后切。

    - 服务端在 SteamOS，且请求来自本机回环（客户端 / QAM / 掌机浏览器）
      → data-runtime="deck"，保持统一的 Noto 字体；
    - 其余（PC 浏览器经局域网访问、开发机）→ "pc"，恢复改动前的字体。
    """
    client = request.client.host if request.client else ""
    runtime = "deck" if SERVING_ON_STEAMOS and netcfg.is_loopback(client) else "pc"
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<html ", f'<html data-runtime="{runtime}" ', 1)
    return HTMLResponse(html)


# 静态网页控制台（放最后挂载，避免覆盖 API 路由）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
