"""DeckMeido 后端服务：FastAPI + WebSocket 广播 + LLM 对话"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import tomllib

from fastapi import FastAPI, WebSocket, Query, HTTPException, Depends, Response, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .llm import LLMClient
from . import approval
from . import apicfg, audit, audiomute, depcheck, devlog, i18n, media, memory, netcfg, netmode, ocr, onebot, pairing, paths, persona, policy, power, presets, prompts, qqtext, remote_input, selfcheck, singleton, sleepmode, status_line, sudo_auth, tools, updater, usage, wake
from .session_store import store
from . import session_store
from . import agent_engine
from . import ws_hub
from . import overlay_host
from . import routes_config, routes_files, routes_logs, routes_qq, routes_sessions, routes_system
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
    # 旧配置里的 token 键会被忽略：局域网鉴权改用「已配对设备的 Bearer 令牌」。
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
# 定位是「文字转录 + 翻译」而不是「看图说话」：用户拿它翻论文、说明书、游戏外文，
# 要的是原文和译文本身，不需要模型解读画面、猜剧情，也不该因为文字涉及暴力、
# 医学、法律等学术内容就拒译或加免责声明。
DEFAULT_TRANSLATE_TEMPLATE = (
    "把图片里的文字识别出来，先原样抄一遍，再逐句翻译成简体中文。\n"
    "只处理文字：不描述画面、不解释背景、不评价内容，也不要补充图里没有的信息。\n"
    "这是纯文字转录和翻译任务，用于阅读和学习：按原文照实翻，不做内容审查，"
    "不因题材严肃或敏感就省略、软化或拒绝翻译，也不要加提醒和免责声明。\n"
    "已经是中文就原样抄出并说明无需翻译。保持原文的换行、段落和标点；"
    "表格按行列对齐，公式和符号照抄不译。"
)
DEFAULT_TRANSLATE_TEMPLATE_EN = (
    "Recognize the text in the image, copy it out verbatim first, then translate it sentence by sentence into English.\n"
    "Handle text only: do not describe the picture, explain context, or add anything not in the image.\n"
    "This is a plain transcription and translation task for reading and study: translate faithfully, "
    "do not censor, soften, omit, or refuse because the subject matter is serious or sensitive, "
    "and do not add warnings or disclaimers.\n"
    "If the text is already in English, just copy it and say no translation is needed. "
    "Keep the original line breaks, paragraphs and punctuation; align tables by row and column, "
    "and copy formulas and symbols without translating them."
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
# 最近一次请求的 token 用量：由服务商 usage 或字符估算更新。
# 按会话记（sid → 用量）：QQ 频道、后台续跑和网页会话可能同时在跑，
# 一个全局单值会被后结束的那一轮覆盖，界面上的上下文用量就成了别人的数字。
_ctx_used_by_sid: dict[str, int] = {}


def _ctx_get(sid: str = "") -> int:
    """取某个会话最近一轮的用量（不传 sid 就取当前会话）"""
    sid = sid or store.turn.get("sid") or store.current_sid
    return _ctx_used_by_sid.get(sid, 0)


def _ctx_set(sid: str, used: int) -> None:
    """记下某个会话最近一轮的用量；顺手丢掉已经不存在的会话，别让字典无限长"""
    sid = sid or store.turn.get("sid") or store.current_sid
    if not sid:
        return
    if used:
        _ctx_used_by_sid[sid] = int(used)
    else:
        _ctx_used_by_sid.pop(sid, None)
    if len(_ctx_used_by_sid) > 32:
        alive = {store.current_sid, *(store.index().keys())}
        for k in [k for k in _ctx_used_by_sid if k not in alive]:
            _ctx_used_by_sid.pop(k, None)


def _ctx_drop(sid: str) -> None:
    """会话被删掉时把它的用量一起丢掉（QQ 的 sid 是固定算出来的，会复用）"""
    if sid:
        _ctx_used_by_sid.pop(sid, None)


# 后台任务的强引用池。asyncio 只持有 create_task 返回值的弱引用，
# 任务跑完前若没有别的引用，可能被 GC 掉（表现为「有时候那条广播没发出去」
# 或者异常无处可寻）。统一从这里派生，跑完自动移除并记录异常。
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro, label: str = "") -> asyncio.Task:
    """派生一个后台任务并持有到它结束；异常统一落到 devlog，不再静默丢失"""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BG_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            devlog.debug(f"main._spawn.{label or 'task'}", error=repr(exc))

    task.add_done_callback(_done)
    return task


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
    val = _estimate_tokens(_build_llm_msgs_sync(store.history, SYSTEM_PROMPT, sid))
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


def _build_llm_msgs_sync(hist: list[dict], sys_prompt: str, sid: str = "",
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


async def _build_llm_msgs(hist: list[dict], sys_prompt: str, sid: str = "",
                          hist_max: int = 0, item_max_chars: int = 0) -> list[dict]:
    """异步版：只把「等上传文件落盘」这一步放进事件循环外

    _to_vision_content 在图片刚粘上、文件还在写盘时要最多等 1 秒（见 _wait_upload）。
    这段以前是 time.sleep，直接卡住整个事件循环 —— 流式输出、WS 广播、状态轮询
    全都跟着停。其余构造逻辑仍是同步的，放在同一个函数里现读现写，不额外起线程。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    # 先把历史里出现的上传路径都确认一遍（一次并发等，而不是每张各等 1 秒）
    urls = {u for m in hist for u in _UPLOAD_RE.findall(str(m.get("llm") or m.get("content") or ""))}
    if urls:
        await asyncio.gather(*(_wait_upload_async(u) for u in urls))
    return _build_llm_msgs_sync(hist, sys_prompt, sid, hist_max, item_max_chars)


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
# 能直接塞给模型的格式。其它格式（AVIF/HEIF/BMP/TIFF/ICO/SVG…）上游网关多半不认，
# 而浏览器也常有解不了的，统一先用 PIL 转成 JPEG 再发
_LLM_IMG_MIME = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}


def _IMG_MAGIC(blob: bytes) -> str:
    """按文件头认图片格式，认不出返回空串

    粘贴/拖进来的文件常常没有扩展名，或者扩展名和内容对不上（png 改名 jpg），
    只信后缀会把 data:image/xxx 写错，模型端直接 400。
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:2] == b"BM":
        return "bmp"
    if blob[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if blob[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if blob[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    if blob[4:8] == b"ftyp":
        return "heic"
    return ""


def _image_part(blob: bytes, suffix: str = "") -> dict | None:
    """把一份图片字节变成多模态 image_url 部件

    统一在这里做三件事：认格式（不靠扩展名猜，改看文件头）、超过软门槛压成 JPEG、
    上游不认的格式（AVIF/HEIF/BMP/TIFF/ICO…）转成 JPEG。
    返回值直接可塞进 content 列表；真读不出来（损坏、PIL 没装）返回 None。
    """
    if not blob:
        return None
    magic = _IMG_MAGIC(blob)          # 文件头认出的格式，认不出为空
    declared = (suffix or "").lower().lstrip(".").replace("jpg", "jpeg")
    # 文件头优先：粘贴进来的图常常没有扩展名，或者扩展名和内容对不上
    fmt = magic or declared
    # PNG/JPEG/WEBP/GIF 原样发；其余（AVIF/HEIF/BMP/TIFF/ICO…）先转 JPEG
    native = fmt in _LLM_IMG_MIME
    if native and not magic:
        # 扩展名说认得、文件头却认不出：多半是损坏或假扩展名，发出去上游会 400
        return None
    oversize = len(blob) > _IMG_SOFT_BYTES
    # 格式不原生、或图太大（历史重发时省流量）都过一次 JPEG 转码
    if not native or oversize:
        packed = _jpeg_for_llm(blob)
        if packed is not blob:
            return {"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,"
                                         + base64.b64encode(packed).decode()}}
        # 压不小（小图转 JPEG 反而更大）→ 原生格式原样发；不原生只能放弃
        if not native:
            return None
    return {"type": "image_url",
            "image_url": {"url": f"data:image/{fmt};base64,{base64.b64encode(blob).decode()}"}}


def _read_image_part(fp: Path) -> dict | None:
    """按路径读一张图并转成 image_url 部件（上传图片 / 工作区引用图片共用）"""
    try:
        blob = fp.read_bytes()
    except OSError:
        return None
    return _image_part(blob, fp.suffix)


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

# 智能音箱模式：常驻唤醒检测在后台跑，默认开启（[wake] enabled=true）。
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


def config_prefs() -> dict:
    """所有端共用的偏好项（网页/客户端/插件/大屏都用同一份口径）

    /api/config 全量回显给设置页，/api/status 也带上一份：面板、大屏这类
    只轮询状态的端据此显示"当前设置"，不用每次都去拉整份配置。
    网页改了设置以后由 POST /api/config 广播 config_changed，
    各端收到就重拉，避免"网页已经是自动审批、面板还显示手动审批"。
    """
    cfg_agent = CFG.get("agent") or {}
    stt_cfg = CFG.get("stt") or {}
    return {
        # 审批模式：manual / auto / custom / full（自定义档的四组开关一起给）
        "permission_mode": PERMISSION_MODE,
        "permission_custom": dict(CUSTOM_PERM),
        # 带画面询问：有效值 + 分项原始值（大屏按原始值回显开关）
        "with_shot": chat_voice_enabled() and bool(cfg_agent.get("with_shot", True)),
        "with_shot_pref": bool(cfg_agent.get("with_shot", True)),
        # 语音输入 / 录音时静音外放
        "stt_enable": stt.enable,
        "stt_enable_pref": bool(stt_cfg.get("enable", True)),
        "stt_duck_output": audiomute.enabled(),
        # 「对话与语音」整组总开关
        "chat_voice": chat_voice_enabled(),
        "wake_enabled": wake_service.enabled,
        "ui_language": UI_LANG,
        "reasoning_effort": llm.reasoning_effort,
    }


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


def _set_session_origin(sid: str, client: str | None) -> None:
    """按发起端给会话打来源标记（侧栏分组用）

    client 为空（重试这类内部链路）或认不出来时不动：沿用这个会话原来的
    来源，否则一点「重试」就把分组刷成「其它」。
    """
    if not sid or not client:
        return
    o = session_store.normalize_origin(client, sid)
    if o:
        with store.lock:
            store.origin_map[sid] = o


def _set_session_device(sid: str, device: str | None) -> None:
    """记下这个会话是哪台设备发起的（排障用，不参与分组）

    只认第一次：会话里换了端接着说，不把归属改写成后来那台机器 ——
    否则换台电脑发一句话，原会话的归属就漂走了，排查时反倒看不出是谁建的。
    """
    store.set_session_device(sid, (device or "").strip() or None)


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
    except Exception as exc:
        devlog.debug("main.refresh_stream_parts", error=repr(exc))
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
    _spawn(manager.broadcast(
        {"type": "delta", "text": text, "final": False, **tag}), "flush_delta")


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
    _spawn(manager.broadcast(
        {"type": "thought_delta", "text": text, **st["tag"]}), "flush_thought")


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
        _spawn(manager.broadcast(
            {"type": "thought_delta", "text": text, **st["tag"]}), "flush_thought_now")


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
        _spawn(manager.broadcast(
            {"type": "delta", "text": text, "final": False, **st["tag"]}), "flush_delta_now")


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


_UPLOAD_RE = re.compile(
    r"!\[[^\]]*\]\((/uploads/[^\s)]+?\.(?:png|jpe?g|webp|gif|bmp|avif|heic|heif|tiff?|ico))\)",
    re.I)
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


def _upload_path(url: str) -> Path | None:
    """把 /uploads/xxx 这类链接还原成磁盘路径，找不到返回 None

    主位置是 userdata/uploads/（新版上传全落这里，也由 /uploads 静态挂载）；
    再兜一下 static/uploads/——老版本上传的图躺在那儿，历史消息里的链接还在，
    只认新目录的话那些老图会永远显示「未能附上」。
    """
    name = Path(url).name
    if not name:
        return None
    for base in (paths.uploads_dir(), STATIC_DIR / "uploads"):
        fp = base / name
        if fp.is_file():
            return fp
    return None


def _ocr_text(fp: Path) -> str:
    """本机离线 OCR：认出图里的文字，失败或没字返回空串

    只在模型看不到图时才用（纯文本模型 / 图片读不出来），
    所以这里吞掉异常，只记日志——OCR 是兜底，不能反过来把整轮对话搞挂。
    """
    try:
        text = ocr.OCR_ENGINE.recognize(fp)
    except Exception as exc:
        devlog.log("ocr_failed", path=str(fp), error=str(exc)[:200])
        return ""
    devlog.log("ocr_done", path=str(fp), chars=len(text),
               elapsed=round(ocr.OCR_ENGINE.last_elapsed, 2))
    return text


def _wait_upload(url: str, tries: int = 3, wait: float = 0.3) -> Path | None:
    """等上传文件落盘：粘图后立刻回车时，消息可能比写盘早到几百毫秒

    以前这种竞态只能退化成「图片未能附上」，用户看到的就是「明明传了却看不到图」。
    总等待不超过 1 秒，超出仍按缺失处理。
    """
    fp = _upload_path(url)
    for _ in range(tries):
        if fp:
            return fp
        time.sleep(wait)
        fp = _upload_path(url)
    return fp


async def _wait_upload_async(url: str, tries: int = 3, wait: float = 0.3) -> Path | None:
    """_wait_upload 的异步版：等待期间让出事件循环

    聊天链路（async）一律用这个；线程里跑的同步代码仍可用上面那个。
    """
    fp = _upload_path(url)
    for _ in range(tries):
        if fp:
            return fp
        await asyncio.sleep(wait)
        fp = _upload_path(url)
    return fp


def _to_vision_content(text: str):
    """消息里含上传图片引用时转成多模态 content（供 vision 模型看图）

    上传的图落在 userdata/uploads/，由 /uploads 静态挂载给浏览器看；
    不能拿 STATIC_DIR 去拼，否则文件永远找不到、模型只收到一行「![图片](...)」。
    格式不做限制：AVIF/HEIF/BMP 这类上游不认的，在 _read_image_part 里转成 JPEG。
    模型不支持视觉时不再走图片部件，改为本地 OCR 出文字附上（见 _blind_ocr_content）。
    """
    matches = _UPLOAD_RE.findall(text)
    if not matches:
        return text
    if not llm.supports_vision:
        return _blind_ocr_content(text, matches)
    parts = []
    lost = 0
    for url in matches:
        fp = _upload_path(url)
        if not fp:
            devlog.log("upload_missing", path=url)
            lost += 1
            continue
        part = _read_image_part(fp)
        if not part:
            # 区分「文件不在」和「文件在但读不出图」：后者多半是格式怪或内容损坏。
            # 统计与文件头都现读，读不到（刚被删/被清）就报 unknown，不在这里再抛
            size = magic = 0
            try:
                size = fp.stat().st_size
                with fp.open("rb") as fh:
                    magic = _IMG_MAGIC(fh.read(16))
            except OSError:
                pass
            devlog.log("image_bad_format", path=str(fp), suffix=fp.suffix,
                       size=size, magic=magic or "unknown")
            lost += 1
            continue
        parts.append(part)
    if not parts:
        # 图一张都没读出来：不能只把原文还给模型——它会以为图在别处，
        # 转头去 find 磁盘、装 OCR 工具乃至凭文件名瞎猜画面内容。
        # 先试一次本地 OCR，认出来就当好文本给它；认不出再明确说没收到图
        raw = "\n\n".join(t for t in (
            _ocr_text(fp) for fp in (_wait_upload(u) for u in matches) if fp
        ) if t.strip())
        if raw.strip():
            return prompts.part("image_ocr").format(text=raw.strip())
        devlog.log("ocr_skip", paths=len(matches), reason="no_text")
        return prompts.part("image_missing").format(text=_UPLOAD_RE.sub("[图片]", text))
    if lost:
        devlog.log("upload_partial", lost=lost, total=len(matches))
        parts.append({"type": "text", "text": prompts.part("image_partial")})
    parts.append({"type": "text", "text": _UPLOAD_RE.sub("[图片]", text)})
    return parts


def _blind_ocr_content(text: str, matches: list[str]) -> str:
    """纯文本模型看到带图的消息：本机 OCR 出文字，拼成纯文本交给它

    这一步是「翻译图片文字」在 DeepSeek 这类模型上唯一能走通的路：
    直接塞 image_url 要么 400、要么被静默丢掉。OCR 认不出字时退回
    「图片未能附上」，同样明确劝阻它翻磁盘。
    """
    blocks = []
    for url in matches:
        fp = _wait_upload(url)
        if not fp:
            devlog.log("ocr_skip", path=url, reason="upload_missing")
            continue
        raw = _ocr_text(fp)
        if raw.strip():
            blocks.append(raw.strip())
        else:
            devlog.log("ocr_skip", path=str(fp), reason="no_text")
    body = _UPLOAD_RE.sub("[图片]", text)
    if not blocks:
        return prompts.part("image_missing").format(text=body)
    return prompts.part("image_ocr").format(text="\n\n".join(blocks)) + "\n\n" + body


def _blindify_images(msgs: list[dict]) -> None:
    """纯文本模型这一轮不看图：把每条消息里的图片部件就地换成一句说明

    历史里 _build_llm_msgs 已经把旧图表过一遍，但本轮新展开的那张（用户刚上传、
    刚引用的图）还是真的 image_url。纯文本模型收到它有两种坏结果：上游直接 400，
    或者悄悄丢掉图、模型只看到一行「![图片]」，于是自己去找文件、装 OCR 猜内容。
    换成明确的「看不到，告诉用户」文案，比让它瞎猜好。
    """
    note = {"type": "text", "text": prompts.part("image_blind")}
    for m in msgs:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        if not any(isinstance(p, dict) and p.get("type") == "image_url"
                   for p in content):
            continue
        m["content"] = [note if isinstance(p, dict) and p.get("type") == "image_url"
                        else p for p in content]


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
        part = _read_image_part(fp)
        if not part:
            devlog.log("img_ref_unreadable", path=str(fp))
            continue
        parts.append(part)
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
    # index.json 是侧栏索引，*.side.json 是流式正文的副档案：
    # 都不是会话文件，混进来的话会被当成坏文件反复改名
    files = session_store.all_session_files()
    # 没有 id 的会话文件（典型是 sid 为空时写出的 "sessions/.json"）是历史遗留
    # 脏数据：它既不属于任何会话，也没法在侧栏点开，留着只会在「恢复会话」时
    # 被当成一个无来源的空会话。启动时顺手清掉。
    # 注意 Path(".json").stem 在 Python 里是 ".json" 而不是空串（点开头算隐藏文件名），
    # 所以两种写法都要判
    keep: list[Path] = []
    for f in files:
        if f.stem and f.stem != ".json":
            keep.append(f)
            continue
        try:
            f.unlink()
            print(f"[DeckMeido] 已清理无 id 的会话文件 {f.name}", file=sys.stderr)
        except OSError as e:
            print(f"[DeckMeido] 清理无 id 的会话文件 {f.name} 失败：{e}", file=sys.stderr)
    files = keep
    # 一个会话文件都没有：和上面「全部不可用」一样，只在内存里开一个新会话。
    # 这里以前会立刻 save_current_session()，于是每次启动都留下一条空会话
    # （id 还是固定的 "default"，多了会撞名），侧栏就全是点进去空空的「新会话」
    if not files:
        store.current_sid = _fresh_sid()
        store.history = []
        _ctx_set(store.current_sid, 0)
        return

    # 逐个试：以前只认 mtime 最新的那一个，它坏了就 history.clear() 整个重来，
    # 几十轮历史当场归零。现在坏一个就跳过试下一个，全坏才开新会话。
    # 注意要排除「空会话文件」：空文件不是损坏，但拿它当恢复结果同样等于清空，
    # 所以空 history 的文件也当作不合格，继续往旧文件里找。
    def _restore(data: dict, src: Path) -> bool:
        """从一个会话文件恢复出完整状态；成功返回 True"""
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
            hist = store.history
            retry = ""
            for m in reversed(hist):
                if m.get("role") == "user":
                    retry = str(m.get("content") or m.get("brief") or "")
                    break
            note = "\n\n（以上回答被中断，已保留屏幕上出现过的全部内容）"
            store.history.append({"role": "assistant",
                                  "content": (partial_text.rstrip() + note) if partial_text else note.strip(),
                                  "events": evs, "interrupted": True,
                                  "retry_text": retry,
                                  "ts": (partial.get("ts") if isinstance(partial, dict) else None) or time.time()})
            print(f"[DeckMeido] 已恢复被中断的回答（{len(partial_text)} 字，{len(evs)} 个事件）",
                  file=sys.stderr)
        _ctx_set(store.current_sid, _estimate_tokens(
            _build_llm_msgs_sync(store.history, SYSTEM_PROMPT, store.current_sid)))
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
    _ctx_set(store.current_sid, 0)
    # 只在内存里建，不落盘也不进索引：这个会话一句都没说过，写出去就是一条空会话，
    # 而后端每次重启都会再建一条 —— 用户看到的就是侧栏里越堆越多、点进去全空的
    # 「新会话 xx:xx」。等它真说了第一句话，正常落盘路径会带上来源和标题写出去。


_init_sessions = init_sessions
# 侧栏索引要先建好：/api/sessions 只读它，不建就是空列表。
# 放在 init_sessions 之前：恢复会话时写的那个文件会顺带把索引补齐一次
store.load_index()
_init_sessions()

# 待确认的危险操作 {name, args, description, sid, epoch, turn_id, client, device}
# sid/epoch/client/device 是「谁在什么会话里提的」：确认只认原会话原来源，
# 否则任何连上的客户端随便一句「好」就能放行别人发起的危险命令
pending_action: dict | None = None


def _pending_get() -> dict | None:
    """读挂起的危险操作。

    只做「取一次快照」：调用方拿到后自己判断是否匹配，别在读和用之间再读第二次，
    否则两轮对话并发时可能读到别人刚挂上的那一条。
    """
    return pending_action


def _pending_set(p: dict | None) -> None:
    """挂上/撤销待确认操作（传 None 撤销）"""
    global pending_action
    pending_action = p


def _pending_claim(p: dict) -> bool:
    """认领一条待确认操作：只有当前挂着的正好是这一条时才清掉

    以前是 `action, pending_action = pending_action, None` —— 两轮对话同时确认时，
    后到的那个会先把 pending 清掉，先到的那个还拿着旧引用照常执行，
    等于同一个危险操作被执行两次。这里用身份比对保证只有一个能认领成功。
    """
    global pending_action
    if pending_action is not p:
        return False
    pending_action = None
    return True



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

# 连接集合与广播在 ws_hub；这里保留 manager 这个名字，调用点不用改
manager = ws_hub.manager


def _wake_notify(status: dict):
    """唤醒状态机变化：广播给所有页面（眼睛页/设置页都靠它即时刷新）"""
    return manager.broadcast({"type": "wake_state", "data": status})


wake_service.set_handlers(on_state=_wake_notify)


remote_input.manager.set_hook(ws_hub._broadcast_remote_input)


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


def _bearer_token(request: Request) -> str:
    """请求头里的设备令牌（Authorization: Bearer <token>）。

    不再用 Cookie：客户端（WebView / 网页）自己把令牌放在请求头里，
    后端只按哈希比对。请求头不会被浏览器自动携带，也就不存在 CSRF。
    """
    raw = (request.headers.get("authorization") or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _auth_device(request: Request) -> dict | None:
    token = _bearer_token(request)
    if not token and request.method in ("GET", "HEAD"):
        # 只读资源（<img>/<a>/<audio> 打不开自定义请求头）可以把令牌放进查询参数
        token = (request.query_params.get("token") or "").strip()
    return pairing.manager.devices.authenticate(token)


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


# 总闸挂在整个 app 上，这几条必须未配对可用：首页与静态资源（否则连配对
# 页面都打不开）、启动时问登录状态的 auth/session、鉴权页显示的版本号、
# 界面文案，以及配对流程本身（未配对设备只有这些接口能访问）。
AUTH_PUBLIC_PATHS = frozenset({
    "/api/auth/session", "/api/version", "/api/i18n", "/api/pair/waiting",
})
AUTH_PUBLIC_PREFIXES = ("/api/pair/",)
# 首页与静态资源不走路由，用 URL 前缀判断（挂载点不会进 request.url.path）
AUTH_PUBLIC_URL_PREFIXES = ("/static/", "/uploads/", "/files/")


def _auth_public(request: Request) -> bool:
    path = request.url.path
    if path in AUTH_PUBLIC_PATHS or path.startswith(AUTH_PUBLIC_PREFIXES):
        return True
    return path == "/" or path.startswith(AUTH_PUBLIC_URL_PREFIXES)


def require_auth(request: Request):
    """接口总闸：本机回环/本机网卡来源免验证，局域网必须带已配对设备令牌。

    令牌由客户端放在 Authorization 请求头里（不用 Cookie），静态页面本身
    不设防，未配对时前端会弹 PIN 配对，受保护接口统一 401。
    """
    if _auth_public(request):
        return
    ip = _client_ip(request)
    if netcfg.is_this_host(ip):
        return
    if _auth_device(request):
        return
    _reject_unauthorized(ip, "api", request.url.path)


# require_auth 由「整组 router.dependencies」挂载，FastAPI 注册时会重新解析它的注解。
# main.py 启用了 from __future__ import annotations，注解在源码里是字符串；一旦
# 求值失败（Python 3.14 下会退回 annotationlib.Format.FORWARDREF，get_typed_annotation
# 只处理 str），request 就会被当成必填查询参数，调用时缺参 →
# TypeError: require_auth() missing 1 required positional argument: 'request'。
# 这里把已求值的类型对象直接写回 __annotations__，让注册时不再做字符串求值。
require_auth.__annotations__ = {"request": Request}


def _ws_authorized(ws: WebSocket) -> bool:
    # 掌机自己（含通过本机局域网 IP 访问的启动器 WebView）直接放行；
    # 局域网设备从 ?token= 带设备令牌（WebSocket 不能自定义请求头）
    if netcfg.is_this_host(ws.client.host if ws.client else ""):
        return True
    token = ws.query_params.get("token") or (ws.headers.get("authorization") or "")
    return bool(pairing.manager.devices.authenticate(token))


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
    used = _ctx_get() or _cached_estimate()
    pct = round(min(used / win, 1.0) * 100, 1) if win else 0
    # 按游戏提示词预设：游戏是哪款由插件探测后上报，后端不再重复实现一套探测
    preset_state = presets.active_view()
    return {
        "battery": int(battery) if battery.isdigit() else None,
        "charging": charging or None,
        "load": load or None,
        "mode": "gaming" if gaming else "desktop",
        # 后端是否正在跑一轮对话：前端重连后据此恢复 ⏹ 按钮状态，
        # 避免断联时正在跑的那轮看起来像「已经停了」
        "running": _chat_busy(),
        # 本轮开始的时间戳（没有在跑则为 0）：前端据此显示「已等 N 秒」，
        # 工具链跑得久时也能看出后端还活着，而不是一屏静默的「没反应」
        "running_since": (store.turn.get("started") or 0) if _chat_busy() else 0,
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
        # 语音合成实际用哪个引擎、为什么降级：tts_engine / tts_ready / tts_error
        **tts.summary(),
        # 各端共用的偏好项（审批模式、带画面、语音输入等）：面板/大屏只轮询
        # 状态也能拿到真实设置，不会出现「网页已是自动审批、面板还显示手动」
        **config_prefs(),
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
        busy=_chat_busy(),
        configured=llm.configured,
        chat=lambda messages: llm.chat_with_tools_stream(
            messages, [], max_tokens=status_line.MAX_TOKENS, temperature=0.3),
    )


# ---------- API ----------

app = FastAPI(title="DeckMeido")

# 分包路由：在 app 创建后注入 main 的鉴权闸门与 _client_ip，再挂载
routes_logs.init(require_auth, _client_ip)
app.include_router(routes_logs.router)


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """页面本身禁止缓存。

    index.html 是全局唯一的前端入口，改完必须立刻生效。默认 StaticFiles 只带
    ETag + Last-Modified，而 QtWebEngine（原生客户端）和部分浏览器会拿旧副本，
    导致改完代码后客户端里还是老界面，看着像"改动没生效"。
    CSS/JS 也一起走 no-cache（只带 ETag 校验，内容没变仍是 304）：
    否则 HTML 更新了、样式还是旧副本，界面会错位。
    """
    resp = await call_next(request)
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype:
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    elif any(t in ctype for t in ("text/css", "javascript")):
        # 每次都回源问一句：没变就 304，变了立刻拿新的
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def client_from_ua(ua: str) -> str:
    """按 UA 猜发起端：手机浏览器算 mobile，其余都算 web

    ws 连接和 /api/sessions/create 共用这一份判断，两处口径必须一致：
    同一台手机「说话时自动建的会话」和「点＋建的会话」要落进同一个分组。
    插件（qam）和桌面客户端（client）不靠 UA，由调用方显式声明。
    """
    ua = ua or ""
    return "mobile" if ("Mobile" in ua or "Android" in ua) else "web"


@app.middleware("http")
async def _lan_guard(request, call_next):
    """lan_enable 关闭时拒绝非本机来源。

    正常启动会直接绑定 127.0.0.1；这里再兜一层，防止旧快捷方式 / 手写的
    systemd 单元仍然用 0.0.0.0 启动时把局域网暴露出去。
    """
    server_cfg = CFG.get("server", {})
    ip = request.client.host if request.client else ""
    if not netcfg.lan_enabled(server_cfg) and not netcfg.is_this_host(ip):
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
        except Exception as exc:
            devlog.debug("main._run_tool_busy", error=repr(exc))
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
    另一个用途：AVIF/HEIF/BMP/TIFF 这类上游网关不认的格式，也借这里转成 JPEG。
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
        # 压完反而更大（本来就小的图）就保留原样；转码格式时不能走这条路，
        # 所以调用方拿返回值是否 is 原字节判断「转码失败」
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


# ---------- 文件与终端接口 ----------
# 上传/浏览/预览/文件操作/终端（含 /ws/terminal）都在 routes_files：这里保留同名引用，
# 其它模块（含测试）按 main.xxx 的既有写法不用改
_FsReq = routes_files._FsReq
_IMG_EXT = routes_files._IMG_EXT
_TEXT_EXT = routes_files._TEXT_EXT
_UPLOAD_MAX = routes_files._UPLOAD_MAX
UPLOAD_FREE_RESERVE = routes_files.UPLOAD_FREE_RESERVE
_fmt_bytes = routes_files._fmt_bytes
# 上传上限的实现在 routes_files，但入口要在这里：测试/调用方认这个名字，
# 换成整份替换（mock.patch.object(main, "upload_limits")）时路由侧也能读到
def upload_limits(path) -> dict:
    """目标目录所在磁盘的可用空间与单文件上限（唯一判定口径）"""
    return routes_files.upload_limits(path)

upload_limits._routes_files_delegate = True

_upload_limit_error = routes_files._upload_limit_error
_upload_target_dir = routes_files._upload_target_dir
_extract_text = routes_files._extract_text
_file_kind = routes_files._file_kind
_fs_workspace_roots = routes_files._fs_workspace_roots
_fs_workspace_roots._routes_files_delegate = True
_fs_denied_response = routes_files._fs_denied_response
_workspace_file = routes_files._workspace_file
# 路由函数在 routes_files.register(app) 时才定义，末尾统一补上
api_upload = None
api_upload_to = None
api_upload_limit = None
api_upload_stream = None
api_files = None
api_preview = None
api_browse = None
api_fs = None
api_terminal = None
api_terminal_stop = None
api_terminal_run = None
api_workspace_file = None
api_file_stat_batch = None
api_zip = None
api_file_stat = None



# 状态接口（/api/status*、/api/net/probe）在 server/routes_system.py


# 当前正在处理的对话任务（用于终止按钮）
current_task: asyncio.Task | None = None
# 启动/收尾时串行化：以前是「先读 current_task 判断，再赋值」，
# 两个请求同时进来（网页点发送 + QQ 推消息）都能通过判断，
# 后赋值的那个把前一个任务的引用覆盖掉 —— 前一轮从此取消不掉，
# 还会和新的那一轮同时改 store.history。
_chat_start_lock = asyncio.Lock()


def _chat_busy() -> bool:
    """是否有一轮对话在跑（供 /status、状态行、重试等只读判断用）"""
    t = current_task
    return t is not None and not t.done()


async def start_chat(text: str, announce: bool = True, **kwargs):
    """把一轮对话放到可取消的任务里执行。announce=False 时不重复广播用户消息（重试用）

    **kwargs 原样转给 handle_chat（图片、工作区根），插件那条路要用。
    """
    global current_task
    async with _chat_start_lock:
        # 同一时刻只允许一轮：上一轮没结束就不抢跑，否则引用被覆盖、
        # 两轮同时写同一个 store.history，最后落盘的会话是两次回答的混合体
        if _chat_busy():
            return {"reply": "上一轮还没结束，请等它跑完或先点终止", "busy": True,
                    "events": []}
        current_task = asyncio.create_task(handle_chat(text, announce, **kwargs))
    task = current_task
    try:
        return await task
    except asyncio.CancelledError:
        return {"reply": "已终止当前操作", "events": [], "cancelled": True}
    finally:
        async with _chat_start_lock:
            # 只清理自己登记的那一个：理论上不会出现被别人覆盖的情况
            # （上面已禁止并发启动），这里再比对一次身份，避免以后改动打破前提
            if current_task is task:
                current_task = None
        # 收尾必须补一条 running=false。插件（大屏 / QAM）只吃 ws 推送、不轮询
        # HTTP，而 run_agent 中途那几次 status 广播都发生在 current_task 置空之前，
        # 少了这一条，最后一次「running=true」会永久挂在面板上——表现就是
        # 网页端这一轮早就结束，插件面板一打开却立刻显示「思考中…」。
        # 成功、异常、取消（含用户点终止）都会走到这里，三种收尾口径一致。
        try:
            # 必须绕开 TTL 缓存：这一条是「本轮已结束」的收尾广播，
            # 吃到之前的缓存就会把 running=true 再挂一轮
            await asyncio.shield(manager.broadcast(
                {"type": "status", "data": await status_async(force=True)}))
        except Exception as exc:
            devlog.debug("main.start_chat", error=repr(exc))
            pass


def _wake_cancel_chat() -> None:
    """唤醒轮次的终止：start_chat 内部的 handle_chat 任务不会被外层取消连带，
    这里显式取消登记在案的 current_task。"""
    task = current_task
    if task is not None and not task.done():
        task.cancel()


wake_service.set_handlers(chat=start_chat, cancel_chat=_wake_cancel_chat)


# ---------- 本文件内的接口统一鉴权 ----------
# 下面这批路由直接挂在 app 上（与已拆出的 routes_* 模块不同，它们各自在建
# router 时挂了一次依赖）。这里同样只挂一次：不再在每条路由上重复写
# dependencies=[Depends(require_auth)]。
app.router.dependencies.append(Depends(require_auth))


@app.post("/api/cancel")
async def api_cancel(request: Request, client: str = "", device: str = ""):
    """终止当前这一轮，并把「谁点的」记进日志。

    以前这里只留一行 `200 OK`，于是出现「这一轮莫名其妙被终止」时，
    只能去背调哪个浏览器标签页在发请求（会话 #65e2b8 里就为此猜错了两轮）。
    现在把调用方的 client/device 和来源 IP 一起打出来，插件、客户端、
    网页各自带自己的标识，对不上就说明是别人的页面在点。
    """
    who = f"client={client or '?'} device={device or '?'} ip={request.client.host if request.client else '?'}"
    if _chat_busy():
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


class TTSPreviewRequest(BaseModel):
    # 试听文本；留空用一句内置短句
    text: str = ""


class TTSSayRequest(BaseModel):
    # 手机端要播报的文本；长度上限在接口里再夹一次
    text: str
    language: str = ""


class PairConfirmRequest(BaseModel):
    pair_id: str
    pin: str
    # 保持登录：勾选时设备令牌按 30 天有效期提示给客户端，否则浏览器关掉即视为可丢弃
    remember: bool = True
    # 计算机名（可选）：浏览器读不到系统主机名，由前端从 WebUI 设置带上；
    # 没有就用 UA 推断的代号兜底
    name: str = ""


class PairDecisionRequest(BaseModel):
    pair_id: str
    allow: bool


class PairIdRequest(BaseModel):
    pair_id: str


# QQ 配置请求体与路由同在 routes_qq
OneBotConfigUpdate = routes_qq.OneBotConfigUpdate


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


# 配置接口（GET/POST /api/config）与请求模型在 server/routes_config.py


@app.get("/api/memory")
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


@app.post("/api/memory")
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


@app.delete("/api/memory/{mid}")
async def api_delete_memory(mid: str):
    if not memory.remove(mid):
        raise HTTPException(status_code=404, detail="这条记忆已不存在")
    return {"ok": True}


@app.delete("/api/memory")
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

@app.get("/api/soul/export")
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


@app.post("/api/soul/import")
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


# ---------- 设备配对 ----------
# 浏览器侧：start → 等掌机允许 → confirm（PIN）→ 拿到设备令牌（自己存本地）。
# 掌机侧：pending / refresh / decision 三个接口都要求本机来源或已配对令牌。

def _pair_http(exc: pairing.PairError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@app.get("/api/auth/session")
async def api_auth_session(request: Request):
    """未登录也能查：当前是不是已经凭设备令牌或本机来源登录。

    网页打开时先问一次，已配对就直接进主界面；未配对则弹 PIN 配对。
    """
    local = netcfg.is_this_host(_client_ip(request))
    device = None if local else _auth_device(request)
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


# 日志与诊断接口（/api/logs*、/api/debug/render）在 server/routes_logs.py


@app.get("/api/usage")
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


@app.get("/api/usage/balance")
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


@app.get("/api/usage/export")
async def api_usage_export():
    """导出按日聚合的原始 JSON（浏览器直接下载）"""
    data = await asyncio.to_thread(usage.export_data)
    body = json.dumps(data, ensure_ascii=False, indent=1)
    name = "deckmeido-usage-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/usage/clear")
async def api_usage_clear(request: Request):
    """清空本地用量统计（不碰配置、会话与余额）"""
    removed = await asyncio.to_thread(usage.clear)
    audit.record("config", "usage_clear", "ok", ip=_client_ip(request),
                 detail=str(removed))
    return {"ok": True, "removed": removed}


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
async def api_pair_confirm(req: PairConfirmRequest, request: Request):
    """提交 PIN：一次性、60 秒有效、最多 3 次；成功后把设备令牌回给浏览器自己保存。"""
    ip = _client_ip(request)
    try:
        # 浏览器在开始配对时就把计算机名报上来了（浏览器拿不到系统主机名，
        # 只能读 WebUI 设置或让用户填），这里用它替换 UA 推断出来的
        # 「Chrome / Windows」这种代号，设备列表里才认得出是哪台机器
        if req.name:
            pairing.manager.set_name(req.pair_id, req.name)
        device, token = pairing.manager.confirm(req.pair_id, req.pin, ip)
    except pairing.PairError as exc:
        # 不按 IP 计数：未允许 / 已过期 / 已失效都是正常流程状态。
        # 输错 PIN 的上限由单次配对会话自己管（3 次作废）。
        raise _pair_http(exc) from exc
    return {"ok": True, "device": device, "token": token,
            "remember": bool(req.remember), "device_days": _device_days()}


@app.get("/api/pair/waiting")
async def api_pair_waiting():
    """掌机 QAM 用来「主动提醒」的轻量探测：只回待配对数量。

    收不到这台掌机外的任何信息（不带 PIN / IP / UA，也不含设备列表），
    所以不需要鉴权：QAM 在非设置页签、甚至面板没打开时也能每几秒问一次，
    发现有新请求再提示主人去设置页看 PIN。
    """
    return {"ok": True, "count": len(pairing.manager.pending())}


@app.get("/api/pair/pending")
async def api_pair_pending(history: bool = Query(False)):
    """掌机设置页：待配对设备 + PIN + 倒计时。

    history=1 时连同最近 5 分钟内已结束的会话一起返回，让设置页能显示
    「已允许 / 已拒绝」而不是点完就消失。插件与计数仍用默认的进行中列表。
    """
    sessions = pairing.manager.pending(include_finished=bool(history))
    return {"ok": True, "sessions": sessions, "device_days": _device_days()}


@app.post("/api/pair/refresh")
async def api_pair_refresh(req: PairIdRequest):
    try:
        return {"ok": True, **pairing.manager.refresh(req.pair_id)}
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc


@app.post("/api/pair/decision")
async def api_pair_decision(req: PairDecisionRequest, request: Request):
    try:
        return {"ok": True, **pairing.manager.decide(
            req.pair_id, bool(req.allow), _client_ip(request))}
    except pairing.PairError as exc:
        raise _pair_http(exc) from exc


# 设备接口（/api/devices*）与 DeviceRenameRequest / DeviceIdRequest 在 server/routes_system.py


# ---------- 登录与配对审计 ----------

@app.get("/api/audit")
async def api_audit(limit: int = Query(default=50, ge=1, le=audit.MAX_EVENTS)):
    """只读审计：最近若干条鉴权失败 / 配对尝试，不包含 PIN 或设备令牌。"""
    return {"ok": True, "events": audit.recent(limit)}


@app.post("/api/tts")
async def api_set_tts(enabled: bool = Query(...)):
    """只切语音播报开关：给 Decky 插件面板用

    QAM 里没法像网页设置页那样填一整张表单，只带一个参数，走的还是
    update_config 同一条热更新路径（tts.enable 立即生效，不用重启）。
    """
    save_config({"tts": {"enable": bool(enabled)}})
    return {"ok": True, "tts_enable": tts.enable}


@app.get("/api/tts")
async def api_tts_status():
    """语音合成状态：引擎是否可用、模型是否已下载、下载进度、署名信息。"""
    return {"ok": True, **tts.status()}


@app.post("/api/tts/download")
async def api_tts_download():
    """后台下载语音模型（约 38MB）；重复点击不会开第二个线程。"""
    # 离线模式：模型必须联网下载，直接返回原因，不启动下载线程
    if netmode.enabled():
        raise HTTPException(status_code=503, detail=netmode.reason("语音模型下载"))
    return {"ok": True, **tts.start_download()}


@app.post("/api/tts/download/cancel")
async def api_tts_download_cancel():
    return {"ok": True, **tts.request_cancel()}


@app.get("/api/selfcheck")
async def api_selfcheck():
    """桌面控件自检的可补装组件清单 + 当前安装任务"""
    return {"ok": True, **selfcheck.snapshot()}


@app.post("/api/selfcheck/install")
async def api_selfcheck_install(component: str = Query(...)):
    """后台线程安装单个组件；同一时间只允许一个任务（重复点击返回 busy）"""
    loop = asyncio.get_running_loop()

    def hook(payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "selfcheck_progress", **payload}), loop)
        except Exception as exc:
            devlog.debug("main.hook", error=repr(exc))
            pass

    return selfcheck.start(component, hook)


class SelfcheckPasswordBody(BaseModel):
    component: str
    password: str


@app.post("/api/selfcheck/password")
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


@app.get("/api/sudo/status")
async def api_sudo_status():
    """当前 sudo 授权状态：是否可用、还要多久失效、是否有待输入密码的请求"""
    st = sudo_auth.cache_status()
    return {"ok": True, **st, "pending": sudo_auth.pending_status(),
            "ttl": int(sudo_auth.ttl())}


class SudoPasswordBody(BaseModel):
    password: str


@app.post("/api/sudo/cancel")
async def api_sudo_cancel():
    """界面点「取消」：撤掉待授权请求，被拦下的命令不再等密码"""
    return {"ok": True, "cancelled": sudo_auth.cancel_pending()}


@app.post("/api/sudo/password")
async def api_sudo_password(req: SudoPasswordBody):
    """界面提交管理员密码：`sudo -A -v` 校验一次，通过后内存缓存 TTL 时长。

    密码只在后端内存里流转，不落日志、不写配置、不在响应里回显。
    """
    res = await asyncio.to_thread(sudo_auth.provide_password, req.password, "ui")
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    held = res.pop("resume", None)
    if held:
        res["resumed"] = _resume_after_sudo(held)
    return res


def _resume_after_sudo(held: dict) -> bool:
    """密码通过后自动续跑被拦下的那条命令。

    之前只做了一半：命令被拦下 → 用户输密码 → 授权成功，但没有任何环节
    重新发起那一轮，模型就停在那儿等用户催（会话 042b6a 就是这么卡住的）。

    这里用 announce=False 起一轮新对话，不让界面重复出现一条「用户消息」；
    上一轮还在跑时不抢跑（current_task 单例会被覆盖），改由界面提示用户。
    """
    if _chat_busy():
        devlog.debug("main._resume_after_sudo", reason="busy, skipped",
                     command=held.get("command", "")[:120])
        return False
    command = str(held.get("command") or "").strip()
    if not command:
        return False
    text = (f"管理员授权已经通过。刚才被拦下、没有执行的命令是：\n{command}\n"
            "请现在直接重新执行它（sudo 参数已经可用，不要向我索要密码），"
            "然后把执行结果告诉我，并继续完成原来那一轮剩下的步骤。")

    async def _run() -> None:
        try:
            await start_chat(text, announce=False)
        except Exception as exc:
            devlog.debug("main._resume_after_sudo", error=repr(exc))

    _spawn(_run(), "_resume_after_sudo")
    devlog.debug("main._resume_after_sudo", reason="started",
                 command=command[:120])
    return True


@app.get("/api/selfcheck/progress")
async def api_selfcheck_progress():
    return {"ok": True, "job": selfcheck.job_snapshot()}


@app.post("/api/tts/preview")
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


@app.post("/api/tts/say")
async def api_tts_say(req: TTSSayRequest):
    """给远程客户端（手机）合成语音，直接返回 WAV 字节。

    和 /api/tts/preview 的区别：preview 在掌机本机播放，只回状态；这个接口
    只合成、不播放，生成的文件随响应结束删除。鉴权和其它 API 一样走设备
    令牌，不额外开旁路。
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


@app.get("/api/ask-template")
async def api_get_ask_template():
    """「截屏询问」提示词模板：插件面板展开设置区时读一次"""
    return {"ok": True, "text": ASK_TEMPLATE, "default": DEFAULT_ASK_TEMPLATE}


@app.post("/api/ask-template")
async def api_set_ask_template(req: AskTemplateUpdate):
    """改「截屏询问」提示词模板：空 / 全空白表示恢复默认

    模板只影响下一次点「截屏询问」，已经发出的那一轮不受影响。
    """
    tpl = req.text.strip() or DEFAULT_ASK_TEMPLATE
    save_config({"agent": {"ask_template": tpl}})
    return {"ok": True, "text": ASK_TEMPLATE}


@app.get("/api/translate-template")
async def api_get_translate_template():
    """「截图翻译」提示词模板：和大屏模型页那一栏是同一份配置"""
    return {
        "ok": True,
        "text": TRANSLATE_TEMPLATE,
        "default": DEFAULT_TRANSLATE_TEMPLATE,
    }


@app.post("/api/translate-template")
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


@app.get("/api/presets")
async def api_get_presets():
    return _presets_view()


@app.post("/api/presets")
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


@app.post("/api/presets/active")
async def api_set_active_game(req: ActiveGameBody):
    """插件检测到游戏开始/退出时上报，命中预设就套用，退出就回默认

    游戏探测只在插件里做（那里本来就有 Steam appid/游戏名的唯一实现），
    后端不重复写第二套；这个接口只负责切换运行时提示词。
    """
    return {"ok": True, "current": presets.set_active_game(req.appid, req.name)}


@app.patch("/api/presets/{preset_id}")
async def api_update_preset(preset_id: str, req: PresetUpdateBody):
    """设置页编辑内容 / 改名；改名不影响绑定的游戏"""
    try:
        item = presets.update(preset_id, prompt=req.prompt, title=req.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="预设不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "preset": item, "current": presets.active_view()}


@app.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: str):
    if not presets.delete(preset_id):
        raise HTTPException(status_code=404, detail="预设不存在")
    return {"ok": True, "current": presets.active_view()}


# ---------- 语音输入 ----------

@app.get("/api/stt")
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


@app.post("/api/stt/download")
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


@app.post("/api/stt/download/cancel")
async def api_stt_download_cancel():
    """取消正在下载的模型（已下载的 .part 会被清掉，不会留下半个包）"""
    return {"ok": True, **stt.request_cancel()}


@app.post("/api/stt/toggle")
async def api_set_stt(enabled: bool = Query(...)):
    """只切语音输入开关：给 Decky 面板用

    关掉时 apply_config 会顺手 stt.unload()，把识别模型从内存里放掉
    （SenseVoice int8 常驻约 100MB+），Dock 上的 15W 预算也少一份占用。
    """
    save_config({"stt": {"enable": bool(enabled)}})
    return {"ok": True, "stt_enable": stt.enable}


@app.post("/api/stt/duck")
async def api_stt_duck(on: bool = Query(...)):
    """录音开始/结束时静音、恢复系统输出

    失败一律静默降级：接口永远返回 ok=True，前端不需要处理错误，
    更不会因为静音失败把录音拦下来。多端同时录音时按引用计数恢复。
    """
    if on:
        return audiomute.duck()
    return audiomute.restore()


@app.post("/api/stt/transcribe")
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
    if not netcfg.is_this_host(ip):
        raise HTTPException(status_code=403, detail="智能音箱模式只允许本机访问")


@app.get("/api/wake/state")
async def api_wake_state(request: Request):
    _wake_local_only(request)
    # 睡眠模式的倒计时一起带上：眼睛页本来就每 250ms 轮询这个接口，
    # 再单独去拉整份 /api/status 太重，而倒计时恰恰需要它够快。
    return {"ok": True, **wake_service.status(),
            "sleep": sleepmode.service.status()}


@app.get("/api/music/now")
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


@app.post("/api/music/action")
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


@app.post("/api/wake/toggle")
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


@app.post("/api/wake/stop")
async def api_wake_stop(request: Request):
    """停止聆听：立刻停采集并退出智能音箱模式；播报用 /api/wake/tts-stop。"""
    _wake_local_only(request)
    save_config({"wake": {"enable": False}})
    return {"ok": True, **await wake_service.disarm()}


@app.post("/api/wake/exit")
async def api_wake_exit(request: Request):
    """退出智能音箱模式：关总开关并释放麦克风/电源保持。"""
    _wake_local_only(request)
    save_config({"wake": {"enable": False}})
    return {"ok": True, **await wake_service.disarm()}


@app.post("/api/sleep/cancel")
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
        except Exception as exc:
            devlog.debug("main._boot", error=repr(exc))
            pass

    _spawn(_boot(), "wake_boot")


@app.on_event("shutdown")
async def _wake_shutdown():
    try:
        await wake_service.disarm()
    except Exception as exc:
        devlog.debug("main._wake_shutdown", error=repr(exc))
        pass
    # 进程退出兜底：即使上面哪一步异常，也不能把用户的音乐音量停在静音状态
    try:
        media.unmute_for_record()
    except Exception as exc:
        devlog.debug("main._wake_shutdown", error=repr(exc))
        pass
    # 共享 HTTP 连接池是模块级全局对象，关掉才不会有 keep-alive 连接挂着
    try:
        await tools.aclose_http()
    except Exception as exc:
        devlog.debug("main._wake_shutdown", error=repr(exc))
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


@app.get("/api/update/check")
async def api_update_check():
    try:
        info = await updater.check()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return info


@app.post("/api/update/apply")
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


@app.post("/api/restart")
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
        except Exception as exc:
            devlog.debug("main.hook", error=repr(exc))
            pass

    sudo_auth.set_hook(hook)
    _spawn(update_watch_loop(), "update_watch")


@app.get("/api/models")
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


@app.post("/api/retry")
async def api_retry():
    # 只有本会话挂起的操作才挡重试；别的会话挂着的与本端无关
    pend = _pending_get()
    if pend and _pending_matches(pend, store.current_sid, store.epoch, None, None):
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
    except Exception as exc:
        devlog.debug("main._qq_log", error=repr(exc))
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
        return qqtext.get("guest_no_model")
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
        return qqtext.get("guest_llm_fail")
    out, changed = onebot.redact_output(str(out or ""))
    if changed:
        _qq_log("info", "output_redacted", "群友回复命中脱敏规则")
    # 回复长度是群档策略的一部分，必须在代码层兜底，不能只靠提示词
    out = onebot.clip_guest_reply(out)
    return out or qqtext.get("guest_empty")


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
    pol = turn_policy or policy.OWNER_POLICY
    # 本轮归属快照：整轮全程只认这份 sid/epoch，中途别的端新建/切换会话也不影响它
    # sid 可由外部通道（QQ）显式指定：QQ 用自己的独立会话，不往本地会话里写
    turn_sid = sid or store.current_sid
    if not turn_sid:
        # 会话列表被删干净了（current_sid 为空）：新消息落到一个全新会话里，
        # 侧栏按来源把它分到对应组，而不是强留一个空会话占位
        turn_sid = store.current_sid = _fresh_sid()
    turn_epoch = store.epoch
    # 本轮来源：侧栏据此把会话自动分到网页 / Deck / 插件 / Onebot / 智能音响几组。
    # 同一会话里换了端发问（网页接着插件说）也照样跟着最近一轮走。
    # client 为空（重试、唤醒这类内部链路，或是老版本前端没带）时不能就这么空着：
    # 先看上一轮是谁发起的（store.turn 里还没被本轮覆盖），再退回这个会话已经
    # 记下的来源。两处都没有才真的留空 —— 那样侧栏会把它归到「调试」组，
    # 用户换台电脑就冒出一条无主会话
    origin_client = client or store.turn.get("client") or ""
    if not origin_client:
        origin_client = store.origin_map.get(turn_sid) or ""
    _set_session_origin(turn_sid, origin_client)
    _set_session_device(turn_sid, device)
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
        return {"reply": qqtext.get("image_bad_input"), "events": []}
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
    # 用量按会话记：切换会话时取到的是那个会话自己的数，
    # 没记过（刚启动/刚切换）就按当前历史重新估算一次
    # （_cached_estimate 会按条数缓存，刚追加过用户消息，这里必然重算）；
    # 压缩失败也不能让这一轮起不来
    try:
        if _need_compact(_ctx_get() or _cached_estimate()):
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
        elif (pend := _pending_get()) and not _pending_matches_turn(
                pend, turn_sid, turn_epoch, turn_id, client, device):
            # 挂起的操作不是这个会话/这个端提的：既不能替别人确认，也不该
            # 因为别处的对话把这边的手头操作清掉，按普通消息继续
            reply = await _run_agent_guarded(events, hist=hist, image_part=shot_ref,
                                             tool_log=tool_log, client=client,
                                             turn_ctx=turn_ctx,
                                             shot_lock=shot_lock, surface=surface,
                                             turn_policy=pol)
        elif (pend := _pending_get()) and (tools.is_confirm(text) or policy.is_hard_confirm(text)):
            # 用户确认了挂起的危险操作：这是显式的「窗口里点了确认」，
            # 不是历史文本推断出来的授权，policy 用 approved=True 放行并记账。
            # 认领用 _pending_claim：两轮同时确认时只有一个能执行，避免同一个
            # 危险操作被执行两次。没认领到说明已经被别处执行过，这句按普通消息走
            if not _pending_claim(pend):
                reply = await _run_agent_guarded(events, hist=hist, image_part=shot_ref,
                                                 tool_log=tool_log, client=client,
                                                 turn_ctx=turn_ctx,
                                                 shot_lock=shot_lock, surface=surface,
                                                 turn_policy=pol)
            else:
                action = pend
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
        elif (pend := _pending_get()) is not None and _pending_claim(pend):
            # 有新消息但未确认，取消挂起操作
            action = pend
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
            killed_text = qqtext.get("killed_empty") + (
                qqtext.get("error_steps", digest=digest) if digest
                else qqtext.get("steps_none"))
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
        reply = qqtext.get("error_wrap", error=e)
        digest = _tool_results_digest(tool_log)
        if digest:
            reply += qqtext.get("error_steps", digest=digest)
        elif events:
            reply += qqtext.get("steps_unsummarized")
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
            _spawn(_summarize_title(turn_sid, first), "summarize_title")
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
                _spawn(tts.speak(reply), "tts_speak")
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
    _ctx_set(sid, _estimate_tokens(_build_llm_msgs_sync(hist, SYSTEM_PROMPT, sid)))
    # 游标要和会话文件一起持久化，否则重启后模型会重新吃到全量历史
    if sid == store.current_sid:
        save_current_session()
    else:
        save_session_snapshot(sid, hist)
    note = (f"{'已自动压缩上下文' if auto else '已压缩上下文'}"
            f"（{len(old)} 条旧消息 → 摘要，聊天记录完整保留）")
    await manager.broadcast({"type": "system", "text": note, "ts": time.time()})
    return summary


@app.post("/api/compact")
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


def _is_steamos() -> bool:
    """这台机器是不是 Steam Deck（SteamOS / Holo）。

    认 /etc/os-release 的 ID 而不是装了什么桌面，开发机（Windows / 普通发行版）
    一律为 False，不影响 PC 端的既有行为。
    """
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip() in ("ID", "ID_LIKE", "NAME") and "steamos" in val.lower():
            return True
    return False


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


@app.get("/api/sysinfo")
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
        # 运行环境是不是掌机：前端据此关掉「关页面二次确认」，也用来在
        # 托盘进「音响模式」时不做 PC 那套退出提示
        data["handheld"] = _is_steamos()
        data.update(_sd_card_info())
        return data
    except Exception as e:
        return {"error": str(e)}


class OpenFileReq(BaseModel):
    path: str


@app.post("/api/open_file")
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


class OpenUrlReq(BaseModel):
    url: str


@app.post("/api/open_url")
async def api_open_url(req: OpenUrlReq):
    """网页里点外链 → 交给掌机的默认浏览器打开。

    Steam Deck 的 Decky 页框（QWebEngine）不带新窗口能力：`target="_blank"`
    和 `window.open` 都会被静默丢弃，表现为「点了没反应」，而 PC 浏览器
    正常开新标签。所以掌机上改由服务端 `xdg-open` 拉起系统浏览器。
    只放行 http/https，避免把本地文件或自定义协议交给外部程序。
    """
    url = (req.url or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="only http(s) urls are allowed")
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"打开浏览器失败：{exc}") from exc
    return {"ok": True}


# 会话相关接口（/api/sessions*、/api/messages/delete、/api/checkpoint）
# 在 server/routes_sessions.py


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


# 二维码接口（/api/qrcode）在 server/routes_system.py


# ---------- 操作遮罩 ----------
# agent 真正操作设备时，桌面上盖一层半透明遮罩；用户点一下遮罩即终止当前操作。
# 子进程管理（拉起/接管/回收/看门狗/穿透握手）在 overlay_host，这里保留同名引用
_overlay_proc = None
_overlay_state = {"show": False, "text": "", "passthrough": False, "lift": False, "pointer": False}
_overlay_ack = {"passthrough": False, "lift": False}  # 遮罩进程确认已让开鼠标 / 已抬高
_overlay_lock = threading.Lock()

overlay_host.init(lambda: sys.modules[__name__])

_xauthority = overlay_host._xauthority
_overlay_env = overlay_host._overlay_env
ensure_overlay = overlay_host.ensure_overlay
_ensure_overlay_locked = overlay_host._ensure_overlay_locked
stop_overlay = overlay_host.stop_overlay
_reap_children = overlay_host._reap_children
_overlay_pids = overlay_host._overlay_pids
set_overlay = overlay_host.set_overlay
set_overlay_async = overlay_host.set_overlay_async
_OVERLAY_MAX_HOLD = overlay_host._OVERLAY_MAX_HOLD
_overlay_watchdog = overlay_host._overlay_watchdog
set_overlay_passthrough = overlay_host.set_overlay_passthrough
set_overlay_pointer = overlay_host.set_overlay_pointer
set_overlay_lift = overlay_host.set_overlay_lift
overlay_host.start_watchdog()


@app.get("/api/controls")
async def api_controls(force: int = Query(default=0)):
    """控件自检：force=1 时跳过缓存重新实测（网页上点徽章刷新用）

    自检里是一串 subprocess 探针，必须走线程，否则缓存过期的瞬间会把事件循环卡住。
    """
    return await asyncio.to_thread(depcheck.summary, bool(force))


@app.get("/api/overlay_state")
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
    except Exception as exc:
        devlog.debug("main._shutdown_overlay", error=repr(exc))
        pass
    # 退出前抢救一次：正在生成的正文与内存历史立刻落盘。
    # 客户端退出、systemctl restart、升级重启都走这条路径，
    # 少了这一步，被打断的那一轮回答就会整段消失（用户视角就是「记录丢了」）
    try:
        text = _stream_buf().get("text") or ""
        if store.turn.get("active") and text.strip():
            persist_stream(text, force=True, parts=_stream_buf().get("parts") or [])
            print(f"[DeckMeido] 退出前已保存未完成的回答（{len(text)} 字）", file=sys.stderr)
        # 这一轮还没收完就被打断（关机、重启、Ctrl+C）：把半截正文和已做过的事件
        # 补成一条带 interrupted 标记的回答。否则重启后前端只剩一个空的「思考」块，
        # 用户既不知道发生了什么，也没有重试入口（会话 211bdd 的现象）
        if store.turn.get("active"):
            _mark_turn_interrupted()
            # 在跑的子进程也一并收掉：重启后它们已经成了没人管理的孤儿
            try:
                tools.kill_live_procs()
            except Exception:
                pass
        # 这里刻意不写内存 history：正文在 handle_chat 收尾时已落盘，用户消息开头就落盘了，
        # 内存不比文件新。而「拿内存覆盖文件」在内存为空/偏旧时会直接把历史冲掉——
        # 那才是真正的「聊天记录丢了」。只补流式残留，别碰已落盘的历史。
    except Exception as e:
        print(f"[DeckMeido] 退出前落盘失败：{e}", file=sys.stderr)
    # 侧栏索引平时是延迟合并写的，退出前补一次，别让最后几条会话的标题/条数丢了
    try:
        store.flush_index()
    except Exception as exc:
        devlog.debug("main._shutdown_overlay", error=repr(exc))
        pass


# 电源意图记账与引擎同源：实现在 agent_engine，这里转发保持既有导入名可用
_exit_code = agent_engine._exit_code
_arm_power_intent = agent_engine._arm_power_intent
_record_power_intent = agent_engine._record_power_intent



# ---------- 优雅退出 ----------
# systemctl restart / stop、升级重启、以及用户直接关终端，都会给进程发 SIGTERM。
# uvicorn 自己就处理这个信号：停连接 → 发 lifespan shutdown → 退出。
# 所以这里不接管信号，只在 on_event("shutdown") 里把「正在跑但没跑完」的那一轮
# 落盘并打上 interrupted 标记；否则重启后前端只剩一个空的「思考」块
# （会话 211bdd 就是这样）。


def _mark_turn_interrupted() -> None:
    """把「正在跑、但再也没机会收尾」的这一轮补成带 interrupted 标记的回答。

    以前的退出路径只把半截正文写进流式副档案，重启后由恢复逻辑拼一句
    「以上回答被中断」；但只有事件、没有正文的轮次（比如会话 211bdd：
    刚调完 poweroff，正文还是空的）拼出来就是一个空「思考」块，用户
    看不出发生了什么。这里直接往历史里补一条，带上 interrupted 与
    retry_text（用户那一轮的原话），前端据此显示「被中断」并给出重试按钮。
    """
    buf = _stream_buf()
    parts = buf.get("parts") or []
    text = "\n\n".join((p.get("content") or "").strip()
                       for p in parts if p.get("type") == "text").strip()
    if not text:
        text = (buf.get("text") or "").strip()
    evs = [p.get("event") for p in parts if p.get("type") == "event"]
    if not text and not evs:
        return
    hist = store.history
    # 最后一轮已经是带标记的助手消息就别重复补
    if hist and hist[-1].get("role") == "assistant" and hist[-1].get("interrupted"):
        return
    retry = ""
    for m in reversed(hist):
        if m.get("role") == "user":
            retry = str(m.get("content") or m.get("brief") or "")
            break
    hist.append({"role": "assistant", "content": text, "events": evs,
                 "interrupted": True, "retry_text": retry, "ts": time.time()})
    try:
        _write_session(store.turn.get("sid") or store.current_sid, hist)
        store.drop_stream_side(store.turn.get("sid") or store.current_sid)
    except Exception as e:
        print(f"[DeckMeido] 写回中断标记失败：{e}", file=sys.stderr)
    print(f"[DeckMeido] 退出前已标记中断（{len(text)} 字，{len(evs)} 个事件）",
          file=sys.stderr)


# 拒绝识别与引擎入口同源：实现在 agent_engine，这里转发保持既有导入名可用
_REFUSAL_MARKS = agent_engine._REFUSAL_MARKS
_looks_like_refusal = agent_engine._looks_like_refusal
_llm_error_text = agent_engine._llm_error_text


async def _run_agent_guarded(events: list[dict], hist: list[dict] | None = None,
                             image_part: str | None = None,
                             tool_log: list[dict] | None = None,
                             client: str | None = None,
                             turn_ctx: policy.TurnContext | None = None,
                             shot_lock: bool | None = None,
                             surface: str | None = None,
                             turn_policy: policy.TurnPolicy | None = None) -> str:
    """引擎入口的薄壳：实现在 agent_engine，注入本模块供其现读运行态"""
    return await agent_engine._run_agent_guarded(
        events, hist=hist, image_part=image_part, tool_log=tool_log,
        client=client, turn_ctx=turn_ctx, shot_lock=shot_lock,
        surface=surface, turn_policy=turn_policy)


async def run_agent(events: list[dict], extra_system: str = "",
                    hist: list[dict] | None = None,
                    image_part: str | None = None,
                    tool_log: list[dict] | None = None,
                    from_client: str | None = None,
                    _shot_lock: bool | None = None,
                    surface: str | None = None,
                    turn_ctx: policy.TurnContext | None = None,
                    turn_policy: policy.TurnPolicy | None = None) -> str:
    """LLM + 工具调用循环的薄壳：实现在 agent_engine"""
    return await agent_engine.run_agent(
        events, extra_system=extra_system, hist=hist, image_part=image_part,
        tool_log=tool_log, from_client=from_client, _shot_lock=_shot_lock,
        surface=surface, turn_ctx=turn_ctx, turn_policy=turn_policy)


@app.get("/shots/local")
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


@app.post("/api/screenshot")
async def api_screenshot():
    """插件端专用：抓一张当前画面，返回压缩 JPEG 的 base64

    游戏模式下插件自己从 gamescope 的 PipeWire 节点抓帧，不走这条；桌面
    模式（含从桌面进 Big Picture）插件没有抓帧能力，转到这里复用本后端
    spectacle/mss 那条桌面路径，避免插件端再养一套抓帧逻辑。
    """
    return await tools.screenshot_payload()


# ---------- QQ 远程控制通道（OneBot 11 / NapCat 客户端） ----------

# 客户端、回合驱动、配置/语音接口都在 routes_qq：这里保留同名引用，
# 其余调用点（apply_config / 测试 / _qq_log）的写法不变
onebot_client = routes_qq.onebot_client
_qq_nickname = routes_qq._qq_nickname
_qq_status_info = routes_qq._qq_status_info
_qq_screenshot = routes_qq._qq_screenshot
_qq_maybe_launch_after_install = routes_qq._qq_maybe_launch_after_install
_qq_clear_pending = routes_qq._qq_clear_pending
_qq_run_turn = routes_qq._qq_run_turn
_turn_screenshot_blob = routes_qq._turn_screenshot_blob
_qq_image_data_url = routes_qq._qq_image_data_url
_qq_apply_config = routes_qq._qq_apply_config
_qq_synth_voice = routes_qq._qq_synth_voice
_qq_voice_saved = routes_qq._qq_voice_saved
_qq_public_config = routes_qq._qq_public_config
_voice_path = routes_qq._voice_path
VOICE_NAME_RE = routes_qq.VOICE_NAME_RE
VoicePlayRequest = routes_qq.VoicePlayRequest
# QQ 路由函数（api_qq_* / _boot_onebot 等）在 routes_qq.register(app) 时才定义，
# 它的调用点在文件末尾、早于 configure_from_cfg；这里先留空，末尾统一补上
api_qq_status = None
api_qq_group_names = None
api_qq_logs = None
api_qq_disconnect = None
api_qq_connect = None
api_voice_file = None
api_voice_play = None
api_qq_config = None
api_save_qq_config = None
_boot_onebot = None
_stop_onebot = None


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


def asset_ver() -> str:
    """静态资源的版本串：运行版本 + static 目录里最新的改动时间（秒）。

    任一 CSS/JS 被替换，时间戳就变，页面里引用它们的 URL 也跟着变，
    浏览器和 QtWebEngine 的本地旧副本自然失效。
    """
    try:
        newest = max(
            (p.stat().st_mtime for p in STATIC_DIR.iterdir()
             if p.suffix in (".css", ".js")),
            default=0,
        )
    except OSError:
        newest = 0
    return f"{VERSION}-{int(newest)}"


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def index_page(request: Request):
    """首屏注入运行环境标记：字体在 CSS 里按 data-runtime 切换，不靠 JS 后切。

    - 服务端在 SteamOS，且请求来自本机回环（客户端 / QAM / 掌机浏览器）
      → data-runtime="deck"，保持统一的 Noto 字体；
    - 其余（PC 浏览器经局域网访问、开发机）→ "pc"，恢复改动前的字体。
    """
    client = request.client.host if request.client else ""
    # 掌机自己打开页面：标记本机运行，前端据此绝不显示配对页
    local = netcfg.is_this_host(client)
    runtime = "deck" if SERVING_ON_STEAMOS and local else "pc"
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<html ", f'<html data-runtime="{runtime}" ', 1)
    # <body> 单独成行时后面没有空格，两种写法都要认
    home = f'data-home="{1 if local else 0}"'
    if "<body " in html:
        html = html.replace("<body ", f"<body {home} ", 1)
    else:
        html = html.replace("<body>", f"<body {home}>", 1)
    # 静态资源带版本参数：CSS/JS 不带指纹时，浏览器和 QtWebEngine 会一直
    # 用本地旧副本（改完样式看不到变化）。这里统一拼上运行版本 + 静态文件
    # 的最新改动时间，内容一变链接就变，绕开所有中间缓存。
    html = re.sub(
        r'(href|src)="/(app\.css|xterm\.css|xterm\.min\.js|xterm-addon-fit\.min\.js'
        r'|upload-helpers\.js)"',
        lambda m: f'{m.group(1)}="/{m.group(2)}?v={asset_ver()}"',
        html,
    )
    # no-store：index.html 里带着 data-home / data-runtime 这类按请求注入的标记，
    # 一旦被 WebView 或中间层缓存，换设备访问就会拿到别人的那份（掌机看到
    # data-home="0" 又冒出配对页）。首页本来就小，禁止缓存最省心。
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# 配置路由放在末尾挂载：routes_config 通过 get_main() 回读本模块的运行态，
# 此时模块里所有全局量（CFG / llm / API_STATE / PERSONA …）都已就绪。
routes_config.init(require_auth, _client_ip, lambda: sys.modules[__name__])
app.include_router(routes_config.router)

# 会话路由同样要回读 main 的运行态（pending_action / current_task / _ctx_used）
routes_sessions.init(require_auth, _client_ip, lambda: sys.modules[__name__], set_overlay)
app.include_router(routes_sessions.router)

# 状态与设备接口同样回读 main 的运行态（status_async / phone_url / _pair_http）
routes_system.init(require_auth, _client_ip, lambda: sys.modules[__name__])
app.include_router(routes_system.router)

# Agent 引擎（LLM + 工具循环）：同样现读本模块的运行态
agent_engine.init(lambda: sys.modules[__name__])

# WebSocket 端点：连接管理在 ws_hub，端点要现读本模块的 start_chat / status
ws_hub.init(lambda: sys.modules[__name__])
ws_hub.register(app)

# QQ 通道：客户端、回合驱动与配置/语音接口都在 routes_qq
routes_qq.setup(require_auth, lambda: sys.modules[__name__])
onebot_client = routes_qq.onebot_client
routes_qq.register(app)
# 路由函数定义在 register 里，注册完成后才取得到；供测试与其它模块按 main.xxx 调用
for _name in ("api_qq_status", "api_qq_group_names", "api_qq_logs",
              "api_qq_disconnect", "api_qq_connect", "api_voice_file",
              "api_voice_play", "api_qq_config", "api_save_qq_config",
              "_boot_onebot", "_stop_onebot"):
    globals()[_name] = getattr(routes_qq, _name)
routes_qq.configure_from_cfg()

# 文件与终端接口：上传/浏览/文件操作/终端都在 routes_files，回读本模块的运行态
routes_files.init(require_auth, lambda: sys.modules[__name__])
routes_files.register(app)
# 路由函数在 register 里定义，注册后补齐同名全局（测试与其它模块按原名调用）
for _name in routes_files.__all_routes__:
    globals()[_name] = getattr(routes_files, _name)


# 静态网页控制台（放最后挂载，避免覆盖 API 路由）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
