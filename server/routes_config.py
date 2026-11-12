"""配置接口：GET/POST /api/config

从 main.py 平移出来的第二批路由。这里依赖 main 的运行态（CFG / llm / API_STATE /
PERSONA 等会在热更新时被重新赋值），所以统一通过 init() 注入一个「取 main 模块」的
回调，每次请求现读，绝不在本模块缓存值。
"""
from __future__ import annotations

import httpx
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import apicfg, audit, audiomute, devlog, memory, netcfg, netmode, paths, persona, presets, remote_input, remote_terminal, sleepmode, status_line, tools, updater
from .tts import LANGUAGES, SPEAKER_LABELS, normalize_speaker, speaker_from_legacy

router = APIRouter()


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


def _mask_key(key: str) -> str:
    """Key 只用于展示：前 6 位 + 后 4 位，太短则几乎全遮

    打码后前端只用于「展示已配置」，
    真正用 Key 的请求都在服务端完成。
    """
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


def init(require_auth, client_ip, get_main) -> None:
    """由 main 在 app 创建后调用。

    get_main() 返回 main 模块本身（main 在文件末尾调用 init，此时模块已就绪）。
    CFG / llm / API_STATE / PERSONA / WORKSPACE_OK 等热更新会换绑的全局量
    一律 m.xxx 现读，不在本模块留引用。
    """
    auth = [Depends(require_auth)]

    @router.get("/api/config", dependencies=auth)
    async def api_get_config():
        m = get_main()
        llm = m.llm
        tts = m.tts
        stt = m.stt
        CFG = m.CFG
        API_STATE = m.API_STATE
        PERSONA = m.PERSONA
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
            "with_shot": m.chat_voice_enabled()
            and bool((CFG.get("agent") or {}).get("with_shot", True)),
            "with_shot_pref": bool((CFG.get("agent") or {}).get("with_shot", True)),
            # 语音输入开关：和语音播报一样，面板上要能显示和切换（stt.enable 已是有效值）
            "stt_enable": stt.enable,
            "stt_enable_pref": bool((CFG.get("stt") or {}).get("enable", True)),
            # 整组总开关：设置页标题行的开关与分项置灰都看它
            "chat_voice": m.chat_voice_enabled(),
            # 录音期间静音外放（[stt] duck_output，默认开）；duck 里带工具可用性，
            # 设置页据此提示「这台机器不支持静音」，但开关仍可保存
            "stt_duck_output": audiomute.enabled(),
            "stt_duck": audiomute.status(),
            # 智能音箱模式：设置页的开关/状态；默认 false，唤醒后直接发送
            "wake_enabled": m.wake_service.enabled,
            "wake_state": m.wake_service.status(),
            # [ui] language：网页/客户端据此显示语言下拉，切换后整页重载
            "ui_language": m.UI_LANG,
            "permission_mode": m.PERMISSION_MODE,
            # 自定义档的四组开关：设置页打开时按它落位
            "permission_custom": dict(m.CUSTOM_PERM),
            "workspace": str(tools.WORKSPACE),
            "port": CFG["server"].get("port", 18765),
            # 局域网访问开关：默认 false 只监听 127.0.0.1；开启后设置页显示地址与配对入口
            "lan_enable": netcfg.lan_enabled(CFG.get("server") or {}),
            # [runtime].dev_mode：网页设置页的开发者模式开关；保存后立即生效
            "dev_mode": devlog.enabled(),
            # [runtime].offline_mode：设置页开关与界面横幅都读它
            "offline_mode": netmode.enabled(),
            # 局域网访问地址：掌机 / 大屏 / 客户端设置页直接展示，别的设备照这个连
            "lan_url": f"http://{m._lan_ip()}:{CFG['server'].get('port', 18765)}",
            "device_days": m._device_days(),
            "llm_configured": llm.configured,
            "version": m.VERSION,
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
            "status_prompt_effective": m.status_prompt_text(),
            # 按游戏提示词预设：当前游戏/当前预设状态 + 预设条数
            "preset": presets.active_view(),
            "presets_count": len(presets.list_presets()),
            # 数据根（配置/会话/记忆/上传/QQ 归档都在这里），排障用
            "data_dir": str(paths.data_dir()),
            "data_root": paths.data_root_label(),
            # 「截屏询问」的提示词模板：插件设置区里可以直接改
            "ask_template": m.ASK_TEMPLATE,
            "ask_template_default": m.DEFAULT_ASK_TEMPLATE,
            # 「截图翻译」的提示词模板：同一套存储与热更新路径
            "translate_template": m.TRANSLATE_TEMPLATE,
            "translate_template_default": m.DEFAULT_TRANSLATE_TEMPLATE,
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
            "terminal_remote_shell": m._terminal_settings().enabled,
            "terminal_idle_timeout": m._terminal_settings().idle_timeout_s,
            "terminal_max_sessions": m._terminal_settings().max_sessions,
            "terminal_max_output_bytes": m._terminal_settings().max_output_bytes,
            "terminal_shell": m._terminal_settings().shell,
            "terminal_cwd": m._terminal_settings().cwd,
        }

    @router.post("/api/config", dependencies=auth)
    async def api_set_config(req: ConfigUpdate, request: Request):
        m = get_main()
        llm = m.llm
        CFG = m.CFG
        API_STATE = m.API_STATE
        PERSONA = m.PERSONA
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
        legacy_url = m._norm_base_url(req.base_url) if req.base_url is not None else None
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
            llm_updates["model"] = await m._pick_model(
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
            updates["ui"] = {"language": m.ui_language({"ui": {"language": req.ui_language}})}
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
                sec["custom"] = dict(m.CUSTOM_PERM)
            updates["security"] = sec
        if req.workspace is not None and req.workspace.strip():
            updates["workspace"] = {"dir": req.workspace.strip()}
        if req.ask_template is not None:
            # 清空即恢复默认：模板是「截屏询问」唯一的提问来源，
            # 留成空串会让这个按钮发不出去任何内容
            tpl = req.ask_template.strip() or m.DEFAULT_ASK_TEMPLATE
            updates.setdefault("agent", {})["ask_template"] = tpl
        if req.status_prompt is not None:
            # 状态栏提示词同理：清空恢复默认，避免状态栏只剩一句空话
            updates.setdefault("agent", {})["status_prompt"] = \
                req.status_prompt.strip() or m.DEFAULT_STATUS_PROMPT
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
            m.reload_persona()
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
            audit.record("config", "lan", "ok", ip=client_ip(request),
                         detail="on" if req.lan_enable else "off")
        if req.port is not None:
            port = netcfg.valid_port(req.port)
            if port is None:
                raise HTTPException(status_code=400, detail="端口需为 1024-65535 之间的整数")
            updates.setdefault("server", {})["port"] = port
            audit.record("config", "port", "ok", ip=client_ip(request), detail=str(port))
        if req.dev_mode is not None:
            # 写 [runtime]，apply_config 里 devlog.configure 会立刻切换级别与文件。
            # 关闭开发者模式不再自动清理 debug 日志（设置页已无该开关，固定为「不清理」），
            # 需要清理时由用户点「清空调试日志」。
            updates.setdefault("runtime", {})["dev_mode"] = bool(req.dev_mode)
            audit.record("config", "dev_mode", "ok", ip=client_ip(request),
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
            m.save_config(updates)
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
                "workspace_ok": m.WORKSPACE_OK,
                "workspace_error": m.WORKSPACE_ERROR,
                # 回传归一之后的实值：前端滑条按它落位（比如 DeepSeek 上的「中」实际是 high）
                "reasoning_effort": llm.reasoning_effort,
                "with_shot": m.chat_voice_enabled()
                and bool((CFG.get("agent") or {}).get("with_shot", True)),
                "with_shot_pref": bool((CFG.get("agent") or {}).get("with_shot", True)),
                "chat_voice": m.chat_voice_enabled(),
                "model": llm.model,
                # 绑定地址与端口都只在后端启动时读取，改完必须重启核心
                "port": CFG["server"].get("port", 18765),
                "lan_enable": netcfg.lan_enabled(CFG.get("server") or {}),
                # dev_mode 保存即生效，不需要重启；重启项只算绑定地址与端口
                "dev_mode": devlog.enabled(),
                # 离线模式保存即生效
                "offline_mode": netmode.enabled(),
                # 远程终端开关回传实值，设置页不用再拉一次
                "terminal_remote_shell": m._terminal_settings().enabled,
                "terminal_idle_timeout": m._terminal_settings().idle_timeout_s,
                "terminal_max_sessions": m._terminal_settings().max_sessions,
                "terminal_max_output_bytes": m._terminal_settings().max_output_bytes,
                "terminal_shell": m._terminal_settings().shell,
                "restart_required": bool(req.lan_enable is not None or req.port is not None)}
