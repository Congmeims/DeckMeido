"""QQ 通道：OneBot 客户端、回合驱动、配置接口与语音回放

从 main.py 平移出来的第四批路由。这一段包含的东西本来就是一整块：
  - _qq_run_turn 等回合函数（onebot 回调进来的一轮对话）
  - OneBotClient 的创建与配置、启动/停止钩子
  - /api/qq/* 状态、日志、连接、配置
  - /api/voice/* 语音条回放

所有要用到 main 运行态的地方（handle_chat / _chat_busy / pending_action /
_guest_system_prompt / CFG / save_config / PERSONA …）都经 get_main() 现读。
"""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import devlog, onebot, paths, policy, qqtext, session_store, themecolor, tools, tts
from .session_store import store
from .tts import SPEAKER_LABELS, TTS

router = APIRouter()

# 由 main 注入：鉴权依赖、main 模块本身
require_auth = None
get_main = None

# onebot 客户端实例由本模块持有；main 侧同样引用同一份（见 init 后的绑定）
onebot_client: onebot.OneBotClient | None = None

# 语音落盘用的文件名：只允许 16 位十六进制 + .wav，路由按这个格式白名单校验
VOICE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.wav$")


def _m():
    return get_main()


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
    # 断线重连退避（秒）：最小/最大间隔，保存后下一次断开即按新值退避
    reconnect_min: float | None = None
    reconnect_max: float | None = None


class VoicePlayRequest(BaseModel):
    url: str


# ---------- 回合驱动 ----------

def _qq_nickname() -> str:
    """触发前缀就是软件里配置的昵称；昵称为空时由 onebot 退回默认词"""
    return str(_m().PERSONA.get("name") or "")


async def _qq_status_info() -> dict:
    m = _m()
    gaming = await asyncio.to_thread(tools.mode_is_gaming)
    return {
        "core": "运行中",
        "mode": "游戏模式" if gaming else "桌面模式",
        "version": m.VERSION,
    }


async def _qq_screenshot() -> tuple[bytes | None, str]:
    """抓一张当前画面给 /shot 用；返回 (图片字节, 说明)

    走和图聊同一套截图能力（1280×800，游戏模式抓 gamescope，桌面模式用
    spectacle/mss），优先回传压缩过的 vision JPEG，避免 base64 太大。
    """
    m = _m()
    try:
        note = await m._run_tool("screenshot", {})
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
        shots = sorted((m.STATIC_DIR / "shots").glob("vision_*.jpg"),
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
    m = _m()
    cfg = onebot.parse_config(m.CFG.get("qq") or {})
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
            m._qq_log("warn", "自动启动跳过", "安装命令未成功退出")
            continue
        target = _qq_install_target(cmd)
        if not target:
            m._qq_log("info", "自动启动跳过", "猜不到可执行名")
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
            m._qq_log("error", "自动启动失败", f"{type(e).__name__}: {e}")
            return {"reply": qqtext.get("launch_fail", exe=exe, error=type(e).__name__)}
        m._qq_log("info", "自动启动", f"{exe}")
        blob, _note = await _qq_screenshot()
        caption = qqtext.get("launch_ok", target=target)
        if blob:
            return {"reply": caption, "caption": caption, "image": blob}
        return {"reply": caption + qqtext.get("shot_missing", caption="")}
    return {}


def _qq_clear_pending(device: str, sid: str) -> None:
    """QQ 侧确认超时后，把仍然属于这条来源的挂起操作撤掉"""
    m = _m()
    p = m._pending_get()
    if (isinstance(p, dict) and (p.get("client") or "") == "qq"
            and (p.get("device") or "") == device
            and (p.get("sid") or "") == sid):
        m._pending_set(None)


async def _qq_run_turn(text: str, *, sid: str, device: str,
                       readonly: bool, turn_id: str = "",
                       image: bytes | None = None,
                       turn_note: str = "",
                       proactive: bool = False) -> dict:
    """QQ 通道的一轮对话

    走现有 handle_chat，但用 QQ 自己的会话（sid）和来源标记（device）：
    本地界面正在看的会话不会被追答写进去，历史里也能分清每条来自哪个 QQ / 群。
    proactive=True 表示这一轮是「没人叫、按概率自己插话」，只用于日志区分。

    来源授权闸门：只有 onebot 明确标记为已授权（True）才开回合。
    未授权时直接返回，不写会话、不广播、不跑工具——避免界面里出现一条
    「已开始询问卸载哪个」的会话，让用户误以为它在干活。
    """
    m = _m()
    if not onebot.qq_turn_authorized():
        onebot.log_tool_blocked("qq_turn")
        m._qq_log("warn", "回合拒绝",
                  f"reason=unauthorized_source uin={device}")
        return {"reply": "", "denied": True, "unauthorized": True}
    if not turn_id:
        turn_id = "qq-" + secrets.token_hex(6)
    # 挂起的危险操作只认「同一个 QQ 来源」的确认，并且必须沿用那一轮的 turn_id，
    # 才符合 handle_chat 的认领校验（见 _pending_matches_turn）。
    # 会话已经切走（epoch 变了）或者 sid 对不上的，确认也放行不了，直接作废
    pend = m._pending_get()
    if (isinstance(pend, dict) and (pend.get("client") or "") == "qq"
            and (pend.get("device") or "") == device):
        if onebot.is_confirm_text(text) or onebot.is_hard_confirm(text):
            if ((pend.get("sid") or "") == sid
                    and (pend.get("epoch") or 0) == store.epoch
                    and (pend.get("turn_id") or "")):
                turn_id = str(pend.get("turn_id"))
            else:
                m._pending_set(None)
    # 本地端正在跑另一轮时不要插进来：store.turn / pending_action 都是全局单份的，
    # 两边同时跑会把「这一轮是谁提的」搅乱。等本地这轮结束再发即可
    # （start_chat 里还有一道 _chat_start_lock 兜底，这里提前返回是为了给出
    #  更贴切的提示语，而不是让 QQ 用户吃到一条通用忙碌文案）
    if (store.turn.get("active") and (store.turn.get("client") or "") != "qq") or m._chat_busy():
        return {"reply": qqtext.get("local_busy"), "busy": True}
    # QQ 的会话独立于本地当前会话：历史从它自己的文件读，不碰 store.history
    if sid in store.deleted_sids:
        # 这个 sid 之前被用户删过。QQ 的 sid 是按 uin/群号算死的，
        # 上一轮已收尾的话就允许开一条全新的，但历史必须从空开始 ——
        # 绝不能把删掉那轮的内容（或它的摘要）再带回来
        active_sid = str(store.turn.get("sid") or "") if store.turn.get("active") else ""
        if not store.allow_fresh(sid, active_sid):
            return {"reply": qqtext.get("session_closing"), "busy": True}
        hist = []
    elif sid == store.current_sid:
        hist = store.history
    else:
        data = await asyncio.to_thread(store.read_session, sid)
        hist = data.get("history") if isinstance(data.get("history"), list) else []
    image_part = _qq_image_data_url(image)
    turn_started = time.time()
    # 群聊来源：sid 形如 qq-g-<群号>（见 onebot.session_sid），私聊是 qq-p-<QQ号>
    is_group = sid.startswith("qq-g-")
    if is_group:
        # 会话标题要写「群名 · 尾号」，群名在这里先备好：走的是 OneBot 的
        # 10 分钟缓存，常见情况下不产生网络请求；取不到就只显示尾号，不阻塞这一轮
        gid = sid.split("-")[2] if sid.count("-") >= 2 else ""
        client = onebot_client
        try:
            names = await asyncio.wait_for(
                client.group_names([gid]), timeout=3.0) if (gid and client) else {}
        except Exception:
            names = {}
        if names.get(gid):
            store.qq_group_names[sid] = names[gid]
    # 场景策略表：群友档零工具/无长期记忆/400 字/脱敏；主人档能力不变
    turn_policy = policy.GUEST_POLICY if readonly else policy.OWNER_POLICY
    # 群友提示词也留着给 _guest_reply 做灰度回退用
    guest_text = m._guest_system_prompt() if readonly else ""
    # 引用了别人的图等「看图受限」的情况：附注只进本轮提示，不写进历史
    if turn_note and not readonly:
        guest_text = (guest_text + "\n" if guest_text else "") + turn_note
    try:
        result = await m.handle_chat(text, client="qq", device=device,
                                     turn_id=turn_id, surface=policy.SURFACE_CHAT,
                                     sid=sid, hist=hist,
                                     force_confirm=True, image=image_part,
                                     guest=bool(readonly),
                                     guest_prompt_text=guest_text,
                                     turn_note=turn_note,
                                     turn_policy=turn_policy)
    finally:
        # QQ 这轮的用量单独记在它自己的 sid 上（_ctx_set 按会话存），
        # 本地会话的 /status 不会被 QQ 的数字串味，这里无需再归零
        pass
    # 排障：QQ 会话到底有没有写进索引。dev_mode 打开时这条会落到
    # userdata/logs/debug-<时间>.log，搜 qq.session 即可；平时不写文件
    try:
        devlog.log("qq.session", sid=sid, readonly=bool(readonly),
                   messages=int(store.index().get(sid, {}).get("messages") or 0))
    except Exception as exc:
        devlog.debug("main._qq_run_turn", error=repr(exc))
        pass
    out = {"reply": str(result.get("reply") or "")}
    p = m._pending_get()
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
    # 群聊里不回发：那是 Deck 自己的桌面画面，群友不该看到。
    if not out.get("image") and not is_group:
        blob = await asyncio.to_thread(_turn_screenshot_blob, turn_started)
        if blob:
            out["image"] = blob
            out["caption"] = out.get("caption") or out["reply"] or qqtext.get("shot_caption")
            m._qq_log("info", "回合附图", f"{len(blob)} 字节")
    # 本轮 search_image / search_wallpaper 搜到的图：网页端是聊天里的候选图网格，
    # QQ 侧原本只有一句「已经铺在聊天里啦」，图全丢在服务端，用户什么都看不到。
    # 这里把这些图按顺序原样发回去（群聊也发：搜的是网图，不是 Deck 桌面）。
    cands = _turn_candidate_blobs(result.get("events") or [])
    if cands:
        out["candidates"] = cands
        m._qq_log("info", "回合候选图", f"{len(cands)} 张")
    return out


def _turn_candidate_blobs(events: list) -> list[bytes]:
    """取本轮搜图工具落盘的原图，按候选顺序返回可发送的字节。

    只认工具事件里明确挂上的 candidates；单张超过发送上限的直接跳过，
    不在这里压图（压图归发送层管，这里只负责把文件读出来）。
    """
    out: list[bytes] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "tool":
            continue
        for c in ev.get("candidates") or []:
            cid = str((c or {}).get("id") or "")
            try:
                p = themecolor.candidate_path(cid)
                if p is None or not p.is_file():
                    continue
                blob = p.read_bytes()
            except OSError:
                continue
            if blob:
                out.append(blob)
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
    cfg = onebot.parse_config(raw)
    onebot_client.configure(cfg)
    # 保存配置时顺手把归档目录建好（还没存过图时也不至于「目录不存在」）
    onebot.ensure_archive_dirs(cfg)


def _qq_voice_wav(text: str) -> Path:
    """QQ 语音条合成（线程内跑）：按 tts.speaker 选发音人，产出临时 wav，调用方负责删

    robot 走 espeak-ng，maid 走 piper-plus；两者失败都抛 TTSError，
    由 onebot 记 voice.synth 后只发文字（现有回退逻辑不变）。
    tts 就是 main 里建好并注册自检的那个 TTS 实例，直接调它的方法；
    以前写成 tts.active() 是把实例当模块找全局单例，会 AttributeError。
    """
    if tts is None:
        raise RuntimeError("TTS 未初始化")
    return tts.synth_wav(text)


async def _qq_synth_voice(text: str) -> Path:
    return await asyncio.to_thread(_qq_voice_wav, text)


async def _qq_voice_saved(ev, path: Path, seconds: float, size: int) -> None:
    """QQ 语音条实际发出后：留一份 wav 到 userdata/voice，并写进会话消息元数据

    历史里只存 {url, seconds, speaker, bytes}，音频本体放独立文件，
    不会把 base64 撑进会话 JSON。写入排在会话 IO 线程里，和正常落盘串行。
    """
    manager = _m().manager
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


# ---------- 配置接口 ----------

def _qq_id_list(value) -> list[str]:
    """把数组或「逗号/换行分隔」的文本归一成去重后的 QQ 号列表"""
    return onebot.normalize_ids(value)


def _qq_public_config() -> dict:
    """远程连接页要回填的字段；超时/心跳这类只在配置文件里调"""
    cfg = onebot.parse_config(_m().CFG.get("qq") or {})
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
        # 「打开文件夹」按钮的落点：按当前配置算出的真实路径（留空时是默认归档目录），
        # 前端不去猜路径拼接规则，免得 override 与默认值两套逻辑各写一遍
        "image_dir_effective": str(onebot.image_archive(cfg)[1]),
        "text_log_effective": str(onebot.text_log_location(cfg)),
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
        # 重连退避：设置页可改，保存后下一次断开即生效
        "reconnect_min": cfg.reconnect_min,
        "reconnect_max": cfg.reconnect_max,
        "confirm_timeout": cfg.confirm_timeout,
        "prefix": onebot.prefix_for(_qq_nickname()),
    }


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


def register(app) -> None:
    """由 main 在 app 创建后调用：挂路由 + 建 OneBotClient + 启停钩子"""
    # 整组路由统一走鉴权：在建 router/app 时挂一次依赖，
    # 不再逐条写 dependencies=[Depends(require_auth)]
    router.dependencies.append(Depends(require_auth))
    app.include_router(router)

    @router.get("/api/qq/status")
    async def api_qq_status():
        """QQ 通道的连接状态与统计（令牌不回显），供远程连接页用"""
        return onebot_client.state()

    @router.get("/api/qq/group-names")
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

    @router.get("/api/qq/logs")
    async def api_qq_logs(limit: int = Query(default=100, ge=1, le=300)):
        """QQ 事件日志：只含事件与计数，不含令牌/消息正文；新的在前"""
        return {"logs": onebot_client.logs(limit), "state": onebot_client.state()}

    @router.post("/api/qq/disconnect")
    async def api_qq_disconnect():
        """手动断开 OneBot：只停连接，不改配置（开启状态保持不变）"""
        await onebot_client.stop()
        return {"ok": True, "state": onebot_client.state()}

    @router.post("/api/qq/connect")
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

    @router.get("/api/voice/{name}")
    async def api_voice_file(name: str):
        """语音条原件（wav）；网页走设备令牌（查询参数），插件走本机来源"""
        if not VOICE_NAME_RE.match(name):
            raise HTTPException(status_code=404, detail="语音文件不存在")
        path = paths.voice_dir() / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="语音文件不存在")
        return FileResponse(path, media_type="audio/wav")

    @router.post("/api/voice/play")
    async def api_voice_play(req: VoicePlayRequest):
        """QAM / WebKit 播不了 <audio> 时的降级：用后端现有播放器在本机播放"""
        global _voice_proc
        path = _voice_path(req.url)
        player = TTS._player()
        if not player:
            raise HTTPException(
                status_code=501,
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

    @router.get("/api/qq/config")
    async def api_qq_config():
        return _qq_public_config()

    @router.post("/api/qq/config")
    async def api_save_qq_config(req: OneBotConfigUpdate):
        """保存 QQ 配置；只改配置与下次启动行为，不顺手触发连接

        写盘统一走 write_config（先校验后原子写）。连接/重连只能由
        /api/qq/connect 或设置页「重新连接」显式触发。
        """
        m = _m()
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
            m.save_config({"qq": updates})
            # 保存只改配置/下次启动行为。关闭通道时断开现有连接；
            # 其余情况一律不自动连接——连接必须由用户显式点击。
            if not onebot_client.state().get("enabled"):
                await onebot_client.stop()
        return {"ok": True, "state": onebot_client.state(), "config": _qq_public_config()}

    @app.on_event("startup")
    async def _boot_onebot():
        # 默认不自启：enabled 只表示「允许运行」，重启后必须手动点连接。
        # 只有显式打开 auto_connect 才会在 core 启动时自动建连。
        if onebot_client.should_auto_connect():
            await onebot_client.start()

    @app.on_event("shutdown")
    async def _stop_onebot():
        await onebot_client.stop()

    _REGISTERED.update(
        api_qq_status=api_qq_status,
        api_qq_group_names=api_qq_group_names,
        api_qq_logs=api_qq_logs,
        api_qq_disconnect=api_qq_disconnect,
        api_qq_connect=api_qq_connect,
        api_voice_file=api_voice_file,
        api_voice_play=api_voice_play,
        api_qq_config=api_qq_config,
        api_save_qq_config=api_save_qq_config,
        _boot_onebot=_boot_onebot,
        _stop_onebot=_stop_onebot,
    )
    _expose_routes()


def setup(require_auth_fn, get_main_fn) -> None:
    """建 OneBotClient 并挂上回调（main 在 apply_config 之前调用）"""
    global require_auth, get_main, onebot_client
    require_auth = require_auth_fn
    get_main = get_main_fn
    onebot_client = onebot.OneBotClient(
        runner=_qq_run_turn,
        screenshot=_qq_screenshot,
        status_provider=_qq_status_info,
        nickname=_qq_nickname,
        pending_clear=_qq_clear_pending,
        # 单条图文的 base64 超 3MB 时，压到长边 1024 / q82 再发
        image_optimizer=lambda blob: _m()._jpeg_for_llm(blob, max_side=1024, quality=82),
        voice_synth=_qq_synth_voice,
        voice_saved=_qq_voice_saved,
    )
    # 首次配置由 main 在 CFG 就绪后调用 configure_from_cfg()
    return onebot_client


def configure_from_cfg() -> None:
    """按当前配置初始化客户端（main 在 CFG 加载后调用一次）"""
    cfg = onebot.parse_config(_m().CFG.get("qq") or {})
    onebot_client.configure(cfg)
    # 启动就把归档目录和占位文件备好：用户第一次点「打开图片文件夹」就有内容
    onebot.ensure_archive_dirs(cfg)


def _expose_routes() -> None:
    """把路由函数提到模块级，供 main 与测试按原名调用

    路由函数定义在 register 里（要用到注册时的鉴权依赖），但 main 侧的
    薄壳引用和既有测试都是按 routes_qq.api_qq_status 这样取名的。
    register 收尾时调用本函数补齐这批名字。
    """
    g = globals()
    for name in ("api_qq_status", "api_qq_group_names", "api_qq_logs",
                 "api_qq_disconnect", "api_qq_connect", "api_voice_file",
                 "api_voice_play", "api_qq_config", "api_save_qq_config",
                 "_boot_onebot", "_stop_onebot"):
        fn = _REGISTERED.get(name)
        if fn is not None:
            g[name] = fn


_REGISTERED: dict = {}
