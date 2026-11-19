"""会话接口：/api/sessions*、/api/messages/delete、/api/checkpoint

从 main.py 平移出来的第三批路由。这些端点读写大量 main 模块级状态
（pending_action / current_task / _ctx_used 等会被整体重新赋值），
所以统一通过 get_main() 现读现写，本模块不缓存任何一份。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import audit, devlog, onebot, session_store, tools
from .session_store import store

router = APIRouter()


class SwitchReq(BaseModel):
    id: str


class CreateReq(BaseModel):
    """新建会话的发起端（client/mobile/qam/...）+ 发起设备，用于侧栏分组与排障"""
    client: str = ""
    device: str = ""


class MetaReq(BaseModel):
    """一段 UI 标记的读写：键名 + 值（空字符串等于删掉）"""
    value: str = ""

    # 只有普通标记，没有敏感数据；限制长度防止被当成存储用
    @classmethod
    def clamp(cls, value: str) -> str:
        return (value or "").strip()[:200]


class MsgDeleteReq(BaseModel):
    ts: float | None = None          # 按消息时间戳定位（前端每条消息都带 data-ts）
    role: str = "assistant"          # 兜底：没有 ts 时按角色删最后一条
    with_reply: bool = False         # 删用户消息时，连它下面那条回答一起删


def init(require_auth, client_ip, get_main, set_overlay) -> None:
    """由 main 在 app 创建后调用；set_overlay 用于删除当前会话前强制收掉浮层"""
    auth = [Depends(require_auth)]

    @router.get("/api/sessions", dependencies=auth)
    async def api_sessions():
        """侧栏列表：只读索引，不再把每个会话文件整份解析一遍

        以前会话一多，切会话 / 删会话（各端都会重拉这个接口）要解析几 MB JSON，
        而且全在事件循环里。索引只存四行字段，落盘时顺带更新。
        """
        # 次级键用 id：同一秒落盘的多个会话顺序固定，强刷不会来回抖
        # origin 给前端做自动分组；老会话没有就按 sid / 标题回退推断一次
        items = []
        for it in session_store.session_order(store.index().values()):
            row = dict(it)
            row["origin"] = session_store.normalize_origin(
                row.get("origin") or "", row.get("id") or "", row.get("title") or "")
            items.append(row)
        return {"current": store.current_sid, "items": items}

    @router.get("/api/meta/{key}", dependencies=auth)
    async def api_meta_get(key: str):
        """读一段 UI 标记（如「引导看过没有」）

        这类标记以前只放前端 localStorage，而桌面客户端的 WebEngine 配置目录
        在重启后不保留，于是每次启动都当新用户、引导反复弹。放到服务端数据目录
        后，网页、客户端、局域网页面共用同一份。
        """
        return {"value": session_store.read_meta(key)}

    @router.post("/api/meta/{key}", dependencies=auth)
    async def api_meta_set(key: str, req: MetaReq):
        session_store.write_meta(key, MetaReq.clamp(req.value))
        return {"ok": True}

    def _current_session_untouched() -> bool:
        """当前会话是不是「一句话都没说过」的空会话

        判据是历史里一条 user / assistant 消息都没有（系统提示、tool 消息不算）。
        只看 store.history 而不是磁盘：内存里的历史就是唯一真相，磁盘文件
        可能因为空写保护还没落盘。

        current_sid 为空（上一个会话刚被删干净，见 delete 分支）时不算「空会话」：
        这时压根没有会话，得走下面的新建分支分配一个 sid，否则点「＋」什么都不会发生。
        """
        if not store.current_sid:
            return False
        return not any(m.get("role") in ("user", "assistant") for m in store.history)

    @router.post("/api/sessions/create", dependencies=auth)
    async def api_session_create(req: CreateReq, request: Request):
        m = get_main()
        # 会话列表由索引维护，而索引没写过盘的会话只活在内存里
        # （点「＋」新建、一句话没说就走了的那种）。它的内容从头到尾都在
        # 内存和其它地方，这里再 save 一次只会把它变成磁盘上一条空会话 ——
        # 返回给前端之前先把这类纯空会话从索引里摘掉，别让侧栏凭空多一条。
        # 真正说过话的会话照旧存，touch=False 不刷新排序。
        if store.current_sid:
            if _current_session_untouched():
                store.index_drop_mem(store.current_sid)
            else:
                m.save_current_session(touch=False)
        # 已经在空会话里就原地不动，各端也不用重刷列表。
        # 但发起端和设备要补记一下：在 A 电脑点「＋」开了个空输入框，
        # 到 B 电脑再点「＋」时命中的还是这个空会话 —— 不补记的话，
        # 它永远带着 A 的分组和归属，看起来就像「在 B 上新建的会话跑到了 A 组」。
        # 会话一句没说过、没有任何内容，改归属不会影响历史
        if _current_session_untouched():
            if store.current_sid:
                origin = req.client or m.client_from_ua(request.headers.get("user-agent", ""))
                m._set_session_origin(store.current_sid, origin or None)
                store.set_session_device(store.current_sid,
                                         (req.device or "").strip() or None, replace=True)
            return {"ok": True, "id": store.current_sid, "unchanged": True}
        store.current_sid = m._fresh_sid()
        # 换对象而不是原地 clear：还在跑的那一轮持有旧列表引用，
        # 原地清空会把它的历史一起抹掉，结果就变成「有问无答」
        store.history = []
        # 上下文占用按会话记（main._ctx_set），新会话号还没用过，自然是 0
        m._ctx_set(store.current_sid, 0)
        # 代次 +1：正在跑的那一轮会据此发现自己的会话已被换掉
        store.bump_epoch()
        store.turn["active"] = False
        # 新建时就记下来源：否则这条会话在说第一句话之前 origin 是空的，
        # 侧栏会把它归到「调试」，看不出是从客户端/网页/掌机面板建的。
        # 前端没传（旧缓存页面）时按 UA 兜一层，和聊天链路 start_chat 的判断同源，
        # 免得同一台手机「说话建的会话」和「点＋建的会话」落进不同分组
        origin = req.client or m.client_from_ua(request.headers.get("user-agent", ""))
        # 来源只记在内存里，等这一轮真说了话再随正常落盘写出去。
        # 这里以前会 save_current_session()，于是每点一次「＋」就立刻在侧栏留下
        # 一条点进去空空的「新会话」，而它本来就是为了「换一个干净的输入框」。
        m._set_session_origin(store.current_sid, origin or None)
        # 设备号只做归属记录（排障用），跟着第一条消息一起随会话落盘。
        # 新会话号是新生成的，内存里不会残留旧的归属，按「只认第一次」记即可
        store.set_session_device(store.current_sid, (req.device or "").strip() or None)
        # 代次已经变了，前端要把当前会话号换掉；历史是空的，一起推一份让各端清屏。
        # 不发 session_changed 老列表不会刷新，新建的那个会话号也传不出去
        await m.manager.broadcast({"type": "history", "messages": store.history,
                                   "sid": store.current_sid})
        await m.manager.broadcast({"type": "session_changed", "current": store.current_sid})
        await m.manager.broadcast({"type": "system", "text": "已开始新会话", "ts": time.time()})
        return {"ok": True, "id": store.current_sid}

    @router.post("/api/sessions/switch", dependencies=auth)
    async def api_session_switch(req: SwitchReq):
        m = get_main()
        p = m._sess_path(req.id)
        if not p.exists():
            raise HTTPException(status_code=404, detail="session not found")
        # 查看动作不写时间戳：仅切走前的收尾写入，排序仍按最近真实活动
        m.save_current_session(touch=False)
        try:
            # 整份会话可能有几 MB，读盘同样不能压在事件循环里
            body = await asyncio.to_thread(p.read_text, encoding="utf-8")
            data = json.loads(body)
        except Exception as e:
            # 文件坏了就明确拒绝：以前这里直接抛 500，前端只能笼统提示「切换失败」，
            # 而用户看到的是会话列表里明明有这一条
            raise HTTPException(status_code=500,
                                detail=f"会话文件读取失败：{e}") from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="会话文件格式异常")
        store.current_sid = data.get("id", req.id)
        # 同样换对象：在跑的那一轮仍持有旧列表，不受切换影响
        store.history = data.get("history", [])
        comp = data.get("compact")
        if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
            m._set_compact(store.current_sid, comp.get("summary") or "",
                           min(int(comp.get("upto") or 0), len(store.history)))
        else:
            store.drop_compact(store.current_sid)
        # 切换会话也要换 pending_intent：否则 A 会话的密码授权会被带进 B 会话
        if m._restore_intent_map(store.current_sid, data):
            try:
                m._write_session(store.current_sid, store.history)
            except Exception as e:
                print(f"[DeckMeido] 切换会话时写回 pending_intent 失败：{e}", file=sys.stderr)
        m._ctx_set(store.current_sid, m._estimate_tokens(
            await m._build_llm_msgs(store.history, m.SYSTEM_PROMPT, store.current_sid)))
        store.bump_epoch()
        store.turn["active"] = False
        await m.manager.broadcast({"type": "history", "messages": store.history,
                                   "sid": store.current_sid})
        await m.manager.broadcast({"type": "session_changed", "current": store.current_sid})
        return {"ok": True}

    @router.get("/api/sessions/{sid}/history", dependencies=auth)
    async def api_session_history(sid: str):
        """只读历史：给手机端预览会话，不改变服务端 current_sid。

        网页控制台的「切换会话」是全局动作（会同时广播给掌机和其他客户端），
        手机翻历史不该把别人正在看的会话切走。这里只读文件，不碰内存里的
        current_sid / history / compact，也不触发 history 广播。
        """
        m = get_main()
        if not m._valid_session_id(sid):
            raise HTTPException(status_code=404, detail="session not found")
        p = m._sess_path(sid)
        if not p.exists():
            raise HTTPException(status_code=404, detail="session not found")
        try:
            body = await asyncio.to_thread(p.read_text, encoding="utf-8")
            data = json.loads(body)
        except Exception as e:
            raise HTTPException(status_code=500,
                                detail=f"会话文件读取失败：{e}") from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="会话文件格式异常")
        hist = data.get("history")
        if not isinstance(hist, list):
            hist = []
        idx = store.index().get(sid) or {}
        return {
            "ok": True,
            "id": data.get("id") or sid,
            "title": data.get("title") or idx.get("title") or "",
            "created": data.get("created"),
            "updated": data.get("updated") or idx.get("updated") or 0,
            "messages": hist,
        }

    @router.post("/api/messages/delete", dependencies=auth)
    async def api_message_delete(req: MsgDeleteReq):
        """删除会话里的单条消息。

        只从「当前会话的历史」里摘掉那一条，并立刻落盘；不发 history 全量重推，
        否则整屏会重绘、滚动位置跳到顶上。前端自己把 DOM 上的那条摘掉即可。

        为什么按 ts 定位而不是按序号：界面上的顺序和 history 不完全一一对应
        （一轮助手回答在界面上可能拆成 思考/正文/工具 好几块，但在 history 里
        只有一条），用序号会删错行。ts 是每条消息落盘时就写进去的，唯一且稳定。
        """
        m = get_main()
        if store.turn.get("active"):
            raise HTTPException(status_code=409, detail="本轮回答还在生成，请等它结束再删除")

        hidx = -1
        if req.ts:
            # 时间戳浮点比较留一点容差：JSON 往返可能丢精度
            best = None
            for i, msg in enumerate(store.history):
                t = msg.get("ts")
                if not isinstance(t, (int, float)):
                    continue
                if abs(float(t) - float(req.ts)) <= 0.001:
                    best = i
                    break
            hidx = best if best is not None else -1
        if hidx < 0:
            # 没有匹配到 ts：退化成「删同角色最后一条」，避免前端按钮完全失效
            want = req.role if req.role in ("user", "assistant") else "assistant"
            for i in range(len(store.history) - 1, -1, -1):
                if store.history[i].get("role") == want:
                    hidx = i
                    break
        if hidx < 0:
            raise HTTPException(status_code=404, detail="找不到这条消息")

        target = store.history[hidx]
        removed = 1
        # 删用户消息时，把紧跟着的那条助手回答一起带走：
        # 只删提问留着回答，模型下次读历史会看到「没有问题的答案」，很容易胡接
        if req.with_reply and target.get("role") == "user":
            if hidx + 1 < len(store.history) and store.history[hidx + 1].get("role") == "assistant":
                del store.history[hidx + 1]
                removed += 1
        del store.history[hidx]

        # 压缩游标要跟着收：否则游标指向的条数比历史还长，
        # 发给模型的上下文会整段错位（摘要跳过了还没被摘要的消息）
        sid = store.current_sid
        comp = m._compact_for(sid)
        if comp["upto"] > len(store.history):
            m._set_compact(sid, comp["summary"], len(store.history))

        m.save_current_session()
        m._ctx_set(store.current_sid, m._estimate_tokens(
            await m._build_llm_msgs(store.history, m.SYSTEM_PROMPT, store.current_sid)))
        await m.manager.broadcast({"type": "session_changed", "current": store.current_sid})
        return {"ok": True, "removed": removed, "left": len(store.history)}

    @router.delete("/api/sessions/{sid}", dependencies=auth)
    async def api_session_delete(sid: str, request: Request):
        m = get_main()
        # 删除一个还在跑的会话前，先强制终止它在跑的那一轮，否则它那轮 run_agent
        # 协程仍持有 store.history 并继续往外写，会把上一个会话的流程串进新会话。
        task = m.current_task
        if sid == store.current_sid and task and not task.done():
            task.cancel()
            set_overlay(False)
            await tools.abort_all_procs()
            # 给 cancel 留一拍，让 handle_chat 的 CancelledError 分支先落一条终止消息，
            # 避免删除后它又异步补写回历史
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:
                devlog.debug("main.api_session_delete", error=repr(exc))
                pass
        # 先拉黑再删文件：反过来的话，删完到拉黑之间那一瞬间的落盘（流式回调）
        # 就能把文件写回来，等于白删
        store.deleted_sids.add(sid)
        # 内存里按 sid 挂着的摘要/意图/流式缓冲也一并清掉。QQ 通道的 sid 是按
        # uin/群号算死的，不清的话同一个人下一条消息又会把旧摘要当上文复读
        store.forget_session(sid)
        m._ctx_drop(sid)
        m._stream_bufs.pop(sid, None)
        if sid.startswith("qq-"):
            try:
                onebot.drop_session(sid)
            except Exception as exc:
                devlog.debug("main.api_session_delete", error=repr(exc))
                pass
        pend = m.pending_action
        if isinstance(pend, dict) and (pend.get("sid") or "") == sid:
            m.pending_action = None
        # 删除是低频操作，但这里要「等」而不是「发出去就走」：删完到 IO 线程真正执行
        # 之间的那一小段里若有落盘排队，文件就会被写回来，等于白删。等它又不该卡住事件循环，
        # 所以用 to_thread 把 .result() 挪到线程里
        await asyncio.to_thread(session_store.io_submit(store.index_drop, sid).result)
        p = m._sess_path(sid)
        if p.exists():
            try:
                # 软删除：移进 userdata/trash/ 而不是直接 unlink。误删（含自动化脚本
                # 连点两次确认）还能从回收站找回，列表则因为索引已摘除而不再返回
                await asyncio.to_thread(
                    session_store.io_submit(session_store.trash_session, sid).result)
            except OSError as e:
                store.deleted_sids.discard(sid)
                audit.record("session", "delete", "fail",
                             ip=client_ip(request), detail=f"{sid}: {e}")
                raise HTTPException(status_code=500, detail=f"删除失败：{e}")
            audit.record("session", "delete", "ok",
                         ip=client_ip(request), detail=f"{sid} -> trash/")
        else:
            audit.record("session", "delete", "ok",
                         ip=client_ip(request), detail=f"{sid} (文件已不存在)")
        # 流式副档案跟着一起删：留着的话下次启动会把它当「被中断的回答」恢复出来，
        # 删掉的会话就凭空冒出一条消息
        await asyncio.to_thread(session_store.io_submit(store.drop_stream_side, sid).result)
        if sid == store.current_sid:
            # 删掉的是当前会话。以前这里无条件 _fresh_sid() 再落盘一个空会话，
            # 于是「删一个」必然「多一个」；现在先试着切到剩下最新的那个，
            # 真的一个都不剩了才新建（且标题带时间，不再和别的「新会话」重名）
            store.bump_epoch()
            store.turn["active"] = False
            # 候选顺序按索引里的 updated（比 stat 每个文件便宜得多），
            # 真正要恢复的那一份才读文件
            cand = [i["id"] for i in session_store.session_order(store.index().values())
                    if i.get("messages") and i["id"] not in store.deleted_sids]
            nxt = None
            for cand_sid in cand:
                d = await asyncio.to_thread(session_store.io_submit(store.read_session, cand_sid).result)
                if d.get("history"):
                    nxt = d
                    break
            if nxt is None:
                # 全删干净了就别再自动造一个空会话：用户要的是「删干净」，
                # 以前这里必然新建一个，侧栏永远至少挂着一条。
                # 当前会话号先空着，下一条消息进来时会分配新 sid（见 main.handle_chat）
                store.current_sid = ""
                store.history = []
            else:
                store.current_sid = nxt.get("id") or m._fresh_sid()
                # 换对象而不是原地 clear：在跑的那一轮仍持有旧列表引用
                store.history = nxt.get("history") or []
                comp = nxt.get("compact")
                if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
                    m._set_compact(store.current_sid, comp.get("summary") or "",
                                   min(int(comp.get("upto") or 0), len(store.history)))
                else:
                    store.drop_compact(store.current_sid)
                m._ctx_set(store.current_sid, m._estimate_tokens(
                    await m._build_llm_msgs(store.history, m.SYSTEM_PROMPT, store.current_sid)))
            await m.manager.broadcast({"type": "history", "messages": store.history,
                                       "sid": store.current_sid})
            await m.manager.broadcast({"type": "session_changed", "current": store.current_sid})
        return {"ok": True, "current": store.current_sid}

    @router.post("/api/checkpoint", dependencies=auth)
    async def api_checkpoint():
        """客户端退出前先来这儿「打个招呼」：把还没落盘的东西全部刷下去。

        以前客户端退出直接 SIGTERM，后端跑着的这一轮回答就整段消失。
        现在客户端先调这里（拿到 ok 后再发 SIGTERM），正在生成中的正文至少保住到此刻。
        """
        m = get_main()
        flushed = False
        try:
            buf = m._stream_buf()
            text = buf.get("text") or ""
            if store.turn.get("active") and text.strip():
                m.persist_stream(text, force=True, parts=buf.get("parts") or [])
                flushed = True
        except Exception as exc:
            devlog.debug("main.api_checkpoint", error=repr(exc))
            pass
        # 写盘已经丢给 IO 线程排队了，这里要等它真的落完再放客户端走，
        # 否则 SIGTERM 一到，排队中的那一次写入就跟着一起没了。
        # 等待要放到线程里：这一等最长 5 秒，直接在事件循环里等就别想再处理任何请求了
        try:
            await asyncio.to_thread(session_store.io_submit(lambda: None).result, 5)
        except Exception as exc:
            devlog.debug("main.api_checkpoint", error=repr(exc))
            pass
        return {"ok": True, "flushed": flushed, "sid": store.current_sid,
                "turns": len(store.history)}
