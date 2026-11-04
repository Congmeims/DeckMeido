"""数据根解析：env > userdata，解析不到就明确报错；QQ 归档跟随数据根"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import paths, qqbot, session_store


class DataRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-root-"))
        self.install = self.tmp / "install"
        self.install.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "DECKMEIDO_INSTALL_DIR": str(self.install),
        })
        self._env.start()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)

    def tearDown(self):
        self._env.stop()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_var_wins(self):
        envdir = self.tmp / "custom"
        envdir.mkdir()
        with mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(envdir)}):
            self.assertEqual(paths.data_dir(), envdir)
            self.assertEqual(paths.data_root_label(), "env")

    def test_userdata_preferred(self):
        (self.install / "userdata").mkdir()
        self.assertEqual(paths.data_dir(), self.install / "userdata")
        self.assertEqual(paths.data_root_label(), "userdata")

    def test_missing_userdata_raises_with_hint(self):
        with self.assertRaises(RuntimeError) as ctx:
            paths.data_dir()
        self.assertIn("DECKMEIDO_DATA_DIR", str(ctx.exception))

    def test_env_unwritable_raises(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(blocker)}):
            with self.assertRaises(RuntimeError) as ctx:
                paths.data_dir()
        self.assertIn("DECKMEIDO_DATA_DIR", str(ctx.exception))

    def test_qq_archive_follows_data_root(self):
        (self.install / "userdata").mkdir()
        root = qqbot.archive_root("")
        self.assertEqual(root, self.install / "userdata" / "meidobot")
        cfg = qqbot.parse_config({})
        index, images = qqbot.image_archive(cfg)
        self.assertEqual(index, root)
        self.assertEqual(images, root / "images")
        log = qqbot.text_log_location(cfg)
        self.assertEqual(log, root / "text_log.txt")

    def test_session_paths_under_data_root(self):
        (self.install / "userdata").mkdir()
        store = session_store.SessionStore()
        p = store.sess_path("s1")
        self.assertEqual(p, self.install / "userdata" / "sessions" / "s1.json")
        self.assertEqual(session_store.all_session_files(), [])


if __name__ == "__main__":
    unittest.main()
