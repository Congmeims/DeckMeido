"""日志轮转：按大小触发、带序号的轮转文件保持固定份数"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import devlog

KB = 1024
MAX_BYTES = devlog.MAX_BYTES
KEEP = devlog.BACKUP_COUNT


class LogRotateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-rot-"))
        self._env = mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(self.tmp)})
        self._env.start()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        devlog._USAGE_CACHE.update({"ts": 0.0, "data": None})

    def tearDown(self):
        self._env.stop()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        devlog._USAGE_CACHE.update({"ts": 0.0, "data": None})

    def _write(self, name: str, kb: int) -> Path:
        path = self.logs / name
        path.write_bytes(b"x" * (kb * KB))
        return path

    def _size(self, name: str) -> int:
        return (self.logs / name).stat().st_size // KB

    def test_small_file_is_not_rotated(self):
        self._write("backend.log", MAX_BYTES // KB - 1)
        self.assertEqual(devlog.rotate_logs(), [])
        self.assertFalse((self.logs / "backend.log.1").exists())

    def test_generations_shift_and_oldest_is_dropped(self):
        self._write("backend.log", 6000)      # 超过 5 MB 上限，触发轮转
        self._write("backend.log.1", 2000)
        self._write("backend.log.2", 3000)    # 最旧的一份，应当被丢掉

        self.assertEqual(devlog.rotate_logs(), ["backend.log"])

        self.assertEqual(self._size("backend.log"), 0)      # 原地截断，句柄继续可写
        self.assertEqual(self._size("backend.log.1"), 6000)  # 新快照
        self.assertEqual(self._size("backend.log.2"), 2000)  # 旧 .1 后移
        self.assertFalse((self.logs / "backend.log.3").exists())

    def test_extra_generations_beyond_keep_are_removed(self):
        self._write("backend.log", 6000)
        self._write("backend.log.1", 2000)
        self._write("backend.log.2", 3000)
        # 旧版本（keep=4）遗留下来的 .3/.4：没有任何逻辑会再引用，必须回收
        self._write("backend.log.3", 4000)
        self._write("backend.log.4", 1000)

        self.assertEqual(devlog.rotate_logs(), ["backend.log"])

        names = sorted(p.name for p in self.logs.iterdir())
        self.assertEqual(names, ["backend.log", "backend.log.1", "backend.log.2"])
        # 份数固定：当前文件 + keep 份旧文件
        self.assertEqual(len(names), KEEP + 1)

    def test_rotated_file_is_not_rotated_again(self):
        self._write("debug-20260101-120000.log.1", 6000)
        self.assertEqual(devlog.rotate_logs(), [])
        self.assertEqual([p.name for p in self.logs.iterdir()],
                         ["debug-20260101-120000.log.1"])

    def test_usage_and_clear_cover_numbered_files(self):
        self._write("debug-20260101-120000.log", 10)
        self._write("debug-20260101-120000.log.1", 10)
        self._write("debug-20260101-120000.log.2", 10)
        devlog._USAGE_CACHE.update({"ts": 0.0, "data": None})

        usage = devlog.usage()
        self.assertEqual(usage["categories"]["debug"]["files"], 3)

        res = devlog.clear_logs("debug")
        self.assertTrue(res["ok"])
        self.assertEqual(res["deleted"], 3)
        self.assertEqual(set(devlog.log_files("debug")), set())


if __name__ == "__main__":
    unittest.main()
