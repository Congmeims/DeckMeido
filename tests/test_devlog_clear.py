"""日志清理：分类准确、单文件失败不中断、接口不因目录异常变成 500"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import devlog

DEBUG = "debug-20260101-120000.log"
DEBUG_OLD = "debug-20260101-120000.log.1"
INSTALL = "install-20260101-120000.log"
RUN_BACKEND = "backend.log"
RUN_OVERLAY = "overlay.log"
RUN_CRASH = "client-crash.log"


class LogClearTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-logs-"))
        self._env = mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(self.tmp)})
        self._env.start()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        for name in (DEBUG, DEBUG_OLD, INSTALL, RUN_BACKEND, RUN_OVERLAY, RUN_CRASH):
            (self.logs / name).write_text("x" * 10, encoding="utf-8")
        (self.logs / "notes.txt").write_text("ignore", encoding="utf-8")
        (self.logs / "subdir").mkdir()
        devlog._USAGE_CACHE.update({"ts": 0.0, "data": None})

    def tearDown(self):
        self._env.stop()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        devlog._USAGE_CACHE.update({"ts": 0.0, "data": None})

    def test_categories_match_button_labels(self):
        self.assertEqual({p.name for p in devlog.log_files("debug")}, {DEBUG, DEBUG_OLD})
        self.assertEqual({p.name for p in devlog.log_files("install")}, {INSTALL})
        self.assertEqual(
            {p.name for p in devlog.log_files("run")},
            {RUN_BACKEND, RUN_OVERLAY, RUN_CRASH},
        )
        # 非 .log 文件与子目录不参与统计 / 清理
        self.assertNotIn("notes.txt", {p.name for p in devlog.log_files("all")})
        self.assertNotIn("subdir", {p.name for p in devlog.log_files("all")})

    def test_active_files_are_truncated_others_removed(self):
        res = devlog.clear_logs("run")
        self.assertTrue(res["ok"])
        # backend.log / overlay.log 是常驻的「正在写入」文件：截断而非删除
        self.assertEqual(res["truncated"], 2)
        self.assertEqual(res["deleted"], 1)
        self.assertGreater(res["freed_bytes"], 0)
        self.assertEqual((self.logs / RUN_BACKEND).read_text(encoding="utf-8"), "")
        self.assertEqual((self.logs / RUN_OVERLAY).read_text(encoding="utf-8"), "")
        self.assertFalse((self.logs / RUN_CRASH).exists())
        # 其它分类不受影响
        self.assertTrue((self.logs / DEBUG).exists())
        self.assertTrue((self.logs / INSTALL).exists())

    def test_failed_file_is_reported_but_rest_still_cleared(self):
        original = Path.unlink

        def fake_unlink(path, *args, **kwargs):
            # 模拟被其它进程占用：单个文件失败，接口仍要返回结构化结果
            if path.name == DEBUG_OLD:
                raise PermissionError("in use")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fake_unlink):
            res = devlog.clear_logs("debug")

        self.assertFalse(res["ok"])
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["failed_files"][0]["name"], DEBUG_OLD)
        self.assertIn("PermissionError", res["failed_files"][0]["error"])
        # 同批其它文件照常清掉
        self.assertFalse((self.logs / DEBUG).exists())
        self.assertTrue((self.logs / DEBUG_OLD).exists())
        self.assertIsNotNone(res["usage"])

    def test_usage_survives_unavailable_data_dir(self):
        devlog._USAGE_CACHE.update({"ts": 0.0, "data": None})
        with mock.patch("server.paths.logs_dir", side_effect=RuntimeError("数据根不可写")):
            self.assertEqual(devlog.log_files("all"), [])
            usage = devlog.usage()
        self.assertEqual(usage["total"], {"bytes": 0, "files": 0})

    def test_unknown_category_still_raises_for_400(self):
        with self.assertRaises(ValueError):
            devlog.clear_logs("unknown")


if __name__ == "__main__":
    unittest.main()
