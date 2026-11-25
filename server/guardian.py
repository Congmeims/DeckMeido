"""女仆长 MeidoChou：auto 档下监护女仆的操作，决定能否自动放行。

为什么需要她：auto 档的目标是「主人离开座位，女仆也能自己把活干完」。但 DANGEROUS
操作（rm -rf、sudo、停服务、写系统目录等）原来一律挂起等主人打字「确认执行」，
人不在就卡住。女仆长在挂起之前先替主人看一眼：这步让不让女仆自己做？

判断原则（保守侧，护着主人的机器和数据）：
- ALLOW：操作可逆、在工作区内、不破坏数据/系统、符合常规意图 → 放行
- DENY ：不可逆、破坏数据或系统启动能力、明显越界 → 拦下
- ASK  ：拿不准 → 降级回主人确认（原挂起流程）

安全兜底：
- 超高危（approval.ULTRA_RULES：抹盘、格式化、破坏引导等）在 approval.resolve 里
  已经硬 deny，不会送到女仆长面前；其余危险操作（含 dd 写块设备）由她判定 ——
  规则表只看字符串，分不清 python 里的字面量，她能看到完整命令的下文再定夺
- self_update upgrade、远程入口（force_confirm）也不交给女仆长，必须主人亲自点头
- 女仆长超时 / 报错 / 输出无法解析 → 一律 ASK（等主人，安全侧）
- 每次判断记后台日志（devlog + stderr），放行/拦下都留痕，便于事后审计
"""
from __future__ import annotations

import asyncio
import re
import sys
import time

from . import devlog, tools

# 女仆长单次判断的最长等待。超时就降级等主人，不让主人干等，
# 也避免模型卡死把整轮对话拖挂。
GUARDIAN_TIMEOUT = 15.0

# 只认这三个整词，避免女仆长解释一大段时误命中
_DECISION_RE = re.compile(r"\b(ALLOW|DENY|ASK)\b", re.IGNORECASE)

# 判词是内部协议（模型按 ALLOW/DENY/ASK 输出，代码按这三个词分支），
# 但它会拼进 reason 直接显示在聊天和界面上，主人看不懂英文缩写，
# 所以对外一律换中文。日志里仍保留原词，便于 grep。
_DECISION_CN = {"allow": "放行", "deny": "拦下", "ask": "请主人定夺"}

_SYSTEM_PROMPT = """你是 DeckMeido 的女仆长 MeidoChou，负责监护女仆（AI 助手）的每一步操作，判断这步能否在主人不在场时让女仆自己执行。

请仅根据下面给出的「操作内容」判断，输出格式为「判词：一句中文理由」，例如：
DENY：这条命令会抹掉整块硬盘，主人不会同意

判词只能是以下三个词之一（大写）：
- ALLOW：操作安全、可逆、不破坏主人的数据或系统、在合理范围内，可以让女仆直接做
- DENY ：操作危险、不可逆、会破坏数据或系统、可能导致设备无法开机或无法恢复，不能让女仆做
- ASK  ：拿不准、影响较大、或需要主人确认才能决定，请等主人回来确认

判断要点：
1. 删除/覆盖工作区内的普通文件 → 可逆（进回收站），一般 ALLOW；删除系统目录或主人的关键数据 → DENY
2. 写系统关键路径（/etc、/boot 以外的配置）、停关键服务、提权 → 通常 ASK，除非明显无害
3. 命令里有 sudo 但只是装个包、启个服务 → ALLOW；sudo 改系统配置 → ASK
4. 任何会导致设备开不了机、数据永久丢失的 → DENY
5. 先看命令是不是真的在执行那件事：dd / mkfs / wipefs / rm -rf / 这类只出现在字符串里
   （python3 -c "…"、heredoc、echo "…"、写测试用例）属于「文字内容」，并没有真的执行，
   而命令本身只是在跑测试或写文件 → ALLOW
6. 但如果外层是 sh/bash -c "dd …" 这种把命令直接交给 shell 执行的写法，就是真执行 → DENY
7. 不确定就 ASK，不要冒险 ALLOW

只输出这一行，不要复述命令：判词 + 一句理由（30 字内）"""


def _describe(name: str, args: dict) -> str:
    """给女仆长看的操作描述：工具名 + 人类可读摘要 + 原始参数要点"""
    parts = [f"工具：{tools.cn_name(name)}（{name}）"]
    summary = tools.summarize_tool(name, args)
    if summary:
        parts.append(f"操作：{summary}")
    desc = tools.describe(name, args)
    if desc and desc != summary:
        parts.append(f"详情：{desc}")
    # 命令类把完整命令也给上，女仆长需要看参数细节
    if name == "run_command":
        parts.append(f"完整命令：{args.get('command', '')}")
    return "\n".join(parts)


async def judge(name: str, args: dict, verdict, llm, turn_sid: str = "") -> tuple[str, str]:
    """让女仆长判一次。返回 (decision, reason)，decision ∈ {allow, deny, ask}。

    任何异常（超时、模型不可用、输出无法解析）都降级为 ask——
    宁可爱心多问主人一次，也不放过一次危险操作。
    """
    started = time.time()
    op_desc = _describe(name, args)
    reason = ""
    decision = "ask"
    try:
        reply = await asyncio.wait_for(
            llm.chat([
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": op_desc},
            ]),
            timeout=GUARDIAN_TIMEOUT,
        )
        raw = reply or ""
        m = _DECISION_RE.search(raw)
        if m:
            decision = m.group(1).lower()
            # 判词后面那句理由原样带回去：界面和聊天里要按女仆长的口径说，
            # 而不是只丢一句「已拒绝」让主人猜原因
            why = raw[m.end():].strip(" \t\r\n:：，,。.;；-—*`\"'")
            if len(why) > 80:
                why = why[:80].rstrip() + "…"
            # 对外口径用中文判词：界面上只该出现「女仆长判定「拦下」：…」，
            # 不能再把 ALLOW/DENY/ASK 原样丢给主人看
            cn = _DECISION_CN.get(decision, decision)
            reason = f"女仆长判定「{cn}」" + (f"：{why}" if why else "")
        else:
            reason = f"女仆长没说清（输出无法解析），请主人定夺：{raw[:120]!r}"
    except asyncio.TimeoutError:
        reason = f"女仆长思考超时（>{GUARDIAN_TIMEOUT:.0f}s），请主人定夺"
    except Exception as exc:
        reason = f"女仆长这边出了点状况，请主人定夺：{exc!r}"

    elapsed = time.time() - started
    # 后台日志：放行/拦下都留痕，devlog 结构化 + stderr 进 backend.log
    devlog.debug("meidochou.judge", name=name, decision=decision,
                 rule=getattr(verdict, "rule", ""), severity=getattr(verdict, "severity", 0),
                 elapsed=round(elapsed, 2), turn=turn_sid, reason=reason)
    print(f"[DeckMeido][meidochou] {_DECISION_CN.get(decision, decision)} {name}"
          f"（{getattr(verdict, 'label', '')}"
          f"/{getattr(verdict, 'rule', '')}，会话 {turn_sid}，{elapsed:.1f}s）："
          f"{op_desc[:160]} | {reason}", file=sys.stderr, flush=True)
    return decision, reason
