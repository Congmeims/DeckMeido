"""发给 QQ 的提示文案：默认值集中在这里，设置页可逐条覆盖

文案原文都是「代码里写死的中文字面量」，散落在 onebot.py / main.py / menu.py。
集中到这一个文件只做两件事：
  1. 默认值只有一份，改语气改这里；
  2. 设置页可以按 key 覆盖（存在数据根的 qqtexts.json），不用改代码。

调用点一律用 get()：
    qqtext.get("rate_user")
    qqtext.get("unknown_cmd", prefix=prefix, text=route.text)
占位符写 {{prefix}} 这种双花括号，由 _render 一次性替换；用户填错的占位符
原样留在结果里，不抛异常 —— 一条提示文案不值得把整条回复搞挂。

覆盖只落 qqtexts.json，和人设（meidosoul.json）一样不进 config.toml：
这份文件是「她说话的口气」，改坏了删掉即可回到默认。
"""
from __future__ import annotations

import json
import os
import re
import threading

from . import paths

# key -> 默认文案。顺序即设置页的展示顺序（按场景分组见 GROUPS）
DEFAULTS: dict[str, str] = {
    # -- 限流 --
    "rate_user": "发得太快啦，歇一分钟再来吧~",
    "rate_group": "群里消息有点密，大家先缓一缓哈~",
    # -- 指令与确认 --
    "unknown_cmd": "没有这条指令哦：{{text}}。发「{{prefix}} 菜单」就能看到可用指令啦。",
    "cmd_unavailable": "这条指令暂时用不了，稍后再试试吧~",
    "chat_at_empty": "想聊什么直接 @ 我就好，也可以发「{{prefix}} 想说的话」~",
    "chat_prefix_empty": "在昵称后面加一句想聊的话哦，比如「{{prefix}} 在吗」；"
                         "发「{{prefix}} 菜单」就能看到可用指令啦。",
    "need_confirm": "这是危险操作哦，回复「{{phrase}}」才会执行；说别的就取消啦。",
    "need_hard_confirm": "这项操作风险有点高，回复「确认执行」才会执行；说别的就取消啦。",
    "confirm_timeout": "确认超时啦，这次操作作废了哦。",
    "confirm_cancelled": "已取消，这次操作没有执行~",
    # -- 调度 --
    "busy": "上一件事还在处理，稍等一下哈~要停下就发 {{prefix}} 停。",
    "turn_cancelled": "已终止当前操作~",
    "local_busy": "设备正在处理本地端发起的操作，等它结束再发哦；这一条没有提交。",
    "session_closing": "上一条会话还在收尾，等它结束再发哦；这一条没有提交。",
    # -- 命令：/status --
    "status_title": "设备状态",
    "status_core": "核心：{{core}}",
    "status_mode": "模式：{{mode}}",
    "status_qq": "QQ：{{state}}",
    "status_version": "版本：{{version}}",
    "status_pending": "有一项操作在等确认哦（发「确认」执行，说别的就取消）。",
    # -- 命令：/shot、/save_image、/save_text --
    "shot_busy": "上一条还在处理，稍等一下再截图哈~",
    "shot_failed": "没截到当前画面哦（详情见设备日志）。",
    "shot_failed_reason": "没截到当前画面：{{note}}",
    "shot_caption": "截图来啦~",
    "save_image_off": "图片功能没开（allow_image=false），这次没存哦。",
    "save_image_none": "最近没有可保存的图片呢，发一张图、或者引用一张图再发「存图」就好啦。",
    "save_image_fail": "没存成功：{{error}}",
    "save_ok": "保存成功~",
    "save_text_no_reply": "先引用要保存的那条消息，再发「保存文本」哦；当前这条不会被保存。",
    "save_text_missing": "引用的那条消息我没取到呢（可能已经撤回），这次没有保存哦。",
    "save_text_no_text": "引用的消息里没有文字呢，引用一条文字消息，再发「保存文本」就好啦。",
    "save_text_fail": "没保存成功：{{error}}",
    # -- 命令：/memo、/memory、/deps、/cancel、/help --
    "memo_private": "这个指令要在群里发才有效哦；私聊里没有群缓存呢。",
    "memo_cleared": "已清除本群缓存（{{n}} 条）~",
    "memo_empty": "本群没有缓存哦。",
    "memory_private_group": "私聊里没有群记忆呢，回群里查看或清空吧~",
    "memory_no_uin": "拿不到发言人 QQ，没法定位个人记忆哦。",
    "memory_private_only": "私聊记忆只能在私聊里查看或清空哦，免得在群里泄露。",
    "memory_bad_scope": "scope 只认：群 / 人 / 私聊 / all 哦。",
    "memory_none": "{{label}}还没有长期记忆呢。",
    "memory_head": "{{label}}的长期记忆（{{n}} 条）：",
    "memory_item": "{{i}}. [{{kind}}] {{text}}",
    "memory_more": "只显示前 10 条，一共 {{n}} 条哦。",
    "memory_clear_hint": "清空：{{prefix}} 清空记忆 [群/人/私聊/all]，需要管理员确认哦。",
    "memory_cleared": "已清空{{label}}记忆（{{n}} 条）~",
    "memory_nothing": "{{label}}没有可清空的记忆哦。",
    "deps_fail": "依赖检查失败：{{error}}",
    "deps_head": "依赖检查：{{ok}}/{{total}} 项可用，{{na}} 项不适用",
    "deps_note": "（只检测，不会安装任何东西哦）",
    "deps_item": "- {{label}}：{{state}}{{detail}}",
    "deps_ydotool": "- 注意：ydotoold 没设开机自启，重启后会失效哦。",
    "idle": "现在没有正在执行的任务哦。",
    # -- 命令：/help --
    "help_title": "可用指令（发「{{prefix}} 指令」就行，不经过 AI）：",
    "help_item": "{{name}} —— {{summary}}{{flag}}",
    "help_trigger": "触发方式：{{prefix}} + 空格 + 指令（例：{{prefix}} 菜单）；"
                    "{{prefix}} 后面接别的话就当闲聊啦。",
    # -- 媒体 --
    "image_empty": "图片是空的，这次没发出去哦。",
    "image_too_big": "这张图太大啦，压缩后还是发不出去，这次就不发图了哦。",
    "image_fail": "图片没发出去哦（详情见设备日志）。",
    "empty_reply": "（这一轮没有回复）",
    "image_bad_input": "附带的画面没能解析成图片哦（只支持 PNG / JPEG / WebP），这一轮没有发送。",
    "launch_fail": "已经装好啦，但没能自动打开 {{exe}}（{{error}}）。",
    "launch_ok": "已经装好啦：{{target}}，也自动打开了~游戏模式下窗口可能看不到，切回桌面看一眼就好。",
    "shot_missing": "{{caption}}\n（截图没抓到，这次就不附图啦。）",
    # -- 异常 --
    "dispatch_fail": "出了点小问题，这一条没处理成功哦（详情见设备日志）。",
    "truncated_note": "\n……（内容有点长，已截断）",
    "timeout": "（这轮处理超时啦——可能是模型响应慢，或者命令跑得太久。"
               "任务可能已经在后台执行了，换个问法，或者让我帮你检查下结果吧）",
    "timeout_steps": "\n\n超时前已经执行的步骤：\n{{digest}}",
    "retry_timeout": "（重试也超时啦，麻烦再发一次哦）",
    "killed_empty": "（已经终止啦，这一轮还没生成正文）",
    "error_wrap": "（出错了：{{error}}）",
    # 出错、终止、超时三处共用：有步骤结果就贴步骤，没有就说明还没跑过工具
    "error_steps": "\n\n出错前已经执行的步骤：\n{{digest}}",
    "steps_unsummarized": "\n\n（上面这些步骤已经执行啦，结果还没完整汇总哦）",
    "steps_none": "\n\n（还没有执行任何工具）",
    "output_limit": "\n\n（内容到输出上限啦，续写也没能收尾；想让我接着写就说「继续」哦。）",
    "empty_answer": "（没有回复内容）",
    "tool_limit": "（已经连续执行 {{rounds}} 步，到单轮上限啦，后面的步骤没有继续。"
                  "要接着做的话说一声，或者把 max_tool_rounds 调大哦）",
    # -- 群友档 --
    "guest_no_model": "（还没配置模型呢，等主人配好了我再来陪你聊~）",
    "guest_llm_fail": "这一轮没接上呢，稍后再试试吧~",
    "guest_empty": "嗯，我在~",
    # -- 模型调用失败（_llm_error_text） --
    "llm_connect": "模型调用失败：{{label}} {{wait}}。请确认模型服务已启动、地址和端口可以访问；"
                   "如果这是局域网模型，检查一下设备是否还在同一网络吧",
    "llm_read_timeout": "模型调用失败：{{label}} 已经连上了，但等待生成响应超时啦；"
                        "服务可能正在计算或负载比较高，稍后再试试，或者在设置里把超时调大一点吧",
    "llm_status": "模型调用失败：{{label}} {{hint}}（HTTP {{status}}）",
    "llm_raw": "模型调用失败：{{label}} {{brief}}",
    "llm_hint_suffix": "。可以在设置页「接口账户」里切换账户，或者换个模型再试试哦",
    # HTTP 状态码对应的说明：只开放提示句，前缀与后缀走上面三条
    "llm_hint_401": "API Key 无效或已过期",
    "llm_hint_402": "账户余额不足",
    "llm_hint_403": "服务商拒绝了这次请求（权限或地区限制）",
    "llm_hint_404": "接口路径不存在，检查一下 base_url 吧",
    "llm_hint_429": "请求被限流了，稍后再试试哦",
    "llm_hint_500": "服务商内部出错了",
    "llm_hint_502": "网关错误，服务商暂时不可用",
    "llm_hint_503": "服务商暂时不可用（上游模型服务异常，或该模型暂无可用节点）",
    "llm_hint_504": "服务商响应超时",
    "llm_hint_other": "服务商返回错误",
    # 连接阶段等待值：局域网端点只等 2 秒
    "llm_wait_local": "2 秒内连不上哦",
    "llm_wait_remote": "连接失败",
}

# 设置页分组：组名 -> key 列表（同一句在两处出现的只挂一次）
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("限流与闲聊", ("rate_user", "rate_group", "chat_at_empty", "chat_prefix_empty")),
    ("指令与确认", ("unknown_cmd", "cmd_unavailable", "need_confirm",
                    "need_hard_confirm", "confirm_timeout", "confirm_cancelled")),
    ("调度", ("busy", "turn_cancelled", "local_busy", "session_closing")),
    ("设备状态", ("status_title", "status_core", "status_mode", "status_qq",
                  "status_version", "status_pending")),
    ("快捷命令", ("shot_busy", "shot_failed", "shot_failed_reason", "shot_caption",
                  "save_image_off", "save_image_none", "save_image_fail", "save_ok",
                  "save_text_no_reply", "save_text_missing", "save_text_no_text",
                  "save_text_fail", "idle")),
    ("记忆与依赖", ("memo_private", "memo_cleared", "memo_empty",
                    "memory_private_group", "memory_no_uin", "memory_private_only",
                    "memory_bad_scope", "memory_none", "memory_head", "memory_item",
                    "memory_more", "memory_clear_hint", "memory_cleared", "memory_nothing",
                    "deps_fail", "deps_head", "deps_note", "deps_item", "deps_ydotool")),
    ("帮助菜单", ("help_title", "help_item", "help_trigger")),
    ("图片与截图", ("image_empty", "image_too_big", "image_fail", "empty_reply",
                    "image_bad_input", "launch_fail", "launch_ok", "shot_missing")),
    ("异常兜底", ("dispatch_fail", "truncated_note", "timeout", "timeout_steps",
                  "retry_timeout", "killed_empty", "error_wrap", "error_steps",
                  "steps_unsummarized", "steps_none", "output_limit",
                  "empty_answer", "tool_limit")),
    ("群友档", ("guest_no_model", "guest_llm_fail", "guest_empty")),
    ("模型调用失败", ("llm_connect", "llm_wait_local", "llm_wait_remote",
                      "llm_read_timeout", "llm_status", "llm_raw", "llm_hint_suffix",
                      "llm_hint_401", "llm_hint_402", "llm_hint_403", "llm_hint_404",
                      "llm_hint_429", "llm_hint_500", "llm_hint_502", "llm_hint_503",
                      "llm_hint_504", "llm_hint_other")),
]

# 中文名：设置页每条文案的标题（顺序与 DEFAULTS 一致）
LABELS: dict[str, str] = {
    "rate_user": "同一人发太快",
    "rate_group": "同一群发太快",
    "unknown_cmd": "指令不存在",
    "cmd_unavailable": "指令不可用（内部错误兜底）",
    "chat_at_empty": "@ 了但没说话",
    "chat_prefix_empty": "只发昵称前缀、没内容",
    "need_confirm": "危险操作待确认",
    "need_hard_confirm": "高危操作待确认",
    "confirm_timeout": "确认超时",
    "confirm_cancelled": "取消了操作",
    "busy": "上一轮还在跑",
    "turn_cancelled": "操作被终止",
    "local_busy": "设备本地正在处理",
    "session_closing": "会话还在收尾",
    "status_title": "标题行",
    "status_core": "核心状态",
    "status_mode": "运行模式",
    "status_qq": "QQ 通道状态",
    "status_version": "版本",
    "status_pending": "有挂起确认时附加",
    "shot_busy": "上一轮没结束，不截图",
    "shot_failed": "截图失败（无详情）",
    "shot_failed_reason": "截图失败（有原因）",
    "shot_caption": "截图附带的文字",
    "save_image_off": "图片功能关闭",
    "save_image_none": "没有可保存的图",
    "save_image_fail": "存图抛异常",
    "save_ok": "保存成功",
    "save_text_no_reply": "没引用消息",
    "save_text_missing": "引用的消息取不到",
    "save_text_no_text": "引用的消息没文本",
    "save_text_fail": "保存文本抛异常",
    "idle": "没有在跑的任务",
    "memo_private": "群缓存：私聊里用",
    "memo_cleared": "群缓存：已清除",
    "memo_empty": "群缓存：本来就没有",
    "memory_private_group": "群记忆：私聊里用",
    "memory_no_uin": "记忆：取不到发言人 QQ",
    "memory_private_only": "私聊记忆：群里不可查",
    "memory_bad_scope": "记忆：scope 参数非法",
    "memory_none": "记忆：查无记忆",
    "memory_head": "记忆：列表标题",
    "memory_item": "记忆：单条",
    "memory_more": "记忆：超过 10 条",
    "memory_clear_hint": "记忆：清空提示",
    "memory_cleared": "记忆：清空结果",
    "memory_nothing": "记忆：无可清空",
    "deps_fail": "依赖：检查抛异常",
    "deps_head": "依赖：汇总行",
    "deps_note": "依赖：免责说明",
    "deps_item": "依赖：单项",
    "deps_ydotool": "依赖：ydotoold 提醒",
    "help_title": "菜单：标题",
    "help_item": "菜单：单条指令",
    "help_trigger": "菜单：触发方式",
    "image_empty": "出站图片为空",
    "image_too_big": "压缩后仍超限",
    "image_fail": "图片发送失败",
    "empty_reply": "回复内容为空",
    "image_bad_input": "入参图片解析失败",
    "launch_fail": "装好但打不开",
    "launch_ok": "装好并已打开",
    "shot_missing": "截图没抓到",
    "dispatch_fail": "全链路兜底",
    "truncated_note": "长回复被截断",
    "timeout": "单轮处理超时",
    "timeout_steps": "超时后已执行步骤",
    "retry_timeout": "重试也超时",
    "killed_empty": "被终止且无正文",
    "error_wrap": "出错外壳",
    "error_steps": "出错前已执行步骤",
    "steps_unsummarized": "步骤结果未汇总",
    "steps_none": "还没跑过工具",
    "output_limit": "到输出上限",
    "empty_answer": "空回复",
    "tool_limit": "到工具步数上限",
    "guest_no_model": "群友：没配模型",
    "guest_llm_fail": "群友：模型异常",
    "guest_empty": "群友：空回复兜底",
    "llm_connect": "连不上",
    "llm_wait_local": "连不上：局域网等待值",
    "llm_wait_remote": "连不上：公网等待值",
    "llm_read_timeout": "等待响应超时",
    "llm_status": "HTTP 状态码外壳",
    "llm_raw": "没有状态码（原始错误）",
    "llm_hint_suffix": "末尾建议（切换账户）",
    "llm_hint_401": "HTTP 401",
    "llm_hint_402": "HTTP 402",
    "llm_hint_403": "HTTP 403",
    "llm_hint_404": "HTTP 404",
    "llm_hint_429": "HTTP 429",
    "llm_hint_500": "HTTP 500",
    "llm_hint_502": "HTTP 502",
    "llm_hint_503": "HTTP 503",
    "llm_hint_504": "HTTP 504",
    "llm_hint_other": "其他状态码",
}

# 每条最多多少字：都是 QQ 里的一句话，写成长文既不像提示也刷屏
MAX_CHARS = 400

_lock = threading.Lock()
_overrides: dict[str, str] | None = None

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _path():
    """覆盖文件：数据根下的 qqtexts.json（惰性解析）"""
    return paths.data_dir() / "qqtexts.json"


def _load() -> dict[str, str]:
    """读覆盖；文件不存在或坏了就当作没有任何覆盖"""
    global _overrides
    if _overrides is not None:
        return _overrides
    data: dict = {}
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for key, val in data.items():
        # 只认已知 key：旧版本删掉的文案不会被重新启用
        if key in DEFAULTS and isinstance(val, str) and val.strip():
            out[key] = val[:MAX_CHARS]
    _overrides = out
    return out


def _render(template: str, fields: dict) -> str:
    """一次性替换 {{key}}；缺的占位符原样留着，方便在设置页一眼看出拼错"""
    return _PLACEHOLDER.sub(
        lambda m: str(fields[m.group(1)]) if m.group(1) in fields else m.group(0),
        template)


def get(key: str, **fields) -> str:
    """取一条文案并填入占位符；未知 key 返回空串（不会让调用点炸掉）"""
    template = _load().get(key) or DEFAULTS.get(key)
    if template is None:
        return ""
    return _render(template, fields)


def template(key: str) -> str:
    """取原始模板（带占位符），给设置页回填用"""
    return _load().get(key) or DEFAULTS.get(key, "")


def is_overridden(key: str) -> bool:
    return key in _load()


def save(items: dict) -> dict:
    """保存覆盖：值 == 默认值的条目直接删掉，不留冗余

    传空串也表示「恢复默认」。整体覆盖写盘，返回落盘后的实际覆盖表。
    """
    global _overrides
    current = dict(_load())
    for key, val in (items or {}).items():
        if key not in DEFAULTS:
            continue
        text = str(val or "")[:MAX_CHARS]
        # 和默认值一字不差就没必要存，将来改默认值时能跟着一起变
        if not text.strip() or text == DEFAULTS[key]:
            current.pop(key, None)
        else:
            current[key] = text
    with _lock:
        target = _path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        _overrides = current
    return current


def reset(keys: list[str] | None = None) -> dict:
    """清掉覆盖：keys 为空表示全部恢复默认"""
    current = dict(_load())
    if not keys:
        return save({k: DEFAULTS[k] for k in current})
    return save({k: "" for k in keys if k in current})


def view() -> dict:
    """设置页数据：分组、中文名、默认值、当前值、是否被覆盖、占位符列表"""
    overrides = _load()
    groups = []
    for name, keys in GROUPS:
        items = []
        for key in keys:
            tpl = overrides.get(key) or DEFAULTS.get(key, "")
            items.append({
                "key": key,
                "label": LABELS.get(key, key),
                "default": DEFAULTS.get(key, ""),
                "value": tpl,
                "overridden": key in overrides,
                "placeholders": sorted(set(_PLACEHOLDER.findall(DEFAULTS.get(key, "")))),
            })
        groups.append({"name": name, "items": items})
    return {"groups": groups, "count": len(DEFAULTS), "updated": _mtime()}


def _mtime() -> float:
    try:
        return _path().stat().st_mtime
    except OSError:
        return 0.0


def reload() -> None:
    """丢掉内存缓存，下次 get() 重新读盘（测试与手工改文件时用）"""
    global _overrides
    with _lock:
        _overrides = None
