"""工具执行策略：只看「本轮用户消息 + 挂起意图 + 工具参数」，不看模型正文。

为什么需要这一层：模型会在历史里读到自己上一轮答应过的操作（比如「重启我现在
就执行」），即使本轮用户只是问了别的，也可能把那条旧承诺当成授权直接调工具。
所以这里不信任任何历史文本，只认：

1. 本轮用户消息里明确的电源意图；
2. 会话里显式保存、且仍然有效（未过期、boot_id 没变、同一端）的 pending_intent；
3. 用户刚刚点过「确认」的挂起操作（由调用方显式传入 approved=True）。

`PERMISSION_MODE == "full"` 不能绕过这一层 —— 它只决定「要不要弹审批」，
不决定「模型有没有资格执行」。
"""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass

# pending_intent 默认有效期：过期后旧密码 / 旧确认不再构成授权
INTENT_TTL = 600.0

# 电源动作（也是 pending_intent 的 kind 之一）。
# restart 是 systemctl 的子命令，但「systemctl restart <service>」只是服务操作，
# 不能一概当成电源命令；具体的电源判定见 _segment_action。
POWER_ACTIONS = {"reboot", "poweroff", "halt", "shutdown", "restart", "kexec"}

# 只读会话（QQ 群聊里的普通成员）允许调用的工具。
# 群友那一轮是「纯闲聊」：零工具，连截图/读文件都不给（见 classify_remote_tool
# 与 guest 轮次），所以这里刻意收成空集，防止只读白名单被当成后门。
READONLY_TOOLS: set[str] = set()

# systemctl restart 指向这些 unit 时等同于重启/关机，必须继续拦截
_POWER_UNIT_RE = re.compile(
    r"(reboot|poweroff|halt|kexec|shutdown|final)", re.IGNORECASE)

# 明确的中文/英文电源意图
_POWER_TEXT_RE = re.compile(
    r"重启|重新启动|关机|关掉电脑|关闭电脑|断电|拔电|reboot|power[\s-]*off|"
    r"shut[\s-]*down|restart",
    re.IGNORECASE,
)
# 否定说法：用户说「别重启」时不算授权
_NEG_POWER_RE = re.compile(
    r"(不要|不用|别|无需|不必|先别|禁止|don't|do not|no need to)"
    r"[^\n，。；,;]{0,6}(重启|关机|断电|reboot|shutdown|power[\s-]*off|restart)",
    re.IGNORECASE,
)

# 明确要求切换 SteamOS 会话（桌面模式 / 游戏模式）
_MODE_TEXT_RE = re.compile(
    r"桌面模式|游戏模式|切到桌面|切换到桌面|进桌面|进入桌面|切回游戏|回游戏模式|回到游戏|"
    r"switch[\s-]*to[\s-]*(?:desktop|game)|desktop[\s-]*mode|game[\s-]*mode|steamosctl",
    re.IGNORECASE,
)

# 确认类回复（ask_user 问「要重启吗」之后用户回「好」）
_CONFIRM_RE = re.compile(
    r"^(好|好的|行|可以|确认|确定|是|是的|继续|执行|重启|关机|ok|okay|yes|y|sure|go)"
    r"[。！!.]?$",
    re.IGNORECASE,
)

# 密码样文本：不带空格、不带问句/句子标点、不是明显的闲聊或提问
_SECRET_HINT_RE = re.compile(
    r"怎么|如何|为什么|什么|游戏|攻略|画面|截图|帮我|请问|你好|谢谢|"
    r"当前|打不开|卡住|教程|搜索|查一下",
    re.IGNORECASE,
)

# ask_user 的提问里出现这些词时，登记待授权意图
_PASSWORD_ASK_RE = re.compile(r"密码|password|sudo|口令|passwd", re.IGNORECASE)
_REBOOT_ASK_RE = re.compile(r"重启|reboot|restart", re.IGNORECASE)
_SHUTDOWN_ASK_RE = re.compile(r"关机|断电|poweroff|shutdown", re.IGNORECASE)
# 「重启游戏 / 重启应用」不是电源操作，别把它登记成系统重启授权
_NON_SYSTEM_ASK_RE = re.compile(r"游戏|应用|程序|软件|关卡|角色|存档", re.IGNORECASE)

_SEGMENT_SPLIT_RE = re.compile(r"(;|&&|\|\||\||\n)")
_CMD_PREFIXES = {"sudo", "doas", "env", "nohup", "time", "command", "exec",
                 "nice", "stdbuf"}
_SHELL_PROGS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_SUDO_OPTS_WITH_VALUE = {"-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U"}
# systemctl 里带独立值的选项：解析时把值一起跳过
_SYSTEMCTL_OPTS_WITH_VALUE = {
    "-t", "--type", "-s", "--signal", "-p", "--property", "-n", "--lines",
    "-o", "--output", "-H", "--host", "-M", "--machine", "--state",
    "--kill-whom", "--kill-value", "--job-mode", "--message",
}


@dataclass
class TurnContext:
    """一轮对话的授权上下文。

    字段都有默认值：老调用点可以完全不传，行为等价于「没有本轮授权」，
    使工具层默认拒绝电源命令，而不是默认放行。
    """
    sid: str = ""
    turn_id: str = ""
    user_text: str = ""      # 本轮用户消息原文（不是历史拼接结果）
    client: str = ""
    device: str = ""
    boot_id: str = ""
    # 只读会话：QQ 群聊里普通成员的轮次只能聊天，任何会动设备的工具都不许。
    # 名单外的工具一律拒绝，这样以后新增的操作类工具默认也是拒绝的。
    readonly: bool = False
    # 强制二次确认：QQ 这类「远程、无人值守」的入口即使处于 full 模式，
    # 危险工具也必须等用户回「确认」才执行
    force_confirm: bool = False


@dataclass
class PolicyDecision:
    allowed: bool
    code: str = "ok"
    reason: str = ""
    action: str | None = None
    intent: dict | None = None   # 需要回写会话的 pending_intent（状态变化时）


# ---------- 命令识别 ----------

def _segments(cmd: str) -> list[str]:
    return [s for s in _SEGMENT_SPLIT_RE.split(cmd or "")
            if s and not _SEGMENT_SPLIT_RE.fullmatch(s)]


def _tokens(seg: str) -> list[str]:
    try:
        return shlex.split(seg, comments=False, posix=True)
    except Exception:
        return seg.split()


def _strip_prefixes(toks: list[str]) -> list[str]:
    """剥掉 sudo / env / VAR=VALUE 等前缀，留下真实程序及其参数"""
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _CMD_PREFIXES:
            i += 1
            # sudo 的 -u user / -S 之类选项一起跳过
            while i < len(toks) and toks[i].startswith("-"):
                opt = toks[i]
                i += 2 if (opt in _SUDO_OPTS_WITH_VALUE and i + 1 < len(toks)) else 1
            continue
        if re.fullmatch(r"[A-Za-z_]\w*=\S*", t) or t.startswith("-"):
            i += 1
            continue
        break
    return toks[i:]


def _segment_action(seg: str, depth: int = 0) -> str | None:
    """一小段命令里是否在调电源动作；返回 reboot/poweroff/... 或 None"""
    if depth > 3:
        return None
    toks = _strip_prefixes(_tokens(seg))
    if not toks:
        return None
    prog = toks[0].strip("\"'").replace("\\", "")
    prog = prog.rsplit("/", 1)[-1]
    rest = toks[1:]

    # sh -c "systemctl reboot" / bash -c '...'
    if prog in _SHELL_PROGS:
        if "-c" in rest:
            idx = rest.index("-c")
            if idx + 1 < len(rest):
                return power_action(rest[idx + 1], depth + 1)
        return None
    if prog == "eval" and rest:
        return power_action(" ".join(rest), depth + 1)

    if prog in POWER_ACTIONS:
        return "shutdown" if prog == "halt" else prog
    if prog == "systemctl":
        # 跳过 systemctl 的选项，取真正的子命令和它后面的 unit。
        # 带值的选项（-s SIGTERM / -H host / --state=...）要连值一起跳过，
        # 否则值会被当成子命令，漏掉后面的 restart/reboot
        args, i = [], 0
        while i < len(rest):
            a = rest[i]
            if a.startswith("-"):
                if "=" in a or a in _SYSTEMCTL_OPTS_WITH_VALUE:
                    i += 2 if a in _SYSTEMCTL_OPTS_WITH_VALUE else 1
                else:
                    i += 1
                continue
            args.append(a)
            i += 1
        if not args:
            return None
        sub, units = args[0], args[1:]
        if sub in ("reboot", "poweroff", "halt", "shutdown", "kexec"):
            return "shutdown" if sub == "halt" else sub
        if sub == "restart":
            # systemctl restart <service> 是服务操作；只有指向电源 target/
            # systemd-*.service 时才按电源命令处理（宁严勿松）
            if any(_POWER_UNIT_RE.search(u) for u in units):
                return "reboot"
            return None
        return None
    if prog == "steamosctl":
        # steamosctl 的规则保持原样：只认 POWER_ACTIONS 子命令
        for arg in rest:
            if arg.startswith("-"):
                continue
            if arg in POWER_ACTIONS:
                return "shutdown" if arg == "halt" else arg
            break
        return None
    return None


def power_action(cmd: str, depth: int = 0) -> str | None:
    """命令串里有没有电源动作：reboot / poweroff / shutdown / restart / halt"""
    for seg in _segments(cmd):
        act = _segment_action(seg, depth)
        if act:
            return act
    return None


def _segment_steamosctl(seg: str, depth: int = 0) -> str | None:
    """steamosctl 的会话切换：会重建会话、断开当前连接，同样属于破坏性操作"""
    if depth > 3:
        return None
    toks = _strip_prefixes(_tokens(seg))
    if not toks:
        return None
    prog = toks[0].strip("\"'").replace("\\", "").rsplit("/", 1)[-1]
    if prog in _SHELL_PROGS and "-c" in toks[1:]:
        idx = toks.index("-c", 1)
        return steamosctl_action(toks[idx + 1], depth + 1) if idx + 1 < len(toks) else None
    if prog != "steamosctl":
        return None
    for arg in toks[1:]:
        if arg.startswith("-"):
            continue
        if arg.startswith(("switch-to-", "boot-into-")):
            return f"steamosctl {arg}"
        break
    return None


def steamosctl_action(cmd: str, depth: int = 0) -> str | None:
    for seg in _segments(cmd):
        act = _segment_steamosctl(seg, depth)
        if act:
            return act
    return None


def risky_action(name: str, args: dict) -> str | None:
    """需要「本轮用户明确意图」才放行的操作：电源 + SteamOS 会话切换"""
    if name != "run_command":
        return None
    cmd = str((args or {}).get("command") or "")
    return power_action(cmd) or steamosctl_action(cmd)


# ---------- 三档远程风险（只作用于 QQ 远程；本机 remote=False 不变） ----------

@dataclass(frozen=True)
class RiskDecision:
    level: str          # deny / hard_confirm / normal
    rule: str = ""
    reason: str = ""


REMOTE_NORMAL = RiskDecision("normal")


def _R(**kw) -> RiskDecision:
    return RiskDecision(**kw)


# token 边界：避免把 "charm" 里的 rm、"screenshot" 里的 shot 当成命令
def _tok(word: str) -> str:
    return rf"(?<![\w.-]){word}(?![\w.-])"


# 高危：直接拒绝，不生成挂起（写错了没有回头路）
_DENY_RULES: tuple[tuple[str, str, str], ...] = (
    ("rm", r"(?:(?<=[\s;&|(`/])|^)rm(?![\w.-])",
     "删除命令（rm）在 QQ 远程一律拒绝"),
    ("rmdir_recursive", _tok("rmdir") + r"[^\n]*(?:-p|--parents)",
     "rmdir 递归删除在 QQ 远程一律拒绝"),
    ("shred", _tok("shred") + r"(?=\s|$)", "shred 会不可恢复地擦除文件"),
    ("dd", _tok("dd") + r"(?=\s|$)", "dd 直接写块设备，风险不可控"),
    ("mkfs", _tok(r"mkfs(?:\.\w+)?") + r"(?=\s|$)", "格式化文件系统"),
    ("mke2fs", _tok("mke2fs") + r"(?=\s|$)", "格式化文件系统"),
    ("wipefs", _tok("wipefs") + r"(?=\s|$)", "擦除文件系统签名"),
    ("fdisk_write", _tok("fdisk") + r"(?!\s+(?:-l|--list)(?:\s|$))",
     "fdisk 分区写操作"),
    ("parted_write", _tok("parted") + r"[^\n]*\b(?:mkpart|mklabel|rm|resizepart|set)\b",
     "parted 分区写操作"),
    ("copy_to_dev", r"\b(?:mv|cp)\b[^\n]*\s/dev/", "把文件复制/移动到 /dev"),
    ("redirect_dev", r">>?\s*/dev/(?:sd|nvme|mmcblk)", "重定向写入块设备"),
    ("fork_bomb", r":\s*\(\s*\)\s*\{[^\n]*\|[^\n]*&\s*\}", "fork bomb"),
    ("chmod_root", r"chmod\s+-[A-Za-z]*R[A-Za-z]*\s+777\s+/", "chmod -R 777 /"),
    ("find_root_delete", r"find\s+/(?:\s|$)[^\n]*-delete", "find / -delete"),
    ("critical_file", r"(?:>>?|tee|cp|mv|install|truncate|chmod|chown|ln)\s+"
                      r"[^\n]*(?:/etc/(?:passwd|shadow|gshadow|sudoers)|"
                      r"/boot(?:/|\s|$)|(?:~|/home/[^/\s]+)?/\.ssh(?:/|\s|$))",
     "写系统关键文件（/etc/passwd、/boot、~/.ssh 等）"),
    ("systemctl_critical", r"systemctl\s+(?:stop|disable|mask)\s+"
                           r"(?:ssh|sshd|network|NetworkManager|systemd-logind|"
                           r"dbus|display-manager|gdm|sddm|steam|gamescope)",
     "停用/屏蔽关键系统服务"),
    ("history_clear", _tok("history") + r"\s+-c(?:\s|$)", "清空命令历史"),
)

# 高危但可确认：必须回「确认执行」
_HARD_RULES: tuple[tuple[str, str, str], ...] = (
    ("sudo", r"(?:(?<=[\s;&|(`])|^)(?:sudo|doas)(?=\s)",
     "sudo/doas 提权"),
    ("pipe_to_shell", r"(?:curl|wget)[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|fish)",
     "把网络内容直接管道给 shell"),
    ("shell_c_remote", r"(?:sh|bash|zsh)\s+-c\s+[^\n]*(?:curl|wget|base64\s+-d)",
     "在子 shell 里执行远程内容"),
    ("eval_remote", r"\b(?:eval|exec)\b[^\n]*(?:curl|wget|\$\()",
     "eval/exec 执行远程内容"),
    ("base64_pipe", r"base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:sh|bash|zsh)",
     "base64 解码后管道给 shell"),
    ("write_etc_usr", r"(?:>>?|tee|cp|mv|install|chmod|chown|ln|rm)\s+"
                      r"[^\n]*(?<![\w])/(?:etc|usr)(?:/|\s|$)",
     "写入 /etc 或 /usr"),
    ("pkg_install", r"\b(?:apt|apt-get|dnf|yum|zypper)\b[^\n]*"
                    r"\b(?:install|remove|purge|erase|upgrade|dist-upgrade)\b",
     "包管理器安装/卸载"),
    ("pacman", r"\b(?:pacman|paru|yay)\b[^\n]*\s-(?:S|R|U|Sy|Syu|Rs|Rns)\b",
     "pacman 安装/卸载"),
    ("pip_install", r"\b(?:pip|pip3)\b[^\n]*(?:install|uninstall)", "pip 安装/卸载"),
    ("npm_global", r"\bnpm\s+(?:install|i)\b[^\n]*(?:\s-g\b|--global)",
     "npm 全局安装"),
    ("crontab_edit", r"\bcrontab\b\s+(?!-l(?:\s|$))", "修改 crontab"),
    ("systemctl_restart", r"systemctl\s+restart\s+(?!reboot|poweroff|halt|shutdown|kexec)\S",
     "重启非电源服务"),
    ("chmod_exec", r"chmod\s+\+x\s+(\S+)[^\n]*(?:&&|;|\n)\s*(?:\./|/)\S+",
     "chmod +x 后立即执行"),
    ("clone_exec", r"git\s+clone[^\n]*(?:&&|;)[^\n]*(?:\./|bash|sh|make|npm|pip)",
     "git clone 后立即执行"),
)


def _segments_simple(cmd: str) -> list[str]:
    """按 ; && || 与换行切段，够用来判断每条子命令（不做完整 shell 解析）"""
    return [p.strip() for p in re.split(r"(?:&&|\|\||;|\n)", cmd or "") if p.strip()]


def _match_rules(cmd: str, rules) -> RiskDecision | None:
    for rule, pattern, reason in rules:
        if re.search(pattern, cmd, re.IGNORECASE):
            level = "deny" if rules is _DENY_RULES else "hard_confirm"
            return RiskDecision(level, rule, reason)
    return None


def classify_remote_command(cmd: str, depth: int = 0) -> RiskDecision:
    """把一条 QQ 远程命令分成 deny / hard_confirm / normal 三档。

    规则按 token 边界匹配，不用「包含子串」，避免把聊天里的字误判成命令。
    嵌套的 bash -c / sh -c 会递归看里层（最多 3 层）。
    """
    text = str(cmd or "").strip()
    if not text:
        return REMOTE_NORMAL
    # 分段后逐段判，避免 "echo rm" 里的 rm 单独一行也能被抓到；
    # 整段匹配则能覆盖 "sudo rm -rf /" 这类组合
    for seg in [text] + _segments_simple(text):
        hit = _match_rules(seg, _DENY_RULES)
        if hit:
            return hit
    for seg in [text] + _segments_simple(text):
        hit = _match_rules(seg, _HARD_RULES)
        if hit:
            return hit
    if depth < 3:
        for inner in re.findall(
                r"(?:sh|bash|zsh)\s+-c\s+[\"']([^\"']+)[\"']", text, re.IGNORECASE):
            nested = classify_remote_command(inner, depth + 1)
            if nested.level != "normal":
                return nested
        for inner in re.findall(r"\$\(([^)]+)\)", text):
            nested = classify_remote_command(inner, depth + 1)
            if nested.level != "normal":
                return nested
    return REMOTE_NORMAL


def _classify_path(path: str) -> RiskDecision:
    p = str(path or "").strip()
    if not p:
        return REMOTE_NORMAL
    p = p.replace("\\", "/")
    if re.search(r"/etc/(?:passwd|shadow|gshadow|sudoers)(?:$|/)", p):
        return _R(level="deny", rule="critical_file", reason="写系统关键文件")
    if p.startswith("/boot") or "/.ssh" in p or p.startswith("/dev/") or "~/.ssh" in p:
        return _R(level="deny", rule="critical_path", reason="写 /boot、~/.ssh 或 /dev")
    if p.startswith("/etc/") or p.startswith("/usr/"):
        return _R(level="hard_confirm", rule="write_etc_usr", reason="写 /etc 或 /usr")
    return REMOTE_NORMAL


def classify_remote_tool(name: str, args: dict, remote: bool = True) -> RiskDecision:
    """QQ 远程的工具风险：文件写入统一走路径判定，命令走 classify_remote_command"""
    if not remote:
        return REMOTE_NORMAL
    name = str(name or "")
    args = args or {}
    if name == "run_command":
        return classify_remote_command(str(args.get("command") or ""))
    if name in ("write_file", "edit_file"):
        return _classify_path(str(args.get("path") or args.get("file") or ""))
    if name == "install_game":
        return _R(level="hard_confirm", rule="install_game", reason="远程安装游戏")
    if name == "self_update":
        return _R(level="hard_confirm", rule="self_update", reason="远程自更新会替换程序")
    return REMOTE_NORMAL


_HARD_CONFIRM_RE = re.compile(r"^确认执行[。！!.]?$")


def is_hard_confirm(text: str) -> bool:
    return bool(_HARD_CONFIRM_RE.match((text or "").strip()))


def is_soft_confirm(text: str) -> bool:
    return bool(_CONFIRM_RE.match((text or "").strip()))


def blocks_in_locked_shot(name: str, args: dict) -> bool:
    """截图轮（locked_shot）额外禁止的操作：电源 / 会话切换。

    截图轮的普通屏幕操作由 tools.touches_screen 拦；这两类命令不碰屏幕，
    所以必须在这里单独拦，否则模型会一边「看图回答」一边把机器关掉或切走会话。
    """
    return bool(risky_action(name, args))


# ---------- 按入口区分单轮策略 ----------

# 插件发起的一轮属于哪个入口：大屏聊天页 / 大屏询问页 / 截图翻译
# 没标（老前端）按「非 chat」处理，保持原来的单轮行为
SURFACE_CHAT = "chat"

# chat 入口连续「只说不做」时最多补几次催促；补完仍不动手就按最终答复收尾。
# 取 2：第一次用来纠正「我去看看」这类过渡话，第二次留给模型重新组织计划；
# 再多只会烧 token、拖长一轮，收益递减。
MAX_CHAT_CONTINUATIONS = 2

# 像「我先去查一下」这种只表计划、没有结果的短回复。
# 只用明确的短语，不按「我」字泛匹配，避免把正常短答复误判成未完待续
_STALL_PHRASES = (
    "我去看看", "我去查", "我去搜", "我去处理", "我去试试",
    "我先看看", "我先查", "我先去", "我来看一下", "我查一下", "我搜一下",
    "让我看看", "让我先", "我帮你看看", "我这就去", "这就去办",
    "稍等一下", "请稍等", "马上就好", "正在处理中", "正在执行中",
)


def shot_lock_for(client: str | None, surface: str | None) -> bool:
    """本轮带图时是否强制单轮（locked_shot）。

    只有「大屏聊天页」放开：client=bigscreen 且 surface=chat。
    QAM 的全部入口（截图询问 ask、截图翻译 translate、面板里的普通输入）
    都必须一问一答：它们挂在游戏画面上，模型反复截图/反复动手会把用户
    看到的画面换掉。网页/桌面端以及老的无标记请求保持原来的不锁行为。
    """
    c = (client or "").strip()
    if c == "bigscreen":
        return (surface or "") != SURFACE_CHAT
    if c == "qam":
        return True
    return False


def looks_like_stall(reply: str) -> bool:
    """模型只说了「去看看」却没给结果、也没调工具"""
    r = (reply or "").strip()
    if not r or len(r) > 160:
        return False
    return any(p in r for p in _STALL_PHRASES)


def chat_should_continue(client: str | None, surface: str | None,
                         reply: str, used: int) -> bool:
    """大屏聊天页是否该再推一轮（兜底：used 到 MAX_CHAT_CONTINUATIONS 即停）"""
    return ((client or "").strip() == "bigscreen"
            and (surface or "") == SURFACE_CHAT
            and used < MAX_CHAT_CONTINUATIONS
            and looks_like_stall(reply))


# ---------- 本轮用户消息意图 ----------

def _positive_text(text: str) -> str:
    """去掉「别重启」这类否定片段，剩下的才算正向意图"""
    return _NEG_POWER_RE.sub("", text or "")


def has_power_intent(text: str) -> bool:
    """本轮用户消息里是否明确要求电源操作"""
    return bool(_POWER_TEXT_RE.search(_positive_text(text)))


def has_mode_intent(text: str) -> bool:
    """本轮用户消息里是否明确要求切换桌面/游戏模式"""
    return bool(_MODE_TEXT_RE.search(text or ""))


def is_confirm(text: str) -> bool:
    return bool(_CONFIRM_RE.match((text or "").strip()))


def looks_like_password(text: str) -> bool:
    """用户回复像不像一个密码（而不是一句新的任务）"""
    t = (text or "").strip()
    if not t or len(t) > 64:
        return False
    if any(ch in t for ch in "？?。！!，,；;：: \t\n"):
        return False
    if _SECRET_HINT_RE.search(t):
        return False
    return True


def is_secret_or_confirm(text: str) -> bool:
    return is_confirm(text) or looks_like_password(text)


def ask_intent_kind(question: str) -> str | None:
    """ask_user 在问什么：密码 / 重启 / 关机；都不是返回 None"""
    q = question or ""
    if _PASSWORD_ASK_RE.search(q):
        return "sudo_password"
    if _MODE_TEXT_RE.search(q):
        return "steamosctl"
    if _SHUTDOWN_ASK_RE.search(q) and not _NON_SYSTEM_ASK_RE.search(q):
        return "shutdown"
    if _REBOOT_ASK_RE.search(q) and not _NON_SYSTEM_ASK_RE.search(q):
        return "reboot"
    return None


# ---------- pending_intent 生命周期 ----------

def make_intent(kind: str, ctx: TurnContext | None, now: float | None = None,
                ttl: float = INTENT_TTL) -> dict:
    ctx = ctx or TurnContext()
    now = time.time() if now is None else now
    return {
        "kind": kind,
        "status": "pending",
        "created_at": now,
        "expires_at": now + max(1.0, float(ttl)),
        "turn_id": ctx.turn_id,
        "client": ctx.client,
        "device": ctx.device,
        "sid": ctx.sid,
        "boot_id": ctx.boot_id,
    }


def reconcile_intent(intent: dict | None, boot_id: str,
                     now: float | None = None) -> dict:
    """重启后按 boot_id 对账，并顺手处理过期。

    - 机器真的重启过（boot_id 变了）：reboot 记为 executed，密码记为 expired；
    - 还没执行但已经过期：记 expired。
    其它情况原样返回（调用方据此判断有没有变化）。
    """
    if not isinstance(intent, dict) or not intent.get("kind"):
        return {}
    now = time.time() if now is None else now
    out = dict(intent)
    old_boot = str(out.get("boot_id") or "")
    if not old_boot:
        # 老格式（没有 boot_id）无法判断是否跨了重启，按失效处理，绝不放行
        out["status"] = "expired"
        out["reconciled_at"] = now
        return out
    if boot_id and old_boot != boot_id:
        if out.get("kind") in ("reboot", "shutdown"):
            out["status"] = "executed"
        else:
            out["status"] = "expired"
        out["reconciled_at"] = now
        return out
    if out.get("status") in ("pending", "confirmed"):
        try:
            expired = float(out.get("expires_at") or 0) < now
        except (TypeError, ValueError):
            expired = True
        if expired:
            out["status"] = "expired"
            out["reconciled_at"] = now
    return out


def intent_matches(intent: dict, ctx: TurnContext) -> bool:
    """这条意图是不是这个会话、这个端、这次开机下创建的"""
    if not intent:
        return False
    sid = str(intent.get("sid") or "")
    if sid and sid != ctx.sid:
        return False
    client = str(intent.get("client") or "")
    if client and client != ctx.client:
        return False
    device = str(intent.get("device") or "")
    if device and device != ctx.device:
        return False
    boot = str(intent.get("boot_id") or "")
    if boot and boot != ctx.boot_id:
        return False
    return True


# ---------- 入口 ----------

def check(name: str, args: dict, ctx: TurnContext | None = None,
          intent: dict | None = None, approved: bool = False,
          now: float | None = None) -> PolicyDecision:
    """判断这次工具调用是否被授权。

    非破坏性命令直接放行；电源 / 会话切换命令必须满足下面任一条件：
      - 本轮用户消息明确提到对应动作；
      - 存在仍有效的 pending_intent（密码 / 确认），且属于同一个会话与端；
      - 调用方已显式确认过这次挂起操作（窗口里点了「确认」）。
    """
    args = args or {}
    ctx = ctx or TurnContext()
    now = time.time() if now is None else now
    # 只读会话：群聊里的普通成员只能聊天，除纯查询/看图类工具外一律不执行。
    # 放在最前面，电源判定、密码授权、确认放行都不能绕过它
    if ctx.readonly and name not in READONLY_TOOLS:
        return PolicyDecision(
            False, code="readonly", action=risky_action(name, args),
            reason="这个会话只能聊天（QQ 群里的普通成员），不能执行设备操作。",
        )
    action = risky_action(name, args)
    if not action:
        return PolicyDecision(True, code="not_power")
    is_power = action in POWER_ACTIONS

    if approved:
        return PolicyDecision(True, code="approved", action=action)

    if is_power and has_power_intent(ctx.user_text):
        return PolicyDecision(True, code="user_message", action=action)
    if not is_power and has_mode_intent(ctx.user_text):
        return PolicyDecision(True, code="user_message", action=action)

    it = reconcile_intent(intent, ctx.boot_id, now)
    if it and it.get("status") in ("pending", "confirmed") and intent_matches(it, ctx):
        kind = str(it.get("kind") or "")
        if is_power and kind in ("reboot", "shutdown") and is_confirm(ctx.user_text):
            out = dict(it)
            out["status"] = "confirmed"
            out["confirmed_at"] = now
            return PolicyDecision(True, code="intent_confirmed", action=action,
                                  intent=out)
        if not is_power and kind == "steamosctl" and is_confirm(ctx.user_text):
            out = dict(it)
            out["status"] = "confirmed"
            out["confirmed_at"] = now
            return PolicyDecision(True, code="intent_confirmed", action=action,
                                  intent=out)
        if is_power and kind == "sudo_password" and is_secret_or_confirm(ctx.user_text):
            out = dict(it)
            out["status"] = "confirmed"
            out["confirmed_at"] = now
            return PolicyDecision(True, code="sudo_password", action=action,
                                  intent=out)

    what = "电源操作" if is_power else "会话切换"
    return PolicyDecision(
        False, code="denied", action=action,
        reason=(
            f"本轮用户消息没有明确要求{what}（{action}），且没有仍有效的挂起授权"
            f"（历史消息不构成授权）。"
        ),
    )
