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

from . import qqtext

DEFAULT_PREFIX = "meido"
# 去掉标点后的整句长度上限：超过就不当别名（长句明显是聊天）
MAX_ALIAS_CHARS = 12

# 前缀与斜杠指令解析
_SLASH_RE = re.compile(r"^/([A-Za-z]+)(?:\s+([\s\S]+))?$")
# /memory 后面跟这些词时按「清空」处理（英文要求词边界，中文直接前缀）
_MEMORY_CLEAR_RE = re.compile(
    r"^(?:(?:clear|forget|reset)(?:\s+|$)|(?:清空|清除|删除|删掉)\s*)([\s\S]*)$",
    re.IGNORECASE)
# 「像斜杠指令」的宽匹配：只用来给未登记的指令一句明确提示，不参与执行
_SLASH_LIKE_RE = re.compile(r"^/([A-Za-z]+)(?:\s+[\s\S]*)?$")
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
    Spec("memory", ("memory", "mem"),
         ("记忆", "查看记忆", "记忆列表", "我的记忆", "群记忆"),
         "查看当前会话的长期记忆（发「清空记忆」可清空）"),
    Spec("memory_clear", ("memclear",),
         ("清空记忆", "清除记忆", "清空群记忆"),
         "清空当前或指定 scope 的长期记忆（仅管理员）",
         dangerous=True),
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
    arg = (m.group(2) or "").strip()
    # /memory clear ... 是破坏性操作，单独走 memory_clear 的确认流程；
    # 不能把查看记忆也标成危险命令，否则每次 /memory 都要回一句「确认」。
    if kind == "memory":
        cm = _MEMORY_CLEAR_RE.match(arg)
        if cm:
            return Parsed("memory_clear", (cm.group(1) or "").strip())
    return Parsed(kind, arg)


def unknown_slash(text: str) -> str:
    """像斜杠指令但没登记：返回「/名字」用于明确提示，否则返回空串

    未登记的斜杠不再当成普通聊天丢给模型：那样模型可能既回一句
    「没有这条指令」、又自己调截图工具，一条消息变成两次回复。
    """
    m = _SLASH_LIKE_RE.match((text or "").strip())
    if not m:
        return ""
    name = m.group(1)
    return "" if name.lower() in SLASH_MAP else "/" + name


# 昵称后面必须跟空白才算「叫机器人」：`露露你好` 是聊天，`露露 菜单` 才是指令
_NICK_SPLIT_RE = re.compile(r"\s+")
# 中文指令名：把别名里较长的短句排除在「精确指令名」之外，只留短词
_MAX_ZH_COMMAND_CHARS = 4


def zh_names() -> dict[str, str]:
    """中文指令名 → 内部动作名（用户只看中文，英文 key 只是内部动作名）"""
    names: dict[str, str] = {}
    for spec in SPECS:
        for phrase in spec.phrases:
            if len(phrase) <= _MAX_ZH_COMMAND_CHARS:
                names.setdefault(phrase, spec.key)
    return names


ZH_COMMANDS: dict[str, str] = zh_names()


def _split_nick(text: str, nickname: str) -> tuple[bool, str]:
    """按「昵称 + 空格」切开；返回 (是否带昵称前缀, 后面的正文)"""
    body = (text or "").strip()
    prefix = prefix_for(nickname)
    if len(body) < len(prefix):
        return False, body
    if body[:len(prefix)].casefold() != prefix.casefold():
        return False, body
    rest = body[len(prefix):]
    # 昵称后面既没空白也没别的字符（`露露`/`露露  `）：算叫了机器人，只是没带话
    if not rest.strip():
        return True, ""
    if not rest[:1].isspace():
        return False, body
    return True, rest.strip()


def parse(text: str, nickname: str) -> Parsed | None:
    """解析「昵称 + 空格 + 指令或正文」。不带昵称前缀返回 None

    昵称后面第一个词精确命中中文指令名（或旧斜杠指令）才算指令；
    其余一律按闲聊处理。`露露` 单独一句是空闲聊。
    """
    called, rest = _split_nick(text, nickname)
    if not called:
        return None
    if not rest:
        return Parsed("chat", "")
    head = _NICK_SPLIT_RE.split(rest, 1)[0]
    kind = ZH_COMMANDS.get(head)
    if kind and len(head) <= len(rest):
        arg = rest[len(head):].strip()
        if kind == "memory":
            cm = _MEMORY_CLEAR_RE.match(arg)
            if cm:
                return Parsed("memory_clear", (cm.group(1) or "").strip())
        return Parsed(kind, arg)
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


def authorize(kind: str, *, is_admin: bool) -> bool:
    """固定命令的权限判定（唯一入口）

    群白名单在 onebot 入站就判过了（非白名单群零出站）；这里只判身份：
    主人 QQ 在私聊与白名单群里可以执行固定命令；
    群友与陌生人一律不允许执行固定命令，只能走只读聊天。
    """
    if spec_for(kind) is None or not is_admin:
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


# QQ 远程额外高危命令：现有 tools/policy 规则之外的补充。
# 重点覆盖「卸载软件 / 删除目录 / 清理包缓存」这类模型容易顺手执行的命令，
# 命中后按硬确认处理（必须回「确认执行」）。
_QQ_DANGER_RULES: tuple[tuple[str, str], ...] = (
    ("flatpak_uninstall", r"\bflatpak\b[^\n]*\b(uninstall|remove)\b"),
    ("snap_remove", r"\bsnap\b[^\n]*\b(remove|purge)\b"),
    ("pacman_clean", r"\b(?:pacman|paru|yay)\b[^\n]*\s-(?:Sc|ScC|Scc)\b"),
    ("apt_autoremove", r"\b(?:apt|apt-get)\b[^\n]*\bautoremove\b"),
    ("brew_uninstall", r"\bbrew\b[^\n]*\b(uninstall|remove)\b"),
    ("rpm_erase", r"\brpm\b[^\n]*\s-(?:e|E)\b"),
)


def remote_command_danger(cmd: str, depth: int = 0) -> str:
    """QQ 远程额外危险命令：返回命中的规则名，未命中返回空串

    只做补充判定，命中后由调用方走「确认执行」硬确认。
    嵌套的 sh -c 内容同样递归检查，避免换个壳绕过。
    """
    text = str(cmd or "").strip()
    if not text:
        return ""
    for rule, pattern in _QQ_DANGER_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return rule
    if depth < 3:
        for inner in re.findall(
                r"(?:sh|bash|zsh)\s+-c\s+[\"']([^\"']+)[\"']", text, re.IGNORECASE):
            hit = remote_command_danger(inner, depth + 1)
            if hit:
                return hit
    return ""


def help_name(spec: Spec) -> str:
    """帮助文案里给人看的中文指令名：取该命令最短的中文词"""
    return min(spec.phrases, key=len) if spec.phrases else spec.slash[0]


def help_text(nickname: str = "") -> str:
    """帮助文案：只列中文指令，写成「昵称 + 空格 + 指令」"""
    prefix = prefix_for(nickname)
    lines = [qqtext.get("help_title", prefix=prefix)]
    for spec in SPECS:
        flag = "（危险操作，需确认）" if spec.dangerous else ""
        lines.append(qqtext.get("help_item", name=help_name(spec),
                                summary=spec.summary, flag=flag))
    lines.append(qqtext.get("help_trigger", prefix=prefix))
    return "\n".join(lines)
