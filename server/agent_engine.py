"""Agent 引擎：LLM + 工具调用循环

从 main.py 平移出来的最大一块。原来 run_agent 有七百多行，把「拼提示词 /
调模型 / 逐轮派发工具 / 审批与授权闸门 / 广播 / 增量落盘」全搅在一个函数里，
改一处要通读全文。

这里只做「平移」，不改行为：
  - 引擎要读写的 main 模块级状态（llm / SYSTEM_PROMPT / PERMISSION_MODE /
    pending_action / manager …）统一通过 get_main() 现读，
    因为这些量会被 apply_config 整体重新赋值，本模块不能缓存任何一份；
  - main 侧保留同名薄壳转发（run_agent / _run_agent_guarded …），
    其余调用点与测试（monkeypatch main.xxx）的写法都不变。

依赖通过 init() 注入，避免与 main 循环导入。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

from . import devlog, memory, menu, netmode, policy, prompts, qqtext, themecolor, tools
from .session_store import store

# 由 main 在模块末尾调用：get_main 返回 main 模块本身（现读现写）
get_main = None


def init(get_main_fn) -> None:
    global get_main
    get_main = get_main_fn


def _m():
    return get_main()


# ---------- 电源意图记账 ----------

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
        _m()._write_session(ctx.sid, hist)
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


# ---------- 拒绝识别 ----------

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
                             turn_policy: policy.TurnPolicy | None = None,
                             extra_system: str = "",
                             turn_candidates_out: list[dict] | None = None) -> str:
    """带总超时和拒绝重试的 agent 入口：任何环节卡死都不会让对话无限挂起

    image_part 非空表示本轮提问自带画面（这里是历史里那条 [SHOT:文件名] 标记，
    内容已由 _build_llm_msgs 展开成图片部件）。client 决定要不要套插件端那套
    「带图就一问一答」的限制：插件端会自己连着截屏，桌面网页端不会
    """
    m = _m()
    hist = store.history if hist is None else hist
    pol = turn_policy or policy.OWNER_POLICY
    # 单轮超时：配了 max_tool_rounds 才有上限；不限轮数时给足时间（仍有硬超时兜底）
    # 900 秒：长任务（编译、装依赖、连续排查）经常需要更多时间，嫌久可以由用户
    # 自己点终止，而不是被系统提前掐断
    agent_timeout = 900 if m.MAX_TOOL_ROUNDS > 0 else 1800
    try:
        reply = await asyncio.wait_for(
            run_agent(events, hist=hist, image_part=image_part, tool_log=tool_log,
                      from_client=client, turn_ctx=turn_ctx,
                      _shot_lock=shot_lock, surface=surface, turn_policy=pol,
                      extra_system=extra_system,
                      turn_candidates_out=turn_candidates_out),
            timeout=agent_timeout)
    except asyncio.TimeoutError:
        # 超时不能只丢一句「超时了」：这一轮里已经跑完的命令、改过的文件全都要留下，
        # 否则用户既看不到结果，下一轮模型也会把同样的活重做一遍
        note = qqtext.get("timeout")
        digest = m._tool_results_digest(tool_log)
        return note + (qqtext.get("timeout_steps", digest=digest) if digest else "")
    # 系统层兜底：full 模式下模型空口拒绝（没调任何工具）→ 注入纠正指令重试一次。
    # 群友档零工具，「拒绝」是正常回答，不做这套纠正
    if (m.PERMISSION_MODE == "full" and not pol.readonly
            and not any(e.get("type") == "tool" for e in events)
            and _looks_like_refusal(reply)):
        events.append({"type": "thought", "text": "（系统检测到模型越权拒绝，已自动纠正重试）"})
        try:
            reply = await asyncio.wait_for(run_agent(
                events, hist=hist, tool_log=tool_log, from_client=client,
                extra_system=(prompts.part("retry_refuse")
                              + ("\n" + extra_system if extra_system else "")),
                turn_ctx=turn_ctx,
                _shot_lock=shot_lock, surface=surface, turn_policy=pol,
                turn_candidates_out=turn_candidates_out,
            ), timeout=agent_timeout)
        except asyncio.TimeoutError:
            return qqtext.get("retry_timeout")
    return reply


def _llm_error_text(e: Exception) -> str:
    """把服务商/中转的原始报错整理成一句主人能看懂的话。

    httpx 抛出的 "Server error '503 Service Unavailable' for url ..." 后面
    还跟着 MDN 文档链接，直接铺进聊天既吓人也没用。这里只保留三件事：
    哪家账户、什么状态、下一步能做什么；没有状态码时（例如中转把错误塞进
    200 的流里）再退回服务商自己的说明。
    """
    llm = _m().llm
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
    # 先分辨「连不上」和「连上了但服务慢」：两者都可能是超时，但下一步完全不同
    if isinstance(e, (httpx.ConnectTimeout, httpx.ConnectError)):
        # 局域网端点连接阶段只等 2 秒（见 LLMClient），照实说出来
        local = netmode.is_local_url(str(getattr(llm, "base_url", "") or ""))
        return qqtext.get("llm_connect", label=label,
                          wait=qqtext.get("llm_wait_local" if local else "llm_wait_remote"))
    if isinstance(e, httpx.ReadTimeout):
        return qqtext.get("llm_read_timeout", label=label)
    if status:
        # 没有单独一条文案的状态码（408/409 等）退回通用说法
        hint = qqtext.get(f"llm_hint_{status}") or qqtext.get("llm_hint_other")
        msg = qqtext.get("llm_status", label=label, hint=hint, status=status)
    else:
        brief = raw[:240] + ("…" if len(raw) > 240 else "")
        msg = qqtext.get("llm_raw", label=label, brief=brief)
    return msg + qqtext.get("llm_hint_suffix")


async def run_agent(events: list[dict], extra_system: str = "",
                   hist: list[dict] | None = None,
                   image_part: str | None = None,
                   tool_log: list[dict] | None = None,
                   from_client: str | None = None,
                   _shot_lock: bool | None = None,
                   surface: str | None = None,
                   turn_ctx: policy.TurnContext | None = None,
                   turn_policy: policy.TurnPolicy | None = None,
                   turn_candidates_out: list[dict] | None = None) -> str:
    """LLM + 工具调用循环（流式），思考链/工具调用/终端输出写入 events 供 UI 展示

    image_part 非空表示本轮提问自带画面：插件端这一轮把截图工具摘掉（见 round_tools）
    client 是发起端（网页 web / 插件 qam …）；_shot_lock 是否为插件端那套
    「带图就一问一答」的限制，由外面算好后随每轮工具往返递归传下来
    tool_log 由调用方传入：本轮执行过的工具记在这里，被终止时随回答落盘
    （见 handle_chat 的 CancelledError 分支与 _aborted_note）

    turn_policy 是本轮的场景策略表（不传按主人档）：工具集、记忆 scopes、
    是否拼模式段/extra、确认级别都从它取，引擎里不再散落场景判断
    """
    m = _m()
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
        boot_id=m._system_boot_id(),
        readonly=pol.readonly,
        force_confirm=pol.force_confirm(),
        # 完全访问：删除类硬约束让路（会话历史除外，见 policy.check_delete）
        full_access=(m.PERMISSION_MODE == "full"),
    )
    if pol.public_persona:
        # 群友档：公开人设 + 群聊上下文；不拼模式段、extra 和长期记忆，
        # 也不需要为拼提示词去探测当前桌面/游戏模式
        sys_prompt = m._guest_system_prompt()
    else:
        # 当前模式必须实测后告诉模型：游戏模式只能只读 + 从 gamescope 抓帧，
        # 遮罩提示挂 gamescope 覆盖层；桌面模式走 desktop 点按 + spectacle 截图。
        # 两种模式能做的事情不一样，猜错就是「点了没反应」或「抓不到画面」。
        # 探针会起子进程，所以放到线程里跑，别卡住这一轮
        gaming = await asyncio.to_thread(tools.mode_is_gaming)
        sys_prompt = m.SYSTEM_PROMPT + m._mode_block(gaming) + (f"\n{extra_system}" if extra_system else "")
        # 当前外观：换了壁纸（含通过 set_wallpaper 自己换的）之后，下一轮就要知道
        # 界面已经变了，不然模型会按老印象回答「现在是什么配色」
        look = themecolor.describe_current(m)
        if look:
            sys_prompt += "\n\n" + look
        # 长期记忆拼在最末尾（见 _memory_block 里的缓存说明）。每轮都重新取，
        # 用户刚在对话里让它「记住」，这一轮之后就能生效。
        # 记忆 scopes：None = 跟随当前会话；() = 本轮不注入；显式元组 = 临时切到这些 scope
        mem = ""
        if pol.memory_scopes is None:
            mem = m._memory_block()
        elif pol.memory_scopes:
            tokens = memory.set_context(pol.memory_scopes, memory.current_write_scope())
            try:
                mem = m._memory_block()
            finally:
                memory.reset_context(tokens)
        if mem:
            sys_prompt += "\n\n" + mem
    # 只把「摘要 + 游标之后的原文」发给模型；界面历史一条不删。
    # 群友档带策略表里的上下文窗口，主人档两项都是 0，行为不变
    msgs = await m._build_llm_msgs(hist, sys_prompt, turn_sid,
                                   hist_max=pol.history_max_messages,
                                   item_max_chars=pol.history_item_max_chars)
    if not m.llm.supports_vision:
        # 纯文本模型：把图换成一句「没收到」，别让残留的图片部件把上游 400，
        # 也别让它以为图在磁盘某处而自己去 find / 装 OCR
        m._blindify_images(msgs)
    hist_msgs = len(msgs)
    streaming = False  # 是否已开始向用户流式输出正文
    # 本轮正文累积：流式回调里按节流频率落盘，进程被杀最多丢一小段
    stream_text: list[str] = []
    # 本轮时间线的序号：思考 / 正文 / 工具 / 提问每出现一块就 +1，随广播带给前端。
    # 前端按它排序插入，块之间的先后就不再依赖「消息到达顺序」——
    # 断线重连、迟到的 tool_done、别的端补发的片段都可能乱序，
    # 以前只能一股脑 append 到容器末尾，于是屏幕上出现「选项跑到回答上方」
    # 这种明显不合理的排列
    seq_no = 0

    def _next_seq() -> int:
        nonlocal seq_no
        seq_no += 1
        return seq_no
    # 上一轮被用户终止时做过、没做完的工具（见 _aborted_note）：终止就是「别干了」，
    # 不把这段交接说清楚，模型看到自己的话成了半截，下一句就会自作主张接着做
    aborted_note = m._aborted_note(hist)
    if aborted_note:
        msgs.append({"role": "system", "content": aborted_note})
    # 本轮真正执行过的工具，终止时随回答一起落盘，供下一轮做交接
    tool_log: list[dict] = [] if tool_log is None else tool_log
    # 本轮壁纸搜索出的候选（编号 ↔ id）：工具往返不进历史，只留在 events 里，
    # 下一轮模型就忘了自己给过哪几张。落盘前挂到回答上，拼历史时再讲给它听
    turn_candidates: list[dict] = []
    if turn_candidates_out is not None:
        turn_candidates = turn_candidates_out
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
        return m._snapshot_parts(events, tail)

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
        m.refresh_stream_parts(turn_sid, "".join(stream_text), events)

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
            m.set_overlay(False)
            await m.manager.broadcast({"type": "stream_start", "ts": time.time(),
                                       "seq": _next_seq(), **m._turn_tag()})
        # 增量落盘（此函数是 async 的，前端不会感知到这点磁盘开销）
        # _stamp_parts 要遍历全部 events，先问一句要不要写，免得在节流命中的
        # 情况下白拼一遍字符串
        try:
            full = "".join(stream_text)
            if m._stream_would_flush(full, sid=turn_sid):
                m.persist_stream(full, parts=_stamp_parts(full), sid=turn_sid)
        except Exception as exc:
            devlog.debug("main.on_delta", error=repr(exc))
            pass
        # 前端按批渲染，这里把这一片先攒起来
        m._flush_delta_later(turn_sid, text, m._turn_tag())
        # 开头已经说话了，就把思维链收尾。模型经常是「先吐一两句正文，再补一段
        # reasoning」，如果只等本轮结束才发 thought_end，那段思考会挂到正文后面，
        # 界面上看起来就像「答完了还在想」（最终答复末尾下面又冒出一堆思考行）
        if thought_open:
            m.flush_thought_now(turn_sid)
            await m.manager.broadcast({"type": "thought_end", "ts": time.time(), **m._turn_tag()})
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
        nonlocal thought_open, thought_ev
        if not thought_open:
            thought_open = True
            # 先切话题：正文字要排在思维链前面，不然顺序会乱
            m.flush_delta_now(turn_sid)
            _tsq = _next_seq()
            await m.manager.broadcast({"type": "thought_start", "ts": time.time(),
                                       "seq": _tsq, **m._turn_tag()})
            # seq 一并落盘：刷新 / 重连后补渲染的那份 events 也要能排出先后
            thought_ev = {"type": "thought", "text": "", "seq": _tsq}
            events.append(thought_ev)
            _rs.update({"started": True, "chars": 0, "segs": 0, "next": 500})
            devlog.log("reasoning.start", sid=turn_sid, round=round_no)
        # 思考阶段模型吐的片比正文还密（往往一个字一片），逐片广播最费前端
        m._flush_thought_later(turn_sid, text, m._turn_tag())
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
        m.flush_thought_now(turn_sid)
        _rs_end("retry")
        thought_open = False
        m._thought_pend.pop(turn_sid, None)
        # 上一段的半截记录留在 events 里（屏幕上出现过），新一段另起一块
        thought_ev = None

    def _output_budget(used_msgs: list[dict], used: int | None = None) -> int:
        """按当前上下文剩余空间算出本次调用的输出上限。

        旧逻辑固定 4096，长答复/长工具参数会在中途被硬截断，正文漏出半截标记。
        现在：输出额度 = min(配置上限, 上下文窗口 - 已用 - 预留)，且不低于 4096。
        used 传入时直接用调用方刚算好的用量；不传才现数一遍（msgs 只增不减，
        数一次的结果可以直接复用）。
        """
        win = m.context_window()
        if used is None:
            used = m._estimate_tokens(used_msgs)
        # 预留一小块给下一轮的用户消息 / 工具结果
        room = max(win - used - 2048, 4096)
        cap = m.MAX_OUTPUT_TOKENS if m.MAX_OUTPUT_TOKENS > 0 else win
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
            # 带图这轮把 set_wallpaper 也摘掉：它要联网下壁纸，模型手上已经有
            # 用户发来的那张图，容易顺手拿它当壁纸去改主题；而设置壁纸后
            # 服务端会广播整屏刷新，追问的那轮对话会被刷没
            round_tools = [t for t in round_tools
                           if t["function"]["name"] != "set_wallpaper"]
        # 带图提问是「一问一答」：模型答完通常会顺手再截一张确认，于是又对着
        # 新画面续一轮，用户看到的是一条提问连着好几张图和好几段回答。
        # 所以带图这轮答完正文就直接收工，不再继续跑工具轮
        if locked_shot:
            extra_system = (extra_system + "\n" if extra_system else "") + prompts.part("locked_shot")
        # chat 入口的「只说不做」纠偏次数：模型偶尔回一句「我去看看」就不调工具了，
        # 这里最多补两次催促（policy.chat_should_continue），用完仍不动手就按最终答复收尾
        stall_nudges = 0
        while m.MAX_TOOL_ROUNDS <= 0 or round_no < m.MAX_TOOL_ROUNDS:
            round_no += 1
            # 单轮里工具往返也会堆积上下文，超过阈值就先压缩再继续。
            # 压缩只推进游标、不删 history，所以这里重建 msgs 时要保留
            # 本轮新加的工具往返消息（hist_msgs 之后的部分），否则工具链断掉。
            # 每轮只数一遍 msgs：压缩判断和输出额度都用这个值。
            # _estimate_tokens 要遍历全部历史（含几 MB 的图片 part），
            # 一轮算两遍纯属白烧 CPU
            used_now = m._estimate_tokens(msgs)
            if m._need_compact(used_now):
                # 压缩失败（网络抖动、摘要模型报错）不该把整轮对话弄崩：
                # 压缩只是省上下文，压不了就带着原上下文继续跑
                try:
                    await m.compact_history(auto=True, hist=hist, sid=turn_sid)
                    rebuilt = await m._build_llm_msgs(
                        hist, sys_prompt, turn_sid,
                        hist_max=pol.history_max_messages,
                        item_max_chars=pol.history_item_max_chars)
                    extra = [x for x in msgs[hist_msgs:]
                             if x.get("role") in ("tool", "assistant")]
                    msgs[:] = rebuilt + extra
                    hist_msgs = len(rebuilt)
                    # 重建后内容变了，用量跟着重算一次
                    used_now = m._estimate_tokens(msgs)
                    streaming = False
                    # 压缩前已经显示的过渡正文：作为 text 事件留在 events 里继续保存
                    if "".join(stream_text).strip():
                        events.append({"type": "text", "text": "".join(stream_text)})
                    stream_text.clear()
                except Exception as e:
                    print(f"[DeckMeido] 工具轮中压缩失败，继续：{e}", file=sys.stderr)

            thought_ev = None
            thought_live = []
            msg = await m.llm.chat_with_tools_stream(msgs, round_tools, on_delta=on_delta,
                                                     on_thought=on_thought,
                                                     on_retry=_reset_thought,
                                                     max_tokens=_output_budget(msgs, used_now))
            if thought_open:
                # 先把手里的思考片段推完，再发 thought_end，否则尾部几个字会
                # 落在 thought_end 之后，前端那块已经收起来了，看起来就是「思考末尾丢字」
                m.flush_thought_now(turn_sid)
                await m.manager.broadcast({"type": "thought_end", "ts": time.time(), **m._turn_tag()})
                thought_open = False
                _rs_end("round_end")

            # 撞到输出上限时自动续写，把后半段接上（不再把用户丢给「请拆小步骤」）
            cont = 0
            while (msg.get("finish_reason") == "length" and not (msg.get("tool_calls") or [])
                   and cont < m.MAX_CONTINUATIONS):
                cont += 1
                partial = (msg.get("content") or "")
                if not partial.strip():
                    break
                msgs.append({"role": "assistant", "content": partial})
                msgs.append({"role": "user", "content": prompts.part("continue_truncated")})
                used_now = m._estimate_tokens(msgs)
                cont_msg = await m.llm.chat_with_tools_stream(
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
            m._ctx_set(turn_sid, m._split_usage(msg.get("usage")) or used_now)
            await m.manager.broadcast({"type": "status", "data": await m.status_async()})

            # 水位自检：Deck 的可用内存在跑会话时往往只剩一两百 MB，等内核动手就晚了
            # （SIGKILL 之后 Python 一句遗言都留不下）。这里提前把还能丢的东西丢掉：
            # 老工具结果只留给人看的那一截，parts 也已经自带上限
            m._relieve_memory(events)

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
                    note = qqtext.get("output_limit")
                    return (body + note) if body else note.strip()
                print("[DeckMeido] 本轮收尾：模型没有再调用工具")
                return body or qqtext.get("empty_answer")

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
                                  "reason": f"{reason}（连续第 {repeats} 次）",
                                  "seq": _next_seq()}
                    events.append(loop_event)
                    _persist_screen(force=True)
                    await m.manager.broadcast({"type": "tool_done", "event": loop_event,
                                               "ts": time.time(), **m._turn_tag()})
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
                            m._write_session(turn_sid, hist)
                        except Exception as e:
                            print(f"[DeckMeido][policy] 写回待授权意图失败：{e}",
                                  file=sys.stderr)
                        print(f"[DeckMeido][policy] 登记待授权意图 {ask_kind}"
                              f"（会话 {turn_sid}）", file=sys.stderr)
                    ask = {"question": q, "options": opts}
                    ask_seq = _next_seq()
                    events.append({"type": "ask", "seq": ask_seq, **ask})
                    await m.manager.broadcast({"type": "ask", "ts": time.time(),
                                               "seq": ask_seq,
                                               **ask, **m._turn_tag()})
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
                                  "reason": remote_risk.reason, "seq": _next_seq()}
                    events.append(deny_event)
                    await m.manager.broadcast({"type": "tool_done", "event": deny_event,
                                               "ts": time.time(), **m._turn_tag()})
                    print(f"[DeckMeido][risk] 远程拒绝 {remote_risk.rule}"
                          f"（会话 {turn_sid}）：{remote_risk.reason}", file=sys.stderr)
                    m._qq_log("warn", "远程风险拒绝",
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
                                  "reason": decision.reason, "seq": _next_seq()}
                    events.append(deny_event)
                    await m.manager.broadcast({"type": "tool_done", "event": deny_event,
                                               "ts": time.time(), **m._turn_tag()})
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
                        m._write_session(turn_sid, hist)
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
                verdict = m.approval.resolve(name, args, ctx, m.PERMISSION_MODE,
                                             extra_danger=extra_danger)
                if verdict.deny:
                    label = tools.cn_name(name)
                    summary = tools.summarize_tool(name, args)
                    deny_event = {"type": "tool", "name": name, "label": label,
                                  "summary": summary, "files": [], "denied": True,
                                  "reason": verdict.reason, "seq": _next_seq()}
                    events.append(deny_event)
                    await m.manager.broadcast({"type": "tool_done", "event": deny_event,
                                               "ts": time.time(), **m._turn_tag()})
                    msgs.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": f"[{name}] 审批拒绝：{verdict.reason}"})
                    print(f"[DeckMeido][approval] 拒绝 {name}（{verdict.rule}，"
                          f"会话 {turn_sid}）：{verdict.reason}", file=sys.stderr)
                    return (f"这条操作被拦下了（{verdict.label}：{verdict.reason}）。"
                            f"它属于不可逆的破坏性操作，不会执行，也不要换一种写法绕过。"
                            f"如果确实需要，请自己在桌面终端里手动操作。")
                needs_approval = verdict.ask
                # 女仆长放行时记下她的判词：这一步的时间线行要标绿「已放行」，
                # 让主人事后能看出哪几步是她替自己拍的板（不只在 toast 里闪一下）
                guardian_ok = ""

                # auto 档：危险操作先交给女仆长 MeidoChou 监护。
                # 放行（allow）→ 直接执行；拦下（deny）→ 告知女仆并提示主人；拿不准（ask）
                # 或超时异常 → 降级走下面的人工挂起流程。
                # 不交给女仆长的：超高危（approval.ULTRA_RULES，上面已 deny，抹盘/破坏引导
                # 这类没有回头路的操作）、self_update upgrade（会重启服务，必须主人亲自
                # 确认）、远程入口（force_confirm，走人工）。其余极危（如 dd 写盘）也走这里，
                # 因为规则表分不清字符串字面量与真执行，她看得完整命令才判得准。
                if (m.PERMISSION_MODE == "auto" and needs_approval
                        and not ctx.force_confirm and name != "self_update"):
                    from . import guardian as _guardian_mod
                    g_decision, g_reason = await _guardian_mod.judge(
                        name, args, verdict, m.llm, turn_sid)
                    if g_decision == "allow":
                        needs_approval = False
                        guardian_ok = g_reason
                        await m.manager.broadcast({
                            "type": "guardian", "action": "allow",
                            "name": name, "label": tools.cn_name(name),
                            "summary": tools.summarize_tool(name, args),
                            "description": tools.describe(name, args),
                            "reason": g_reason, "ts": time.time(), **m._turn_tag()})
                    elif g_decision == "deny":
                        from . import main as _main_mod
                        ai_name = str((_main_mod.PERSONA or {}).get("name") or "DeckMeido").strip()
                        label = tools.cn_name(name)
                        summary = tools.summarize_tool(name, args)
                        deny_event = {"type": "tool", "name": name, "label": label,
                                      "summary": summary, "files": [], "denied": True,
                                      "guardian_deny": True,
                                      "reason": g_reason,
                                      "seq": _next_seq()}
                        events.append(deny_event)
                        await m.manager.broadcast({"type": "tool_done", "event": deny_event,
                                                   "ts": time.time(), **m._turn_tag()})
                        await m.manager.broadcast({
                            "type": "guardian", "action": "deny",
                            "name": name, "description": tools.describe(name, args),
                            "reason": g_reason, "ts": time.time(), **m._turn_tag()})
                        msgs.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": f"[{name}] 女仆长没让执行这步（{g_reason}）。"
                                                f"不要换个写法绕这层审查；若确实必要，"
                                                f"请让主人切到手动审批档再确认。"})
                        return (f"这步操作被女仆长拦截，没有让{ai_name}执行。\n\n{g_reason}\n\n"
                                "主人若确实需要，请切到手动审批档后确认，或自己手动操作。")
                    # ask：保持 needs_approval = True，走下面的人工挂起

                if needs_approval or remote_risk.level == "hard_confirm":
                    hard = verdict.hard or remote_risk.level == "hard_confirm"
                    # 挂起危险操作，等用户下一轮确认。把「谁在哪提的」一并记下，
                    # 确认时校验（见 _pending_matches），别的端说的「好」不算数。
                    # hard 档只认「确认执行」，onebot 侧会按这个标记收紧确认口径
                    pending = {"name": name, "args": args,
                               "description": tools.describe(name, args),
                               "hard": hard,
                               "sid": store.turn.get("sid") or store.current_sid,
                               "epoch": store.turn.get("epoch") or store.epoch,
                               "turn_id": store.turn.get("turn_id") or "",
                               "client": store.turn.get("client") or "",
                               "device": store.turn.get("device") or ""}
                    m._pending_set(pending)
                    # 通知各端有一条操作在等审批：前端据此显示待审批卡片、
                    # 启用手柄/键盘快捷键（A/Enter 确认、X/Esc 取消）。
                    # 带本轮 sid（_turn_tag），别的会话不会弹出不相关的卡片。
                    await m.manager.broadcast({"type": "pending_approval",
                        "description": pending["description"],
                        "hard": pending["hard"],
                        "label": verdict.label,
                        "reason": verdict.reason or "",
                        "ts": time.time(), **m._turn_tag()})
                    print(f"[DeckMeido] 工具待确认 {name}（模式 {m.PERMISSION_MODE}，"
                          f"{verdict.label}{'/' + verdict.reason if verdict.reason else ''}）："
                          f"{pending['description']}")
                    text_out = prompts.part("pending_approval").format(
                        description=pending['description'])
                    if pending["hard"]:
                        text_out += ("\n这项操作风险较高"
                                     f"（{verdict.label}：{verdict.reason or '高危操作'}），"
                                     "只有回复「确认执行」才会执行。")
                    return text_out

                # full 档：危险操作直接执行前给一个非阻塞提示，让主人知道
                # 女仆干了什么（只 toast，不打断）。SAFE/NOTABLE 不提示，避免刷屏；
                # auto 档女仆长放行已有提示，这里只针对 full。
                if m.PERMISSION_MODE == "full" and verdict.severity >= m.approval.DANGEROUS:
                    await m.manager.broadcast({
                        "type": "full_action",
                        "name": name, "label": tools.cn_name(name),
                        "summary": tools.summarize_tool(name, args),
                        "description": tools.describe(name, args),
                        "ts": time.time(), **m._turn_tag()})

                # 文件改动识别（run_command）
                files = tools.extract_writes(str(args.get("command", ""))) if name == "run_command" else []
                label = tools.cn_name(name)
                summary = tools.summarize_tool(name, args)
                # 模型给的「这一步要干什么」：run_command 的 reason 是必填项，
                # 前端拿它当 Trae 那种一句话提示（「已运行 · 定位字面量」），
                # 比只显示整条命令更容易看出当前在做什么
                intent = str(args.get("reason") or "").strip()
                event = {"type": "tool", "name": name, "label": label,
                         "summary": summary, "files": files, "intent": intent}
                # 女仆长放行的标记：前端据此把这一行画成绿色「已放行」，
                # 落盘回放时也还在，不会像 toast 那样两秒就没了
                if guardian_ok:
                    event["guardian_allow"] = guardian_ok
                # 路径一律给全：AI 给的常常是相对工作区的路径，面板直接拿去
                # 跳目录会落到后端进程的 cwd 上（一般是家目录），跟 AI 看的地方对不上
                if name in ("read_file", "write_file", "edit_file", "list_dir",
                            "ocr_image"):
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
                activity = f"正在{label}：{intent or summary}" if (intent or summary) else f"正在{label}"
                if slow_hint:
                    activity += f"（{slow_hint}）"
                # 工具卡片要插在正文后面：先把手头攒的 delta 推出去，
                # 否则这批字会晚于工具卡片到达，前端顺序就是乱的
                m.flush_delta_now(turn_sid)
                await m.manager.broadcast({"type": "tool_activity", "text": activity, "ts": time.time(), **m._turn_tag()})
                # 工具开跑：带上这一刻的时间戳。前端拿它显示「进行中 12s」，
                # 长任务（装包、扫盘、抓帧）不再只有一个转圈，用户能看出还在动
                tool_started = time.time()
                await m.manager.broadcast({"type": "tool_start", "name": name,
                                           "label": label, "summary": summary,
                                           "intent": intent,
                                           "slow": slow_hint,
                                           "started": tool_started,
                                           "ts": tool_started, **m._turn_tag()})
                # 终端联动：必须在执行之前推「开跑」，面板据此立刻挂一条执行中的记录
                # 并开始轮询实时输出。放到执行后面就只剩「已结束」一条，
                # 执行中的动画和进度全看不见了
                if name == "run_command" and str(args.get("command") or "").strip():
                    claim = tools.CURRENT_CMD
                    await m.manager.broadcast({"type": "term_start",
                                               "command": str(args.get("command")),
                                               "reason": str(args.get("reason") or ""),
                                               "slow": slow_hint,
                                               "cwd": (claim or {}).get("cwd", ""),
                                               "track": bool(claim and claim.get("command") == args["command"]),
                                               # 面板记下推送时刻，用来对齐轮询拿到的 started：
                                               # 对不上说明这条命令已经跑完、CURRENT_CMD 换了，
                                               # 别把它当成还在执行的那条一直转圈
                                               "ts_sent": time.time(),
                                               "ts": time.time(), **m._turn_tag()})
                # 文件浏览器联动：凡是带路径的工具都推一条，面板据此自动跳目录。
                # 只推路径不推内容 —— 面板自己再走 /api/browse 取，权限和越界检查
                # 都留在那一个接口里
                if event.get("path"):
                    await m.manager.broadcast({"type": "fs_follow", "path": event["path"],
                                               "is_dir": bool(event.get("is_dir")) or name == "list_dir",
                                               "ts": time.time(), **m._turn_tag()})
                # 遮罩只在真的要动屏幕的这一步亮起（见上面 handle_chat 里的说明）。
                # 放在真正执行前才 set_overlay(True)：鼠标注入期间 tools 会让遮罩
                # 穿透（set_overlay_passthrough），否则那一下点击会点在遮罩上
                # 被当成「用户终止」。
                # pointer：这一步会不会按下鼠标键（点击/滚动/拖拽/滑动）。之前在 tools
                # 内部自己开穿透，而穿透在遮罩还没显示时什么都不做，于是遮罩随后
                # 才冒出来，正好接住那一下点击 —— 这里必须先让遮罩就位再看这个标志
                on_screen = tools.touches_screen(name, args)
                pointer = on_screen and tools.pointer_action(name, args)
                m.set_overlay_pointer(pointer)
                if on_screen:
                    # 拉起遮罩最长要等 6 秒旧实例退位，必须走线程，否则整个 ws 卡住
                    await m.set_overlay_async(True, activity)
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
                    result, notes = await m._run_tool_busy(name, args)
                finally:
                    if on_screen:
                        m.set_overlay(False)
                    m.set_overlay_pointer(False)
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
                if name in ("search_wallpaper", "search_image"):
                    # 候选图列表随事件带给前端，直接渲染成可点选的图。
                    # 结果文本里也有 id，模型能对上编号，但图片本身不靠它
                    # src_url 是后端去重用的原始地址，不往浏览器送
                    event["candidates"] = [{k: v for k, v in c.items() if k != "src_url"}
                                           for c in tools.LAST_CANDIDATES]
                    event["query"] = str(args.get("query") or "")
                    # 候选编号与 id 的对应表：这一轮的工具结果下一轮就进不了历史，
                    # 用户隔一轮回一句「3」时模型连候选是什么都不记得。这里先存着，
                    # 落盘前挂到本轮回答上（见 handle_chat 的 cand_tag）
                    turn_candidates[:] = [
                        {"no": i + 1, "id": c["id"], "title": c.get("title") or ""}
                        for i, c in enumerate(tools.LAST_CANDIDATES)]
                if name == "screenshot":
                    m2 = re.search(r"/shots/\S+?\.png", result)
                    if m2:
                        event["image"] = m2.group(0)
                    else:
                        # 抓帧失败：接口层要把原因带到前端，不能让用户只看到
                        # 一行「已截屏」却一直没有画面。
                        # kind/hint 是归因（off / black / missing）与对应建议，
                        # 前端据此弹一个带处置办法的卡片，而不是只甩一行红字
                        event["error"] = shot_state.get("error") or "未知原因"
                        event["error_kind"] = shot_state.get("error_kind") or ""
                        event["error_hint"] = shot_state.get("error_hint") or ""
                # 这一步花了多久：前端在工具行右侧显示「1.8s」，执行中的卡片
                # 也据此停表。落盘后刷新页面仍能看到
                event["elapsed"] = round(time.time() - tool_started, 2)
                event["started"] = tool_started
                event["seq"] = _next_seq()
                events.append(event)
                await m.manager.broadcast({"type": "tool_done", "event": event,
                                           "ts": time.time(), **m._turn_tag()})
                _persist_screen()
                # 工具结果前面统一加「[工具名] 」，这类元信息在请求最前面：
                # 同一次调用无论正文多长，前缀都一样，DeepSeek 的前缀缓存才命中得上。
                # 反过来，若这里塞「本次命中缓存」之类的可变文案，后面整段历史
                # 立刻全部失配，一次工具往返就能把几万 token 从命中打回未命中。
                # 长度必须封顶：cat 一个大文件、npm install 刷屏都能吐几十 MB，
                # 原样塞进来这一轮的请求体就会一路涨到发不动，之后每次都带着它重发。
                # 前缀是工具名，截断点之后的内容对缓存无影响，所以封顶不影响命中
                msgs.append({"role": "tool", "tool_call_id": call["id"],
                             "content": f"[{name}] {m._cap_tool_text(result)}"})
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
                            {"type": "text", "text": m._agent_shot_caption(max(turn_shot_no, 1))},
                        ]}
                        if shot_msg is not None:
                            try:
                                msgs.remove(shot_msg)
                            except ValueError:
                                pass
                        msgs.append(fresh)
                        shot_msg = fresh
                    except Exception as exc:
                        devlog.debug("main._screen_call", error=repr(exc))
                        pass
        # 工具轮数用尽：最后再不给工具地问一次，让它基于已有结果收尾回答
        msg = await m.llm.chat_with_tools_stream(
            msgs, [], on_delta, max_tokens=_output_budget(msgs, m._estimate_tokens(msgs)))
        if msg.get("reasoning_content") and thought_ev is None:
            thought_ev = {"type": "thought", "text": msg["reasoning_content"]}
            events.append(thought_ev)
        answer = (msg.get("content") or "").strip()
        limit_note = qqtext.get("tool_limit", rounds=m.MAX_TOOL_ROUNDS)
        # 收尾时模型经常什么都不写：这不是正常答复，要把「为什么停」说清楚
        return f"{answer}\n\n{limit_note}" if answer else limit_note
    except Exception as e:
        # 把失败当正常回复返回，下一轮模型会在自己的历史里读到一句「模型调用失败」，
        # 于是把这句话当成它说过的话继续往下聊。明确标成中断，交给上层提示
        friendly = _llm_error_text(e)
        print(f"[DeckMeido] {friendly}", file=sys.stderr)
        raise RuntimeError(friendly) from e
