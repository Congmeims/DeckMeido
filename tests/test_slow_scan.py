"""慢命令识别与超时预算（server/tools.py）的单元测试。

全盘扫描类命令（find /、du /home、grep -r ... /）既要提示「可能较久」，
又要在默认预算内用更短的超时终止，避免用户只看到一直读秒。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

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


class SlowScanHintTest(unittest.TestCase):
    def test_全盘递归扫描会被识别(self):
        for cmd in (
            "find / -name '*.log'",
            "find /home -type f",
            "du -sh /",
            "du -ah /home/deck",
            "grep -r foo /",
            "grep -R foo /var/log",
            "rg needle /home",
            "ncdu ~",
            "sudo find / -type f",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(tools.slow_scan_hint(cmd), cmd)

    def test_有范围限制的写法不误报(self):
        for cmd in (
            "find ~/Downloads -name '*.zip'",
            "find . -name '*.py'",
            "ls /home/deck",
            "grep foo /home/deck/notes.txt",
            "du -sh /home/deck/project",
            "find /tmp -maxdepth 2 -name x",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(tools.slow_scan_hint(cmd), "", cmd)

    def test_默认预算内压到短超时(self):
        budget, hint = tools.command_budget("find / -name x", 60)
        self.assertEqual(budget, tools.SLOW_SCAN_TIMEOUT)
        self.assertTrue(hint)

    def test_用户主动延长时尊重更长的超时(self):
        budget, _ = tools.command_budget("find / -name x", 180)
        self.assertEqual(budget, 180)
        # 依然受 300 秒上限约束
        budget, _ = tools.command_budget("find / -name x", 999)
        self.assertEqual(budget, 300)

    def test_普通命令不受影响(self):
        budget, hint = tools.command_budget("ls -la", 60)
        self.assertEqual((budget, hint), (60, ""))

    def test_超时上限对普通命令也生效(self):
        self.assertEqual(tools.command_budget("ls", 999), (300, ""))


if __name__ == "__main__":
    unittest.main()
