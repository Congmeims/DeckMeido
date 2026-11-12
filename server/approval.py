"""审批闸门的唯一入口：所有「这个工具调用要不要先问用户」的判断都在这里。

为什么单独成模块：以前这套判断散在 main.py 的多个地方（对话轮、终端面板），
每加一档权限都要靠 grep 找齐所有调用点，漏一处就会出现「设置里开了完全访问，
某个入口却还在拦」这种不一致。现在只有 resolve() 一个出口，调用方只负责
拿到结论后做事：拒绝 / 挂起等确认 / 直接执行。

判断分两层，顺序不能反：

1. 危险级别（severity）：与权限档位无关的判定。从低到高
   SAFE < NOTABLE < DANGEROUS < CRITICAL。
   - CRITICAL 不给确认通道，任何档位（含完全访问）都直接拒绝；
   - DANGEROUS 在 full 之外的档位一律硬确认（要回「确认执行」）；
   - NOTABLE 按档位决定问不问。
2. 权限档位（mode）：manual / custom / auto / full 决定非危险场景问不问。

远程入口（QQ，ctx.force_confirm）在所有档位之上再收紧一档：即使配置是
full，DANGEROUS 及以上仍然必须确认，且必须回「确认执行」。

注意这里只管「要不要问」，不管「准不准执行」。电源/会话切换这类需要本轮
点名授权的判断仍在 policy.check()，两条闸门串联，缺一不可。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import policy, tools

# 严重度：数字越大越严重，比较时不用记名字
SAFE = 0
NOTABLE = 1
DANGEROUS = 2
CRITICAL = 3

SEVERITY_CN = {
    SAFE: "安全",
    NOTABLE: "需留意",
    DANGEROUS: "危险",
    CRITICAL: "极危",
}

# 权限档位；老配置里没有的值一律按 auto 处理
MODES = ("manual", "custom", "auto", "full")
DEFAULT_MODE = "auto"


def normalize_mode(raw) -> str:
    m = str(raw or "").strip().lower()
    return m if m in MODES else DEFAULT_MODE


@dataclass(frozen=True)
class Verdict:
    """一次审批判断的结论

    ask      = 需要用户确认（无法确认则不能执行）
    hard     = 确认时必须回「确认执行」而不是随便一个「好」
    deny     = 直接拒绝，连确认通道都不给
    severity = 命中的最高严重度
    rule     = 命中的规则名，用于日志和界面提示
    reason   = 一句中文说明，直接展示给用户
    """
    ask: bool = False
    hard: bool = False
    deny: bool = False
    severity: int = SAFE
    rule: str = ""
    reason: str = ""

    @property
    def label(self) -> str:
        return SEVERITY_CN.get(self.severity, "")

    @property
    def allowed(self) -> bool:
        return not self.deny

    @property
    def needs_user(self) -> bool:
        return self.ask and not self.deny


# ---------- 第二层：与权限档位无关的硬规则 ----------
# 这些命令写错了没有回头路，或者本身就是破坏行为，不靠「用户点一下确认」兜底。
# 规则按 token 边界匹配，避免把 "charm" 里的 rm 当成 rm。
def _tok(word: str) -> str:
    return rf"(?<![\w.-]){word}(?![\w.-])"


# (rule, pattern, severity, reason)
_HARD_RULES: tuple[tuple[str, str, int, str], ...] = (
    # —— CRITICAL：直接拒绝 ——
    ("mkswap_live", _tok("mkswap") + r"\s+(?:-[^\s]+\s+)*/dev/(?:sd|nvme|mmcblk)",
     CRITICAL, "在磁盘设备上创建交换区，会覆盖原有数据"),
    ("wipe_root", _tok("wipefs") + r"[^\n]*(?:-a|--all)[^\n]*/dev/(?:sd|nvme|mmcblk)",
     CRITICAL, "擦除整个磁盘的文件系统签名"),
    ("mkfs_root", _tok(r"mkfs(?:\.\w+)?") + r"\s+(?:-[^\s]+\s+)*/dev/(?:sd|nvme|mmcblk)",
     CRITICAL, "格式化磁盘设备"),
    ("dd_to_disk", _tok("dd") + r"[^\n]*\bof=/dev/(?:sd|nvme|mmcblk)",
     CRITICAL, "dd 直接写块设备，会抹掉整块盘"),
    ("redirect_disk", r">>?\s*/dev/(?:sd|nvme|mmcblk)",
     CRITICAL, "重定向写入块设备"),
    ("fork_bomb", r":\s*\(\s*\)\s*\{[^\n]*\|[^\n]*&\s*\}", CRITICAL, "fork bomb"),
    ("rm_root", r"(?:(?<=[\s;&|(`])|^)rm\s+(?:-[A-Za-z-]+\s+)*/(?:\s|$|[*])",
     CRITICAL, "删除根目录"),
    ("chmod_root_777", r"chmod\s+-[A-Za-z]*R[A-Za-z]*\s+(?:777|a\+rwx)\s+/(?:\s|$)",
     CRITICAL, "递归放开根目录权限"),
    ("chown_root", r"chown\s+-[A-Za-z]*R[A-Za-z]*\s+[^\s]+\s+/(?:\s|$)",
     CRITICAL, "递归改根目录属主"),
    ("write_passwd", r"(?:>>?|tee|cp|mv|install|truncate)\s+[^\n]*"
                     r"/etc/(?:passwd|shadow|gshadow|sudoers)(?:\s|$)",
     CRITICAL, "改写系统账号文件"),

    # —— DANGEROUS：必须点名确认 ——
    ("pipe_to_shell", r"(?:curl|wget)[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|fish)",
     DANGEROUS, "把网络内容直接管道给 shell 执行"),
    ("base64_pipe", r"base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:sh|bash|zsh)",
     DANGEROUS, "base64 解码后管道给 shell"),
    ("eval_remote", r"\b(?:eval|exec)\b[^\n]*(?:curl|wget|\$\()",
     DANGEROUS, "eval/exec 执行远程内容"),
    ("write_boot", r"(?:>>?|tee|cp|mv|install|truncate|chmod|chown|rm)\s+"
                   r"[^\n]*(?<![\w])/(?:boot|sys|proc)(?:/|\s|$)",
     DANGEROUS, "写入 /boot、/sys 或 /proc"),
    ("write_ssh", r"(?:>>?|tee|cp|mv|install|truncate|rm)\s+"
                  r"[^\n]*(?:\.ssh/)",
     DANGEROUS, "改写 ~/.ssh 下的密钥与授权配置"),
    ("systemctl_critical", r"systemctl\s+(?:stop|disable|mask)\s+"
                           r"(?:ssh|sshd|network|NetworkManager|systemd-logind|"
                           r"dbus|display-manager|gdm|sddm|steam|gamescope)",
     DANGEROUS, "停用/屏蔽关键系统服务"),
    ("history_clear", _tok("history") + r"\s+-c(?:\s|$)", DANGEROUS, "清空命令历史"),
    ("rm_session", r"curl[^\n]*-X\s*DELETE[^\n]*/api/sessions",
     DANGEROUS, "删除会话历史"),
    # -R（删除 known_hosts 条目）/ -y（导出公钥）会改文件或泄露密钥；
    # -l 只是打印指纹，是只读操作，不能一起拦
    ("ssh_keygen", _tok("ssh-keygen") + r"(?:(?!\s-l\b)[^\n])*\s-[A-Za-z]*[Rfy]",
     DANGEROUS, "改动 SSH 密钥"),
    ("crontab_edit", _tok("crontab") + r"\s+(?!-l(?:\s|$))", NOTABLE, "修改 crontab"),
    ("sudo_passwd", _tok("passwd") + r"(?=\s|$)", NOTABLE, "修改账号密码"),
    ("pkexec", _tok("pkexec"), DANGEROUS, "pkexec 提权"),
    ("chmod_777_wide", r"chmod\s+-[A-Za-z]*R[A-Za-z]*\s+777\s+/[^\s]*",
     DANGEROUS, "递归放开目录权限"),
    ("kill_all", r"\bkill(?:all)?\s+(?:-9|-KILL|-SEGV|-11)?\s*(?:-1|--all|-SIGKILL)\b",
     DANGEROUS, "杀掉全部进程"),
)

# 命令里出现这些程序，即使不在上面的规则表里，也至少是 NOTABLE。
# mkfs / wipefs / fdisk / dd 这类在硬规则里已经单独判过设备路径，
# 这里不再统一升到 DANGEROUS —— 否则它们的分级会被抹平。
_WATCH_PROGS = {
    "rm": (NOTABLE, "删除文件"),
    "rmdir": (NOTABLE, "删除目录"),
    "truncate": (NOTABLE, "清空文件内容"),
    "shred": (DANGEROUS, "不可恢复地擦除文件"),
    "sudo": (NOTABLE, "提权执行"),
    "doas": (NOTABLE, "提权执行"),
    "systemctl": (NOTABLE, "操作系统服务"),
    "reboot": (NOTABLE, "重启电脑"),
    "poweroff": (NOTABLE, "关机"),
}

# 只在该程序带「强制/递归」参数时才算危险：rm -rf、rm -r 没有回收站，
# 普通 rm 一个文件只是删掉，按 NOTABLE 走
_FORCE_FLAG_PROGS = {
    "rm": ("-r", "-R", "--recursive", "-f", "--force"),
}

_CMD_PREFIXES = {"sudo", "doas", "env", "nohup", "time", "command", "exec",
                 "nice", "stdbuf"}
_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\n|\|)")


def _cmd_of(name: str, args: dict) -> str:
    """取这次调用里真正要看的命令串（非 run_command 返回空）"""
    if name != "run_command":
        return ""
    return str((args or {}).get("command") or "")


def _prog_of(seg: str) -> str:
    """取一段命令里真正执行的程序名（剥掉 sudo / 环境变量等前缀）"""
    try:
        import shlex
        toks = shlex.split(seg, comments=False, posix=True)
    except Exception:
        toks = seg.split()
    for t in toks:
        if t in _CMD_PREFIXES or re.fullmatch(r"[A-Za-z_]\w*=\S*", t):
            continue
        if t.startswith("-"):
            continue
        return t.strip("\"'").rsplit("/", 1)[-1]
    return ""


def _has_flag(seg: str, flag: str) -> bool:
    """这段命令里有没有该参数（含 -rf 这种合并写法）"""
    for t in seg.split():
        t = t.strip("\"'")
        if t == flag:
            return True
        # -rf 这种合并写法（--force / --recursive 已在上面的全等分支命中）
        if (len(flag) == 2 and flag[0] == "-" and t.startswith("-")
                and not t.startswith("--") and flag[1] in t[1:]):
            return True
    return False


def command_risk(cmd: str) -> Verdict:
    """命令本身的风险级别：与权限档位、工具都无关，只看这串命令干了什么

    返回命中的最高严重度。调用方据此决定「要不要问」「能不能问」。
    """
    text = str(cmd or "").strip()
    if not text:
        return Verdict()

    best = Verdict()
    # 整串先看一遍，再逐段看：整串能覆盖 "sudo rm -rf /" 这类组合，
    # 逐段能避免把 "echo rm" 里的 rm 当真命令
    probes = [text] + [s for s in _SEGMENT_SPLIT_RE.split(text) if s.strip()]
    for probe in probes:
        for rule, pattern, sev, reason in _HARD_RULES:
            if sev <= best.severity:
                continue
            if re.search(pattern, probe, re.IGNORECASE):
                best = Verdict(severity=sev, rule=rule, reason=reason)

    # 程序级兜底：规则表没覆盖到的删除/提权/服务操作至少标出来
    for seg in _SEGMENT_SPLIT_RE.split(text):
        seg = seg.strip()
        prog = _prog_of(seg)
        hit = _WATCH_PROGS.get(prog)
        if not hit:
            continue
        sev, reason = hit
        flags = _FORCE_FLAG_PROGS.get(prog)
        if flags and any(_has_flag(seg, f) for f in flags):
            sev, reason = DANGEROUS, f"{reason}（强制/递归）"
        if sev > best.severity:
            best = Verdict(severity=sev, rule=f"prog:{prog}", reason=reason)

    # 工具层已有的危险判定（DANGER_PATTERNS + 结构分析）作为补充：
    # 规则表是白名单式的，漏了新写法时由它兜住。只补到 NOTABLE ——
    # 这层判断不知道设备路径，一律升级会把 rm 和 dd 抹成同一档。
    # 直接调 _structural_danger 而不是 is_dangerous：后者还会跑一遍
    # DANGER_PATTERNS 正则，把 "grep reboot /var/log" 这种参数里带关键词的
    # 正常命令也标成危险
    if best.severity < NOTABLE and tools._structural_danger(text):
        best = Verdict(severity=NOTABLE, rule="tools.structural",
                       reason="命中危险命令特征")
    return best


def tool_risk(name: str, args: dict) -> Verdict:
    """工具调用的风险级别：命令类走命令判定，其余工具按名字归类"""
    if name == "run_command":
        return command_risk(_cmd_of(name, args))
    if name == "self_update" and str((args or {}).get("action") or "") == "upgrade":
        # 升级会重启服务、打断当前对话，任何档位都要问
        return Verdict(ask=True, hard=True, severity=DANGEROUS,
                       rule="self_update", reason="升级会重启服务并中断对话")
    if name == "install_game":
        return Verdict(severity=NOTABLE, rule="install_game", reason="安装游戏会触发下载")
    if name == "gamepad":
        return Verdict(severity=NOTABLE, rule="gamepad", reason="模拟手柄会操作游戏")
    path = str((args or {}).get("path") or (args or {}).get("file") or "")
    if name in ("write_file", "edit_file") and path:
        p = path.replace("\\", "/")
        if re.search(r"/etc/(?:passwd|shadow|gshadow|sudoers)$", p) or p.startswith("/boot"):
            return Verdict(severity=CRITICAL, rule="critical_path",
                           reason=f"写系统关键路径：{p}")
        if p.startswith(("/etc/", "/usr/", "/boot/", "/sys/", "/proc/")):
            return Verdict(severity=DANGEROUS, rule="write_system",
                           reason=f"写系统目录：{p}")
        if "/.ssh/" in p or p.endswith("/.ssh"):
            return Verdict(severity=DANGEROUS, rule="write_ssh",
                           reason=f"改写 SSH 配置：{p}")
    return Verdict()


# ---------- 第三层：权限档位 ----------
def _ask_by_mode(name: str, args: dict, mode: str, custom: dict,
                 risk: Verdict) -> bool:
    """非硬规则场景下，这一档权限要不要弹确认"""
    if mode == "manual":
        return True
    if mode == "full":
        return False
    if mode == "custom":
        return tools.custom_needs_approval(name, args, custom)
    # auto：只有危险命令才问。这里用 risk 而不是 tools.is_dangerous，
    # 是为了让「本地 rm」这类 NOTABLE 也按危险处理，与注释口径一致
    return risk.severity >= DANGEROUS


def resolve(name: str, args: dict, ctx: policy.TurnContext, mode: str,
            custom: dict | None = None, *, extra_danger: str = "") -> Verdict:
    """审批闸门的唯一入口：返回这次工具调用该怎么处理

    mode        当前权限档位（manual / custom / auto / full）
    custom      自定义档的四组开关，mode != custom 时可传 None
    extra_danger 渠道额外高危提示（QQ 那边 menu.remote_command_danger 的结论）
    """
    args = args or {}
    mode = normalize_mode(mode)
    remote = bool(getattr(ctx, "force_confirm", False))

    risk = tool_risk(name, args)

    # 1) 极危一律拒绝，不给确认通道
    if risk.severity >= CRITICAL:
        return Verdict(deny=True, severity=CRITICAL, rule=risk.rule,
                       reason=risk.reason or "极危操作，已拒绝")

    # 2) 工具自己声明必须确认的（自更新升级：会重启服务、中断对话），
    #    不受档位影响，full 模式下也要问
    if risk.ask:
        return Verdict(ask=True, hard=True, severity=risk.severity,
                       rule=risk.rule, reason=risk.reason)

    # 3) 远程入口：tools.DANGEROUS 与 DANGEROUS 及以上必须硬确认；
    #    NOTABLE 也要问（旧行为：远程 rm 同样按 is_dangerous 硬确认，不能放松）
    if remote:
        if extra_danger:
            return Verdict(ask=True, hard=True, severity=DANGEROUS,
                           rule="remote_extra", reason=extra_danger)
        if risk.severity >= DANGEROUS:
            return Verdict(ask=True, hard=True, severity=risk.severity,
                           rule=risk.rule, reason=risk.reason)
        if risk.severity >= NOTABLE:
            # 远程入口沿用旧行为：tools.is_dangerous 命中的一律硬确认
            hard = (risk.severity >= DANGEROUS
                    or tools.is_dangerous(_cmd_of(name, args)))
            return Verdict(ask=True, hard=hard, severity=risk.severity,
                           rule=risk.rule, reason=risk.reason)
        if name in tools.DANGEROUS:
            return Verdict(ask=True, hard=True, severity=DANGEROUS,
                           rule=f"tool:{name}", reason=f"{tools.cn_name(name)}需要确认")
        return risk

    # 4) 本地档位
    # auto 档只拦 DANGEROUS 及以上：NOTABLE 是给 manual/custom 用的分级，
    # 落到 auto 上就等于「什么都问」，和原来的自动审批不一致
    if risk.severity >= DANGEROUS:
        # 危险命令：即使 full 也要问？不 —— 用户开了 full 就是明确放弃这层。
        # 但 hard 标记保留，任何档位点确认都要回「确认执行」
        if mode == "full":
            return Verdict(severity=risk.severity, rule=risk.rule, reason=risk.reason)
        return Verdict(ask=True, hard=True, severity=risk.severity,
                       rule=risk.rule, reason=risk.reason)

    if risk.severity >= NOTABLE and mode != "auto":
        # 需留意：按档位决定问不问，但不算 hard（一个「好」就够）
        if _ask_by_mode(name, args, mode, custom or {}, risk):
            return Verdict(ask=True, hard=False, severity=risk.severity,
                           rule=risk.rule, reason=risk.reason)
        return risk

    # 5) 安全命令：manual 全问，custom 按组，auto 不问
    if _ask_by_mode(name, args, mode, custom or {}, risk):
        return Verdict(ask=True, hard=False, severity=NOTABLE,
                       rule=f"mode:{mode}", reason="当前权限模式下需要确认")
    return risk
