"""系统状态与设备接口：/api/status*、/api/qrcode、/api/devices*

从 main.py 平移出来的第四批路由。状态采集与局域网地址计算都在 main 里，
这里通过 get_main() 现读，避免复制一份缓存值。
"""
from __future__ import annotations

import asyncio
import base64
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from . import audit, devlog, netmode, pairing

router = APIRouter()


class DeviceRenameRequest(BaseModel):
    device_id: str
    name: str


class DeviceIdRequest(BaseModel):
    device_id: str


def init(require_auth, client_ip, get_main) -> None:
    auth = [Depends(require_auth)]

    @router.get("/api/status", dependencies=auth)
    async def api_status():
        # 完整采集（模式探针 pw-dump、depcheck、tts）全部在线程里跑并带 TTL 缓存，
        # 别阻塞事件循环（WS 与流式输出都在同一个 loop 上）
        return await get_main().status_async()

    @router.get("/api/net/probe", dependencies=auth)
    async def api_net_probe(force: bool = False):
        """公网连通性：online / lan_only / offline

        按需探测、结果缓存；总预算 2 秒，不会拖住启动或设置页。界面拿到非 online
        时只提示「可以切到离线模式」，绝不自动改开关。
        """
        return await asyncio.to_thread(netmode.probe, bool(force))

    @router.get("/api/status/line", dependencies=auth)
    async def api_status_line(state: str = Query(default=""),
                              game: str = Query(default=""),
                              game_confidence: str = Query(default=""),
                              nickname: str = Query(default="")):
        """插件状态栏的一句话（idle/busy/error + 高置信度游戏名 + 昵称）

        不写会话历史、不写 memory.json；失败自动回本地模板，状态栏永不空白。
        """
        m = get_main()
        return await m.build_status_line(state, game, game_confidence, nickname)

    @router.get("/api/qrcode", dependencies=auth)
    async def api_qrcode():
        """局域网访问地址 + 二维码 PNG（base64）"""
        url = get_main().phone_url()
        png_b64 = ""
        try:
            import qrcode

            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:
            devlog.debug("main.api_qrcode", error=repr(exc))
            pass
        return {"url": url, "qr": png_b64}

    @router.get("/api/devices", dependencies=auth)
    async def api_devices():
        m = get_main()
        return {"ok": True, "devices": pairing.manager.devices.list(),
                "device_days": m._device_days()}

    @router.post("/api/devices/rename", dependencies=auth)
    async def api_device_rename(req: DeviceRenameRequest, request: Request):
        m = get_main()
        try:
            device = pairing.manager.devices.rename(req.device_id, req.name)
        except pairing.PairError as exc:
            raise m._pair_http(exc) from exc
        audit.record("device", "rename", "ok", ip=client_ip(request),
                     detail=device.get("name", ""))
        return {"ok": True, "device": device}

    @router.post("/api/devices/remove", dependencies=auth)
    async def api_device_remove(req: DeviceIdRequest, request: Request):
        if not pairing.manager.devices.remove(req.device_id):
            raise HTTPException(status_code=404, detail="设备不存在或已被移除")
        audit.record("device", "remove", "ok", ip=client_ip(request),
                     detail=req.device_id[:8])
        return {"ok": True, "devices": pairing.manager.devices.list()}

    @router.post("/api/devices/remove_all", dependencies=auth)
    async def api_device_remove_all(request: Request):
        n = pairing.manager.devices.remove_all()
        audit.record("device", "remove_all", "ok", ip=client_ip(request), detail=str(n))
        return {"ok": True, "removed": n}
