"""热更新覆盖安装后补可执行位：launch.sh 缺 +x 时桌面快捷方式报「缺少运行权限」"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from server.updater import _ensure_exec_bits


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@unittest.skipUnless(os.name == "posix", "Windows 的 chmod 不改权限位，权限用例只在 Linux/Deck 跑")
class EnsureExecBitsTest(unittest.TestCase):
    def _make(self, root: Path, name: str, mode: int) -> Path:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        p.chmod(mode)
        return p

    def test_scripts_become_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launch = self._make(root, "client/launch.sh", 0o644)
            install = self._make(root, "install.sh", 0o644)
            desktop = self._make(root, "deploy/DeckMeido.desktop", 0o644)
            _ensure_exec_bits(root)
            for p in (launch, install, desktop):
                self.assertTrue(_mode(p) & 0o111, f"{p} 仍不可执行：{oct(_mode(p))}")

    def test_non_scripts_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self._make(root, "server/data.json", 0o644)
            _ensure_exec_bits(root)
            self.assertEqual(_mode(data), 0o644)

    def test_keeps_existing_extra_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = self._make(root, "a.sh", 0o600)
            _ensure_exec_bits(root)
            self.assertEqual(_mode(p), 0o711)


if __name__ == "__main__":
    unittest.main()
