"""版本展示（VERSION + COMMIT）的回归测试。

覆盖三条读取路径：安装目录有 COMMIT / 没有 COMMIT 也没有 git / 只有 git。
以及关键约束：version_display 只用于显示，has_update 仍按 VERSION 的语义版本比较。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import updater  # noqa: E402


class _TempInstallDir(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self._old_install = updater.INSTALL_DIR
        updater.INSTALL_DIR = self.root

    def tearDown(self):
        updater.INSTALL_DIR = self._old_install
        self._td.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")


class VersionDisplayTest(_TempInstallDir):
    def test_有_COMMIT_时显示短提交号(self):
        self.write("VERSION", "0.7.5\n")
        self.write("COMMIT", "f2931ae\n")
        self.assertEqual(updater.current_version(), "0.7.5")
        self.assertEqual(updater.current_commit(), "f2931ae")
        self.assertEqual(updater.version_display(), "0.7.5-f2931ae")

    def test_COMMIT_大小写与长哈希只取前7位(self):
        self.write("VERSION", "v0.7.5\n")
        self.write("COMMIT", "F2931AEB12CD\n")
        self.assertEqual(updater.version_display(), "0.7.5-f2931ae")

    def test_无_COMMIT_无_git_只显示语义版本不加噪声(self):
        self.write("VERSION", "0.7.5\n")
        self.assertEqual(updater.current_commit(), "")
        self.assertEqual(updater.version_display(), "0.7.5")
        self.assertNotIn("unknown", updater.version_display())

    def test_无_COMMIT_有_git_用_rev_parse(self):
        self.write("VERSION", "0.7.5\n")
        (self.root / ".git").mkdir()
        fake = mock.Mock(stdout="f2931ae\n")
        with mock.patch.object(updater.subprocess, "run", return_value=fake) as run:
            self.assertEqual(updater.current_commit(), "f2931ae")
            self.assertEqual(updater.version_display(), "0.7.5-f2931ae")
            self.assertTrue(run.called)

    def test_全都没有时按_0_0_0_且不带后缀(self):
        self.assertEqual(updater.version_display(), "0.0.0")


class UpdateCheckTest(_TempInstallDir):
    """版本比较只看 VERSION 的数字部分，展示后缀不参与 has_update"""

    def setUp(self):
        super().setUp()
        self.write("VERSION", "0.7.5\n")
        self.write("COMMIT", "f2931ae\n")

    def check(self, tag: str) -> dict:
        latest = {"tag": tag, "notes": "", "url": "", "sha256": ""}
        with mock.patch.object(updater, "latest_release", return_value=latest), \
                mock.patch.object(updater, "update_cfg", return_value={}):
            return asyncio.run(updater.check())

    def test_同版本不算更新_即便展示带提交号(self):
        result = self.check("v0.7.5")
        self.assertEqual(result["current"], "0.7.5")
        self.assertFalse(result["has_update"])
        self.assertEqual(updater.version_display(), "0.7.5-f2931ae")

    def test_更高版本才算更新(self):
        result = self.check("v0.7.6")
        self.assertTrue(result["has_update"])
        self.assertEqual(updater.version_display(), "0.7.5-f2931ae")


if __name__ == "__main__":
    unittest.main()
