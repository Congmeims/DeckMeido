"""删除会话后的引用清理：内存摘要、挂起意图、QQ 固定 sid 的墓碑

QQ 的会话 sid 是按 uin/群号算死的：删掉之后同一个人的下一条消息又落到同一个 sid。
只删文件不清内存，会把旧摘要当上下文复读。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import paths, onebot, session_store  # noqa: E402


class ForgetSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = Path(self.tmp.name)
        (self.install / "userdata").mkdir()
        self.env = mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(self.install / "userdata")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = session_store.SessionStore()

    def test_forget_clears_memory_refs(self):
        sid = "qq-p-10001"
        self.store.set_compact(sid, "旧摘要：主人在装游戏", 8)
        self.store.set_intent(sid, {"action": "restart"})
        self.store.created_map[sid] = True
        self.store.wrote_map[sid] = True
        self.store.flushed_sids.add(sid)

        self.store.forget_session(sid)

        self.assertEqual(self.store.compact_for(sid), {"summary": "", "upto": 0})
        self.assertIsNone(self.store.get_intent(sid))
        self.assertNotIn(sid, self.store.created_map)
        self.assertNotIn(sid, self.store.wrote_map)
        self.assertNotIn(sid, self.store.flushed_sids)
        self.assertIn(sid, self.store.deleted_sids)

    def test_deleted_file_has_no_history(self):
        sid = "qq-p-10001"
        p = self.store.sess_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"id": sid, "history": [
            {"role": "user", "content": "记住我不吃辣"}
        ]}, ensure_ascii=False), encoding="utf-8")
        self.store.forget_session(sid)
        p.unlink()

        self.assertEqual(self.store.read_session(sid).get("history") or [], [])

    def test_allow_fresh_only_when_turn_finished(self):
        sid = "qq-p-10001"
        self.store.forget_session(sid)
        # 同一 sid 的一轮还在跑：不放行，避免新旧两轮同时写同一份历史
        self.assertFalse(self.store.allow_fresh(sid, sid))
        # 轮次结束后允许开全新会话，且墓碑随即撤掉（只放行一次）
        self.assertTrue(self.store.allow_fresh(sid, ""))
        self.assertNotIn(sid, self.store.deleted_sids)
        self.assertFalse(self.store.allow_fresh(sid, ""))

    def test_unrelated_session_is_not_revived(self):
        self.assertFalse(self.store.allow_fresh("s-local", ""))


class SessionTrashTests(unittest.TestCase):
    """软删除：会话文件移进 userdata/trash/，列表不再可见，回收站只留最近 N 份"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = Path(self.tmp.name)
        (self.install / "userdata").mkdir()
        self.env = mock.patch.dict(os.environ, {"DECKMEIDO_DATA_DIR": str(self.install / "userdata")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _write(self, sid: str) -> Path:
        p = paths.sessions_dir() / f"{sid}.json"
        p.write_text(json.dumps({"id": sid, "history": []}, ensure_ascii=False), encoding="utf-8")
        return p

    def test_移到回收站且原文件消失(self):
        sid = "local-abc"
        src = self._write(sid)
        target = session_store.trash_session(sid)
        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertFalse(src.exists())
        self.assertEqual(target.parent, paths.trash_dir())
        # 文件名带上原 sid，便于人工找回
        self.assertIn(sid, target.name)

    def test_文件不存在时返回_None(self):
        self.assertIsNone(session_store.trash_session("nope"))

    def test_回收站只保留最近_N_份(self):
        d = paths.trash_dir()
        for i in range(session_store.TRASH_KEEP + 5):
            p = d / f"s-{i:02d}.json"
            p.write_text("{}", encoding="utf-8")
            # mtime 越大越新：i=0 最旧
            os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
        removed = session_store.prune_trash()
        self.assertEqual(len(removed), 5)
        left = sorted(d.glob("*.json"))
        self.assertEqual(len(left), session_store.TRASH_KEEP)
        self.assertFalse((d / "s-00.json").exists())
        self.assertTrue((d / f"s-{session_store.TRASH_KEEP + 4:02d}.json").exists())


class OneBotDropSessionTests(unittest.TestCase):
    def test_drop_session_clears_pending_confirmation(self):
        bot = onebot.OneBotClient(
            runner=lambda *a, **k: None,
            screenshot=lambda: None,
            status_provider=lambda: {},
            nickname=lambda: "meido",
        )
        bot._pending_conf = {
            "qq:10001": {"sid": "qq-p-10001", "turn_id": "t1", "desc": "重启"},
            "qq:g20001": {"sid": "qq-g-20001", "turn_id": "t2", "desc": "关机"},
        }
        bot.drop_session("qq-p-10001")
        self.assertNotIn("qq:10001", bot._pending_conf)
        self.assertIn("qq:g20001", bot._pending_conf)


if __name__ == "__main__":
    unittest.main()
