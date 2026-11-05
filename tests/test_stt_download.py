"""语音模型下载任务：状态机、进度、取消与热刷新

这些都是纯本地逻辑：把 download_model 换成假的，不真的联网下 1GB。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import stt as stt_mod  # noqa: E402


def _wait_idle(s: stt_mod.STT, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = s.download_status()
        if not st["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("下载任务没有在限定时间内结束")


class SttDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = stt_mod.STT(Path(self.tmp.name))

    def test_done_with_progress(self):
        def fake(progress=None, cancelled=None):
            for i in range(1, 5):
                if progress:
                    progress(i * 250, 1000, "downloading")
                time.sleep(0.01)
            return Path(self.tmp.name)

        with mock.patch.object(self.s, "download_model", side_effect=fake):
            first = self.s.start_download()
            self.assertIn(first["state"], ("downloading", "done"))
            done = _wait_idle(self.s)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["percent"], 100.0)
        self.assertEqual(done["error_kind"], "")

    def test_error_kind_is_kept(self):
        def fake(progress=None, cancelled=None):
            raise stt_mod.STTError("连不上 GitHub", kind="network")

        with mock.patch.object(self.s, "download_model", side_effect=fake):
            self.s.start_download()
            st = _wait_idle(self.s)
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["error_kind"], "network")
        self.assertIn("GitHub", st["error"])

    def test_cancel(self):
        def fake(progress=None, cancelled=None):
            for _ in range(200):
                if cancelled and cancelled():
                    raise stt_mod.STTCancelled()
                time.sleep(0.01)
            return Path(self.tmp.name)

        with mock.patch.object(self.s, "download_model", side_effect=fake):
            self.s.start_download()
            time.sleep(0.05)
            self.s.request_cancel()
            st = _wait_idle(self.s)
        self.assertEqual(st["state"], "cancelled")

    def test_hot_refresh_model_ready_without_restart(self):
        """模型文件落盘后，同一个 STT 实例立刻能报 ready（不需要重启后端）"""
        self.assertFalse(self.s.status()["model"])
        root = Path(self.tmp.name) / stt_mod.MODEL_INNER
        root.mkdir(parents=True)
        (root / "model.int8.onnx").write_bytes(b"fake")
        (root / "tokens.txt").write_text("a\n", encoding="utf-8")
        self.assertTrue(self.s.status()["model"])


if __name__ == "__main__":
    unittest.main()
