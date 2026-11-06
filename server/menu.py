"""OneBot 固定命令菜单：命令表、解析、权限与危险动作确认

这些命令不经过 AI：命中就直连既有动作（查状态、截图、取消、存图存文、检查依赖），
省掉一整轮模型调用，也不会因为模型误判而走偏。把「叫什么名字、谁能用、
要不要二次确认」集中成一张表，路由和帮助文案都从这里取。

全部是纯函数（不做 I/O、不依赖进程状态），onebot.py 只负责把结果接到具体动作，
单测直接对着这里跑。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_PREFIX = "meido"
# 去掉标点后的整句长度上限：超过就不当别名（长句明显是聊天）
MAX_ALIAS_CHARS = 12

# 前缀与斜杠指令解析
_SLASH_RE = re.compile(r"^/([A-Za-z]+)(?:\s+([\s\S]+))?$")
_ALIAS_LEAD_RE = re.compile(r"^(麻烦|帮我|给我|请)+")
_ALIAS_TAIL_RE = re.compile(r"(一下)?[吧啊呀呢哦了。！？!?.,，、…~\s]+$")
_ALIAS_PUNCT_RE = re.compile(r"[。！？!?.,，、…~：:；;\"'“”‘’\s]+")

# 和 policy._CONFIRM_RE 保持一致：QQ 侧回「确认」才放行挂起的危险操作
_CONFIRM_RE = re.compile(
    r"^(好|好的|行|可以|确认|确定|是|是的|继续|执行|重启|关机|ok|okay|yes|y|sure|go)"
    r"[。！!.]?$",
    re.IGNORECASE,
)
# 远程高危操作只认这一句，普通「确认/好」不放行
_CONFIRM_HARD_RE = re.compile(r"^确认执行[。！!.]?$", re.IGNORECASE)


@dataclass(frozen=True)
class Spec:
    """一条固定命令

    key       内部动作名，onebot 按它分发到具体处理函数
    slash     斜杠指令名（可多个，第一个用于帮助文案）
    phrases   口语别名（整句匹配，不做关键词包含）
    summary   帮助文案里的一句话说明
    dangerous 是否属于危险动作：命中后不直接执行，先要一次确认
    hard      是否只认「确认执行」这种硬确认
    """

    key: str
    slash: tuple[str, ...]
    phrases: tuple[str, ...] = ()
    summary: str = ""
    dangerous: bool = False
    hard: bool = False


# 命令表：顺序即帮助文案的顺序。
# 注意：没有「安装依赖」——依赖只检查、不安装，避免聊天窗口能改设备环境。
SPECS: tuple[Spec, ...] = (
    Spec("help", ("help", "menu"),
         ("菜单", "帮助", "指令", "命令", "有什么指令", "能用什么指令"),
         "列出可用指令"),
    Spec("status", ("status",),
         ("状态", "什么状态", "在吗", "在不在", "还好吗", "怎么样了",
          "什么情况", "进度", "连上了吗", "活着吗"),
         "设备状态"),
    Spec("shot", ("shot",),
         ("截个屏", "截个图", "截屏", "截图", "来张图", "给张图", "拍张照",
          "拍张图", "看看屏幕", "看看画面", "现在什么画面",
          "现在屏幕上是什么", "发个图", "发张图"),
         "截当前画面"),
    Spec("cancel", ("cancel",),
         ("停", "停下", "停止", "停一下", "别做了", "取消", "中断",
          "算了", "不用了"),
         "取消当前任务"),
    Spec("save_image", ("save_image",),
         ("存图", "存起来", "保存这张"),
         "保存引用的图片"),
    Spec("save_text", ("save_text",),
         ("保存文本", "存文本", "保存这段", "存这段", "保存这句话", "存这段文字"),
         "保存引用的文本"),
    Spec("deps", ("deps", "dependencies", "dep"),
         ("检查依赖", "查依赖", "依赖检查", "依赖状态", "检查环境", "环境检查"),
         "检查依赖状态（只检测，不安装）"),
    Spec("memo_clear", ("memo", "memoclear"),
         ("清除本群缓存", "清空本群缓存", "忘掉群聊", "忘掉群聊内容"),
         "清除本群常识缓存（仅管理员）"),
)

SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}
# 斜杠名 → 内部动作名；指令表里没登记的斜杠指令按聊天处理
SLASH_MAP: dict[str, str] = {name: s.key for s in SPECS for name in s.slash}
COMMANDS: tuple[str, ...] = tuple(s.slash[0] for s in SPECS)
ALIASES: dict[str, str] = {p: s.key for s in SPECS for p in s.phrases}


@dataclass
class Parsed:
    kind: str          # 内部动作名；chat 表示不匹配固定命令
    text: str = ""     # chat 时的正文，或指令后面的参数


def prefix_for(nickname: str) -> str:
    """触发前缀就是软件里配置的昵称；昵称为空时退回默认词"""
    return (nickname or "").strip() or DEFAULT_PREFIX


def match_slash(text: str) -> Parsed | None:
    """只认斜杠指令（/status 这类），不认识的一律返回 None"""
    m = _SLASH_RE.match((text or "").strip())
    if not m:
        return None
    kind = SLASH_MAP.get(m.group(1).lower())
    if not kind:
        return None
    return Parsed(kind, (m.group(2) or "").strip())


def parse(text: str, nickname: str) -> Parsed | None:
    """解析「昵称 + 子命令或正文」。不匹配前缀返回 None

    规则：忽略首尾空白、ASCII 不区分大小写，前缀后可以直接跟子命令，
    也可以空格再接正文。
    """
    body = (text or "").strip()
    prefix = prefix_for(nickname)
    if len(body) < len(prefix):
        return None
    if body[:len(prefix)].casefold() != prefix.casefold():
        return None
    rest = body[len(prefix):].strip()
    if not rest:
        return Parsed("chat", "")
    slash = match_slash(rest)
    if slash:
        return slash
    return Parsed("chat", rest)


def strip_prefix(text: str, nickname: str) -> str:
    """群聊里已经靠 @ 触发，再额外剥掉一层前缀，免得聊天正文里带着它"""
    body = (text or "").strip()
    prefix = prefix_for(nickname)
    if len(body) >= len(prefix) and body[:len(prefix)].casefold() == prefix.casefold():
        return body[len(prefix):].strip()
    return body


def match_alias(text: str) -> tuple[str, str] | None:
    """整句匹配口语别名，返回 (别名, 动作)；不做关键词包含

    先去掉首尾标点做一次精确匹配（避免把「算了」「别做了」的尾字误删），
    再容忍「麻烦/帮我/给我/请」开头与「吧/啊/…」「一下」结尾。
    """
    body = (text or "").strip()
    if not body:
        return None
    bare = _ALIAS_PUNCT_RE.sub("", body)
    if bare and bare in ALIASES:
        return bare, ALIASES[bare]
    cleaned = _ALIAS_LEAD_RE.sub("", bare)
    cleaned = _ALIAS_TAIL_RE.sub("", cleaned)
    if not cleaned or len(cleaned) > MAX_ALIAS_CHARS:
        return None
    if cleaned in ALIASES:
        return cleaned, ALIASES[cleaned]
    return None


def punctuation_stripped(text: str) -> str:
    """去掉常见标点与空白；路由用它对「短句未命中别名」做日志判定"""
    return _ALIAS_PUNCT_RE.sub("", (text or "").strip())


def spec_for(kind: str) -> Spec | None:
    return SPEC_BY_KEY.get(str(kind or ""))


def authorize(kind: str, *, is_admin: bool, in_group: bool = False,
              group_admin_commands: bool = True) -> bool:
    """固定命令的权限判定

    固定命令一律只给管理员：白名单群里的普通成员始终只能只读聊天；
    管理员在群里下指令还要 `group_admin_commands` 放行。
    """
    spec = spec_for(kind)
    if spec is None or not is_admin:
        return False
    if in_group and not group_admin_commands:
        return False
    return True


def is_confirm_text(text: str) -> bool:
    return bool(_CONFIRM_RE.match((text or "").strip()))


def is_hard_confirm(text: str) -> bool:
    """远程高危操作的确认口令：只认「确认执行」"""
    return bool(_CONFIRM_HARD_RE.match((text or "").strip()))


def needs_confirm(spec: Spec | None) -> bool:
    return bool(spec and spec.dangerous)


def confirm_decision(spec: Spec | None, text: str) -> str:
    """对危险命令的一回答复做判定

    返回：
      execute   可以执行
      ask_hard  普通「确认」不够，提示必须回「确认执行」
      cancel    其它任何输入都当作放弃
    非危险命令一律 execute（调用方不必再判断）。
    """
    if not needs_confirm(spec):
        return "execute"
    if spec.hard:
        if is_hard_confirm(text):
            return "execute"
        if is_confirm_text(text):
            return "ask_hard"
        return "cancel"
    return "execute" if is_confirm_text(text) else "cancel"


def help_text(nickname: str = "") -> str:
    """帮助文案：固定命令一览（危险命令会单独标出来）"""
    prefix = prefix_for(nickname)
    lines = ["可用指令（直接回复即可，不经过 AI）："]
    for spec in SPECS:
        flag = "（危险操作，需确认）" if spec.dangerous else ""
        lines.append(f"/{spec.slash[0]} {spec.summary}{flag}")
    lines.append(f"触发前缀：{prefix}（例：{prefix} /status）")
    return "\n".join(lines)
