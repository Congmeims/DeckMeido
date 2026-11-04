"""数据根解析与旧布局兼容（只读回退、写入新位置、QQ 归档改名 meidobot）"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import paths, qqbot, session_store


class DataRootTest(unittest.TestCase):
    def setUp(self):
        paths._logged.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-root-"))
        self.install = self.tmp / "install"
        self.install.mkdir()
        self.fallback = self.tmp / "fallback"
        self.fallback.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "DECKMEIDO_INSTALL_DIR": str(self.install),
        })
        self._env.start()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)
        self._fb = mock.patch.object(paths, "fallback_dir", return_value=self.fallback)
        self._fb.start()

    def tearDown(self):
        self._fb.stop()
        self._env.stop()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)
        paths._logged.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_var_wins(self):
        envdir = self.tmp / "custom"
        envdir.mkdir()
        with mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(envdir)}):
            self.assertEqual(paths.data_dir(), envdir)
            self.assertEqual(paths.data_root_label(), "env")

    def test_userdata_preferred_when_writable(self):
        (self.install / "userdata").mkdir()
        self.assertEqual(paths.data_dir(), self.install / "userdata")
        self.assertEqual(paths.data_root_label(), "userdata")

    def test_missing_userdata_falls_back_with_log(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            root = paths.data_dir()
        self.assertEqual(root, self.fallback)
        self.assertIn("fallback", out.getvalue())

    def test_legacy_config_is_read_only_fallback(self):
        server = self.install / "server"
        server.mkdir()
        legacy = server / "config.toml"
        legacy.write_text("data = 1\n", encoding="utf-8")
        new = paths.config_path()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            found = paths.read_path(new, legacy)
        self.assertEqual(found, legacy)
        self.assertFalse(new.exists())
        self.assertIn("已迁移读取", out.getvalue())

    def test_both_exist_new_wins(self):
        (self.install / "userdata").mkdir()
        new = self.install / "userdata" / "config.toml"
        new.write_text("data = 2\n", encoding="utf-8")
        legacy = self.install / "server" / "config.toml"
        legacy.parent.mkdir()
        legacy.write_text("data = 1\n", encoding="utf-8")
        self.assertEqual(paths.read_path(new, legacy), new)

    def test_write_migrates_to_new_and_keeps_legacy(self):
        legacy = self.install / "server" / "config.toml"
        legacy.parent.mkdir()
        legacy.write_text("data = 1\n", encoding="utf-8")
        new = self.install / "userdata" / "config.toml"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            paths.ensure_migrated(new, legacy)
        self.assertTrue(new.exists())
        self.assertEqual(new.read_text(encoding="utf-8"), "data = 1\n")
        self.assertTrue(legacy.exists())
        self.assertIn("已迁移读取", out.getvalue())

    def test_qq_archive_follows_data_root(self):
        (self.install / "userdata").mkdir(exist_ok=True)
        root, fallback = qqbot.archive_root("")
        self.assertEqual(root, self.install / "userdata" / "meidobot")
        self.assertFalse(fallback)
        cfg = qqbot.parse_config({})
        index, images, _ = qqbot.image_archive(cfg)
        self.assertEqual(index, root)
        self.assertEqual(images, root / "images")
        log, _ = qqbot.text_log_location(cfg)
        self.assertEqual(log, root / "text_log.txt")

    def test_legacy_qq_read_fallback_and_new_write(self):
        data = self.install / "userdata"
        data.mkdir()
        old = data / "qq"
        old.mkdir()
        (old / "text_log.txt").write_text("旧文本\n", encoding="utf-8")
        (old / "inbox_index.jsonl").write_text('{"id":"old"}\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            read = paths.meidobot_read_dir()
        self.assertEqual(read, old)
        self.assertIn("legacy_dir=qq", out.getvalue())
        root, fallback = qqbot.archive_root("")
        self.assertEqual(root, data / "meidobot")
        self.assertTrue((root / "text_log.txt").exists())
        self.assertEqual((root / "text_log.txt").read_text(encoding="utf-8"), "旧文本\n")
        self.assertTrue((root / "inbox_index.jsonl").exists())
        self.assertTrue(old.exists())            # 旧目录不删
        self.assertFalse(fallback)

    def test_both_qq_dirs_new_wins(self):
        data = self.install / "userdata"
        data.mkdir()
        (data / "qq").mkdir()
        new = data / "meidobot"
        new.mkdir()
        self.assertEqual(paths.meidobot_read_dir(), new)

    def test_session_reads_legacy_writes_new(self):
        new = self.tmp / "sessions-new"
        legacy = self.tmp / "sessions-old"
        new.mkdir()
        legacy.mkdir()
        (legacy / "s1.json").write_text(json.dumps({"id": "s1"}), encoding="utf-8")
        with mock.patch.object(session_store, "SESSIONS_DIR", new), \
                mock.patch.object(session_store, "LEGACY_SESSIONS_DIR", legacy):
            store = session_store.SessionStore()
            self.assertEqual(store.sess_path("s1"), legacy / "s1.json")
            self.assertEqual(store.sess_write_path("s1"), new / "s1.json")
            self.assertEqual([p.name for p in session_store.all_session_files()],
                             ["s1.json"])


if __name__ == "__main__":
    unittest.main()
