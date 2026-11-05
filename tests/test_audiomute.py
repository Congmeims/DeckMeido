"""录音期间静音外放：工具选择、引用计数、原本已静音时不擅自打开

全部用假的 which/_run，不碰真实音频设备；在 Windows 开发机上也能跑。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import audiomute  # noqa: E402


class FakeAudio:
    """假的 wpctl/pactl：只记录调用，按预设返回状态"""

    def __init__(self, tool: str = "wpctl", muted: bool = False, fail_set: bool = False):
        self.tool = tool
        self.muted = muted
        self.fail_set = fail_set
        self.calls = []

    def which(self, name: str):
        return f"/usr/bin/{name}" if name == self.tool else None

    def run(self, cmd, timeout=3.0):
        self.calls.append(list(cmd))
        if cmd[0] == "wpctl" and cmd[1] == "get-volume":
            text = "Volume: 0.50" + (" [MUTED]" if self.muted else "")
            return SimpleNamespace(returncode=0, stdout=text, stderr="")
        if cmd[0] == "pactl" and cmd[1] == "get-sink-mute":
            return SimpleNamespace(returncode=0, stdout="Mute: yes\n" if self.muted else "Mute: no\n", stderr="")
        if self.fail_set:
            return SimpleNamespace(returncode=1, stdout="", stderr="failed")
        if cmd[-1] in ("0", "1"):
            self.muted = cmd[-1] == "1"
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class AudioMuteTests(unittest.TestCase):
    def setUp(self):
        self.audio = FakeAudio()
        patcher = mock.patch.object(audiomute.shutil, "which", self.audio.which)
        self.addCleanup(patcher.stop)
        patcher.start()
        run_patcher = mock.patch.object(audiomute, "_run", self.audio.run)
        self.addCleanup(run_patcher.stop)
        run_patcher.start()
        # 模块级状态在用例之间要清干净
        audiomute._enabled = True
        audiomute._clicks = 0
        audiomute._tool = ""
        audiomute._prev_muted = False
        audiomute._timer = None
        self.addCleanup(self._reset)

    def _reset(self):
        audiomute.restore(force=True)
        audiomute._enabled = True
        audiomute._clicks = 0
        audiomute._tool = ""

    def test_duck_then_restore_uses_wpctl(self):
        r = audiomute.duck()
        self.assertTrue(r["ducked"])
        self.assertEqual(r["tool"], "wpctl")
        self.assertTrue(self.audio.muted)
        self.assertIsNotNone(audiomute._timer)

        r2 = audiomute.restore()
        self.assertTrue(r2["restored"])
        self.assertFalse(self.audio.muted)
        self.assertIsNone(audiomute._timer)

    def test_already_muted_is_left_muted(self):
        self.audio.muted = True
        audiomute.duck()
        # 本来就是静音：恢复时不该替用户打开，只允许保持静音的那次写入
        r = audiomute.restore()
        self.assertTrue(r["restored"])
        self.assertEqual(r["was_muted"], True)
        self.assertTrue(self.audio.muted)
        self.assertEqual(
            [c for c in self.audio.calls if c[-1] == "0"], []
        )

    def test_refcount_keeps_muted_until_last(self):
        audiomute.duck()
        audiomute.duck()
        self.assertTrue(self.audio.muted)
        r = audiomute.restore()
        self.assertFalse(r["restored"])
        self.assertTrue(self.audio.muted)
        r2 = audiomute.restore()
        self.assertTrue(r2["restored"])
        self.assertFalse(self.audio.muted)

    def test_force_restore_ignores_refcount(self):
        audiomute.duck()
        audiomute.duck()
        r = audiomute.restore(force=True)
        self.assertTrue(r["restored"])
        self.assertFalse(self.audio.muted)
        self.assertEqual(audiomute._clicks, 0)

    def test_disabled_is_noop(self):
        audiomute.set_enabled(False)
        r = audiomute.duck()
        self.assertFalse(r["ducked"])
        self.assertEqual(audiomute._clicks, 0)
        self.assertFalse(self.audio.muted)

    def test_no_tool_degrades_silently(self):
        self.audio.tool = ""
        r = audiomute.duck()
        self.assertTrue(r["ok"])
        self.assertFalse(r["ducked"])
        self.assertEqual(audiomute._clicks, 0)
        self.assertFalse(audiomute.status()["available"])

    def test_set_failure_does_not_raise(self):
        self.audio.fail_set = True
        r = audiomute.duck()
        self.assertTrue(r["ok"])
        self.assertFalse(r["ducked"])
        self.assertEqual(audiomute._clicks, 0)

    def test_pactl_fallback(self):
        self.audio.tool = "pactl"
        self.audio.muted = False
        r = audiomute.duck()
        self.assertTrue(r["ducked"])
        self.assertEqual(r["tool"], "pactl")
        self.assertTrue(self.audio.muted)
        audiomute.restore()
        self.assertFalse(self.audio.muted)


if __name__ == "__main__":
    unittest.main()
