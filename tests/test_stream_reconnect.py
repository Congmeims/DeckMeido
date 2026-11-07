"""回归：进行中的一轮刷新后，思维链不能整段消失。

88cad046 的现场：模型整段只思考、只调工具的那些回合不刷新流式缓冲，
用户中途刷新页面（WS 重连按 parts 恢复这一轮），屏幕上的思维链就没了。
这里直接从 server.main 的恢复接口验证：

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ENV = ("DECKMEIDO_DATA_DIR", "DECKMEIDO_ALLOW_SECOND_BACKEND", "DECKMEIDO_PORT")
_OLD = {k: os.environ.get(k) for k in _ENV}
_DATA = tempfile.mkdtemp(prefix="deckmeido-test-")

if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")


def _push_env() -> None:
    os.environ["DECKMEIDO_DATA_DIR"] = _DATA
    os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
    os.environ["DECKMEIDO_PORT"] = "18799"


def _pop_env() -> None:
    for k, v in _OLD.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# 导入 server.main 会读配置、做进程守卫；指到临时数据根，别碰真实 userdata
_push_env()
shutil.copy(ROOT / "server" / "config.example.toml", Path(_DATA) / "config.toml")
from server import main  # noqa: E402

_pop_env()


def setUpModule():
    _push_env()


def tearDownModule():
    _pop_env()
    shutil.rmtree(_DATA, ignore_errors=True)


def _wait_for(fn, budget: float = 3.0):
    end = time.time() + budget
    while time.time() < end:
        out = fn()
        if out:
            return out
        time.sleep(0.05)
    return fn()


class StreamPartsTest(unittest.TestCase):
    def setUp(self):
        (Path(_DATA) / "sessions").mkdir(parents=True, exist_ok=True)

    def _events(self) -> list[dict]:
        return [
            {"type": "thought", "text": "先打开 Kazumi，再截一张图确认窗口"},
            {"type": "tool", "name": "run_command", "label": "运行命令",
             "summary": "flatpak run io.github.Kazumi", "files": [], "output": "ok"},
            {"type": "thought", "text": "窗口应该起来了，但我看不到新画面"},
        ]

    def test_reconnect_snapshot_keeps_thoughts(self):
        """重连靠 _inflight_snapshot 恢复这一轮：思维链必须原样在里面"""
        sid = "regress-stream-a"
        main._stream_bufs.pop(sid, None)
        main.refresh_stream_parts(sid, "", self._events())
        snap = main._inflight_snapshot(sid)
        self.assertIsNotNone(snap)
        thoughts = [p["event"]["text"] for p in snap["parts"]
                    if p.get("type") == "event" and p["event"].get("type") == "thought"]
        self.assertEqual(thoughts, ["先打开 Kazumi，再截一张图确认窗口",
                                    "窗口应该起来了，但我看不到新画面"])
        main._stream_bufs.pop(sid, None)

    def test_thoughts_survive_side_file_reload(self):
        """落盘 → 重读：半截的那一轮里思维链也要在，不能只剩正文"""
        sid = "regress-stream-b"
        main._stream_bufs.pop(sid, None)
        main.refresh_stream_parts(sid, "", self._events())
        side = _wait_for(lambda: main.store.read_stream_side(sid))
        self.assertIsInstance(side, dict)
        thoughts = [p["event"]["text"] for p in (side.get("parts") or [])
                    if p.get("type") == "event" and p["event"].get("type") == "thought"]
        self.assertEqual(len(thoughts), 2)
        main.store.drop_stream_side(sid)
        main._stream_bufs.pop(sid, None)

    def test_assistant_events_survive_session_reload(self):
        """正式收尾写进会话文件后，重读历史里的事件（思维链/工具）都还在"""
        sid = "regress-stream-c"
        hist = [
            {"role": "user", "content": "打开 Kazumi", "ts": time.time()},
            {"role": "assistant", "content": "窗口已启动",
             "ts": time.time(), "events": self._events(), "aborted_turn": True},
        ]
        main.store.write_session(sid, hist)
        path = main.store.sess_path(sid)
        loaded = _wait_for(lambda: path.exists() and path.read_text(encoding="utf-8"))
        self.assertTrue(loaded)
        import json
        data = json.loads(loaded)
        msg = data["history"][1]
        self.assertIn("events", msg)
        self.assertEqual([e["text"] for e in msg["events"] if e["type"] == "thought"],
                         ["先打开 Kazumi，再截一张图确认窗口",
                          "窗口应该起来了，但我看不到新画面"])
        path.unlink()


class TerminalStatusTest(unittest.IsolatedAsyncioTestCase):
    """插件（大屏/QAM）只吃 ws 推送：一轮收尾必须补发 running=false。

    3291bf 的现场：网页端这一轮在 15:14:40 就结束了，但最后一次 status
    广播（run_agent 中途那次）还是 running=true，之后再也没有收尾状态，
    面板重新打开时会先收到缓存值，凭空显示「思考中…」。
    """

    def setUp(self):
        self.sent: list[dict] = []

    async def _broadcast(self, msg):
        self.sent.append(msg)

    async def test_success_broadcasts_terminal_status(self):
        async def fake_chat(text, announce=True, **kw):
            return {"reply": "ok", "events": []}

        with mock.patch.object(main, "handle_chat", fake_chat), \
                mock.patch.object(main.manager, "broadcast", self._broadcast), \
                mock.patch.object(main, "get_status", lambda *a, **k: {"running": False}):
            await main.start_chat("生成一个11月份的日历", announce=False)

        self.assertTrue(self.sent, "一轮结束必须广播收尾状态")
        last = self.sent[-1]
        self.assertEqual(last["type"], "status")
        self.assertFalse(last["data"]["running"])

    async def test_cancel_also_broadcasts_terminal_status(self):
        async def slow_chat(text, announce=True, **kw):
            await asyncio.sleep(5)

        with mock.patch.object(main, "handle_chat", slow_chat), \
                mock.patch.object(main.manager, "broadcast", self._broadcast), \
                mock.patch.object(main, "get_status", lambda *a, **k: {"running": False}):
            task = asyncio.create_task(main.start_chat("hi", announce=False))
            await asyncio.sleep(0.05)
            main.current_task.cancel()
            res = await task

        self.assertTrue(res.get("cancelled"))
        self.assertTrue(self.sent, "取消这一轮也必须广播收尾状态")
        self.assertFalse(self.sent[-1]["data"]["running"])


if __name__ == "__main__":
    unittest.main()
