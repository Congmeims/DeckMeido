"""插件端桌面截图接口（POST /api/screenshot）的回归测试。

插件在桌面模式自己抓不到画面（只会从 gamescope 的 PipeWire 节点取帧），
所以转给后端复用 spectacle/mss 那条路径；这里验证两件事：
1. 返回体（base64 + 错误说明）和「不把正在跑的另一轮截图状态写串」；
2. 补会话环境变量（Wayland / D-Bus）只在缺失或明显指错时动手。

Windows 开发机上没装 httpx（只有 tools 的联网部分用），导入前先塞一个
占位模块；下面这些用例都不碰网络。
"""
from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _NoNetworkClient:
    """只为让 tools 的模块级 AsyncClient 能构造出来"""

    def __init__(self, *args, **kwargs):
        pass


_stub = types.ModuleType("httpx")
_stub.AsyncClient = _NoNetworkClient
_stub.HTTPError = RuntimeError
sys.modules.setdefault("httpx", _stub)
# tools 里还有一条 PTY 会话实现，Windows 上没有 ptyprocess，同样只需要能导入
sys.modules.setdefault("ptyprocess", types.ModuleType("ptyprocess"))

from server import tools  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FixSessionEnvTest(unittest.TestCase):
    """spectacle 要连对会话总线，否则会退化成抓 Xwayland 根窗口（黑图）"""

    def test_缺变量时补上_Wayland_和会话总线(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bus").write_bytes(b"")
            (root / "wayland-0").write_bytes(b"")
            # KWin 会在 socket 旁边留锁文件，不能把 wayland-0.lock 当显示名
            (root / "wayland-0.lock").write_bytes(b"")
            env = {"XDG_RUNTIME_DIR": td}
            tools._fix_session_env(env, 1000, td)
            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                             f"unix:path={root / 'bus'}")

    def test_已有的正确值不覆盖(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bus").write_bytes(b"")
            (root / "wayland-0").write_bytes(b"")
            env = {
                "WAYLAND_DISPLAY": "wayland-1",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            }
            tools._fix_session_env(env, 1000, td)
            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                             "unix:path=/run/user/1000/bus")

    def test_继承到_root_的总线地址要改回当前用户(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bus").write_bytes(b"")
            env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus"}
            tools._fix_session_env(env, 1000, td)
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                             f"unix:path={root / 'bus'}")

    def test_没有会话_socket_时不乱设(self):
        with tempfile.TemporaryDirectory() as td:
            env = {}
            tools._fix_session_env(env, 1000, td)
            self.assertNotIn("WAYLAND_DISPLAY", env)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_root_身份不动会话变量(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bus").write_bytes(b"")
            (root / "wayland-0").write_bytes(b"")
            env = {}
            tools._fix_session_env(env, 0, td)
            self.assertEqual(env, {})


class ScreenshotPayloadTest(unittest.TestCase):
    """接口返回体：成功给 base64 JPEG，失败给一句人话，且不写串别处的状态"""

    def setUp(self):
        self._prev = tools.SHOT_STATE

    def tearDown(self):
        tools.SHOT_STATE = self._prev

    def test_成功时返回_base64_并还原上一轮的截图状态(self):
        with tempfile.TemporaryDirectory() as td:
            jpg = Path(td) / "v.jpg"
            jpg.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
            prev_state = {"error": ""}
            tools.SHOT_STATE = prev_state

            async def fake_exec(name, args):
                tools.SHOT_STATE["vision"] = str(jpg)
                return "截图完成"

            with mock.patch.object(tools, "execute", fake_exec):
                out = _run(tools.screenshot_payload())

            self.assertTrue(out["ok"])
            self.assertEqual(base64.b64decode(out["image"]), jpg.read_bytes())
            # 跑完后外面那一轮的状态还在原来的对象上，没被换掉
            self.assertIs(tools.SHOT_STATE, prev_state)

    def test_抓帧失败时给出错误说明(self):
        async def fake_exec(name, args):
            tools.SHOT_STATE["error"] = "spectacle 与 mss 都不可用"
            return "截图失败"

        with mock.patch.object(tools, "execute", fake_exec):
            out = _run(tools.screenshot_payload())

        self.assertFalse(out["ok"])
        self.assertIn("spectacle 与 mss 都不可用", out["error"])

    def test_没有压缩产物时当失败处理(self):
        async def fake_exec(name, args):
            return "截图完成"

        with mock.patch.object(tools, "execute", fake_exec):
            out = _run(tools.screenshot_payload())

        self.assertFalse(out["ok"])
        self.assertIn("没有拿到可用的画面", out["error"])

    def test_工具抛异常也返回错误说明(self):
        async def fake_exec(name, args):
            raise RuntimeError("boom")

        with mock.patch.object(tools, "execute", fake_exec):
            out = _run(tools.screenshot_payload())

        self.assertFalse(out["ok"])
        self.assertIn("抓帧失败", out["error"])


if __name__ == "__main__":
    unittest.main()
