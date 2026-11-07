"""远程终端后端回归测试。

覆盖：
- 开关关闭一律 403（含可读原因）；
- 未配对设备 401；
- 危险命令（含 sh -c 递归）返回 428 need_confirm；
- 正常命令执行、输出可读、审计字段完整；
- 终止会话与待执行命令的审计（terminated=true）；
- 空闲超时回收；
- 单会话输出上限截断；
- 设置页开关/参数可读写；
- WebSocket 订阅事件格式。

    python -m unittest tests.test_remote_terminal -v
"""
from __future__ import annotations

import base64
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
_DATA = tempfile.mkdtemp(prefix="dm-remote-terminal-")
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")
os.environ["DECKMEIDO_DATA_DIR"] = _DATA
os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
os.environ["DECKMEIDO_PORT"] = "18799"
shutil.copy(ROOT / "server" / "config.example.toml", Path(_DATA) / "config.toml")
_cfg_path = Path(_DATA) / "config.toml"
_cfg_path.write_text(
    _cfg_path.read_text(encoding="utf-8").replace(
        "lan_enable = false", "lan_enable = true"), encoding="utf-8")

from server import audit, main, pairing, remote_terminal  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def tearDownModule():
    for k, v in _OLD.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _paired_token(ip: str = "192.168.1.70") -> str:
    started = pairing.manager.start(ip, "DeckMeidoRemoteTerminal/1.0")
    pair_id = started["pair_id"]
    pin = pairing.manager.pending()[0]["pin"]
    pairing.manager.decide(pair_id, True, "127.0.0.1")
    _device, token = pairing.manager.confirm(pair_id, pin, ip)
    return token


def _default_terminal_cfg() -> dict:
    return {
        "remote_shell": False,
        "idle_timeout_seconds": 900,
        "max_sessions": 3,
        "max_output_bytes": 1_048_576,
        "shell": "",
        "cwd": "",
    }


class RemoteTerminalApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 全量 discover 时，其他测试可能把 DECKMEIDO_DATA_DIR 临时清掉；
        # 这里重新钉住自己的临时数据根，TestClient 的 lifespan 才不会找不到 userdata。
        cls._env = mock.patch.dict(os.environ, {
            "DECKMEIDO_DATA_DIR": _DATA,
            "DECKMEIDO_ALLOW_SECOND_BACKEND": "1",
        })
        cls._env.start()
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        cls.token = _paired_token()
        cls.auth = {"Cookie": f"deckmeido_device={cls.token}"}

    @classmethod
    def tearDownClass(cls):
        async def _close():
            await remote_terminal.manager.close_all("test")

        try:
            cls.client.portal.call(_close)
        except Exception:
            remote_terminal.manager.shutdown_sync("test")
        cls.client.__exit__(None, None, None)
        cls._env.stop()

    def setUp(self):
        audit.clear()
        main.CFG["terminal"] = _default_terminal_cfg()
        remote_terminal.manager.configure(main.CFG)

    def tearDown(self):
        async def _close():
            await remote_terminal.manager.close_all("test")

        try:
            self.client.portal.call(_close)
        except Exception:
            remote_terminal.manager.shutdown_sync("test")

    # -- helpers --------------------------------------------------

    def _enable(self, **extra):
        main.CFG["terminal"]["remote_shell"] = True
        main.CFG["terminal"].update(extra)
        remote_terminal.manager.configure(main.CFG)

    def _create(self, source: str = "test") -> dict:
        r = self.client.post("/api/terminal/sessions",
                             json={"source": source}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["session"]

    def _status(self, sid: str) -> dict:
        r = self.client.get(f"/api/terminal/sessions/{sid}", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["session"]

    def _terminate(self, sid: str):
        return self.client.post(f"/api/terminal/sessions/{sid}/terminate",
                                headers=self.auth)

    def _read_output(self, sid: str) -> bytes:
        r = self.client.get(
            f"/api/terminal/sessions/{sid}/output",
            params={"cursor": 0, "max_bytes": remote_terminal.MAX_OUTPUT_PAGE_BYTES},
            headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        return b"".join(base64.b64decode(c["data"]) for c in body["chunks"])

    def _wait(self, predicate, timeout: float = 6.0):
        end = time.time() + timeout
        while time.time() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return None

    # -- 开关与鉴权 -----------------------------------------------

    def test_开关关闭时创建会话返回403且原因可读(self):
        r = self.client.post("/api/terminal/sessions", json={"source": "test"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 403, r.text)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "remote_shell_disabled")
        self.assertIn("remote_shell", body["reason"])
        # 列表接口同样被开关拦住
        r2 = self.client.get("/api/terminal/sessions", headers=self.auth)
        self.assertEqual(r2.status_code, 403, r2.text)

    def test_未配对设备不能创建会话(self):
        r = self.client.post("/api/terminal/sessions", json={"source": "test"})
        self.assertEqual(r.status_code, 401, r.text)
        r2 = self.client.get("/api/terminal/sessions")
        self.assertEqual(r2.status_code, 401, r2.text)

    def test_设置页可读写开关与参数(self):
        r = self.client.get("/api/config", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["terminal_remote_shell"])
        self.assertEqual(body["terminal_idle_timeout"], 900)

        r = self.client.post("/api/config", headers=self.auth, json={
            "terminal_enabled": True,
            "terminal_idle_timeout": 60,
            "terminal_max_sessions": 2,
            "terminal_max_output_bytes": 4096,
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["terminal_remote_shell"])

        body = self.client.get("/api/config", headers=self.auth).json()
        self.assertTrue(body["terminal_remote_shell"])
        self.assertEqual(body["terminal_idle_timeout"], 60)
        self.assertEqual(body["terminal_max_sessions"], 2)
        self.assertEqual(body["terminal_max_output_bytes"], 4096)

        r = self.client.post("/api/config", headers=self.auth,
                             json={"terminal_enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        body = self.client.get("/api/config", headers=self.auth).json()
        self.assertFalse(body["terminal_remote_shell"])
        # 复原为模板默认值，避免影响后续测试
        self.client.post("/api/config", headers=self.auth, json={
            "terminal_idle_timeout": 900,
            "terminal_max_sessions": 3,
            "terminal_max_output_bytes": 1_048_576,
        })

    # -- 危险命令确认 ---------------------------------------------

    def test_危险命令需要确认(self):
        self._enable()
        session = self._create()
        sid = session["id"]

        r = self.client.post(
            f"/api/terminal/sessions/{sid}/exec", headers=self.auth,
            json={"command": "rm -rf /tmp/dm-remote-terminal"})
        self.assertEqual(r.status_code, 428, r.text)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["need_confirm"])
        self.assertEqual(body["error_code"], "confirm_required")
        self.assertIn(body["level"], ("deny", "hard_confirm"))
        self.assertEqual(body["command"], "rm -rf /tmp/dm-remote-terminal")

        # 没有确认时不能真的写进 shell：既没有待完成命令，也没有执行记录
        status = self._status(sid)
        self.assertIsNone(status["last_command"])
        self.assertEqual(status["completed_execs"], 0)

        # sh -c 递归检查：包一层同样要确认
        r = self.client.post(
            f"/api/terminal/sessions/{sid}/exec", headers=self.auth,
            json={"command": 'sh -c "rm -rf /tmp/dm-remote-terminal"'})
        self.assertEqual(r.status_code, 428, r.text)
        self.assertTrue(r.json()["need_confirm"])

        self._terminate(sid)

    # -- 正常执行 / 输出 / 审计 -----------------------------------

    def test_正常执行输出与审计(self):
        self._enable()
        session = self._create(source="mobile")
        sid = session["id"]

        r = self.client.post(
            f"/api/terminal/sessions/{sid}/exec", headers=self.auth,
            json={"command": "echo dm_hello"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["token"])

        output = self._wait(lambda: b"dm_hello" in self._read_output(sid))
        self.assertTrue(output, "应能读到命令输出")

        status = self._wait(
            lambda: (self._status(sid)["last_command"] or {}).get("exit_code") == 0
            and self._status(sid))
        self.assertIsNotNone(status, "命令应在超时前完成并记录退出码")
        self.assertEqual(status["last_command"]["exit_code"], 0)
        self.assertFalse(status["last_command"]["terminated"])

        event = self._wait(
            lambda: next((e for e in audit.recent(30)
                          if e.get("kind") == "terminal"
                          and e.get("action") == "exec"
                          and e.get("detail") == "echo dm_hello"), None))
        self.assertIsNotNone(event, "每次执行都应写一条审计")
        self.assertEqual(event["exit_code"], 0)
        self.assertFalse(event["confirmed"])
        self.assertFalse(event["terminated"])
        self.assertEqual(event["source"], "mobile")
        self.assertEqual(event["session_id"], sid)
        self.assertIn("started", event)
        self.assertIn("ended", event)

        self._terminate(sid)

    def test_终止会话与待执行命令审计(self):
        self._enable()
        session = self._create()
        sid = session["id"]

        r = self.client.post(
            f"/api/terminal/sessions/{sid}/exec", headers=self.auth,
            json={"command": 'python -c "import time; time.sleep(60)"'})
        self.assertEqual(r.status_code, 200, r.text)

        r = self._terminate(sid)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["already_ended"])
        self.assertEqual(body["session"]["state"], "terminated")
        self.assertIsNotNone(body["session"]["exit_code"])

        event = self._wait(
            lambda: next((e for e in audit.recent(30)
                          if e.get("kind") == "terminal"
                          and e.get("action") == "exec"
                          and e.get("terminated") is True), None), timeout=2.0)
        self.assertIsNotNone(event, "被终止的命令也要补一条审计")

        # 已结束的会话：状态可查、再执行/再终止给明确分支
        status = self._status(sid)
        self.assertEqual(status["state"], "terminated")
        r = self.client.post(
            f"/api/terminal/sessions/{sid}/exec", headers=self.auth,
            json={"command": "echo late"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["error_code"], "session_ended")
        r = self._terminate(sid)
        self.assertTrue(r.json()["already_ended"])

    def test_会话不存在返回404(self):
        self._enable()
        r = self.client.get("/api/terminal/sessions/not-a-session",
                            headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["error_code"], "session_not_found")
        r = self.client.post("/api/terminal/sessions/not-a-session/exec",
                             headers=self.auth, json={"command": "echo hi"})
        self.assertEqual(r.status_code, 404, r.text)

    # -- 输出上限 -------------------------------------------------

    def test_输出上限截断并标记(self):
        self._enable(max_output_bytes=1024)
        session = self._create()
        sid = session["id"]

        r = self.client.post(
            f"/api/terminal/sessions/{sid}/exec", headers=self.auth,
            json={"command": 'python -c "print(\'x\'*3000)"'})
        self.assertEqual(r.status_code, 200, r.text)

        status = self._wait(
            lambda: (self._status(sid)["last_command"] or {}).get("exit_code") == 0
            and self._status(sid))
        self.assertIsNotNone(status)
        self.assertTrue(status["truncated"], "超出上限应标记 truncated")
        self.assertLessEqual(status["output_total"], 1024)

        body = self.client.get(
            f"/api/terminal/sessions/{sid}/output",
            params={"cursor": 0,
                    "max_bytes": remote_terminal.MAX_OUTPUT_PAGE_BYTES},
            headers=self.auth).json()
        self.assertTrue(body["truncated"])
        self.assertLessEqual(body["session"]["output_total"], 1024)

        self._terminate(sid)

    # -- WebSocket 订阅 -------------------------------------------

    def test_websocket订阅输出(self):
        self._enable()
        session = self._create()
        sid = session["id"]
        try:
            with self.client.websocket_connect(
                    f"/ws/terminal/{sid}", headers=self.auth) as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertEqual(hello["session"]["id"], sid)
                ws.send_json({"type": "exec", "command": "echo ws_hello"})
                seen_started = False
                seen_output = False
                for _ in range(40):
                    event = ws.receive_json()
                    if event["type"] == "exec_started":
                        seen_started = True
                    elif event["type"] == "output":
                        text = base64.b64decode(event["data"])
                        if b"ws_hello" in text:
                            seen_output = True
                            break
                self.assertTrue(seen_started, "应收到 exec_started 事件")
                self.assertTrue(seen_output, "应收到含命令输出的 output 事件")
        finally:
            self._terminate(sid)


class RemoteTerminalTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_空闲超时回收(self):
        audit.clear()
        mgr = remote_terminal.RemoteTerminalManager(
            remote_terminal.TerminalSettings(
                enabled=True, idle_timeout_s=0.5, max_sessions=1,
                max_output_bytes=4096))
        session = await mgr.create(owner="127.0.0.1", source="timeout")
        try:
            session.last_activity -= 10.0
            expired = await mgr.reap()
            self.assertIn(session.id, expired)
            self.assertEqual(session.state, "timeout")
            self.assertIsNotNone(session.exit_code)
            self.assertTrue(session.terminated)
        finally:
            await mgr.close_all("test")

    async def test_会话数上限(self):
        mgr = remote_terminal.RemoteTerminalManager(
            remote_terminal.TerminalSettings(enabled=True, max_sessions=1,
                                             max_output_bytes=4096))
        first = await mgr.create(owner="127.0.0.1", source="first")
        try:
            with self.assertRaises(remote_terminal.TerminalError) as ctx:
                await mgr.create(owner="127.0.0.1", source="second")
            self.assertEqual(ctx.exception.code, "too_many_sessions")
            self.assertEqual(ctx.exception.status, 429)
        finally:
            await first.terminate("test")
            await mgr.close_all("test")


if __name__ == "__main__":
    unittest.main()
