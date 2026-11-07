"""QQ 回合授权闸门：绕进入站判定时也不执行工具、不建会话

正常链路在 onebot._dispatch_inner 就丢掉非白名单群的消息；
这里模拟「旁路直接进回合」的场景，验证纵深防御：
  - 未授权来源的 QQ 回合直接拒绝，不调用 handle_chat（零会话写入）；
  - 回合内标记为未授权时，工具层一律拒绝执行。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 导入 server.main 会读配置、做进程守卫；指到临时数据根，别碰真实 userdata
_ENV = ("DECKMEIDO_DATA_DIR", "DECKMEIDO_ALLOW_SECOND_BACKEND", "DECKMEIDO_PORT")
_OLD = {k: os.environ.get(k) for k in _ENV}
_DATA = tempfile.mkdtemp(prefix="dm-qq-gate-")
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")
os.environ["DECKMEIDO_DATA_DIR"] = _DATA
os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
os.environ["DECKMEIDO_PORT"] = "18798"
shutil.copy(ROOT / "server" / "config.example.toml", Path(_DATA) / "config.toml")

from server import main, onebot  # noqa: E402

for _k, _v in _OLD.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


class TurnGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_pending = main.pending_action
        main.pending_action = None
        main.store.turn["active"] = False

    def tearDown(self):
        main.pending_action = self._old_pending
        onebot._TURN_AUTHORIZED.set(None)

    async def test_tool_is_refused_when_source_unauthorized(self):
        token = onebot._TURN_AUTHORIZED.set(False)
        executed = []

        async def fake_execute(name, args):
            executed.append((name, args))
            return "不该执行"

        try:
            with mock.patch.object(onebot, "log_tool_blocked") as logged, \
                    mock.patch.object(main.tools, "execute", side_effect=fake_execute):
                out = await main._run_tool("run_command",
                                           {"command": "pacman -Rns foo"})
        finally:
            onebot._TURN_AUTHORIZED.reset(token)
        self.assertEqual(executed, [])
        self.assertIn("未授权", out)
        logged.assert_called_once_with("run_command")

    async def test_unauthorized_qq_turn_never_calls_handle_chat(self):
        token = onebot._TURN_AUTHORIZED.set(False)
        try:
            with mock.patch.object(main, "handle_chat",
                                   side_effect=AssertionError("不得开回合")), \
                    mock.patch.object(main, "_safe_save") as saved, \
                    mock.patch.object(main, "_qq_log"):
                out = await main._qq_run_turn(
                    "卸了重装", sid=main.store.current_sid,
                    device="qq:29999:30003", readonly=False)
        finally:
            onebot._TURN_AUTHORIZED.reset(token)
        self.assertTrue(out.get("denied"))
        self.assertTrue(out.get("unauthorized"))
        saved.assert_not_called()

    async def test_authorized_qq_turn_runs_normally(self):
        token = onebot._TURN_AUTHORIZED.set(True)
        seen = []

        async def fake_chat(text, **kwargs):
            seen.append((text, kwargs))
            return {"reply": "好的"}

        try:
            with mock.patch.object(main, "handle_chat", side_effect=fake_chat):
                out = await main._qq_run_turn(
                    "你好", sid=main.store.current_sid,
                    device="qq:1:10001", readonly=True)
        finally:
            onebot._TURN_AUTHORIZED.reset(token)
        self.assertEqual(out.get("reply"), "好的")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "你好")

    async def test_task_context_carries_authorization_marker(self):
        """create_task 复制上下文：子任务里必须还能读到授权标记"""
        seen = []

        async def child():
            seen.append(onebot.turn_authorized())

        token = onebot._TURN_AUTHORIZED.set(True)
        try:
            task = asyncio.create_task(child())
            await task
        finally:
            onebot._TURN_AUTHORIZED.reset(token)
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
