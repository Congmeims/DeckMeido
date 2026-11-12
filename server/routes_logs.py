"""日志与诊断路由：/api/logs*、/api/debug/render

从 main.py 平移出来的第一批路由。依赖通过 init() 注入，避免与 main 循环导入；
FastAPI 的 Depends 在定义路由时就会求值，所以路由也在 init() 里注册。
"""
from __future__ import annotations

import asyncio
import sys

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import audit, devlog

router = APIRouter()


class RenderReport(BaseModel):
    """前端回报：本轮思考链实际渲染了多少字符、几段（仅 dev_mode 接收）"""
    sid: str = ""
    reasoning_chars: int = 0
    segments: int = 0


class LogClearRequest(BaseModel):
    """清空日志：debug / run / install / all"""
    category: str = "all"


def init(require_auth, client_ip) -> None:
    """由 main 在 app 创建后调用：登记路由并注入鉴权闸门与取 IP 的小工具"""
    auth = [Depends(require_auth)]

    @router.get("/api/logs/usage", dependencies=auth)
    async def api_logs_usage():
        """日志占用：总量 / 分项 / 文件数 / debug 轮转上限"""
        # 目录扫描放线程，避免日志多时卡住事件循环
        return await asyncio.to_thread(devlog.usage)

    @router.post("/api/logs/clear", dependencies=auth)
    async def api_logs_clear(req: LogClearRequest, request: Request):
        """清空日志（不碰配置 / 会话 / 记忆 / 模型）。

        正在写的 backend.log / overlay.log / 当前 debug 文件用 truncate，
        其余直接删除；返回 freed_bytes 与失败清单，前端据此即时刷新占用。
        """
        try:
            result = await asyncio.to_thread(devlog.clear_logs, req.category)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            # 清理本身出任何意外都不该变成 500 纯文本（前端会 JSON.parse 失败）：
            # 把异常类型和原因带回去，让界面能直接显示为什么没清掉
            detail = f"{type(exc).__name__}: {exc}"
            print(f"[DeckMeido] 清空日志失败：{detail}", file=sys.stderr)
            result = {
                "ok": False,
                "category": req.category,
                "deleted": 0,
                "truncated": 0,
                "failed": 1,
                "failed_files": [{"name": "logs", "error": detail}],
                "freed_bytes": 0,
                "usage": None,
            }
        audit.record("config", "logs_clear", "ok" if result["ok"] else "warn",
                     ip=client_ip(request), detail=req.category)
        return result

    @router.post("/api/debug/render", dependencies=auth)
    async def api_debug_render(req: RenderReport):
        """前端回报本轮思考链实际渲染了多少字（仅 dev_mode 可用）

        和 reasoning.end / session.save 三条长度放在一起，能直接看出是哪一环短的：
        LLM 收到 → 落盘 → 界面渲染。
        """
        if not devlog.enabled():
            raise HTTPException(status_code=403, detail="开发者模式未开启")
        devlog.log("ui.render", sid=req.sid, reasoning_chars=req.reasoning_chars,
                   segments=req.segments)
        return {"ok": True}
