"""WebSocket 连接管理：连上/断开、握手快照、心跳、广播

从 main.py 平移出来的第二块。原来「连接集合 + /ws 端点的收发循环 + 各处
manager.broadcast 广播」都散在 main 里，连接生命周期的心跳/清理规则
和业务逻辑混在一处。

这里只做平移，不改行为：
  - 端点里要用到的 main 运行态（store / start_chat / get_status /
    status_async / power …）统一经 get_main() 现读；
  - main 侧保留 `manager` 这个名字（直接引用本模块的 ConnectionManager 实例），
    其余调用点的 manager.broadcast(...) 写法完全不变；
  - /ws 路由注册在 register(app)，保持原有路径与依赖。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from . import audit, devlog, power, singleton
from .session_store import store

get_main = None


def init(get_main_fn) -> None:
    global get_main
    get_main = get_main_fn


def _m():
    return get_main()


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, msg: dict):
        # 并发发：这里串行 await 的话，一条卡住的连接（手机切到后台、网络半死
        # 但没断）就会把后面所有端的流式输出一起堵住
        targets = list(self.active)
        if not targets:
            return
        devlog.log("ws.broadcast", type=msg.get("type"), clients=len(targets), payload=msg)
        results = await asyncio.gather(*(ws.send_json(msg) for ws in targets),
                                       return_exceptions=True)
        for ws, res in zip(targets, results):
            if isinstance(res, BaseException):
                self.active.discard(ws)

    async def close_all(self, code: int = 1000):
        """按指定原因码断开所有连接（设备被移除后用 4401，让前端回到鉴权页）"""
        for ws in list(self.active):
            try:
                await ws.close(code=code)
            except Exception as exc:
                devlog.debug("main.close_all", error=repr(exc))
                pass
        self.active.clear()


manager = ConnectionManager()


async def _broadcast_remote_input(state: dict):
    """跨屏接管状态变化：广播给所有页面（远程控制端和 Deck 本机都要看到）"""
    await manager.broadcast({"type": "remote_input", **state})


def register(app) -> None:
    """把 /ws 端点挂到 app 上（main 建好 app 后调用）"""

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        m = _m()
        client_ip = ws.client.host if ws.client else ""
        if not m._ws_authorized(ws):
            # 先 accept 再 close，浏览器才能拿到 4401 这个原因码，否则只会看到 1006 而无限重连
            audit.record("auth", "ws", "fail", ip=client_ip, reason="unpaired")
            await ws.accept()
            await ws.close(code=4401)
            return
        await manager.connect(ws)
        # 电源抑制的「前台网页」登记：连接一断（关页面、切后台被回收）就算离开
        _view_src = f"ws:{id(ws)}"
        # 来源：优先信调用方显式声明的 client（插件标 qam，桌面客户端标 client），
        # 没声明就按 UA 猜——手机浏览器自动算 mobile，其余算 web。
        # ws_mode 用来记住「这条连接是在什么模式下连上的」：状态广播是全局的，
        # 游戏模式的判定（PipeWire 里的 gamescope 节点）对所有连着的端都成立，桌面网页就会
        # 跟着显示成「游戏模式」。所以按连接自己的会话判定，桌面网页永远是桌面
        ua_client = m.client_from_ua(ws.headers.get("user-agent", ""))
        ws_mode = (await asyncio.to_thread(m.get_status))["mode"]
        # 这两项告诉前端「此刻后端进程里还在跑哪一轮」：
        # inflight = 流式已写过、还没收尾的会话（这一轮的正文还只在半路上，没进 history）
        # boot_id  = 本进程的启动标识，前端据此判断这条连接是「进程重启后的第一条」
        # 还是「只是链路断了一下」。以前前端区分不了，任何一次重连都当成崩溃，
        # 把本地缓存补回整段对话，屏幕上就冒出「已由本机缓存补回」
        inflight = sorted(m._inflight_sids())
        boot_id = singleton.BOOT_ID
        try:
            # 新连接同步历史（带上 sid，前端才能建立会话代次基线，
            # 否则 currentSid 一直是空值，代次过滤整条失效）
            await ws.send_json({"type": "history", "messages": store.history,
                                "sid": store.current_sid, "my_client": ua_client,
                                "inflight": inflight, "boot": boot_id,
                                "partial": m._inflight_snapshot(store.current_sid)})
            devlog.log("ws.send", type="history", messages=len(store.history),
                       sid=store.current_sid, inflight=inflight, boot=boot_id)
            await ws.send_json({"type": "status",
                                "data": {**await m.status_async(), "mode": ws_mode}})
            while True:
                data = await ws.receive_json()
                # 不记录图片 base64（可能有几十 MB），只记类型、文本摘要和是否带图
                devlog.log("ws.recv", type=data.get("type"),
                           text=str(data.get("text") or "")[:300],
                           has_image=bool(data.get("image")),
                           workspace=data.get("workspace"),
                           client=data.get("client"))
                if data.get("type") == "ping":
                    # 前端心跳：回一条 pong 让它知道链路还活着
                    await ws.send_json({"type": "pong"})
                    continue
                if data.get("type") == "history":
                    # 客户端断线重连后主动要一份：握手时后端已经发过一次，
                    # 但断线期间别的端说的话它没收到，得重新对齐
                    await ws.send_json({"type": "history", "messages": store.history,
                                        "sid": store.current_sid, "my_client": ua_client,
                                        "inflight": sorted(m._inflight_sids()),
                                        "boot": boot_id,
                                        "partial": m._inflight_snapshot(store.current_sid)})
                    continue
                if data.get("type") == "view":
                    # 前端「网页是否在前台」的上报（可见即上报，切后台/关页面撤销）。
                    # 桌面模式下只要有网页在前台就不息屏，见 power.AwakeKeeper._want。
                    # 每 30s 一次心跳，不在日志里刷记录
                    power.note_view(_view_src, bool(data.get("active")))
                    continue
                if data.get("type") == "chat":
                    # 开始发问就刷新：这条连接此刻在什么模式下，就是它自己的模式
                    ws_mode = (await asyncio.to_thread(m.get_status))["mode"]
                    try:
                        # 插件那条路要从 ws 直接带图和真实工作区进来：
                        # 它挂在 gamescope 上，请求和流式都在同一条连接里，
                        # 走不了"先 POST 拿结果再订阅"的两段式。
                        ws_root = data.get("workspace")
                        ws_res = await m.start_chat(
                            str(data.get("text", "")).strip(),
                            image=data.get("image") or None,
                            image_prompt=data.get("image_prompt")
                            or "（这是当前游戏画面，结合画面回答用户的问题）",
                            workspace=Path(ws_root).expanduser() if ws_root else None,
                            client=str(data.get("client") or ua_client),
                            device=str(data.get("device") or "").strip() or None,
                            brief=str(data.get("brief") or "").strip(),
                            turn_id=str(data.get("turn_id") or ""),
                            surface=str(data.get("surface") or "").strip() or None,
                        )
                        # 上一轮还没跑完（见 start_chat 的串行化）：这条不是新回答，
                        # 得明说一句，否则前端发完就在那儿干等，看不出被挡住了
                        if isinstance(ws_res, dict) and ws_res.get("busy"):
                            await ws.send_json({"type": "system",
                                                "text": str(ws_res.get("reply") or ""),
                                                "ts": time.time()})
                    except asyncio.CancelledError:
                        pass  # 用户点了终止，保持 ws 连接
                    except Exception as exc:
                        # 一轮对话内部报错不能把整条 ws 打死：旧逻辑异常直接冒到外层，
                        # 连接被断 → 前端 busy 卡死 → 后续消息全排队发不出去
                        try:
                            await ws.send_json({
                                "type": "assistant_message",
                                "text": f"这轮出错了：{exc}",
                                "ts": time.time(), "events": [],
                            })
                        except Exception as exc:
                            devlog.debug("main.ws_endpoint", error=repr(exc))
                            pass
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            devlog.debug("main.ws_endpoint", error=repr(exc))
            pass
        finally:
            # 兜底：不管怎么退出都要摘掉连接，否则 broadcast 会一直往死连接上发
            await manager.disconnect(ws)
            # 连接没了就等于这个页面不在了：撤销前台登记，别让抑制一直挂着
            power.note_view(_view_src, False)
