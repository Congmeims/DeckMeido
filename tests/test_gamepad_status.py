"""虚拟手柄状态灯：必须以后端权限为准，后端没上报时显示「未知」"""
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
# tools 里还有一条 PTY 会话实现，Windows 上没有 ptyprocess
sys.modules.setdefault("ptyprocess", types.ModuleType("ptyprocess"))

from server import tools  # noqa: E402


class PadAvailableTest(unittest.TestCase):
    def test_backend_available_true(self):
        """设备节点存在且后端可写 → 可用"""
        with mock.patch.object(tools.os.path, "exists", return_value=True), \
                mock.patch.object(tools.os, "access", return_value=True):
            ok, reason = tools.pad_available()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_backend_available_false(self):
        """节点存在但后端没权限 → 不可用，并给出具体原因"""
        with mock.patch.object(tools.os.path, "exists", return_value=True), \
                mock.patch.object(tools.os, "access", return_value=False):
            ok, reason = tools.pad_available()
        self.assertFalse(ok)
        self.assertIn("/dev/uinput", reason)


class PluginPassthroughTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 插件 import 时需要 decky；测试环境塞最小 stub（与 test_regressions 一致）
        if "decky" not in sys.modules:
            stub = types.ModuleType("decky")
            stub.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp(prefix="deckmeido-test-")
            stub.DECKY_PLUGIN_DIR = str(ROOT / "plugin")
            stub.logger = types.SimpleNamespace(
                info=lambda *a, **k: None, warning=lambda *a, **k: None,
                error=lambda *a, **k: None, debug=lambda *a, **k: None)
            sys.modules["decky"] = stub
        import plugin.main as m  # noqa: E402
        cls.m = m

    def test_backend_missing_field_is_unknown(self):
        """后端没给 gamepad_ok（旧版本 / 未连上）→ None，让前端显示「未知」"""
        out = self.m._gamepad_from_core({"ok": True, "data": {}})
        self.assertIsNone(out["ok"])
        self.assertEqual(out["reason"], "")

    def test_backend_values_pass_through(self):
        """后端给出的 true/false 原样透传，插件不自己改判"""
        self.assertEqual(
            self.m._gamepad_from_core({"ok": True, "data": {"gamepad_ok": True}}),
            {"ok": True, "reason": ""})
        self.assertEqual(
            self.m._gamepad_from_core({"ok": True, "data": {
                "gamepad_ok": False, "gamepad_reason": "没有写权限"}}),
            {"ok": False, "reason": "没有写权限"})


if __name__ == "__main__":
    unittest.main()
